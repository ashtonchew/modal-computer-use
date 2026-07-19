from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from modal_computer_use.benchmarks.modal_optimization import (
    OPTIMIZED_ACTION_CASE,
    PROFILE_MODAL_ON_DEMAND,
    PROFILE_MODAL_WARM_AVAILABILITY,
    PROFILE_PROVIDER_DEFAULT,
    ModalOptimizationConfig,
    action_attempts_from_case,
    build_modal_optimization_artifact,
    build_preregistration,
    estimate_resource_cost,
    extract_provider_default_profile,
    sanitize_modal_optimization_benchmark,
    select_modal_optimization_region,
    serialize_modal_optimization_benchmark,
    summarize_attempts,
    validate_modal_optimization_artifact,
)
from modal_computer_use.benchmarks.modal_optimization_execution import (
    run_independent_cold_attempts,
    run_warm_action_attempts,
)


def _attempt(index: int, *, status: str = "valid", elapsed_ms: float | None = None):
    return {
        "attempt": index,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "retry_count": 0,
        "failure": None,
        "cleanup": {"attempted": True, "succeeded": True, "error_type": None},
    }


def _artifact() -> dict[str, object]:
    cold = [_attempt(index, elapsed_ms=float(index + 1)) for index in range(20)]
    warm = [_attempt(index, elapsed_ms=float(index + 2)) for index in range(30)]
    claims = [
        {
            **_attempt(index, elapsed_ms=float(index + 3)),
            "pool_hit": True,
            "pool_miss": False,
            "cold_fallback": False,
        }
        for index in range(30)
    ]
    return {
        "schema_version": 1,
        "benchmark": "modal-optimization-results",
        "profiles": {
            PROFILE_PROVIDER_DEFAULT: {
                "comparison_scope": "cross-provider-default-only",
                "providers": ["modal", "daytona", "e2b"],
            },
            PROFILE_MODAL_ON_DEMAND: {
                "comparison_scope": "modal-provider-native-on-demand",
                "cold_attempts": cold,
                "warm_action_attempts": warm,
            },
            PROFILE_MODAL_WARM_AVAILABILITY: {
                "comparison_scope": "modal-on-demand-only",
                "claim_attempts": claims,
            },
            "modal-v2-ab": {
                "status": "not_run",
                "reason": "Connect Tokens are unsupported by Modal V2 in Modal 1.5.2",
                "source_url": "https://modal.com/docs/guide/sandbox-v2",
            },
        },
        "failures": [],
        "provenance": {
            "source_sha": "a" * 40,
            "raw_artifact_sha256": "b" * 64,
            "raw_artifact_path": "benchmark-results/modal-optimization/raw.json",
        },
    }


def test_summary_retains_failed_and_timed_out_attempts_without_tail_overclaim() -> None:
    attempts = [
        _attempt(0, elapsed_ms=10.0),
        _attempt(1, elapsed_ms=20.0),
        _attempt(2, status="failed"),
        _attempt(3, status="timeout"),
    ]

    summary = summarize_attempts(attempts)

    assert summary == {
        "attempted": 4,
        "valid": 2,
        "failed": 1,
        "timeout": 1,
        "p50_ms": 15.0,
        "p95_ms": None,
        "p95_status": "insufficient_valid_samples",
        "minimum_p95_samples": 20,
    }


def test_summary_uses_deterministic_linear_p95_at_preregistered_minimum() -> None:
    attempts = [_attempt(index, elapsed_ms=float(index + 1)) for index in range(20)]

    summary = summarize_attempts(attempts)

    assert summary["p50_ms"] == pytest.approx(10.5)
    assert summary["p95_ms"] == pytest.approx(19.05)
    assert summary["p95_status"] == "reported"


def test_artifact_profile_labels_keep_warm_capacity_modal_only() -> None:
    artifact = _artifact()
    validate_modal_optimization_artifact(artifact)

    invalid = copy.deepcopy(artifact)
    invalid["profiles"][PROFILE_MODAL_WARM_AVAILABILITY]["comparison_scope"] = (
        "cross-provider-default-only"
    )
    with pytest.raises(ValueError, match="Modal on-demand only"):
        validate_modal_optimization_artifact(invalid)


def test_artifact_validation_rejects_dropped_attempts() -> None:
    artifact = _artifact()
    artifact["profiles"][PROFILE_MODAL_ON_DEMAND]["cold_summary"] = {
        "attempted": 19,
        "valid": 19,
        "failed": 0,
        "timeout": 0,
    }

    with pytest.raises(ValueError, match="summary attempted count"):
        validate_modal_optimization_artifact(artifact)


def test_artifact_failed_claims_do_not_count_as_pool_misses() -> None:
    artifact = _artifact()
    failed = artifact["profiles"][PROFILE_MODAL_WARM_AVAILABILITY]["claim_attempts"][0]
    failed.update(
        {
            "status": "failed",
            "elapsed_ms": None,
            "failure": {"phase": "warm_claim", "error_type": "RuntimeError"},
            "pool_hit": False,
            "pool_miss": False,
            "cold_fallback": False,
        }
    )

    validate_modal_optimization_artifact(artifact)

    failed["pool_miss"] = True
    with pytest.raises(ValueError, match="must not invent availability outcomes"):
        validate_modal_optimization_artifact(artifact)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("token", "secret-token"),
        ("base_url", "https://example.test/path?credential=value"),
        ("typed_text", "private payload"),
        ("clipboard", "private clipboard"),
        ("screenshot_bytes", "iVBORw0KGgo="),
        ("stdout", "raw provider response"),
    ],
)
def test_sanitizer_rejects_secret_or_raw_payload_fields(key: str, value: str) -> None:
    artifact = _artifact()
    artifact[key] = value

    with pytest.raises(ValueError, match=r"forbidden|credentialed URL|secret-bearing"):
        sanitize_modal_optimization_benchmark(
            artifact,
            raw_bytes=b"{}",
            raw_artifact_path="benchmark-results/modal-optimization/raw.json",
            harness_commit="a" * 40,
        )


def test_sanitized_serialization_is_deterministic() -> None:
    sanitized = sanitize_modal_optimization_benchmark(
        _artifact(),
        raw_bytes=b'{"raw":true}',
        raw_artifact_path="benchmark-results/modal-optimization/raw.json",
        harness_commit="a" * 40,
    )

    first = serialize_modal_optimization_benchmark(sanitized)
    second = serialize_modal_optimization_benchmark(json.loads(first))

    assert first == second
    assert first.endswith("\n")


def test_preregistration_freezes_commands_policies_and_dependency() -> None:
    config = ModalOptimizationConfig(region="us-west", image_revision="a" * 40)
    commands = {
        "provider_default": "uv run computer-use benchmark compare --iterations 3",
        "region_selection": "uv run computer-use benchmark modal-region-ab --iterations 30",
        "publish_image": "uv run python scripts/publish_modal_images.py --revision " + "a" * 40,
        "benchmark": "uv run python scripts/run_modal_optimization_benchmark.py run",
        "normalize": "uv run python scripts/sanitize_modal_optimization_benchmark.py",
    }

    preregistration = build_preregistration(
        config,
        source_sha="a" * 40,
        dependency_sha="b" * 40,
        generated_at="2026-07-19T00:00:00Z",
        runner_identity={"kind": "local", "architecture": "arm64"},
        sdk_versions={"modal": "1.5.2"},
        commands=commands,
    )

    assert preregistration["source_sha"] == "a" * 40
    assert preregistration["dependency"] == {
        "pull_request": 114,
        "head_sha": "b" * 40,
        "state": "open_unmerged",
    }
    assert preregistration["sample_policy"] == {
        "independent_cold_attempts": 30,
        "warm_action_attempts": 30,
        "warm_claim_attempts": 30,
        "warm_pool_target": 3,
        "warm_idle_seconds": 30.0,
    }
    assert preregistration["failure_policy"]["drop_failed_attempts"] is False
    assert preregistration["retry_policy"] == {
        "harness_retries": 0,
        "replacement_samples": False,
        "provider_sdk_internal_retries": "not_observable",
    }
    assert preregistration["commands"] == commands


def test_action_attempt_rows_preserve_measured_failures_and_timeouts() -> None:
    case = {
        "iterations": 4,
        "action_to_frame_samples_ms": [10.0, 30.0],
        "failures": [
            {"phase": "measure", "iteration": 1, "error_type": "TimeoutError"},
            {"phase": "measure", "iteration": 3, "error_type": "RuntimeError"},
        ],
    }

    attempts = action_attempts_from_case(case, expected_attempts=4)

    assert [attempt["status"] for attempt in attempts] == [
        "valid",
        "timeout",
        "valid",
        "failed",
    ]
    assert [attempt["elapsed_ms"] for attempt in attempts] == [10.0, None, 30.0, None]
    assert attempts[1]["failure"] == {
        "phase": "measure",
        "error_type": "TimeoutError",
    }


def test_action_attempt_rows_reject_hidden_or_extra_samples() -> None:
    with pytest.raises(ValueError, match="sample accounting"):
        action_attempts_from_case(
            {"iterations": 2, "action_to_frame_samples_ms": [1.0], "failures": []},
            expected_attempts=2,
        )


def test_resource_cost_marks_target_only_estimate_partial() -> None:
    cost = estimate_resource_cost(
        duration_seconds=10.0,
        cpu=4.0,
        memory_mib=8192,
        requested_region="us-west",
        includes_runner=False,
    )

    assert cost["status"] == "partial"
    assert cost["region_multiplier"] == 1.75
    assert cost["included"] == ["target_cpu", "target_memory"]
    assert cost["excluded"] == ["runner_compute", "control_plane", "billing_adjustments"]
    assert cost["estimated_usd"] == pytest.approx(
        10.0 * (4.0 * 0.00003942 + 8.0 * 0.00000667) * 1.75
    )


def test_provider_default_extraction_keeps_only_safe_summary_fields() -> None:
    payload = {
        "ok": True,
        "iterations": 3,
        "failures": [],
        "provenance": {"raw_artifact_sha256": "c" * 64},
        "providers": {
            provider: {
                "status": "ok",
                "failures": [],
                "cases": {
                    "product_create_to_first_screenshot": {
                        "successful_iterations": 3,
                        "summary_ms": {"p50": 100.0, "p95": 120.0},
                    },
                    "move_click": {
                        "successful_iterations": 3,
                        "summary_ms": {"p50": 10.0, "p95": 12.0},
                    },
                },
            }
            for provider in ("modal-daemon", "daytona", "e2b")
        },
    }

    profile = extract_provider_default_profile(payload)

    assert profile["comparison_scope"] == "cross-provider-default-only"
    assert profile["providers"] == ["modal", "daytona", "e2b"]
    assert profile["results"]["modal"]["warm_action_move_click_p50_ms"] == 10.0
    assert "cases" not in profile["results"]["modal"]


def test_region_selection_applies_preregistered_tie_break_and_records_digest() -> None:
    payload = {
        "benchmark": "modal-region-ab",
        "iterations": 30,
        "comparison": {
            "regions": {
                "us-west": {"fastest_floor_p50_ms": 100.0},
                "us-east": {"fastest_floor_p50_ms": 104.0},
            }
        },
        "runs": {
            "us-west": {
                "ok": True,
                "metadata": {"environment": {"modal_cold_create_to_ready_ms": 12_000.0}},
            },
            "us-east": {
                "ok": True,
                "metadata": {"environment": {"modal_cold_create_to_ready_ms": 9_000.0}},
            },
        },
    }

    region, evidence = select_modal_optimization_region(payload, raw_bytes=b"region-evidence")

    assert region == "us-east"
    assert evidence["selected"] == "us-east"
    assert evidence["default_region_excluded_from_optimized_placement"] is True
    assert len(evidence["artifact_sha256"]) == 64


def test_independent_cold_attempts_validate_frame_and_record_cleanup() -> None:
    closed: list[str] = []

    class Computer:
        def ensure_browser_ready(self, _config, *, timing) -> None:
            timing.mark("browser_ready")

        def first_valid_frame(self, _config, *, timing) -> bytes:
            timing.mark("first_valid_frame")
            return b"validated"

        def runtime_region(self) -> str:
            return "us-west-2"

        def terminate(self, *, wait: bool) -> None:
            closed.append(f"terminate:{wait}")

        def detach(self) -> None:
            closed.append("detach")

    config = ModalOptimizationConfig(
        region="us-west",
        image_revision="a" * 40,
        cold_attempts=2,
    )
    attempts = run_independent_cold_attempts(
        config,
        create_computer=lambda **_kwargs: Computer(),
    )

    assert [attempt["status"] for attempt in attempts] == ["valid", "valid"]
    assert all("first_valid_frame" in attempt["stages"] for attempt in attempts)
    assert all(attempt["cleanup"]["succeeded"] is True for attempt in attempts)
    assert closed == ["terminate:True", "detach", "terminate:True", "detach"]


def test_warm_action_uses_separate_connect_runner_and_retains_timeout() -> None:
    runner_paths: list[str] = []

    class Computer:
        def ensure_browser_ready(self, _config) -> None:
            pass

        def first_valid_frame(self, _config) -> bytes:
            return b"validated"

        def runtime_region(self) -> str:
            return "us-west-2"

        def metadata(self):
            return SimpleNamespace(sandbox_id="sb-redacted-before-artifact")

        def terminate(self, *, wait: bool) -> None:
            assert wait is True

        def detach(self) -> None:
            pass

    def runner(_config, **kwargs):
        runner_paths.append(kwargs["runner_path"])
        return {
            "surfaces": {
                "daemon-observation-stream": {
                    "cases": {
                        OPTIMIZED_ACTION_CASE: {
                            "action_to_frame_samples_ms": [10.0],
                            "failures": [
                                {
                                    "phase": "measure",
                                    "iteration": 1,
                                    "type": "TimeoutError",
                                }
                            ],
                        }
                    }
                }
            }
        }

    config = ModalOptimizationConfig(
        region="us-west",
        image_revision="a" * 40,
        warm_action_attempts=2,
    )
    attempts, metadata = run_warm_action_attempts(
        config,
        create_computer=lambda **_kwargs: Computer(),
        runner_benchmark=runner,
    )

    assert [attempt["status"] for attempt in attempts] == ["valid", "timeout"]
    assert runner_paths == ["connect"]
    assert metadata["runner_path"] == "same-region-separate-modal-runner:connect"
    assert metadata["target_loopback"] is False


def test_built_live_artifact_passes_the_publication_sanitizer() -> None:
    provider = {
        "iterations": 3,
        "provenance": {"raw_artifact_sha256": "c" * 64},
        "providers": {
            name: {
                "status": "ok",
                "failures": [],
                "cases": {
                    case: {
                        "successful_iterations": 3,
                        "summary_ms": {"p50": 10.0, "p95": 12.0},
                    }
                    for case in ("product_create_to_first_screenshot", "move_click")
                },
            }
            for name in ("modal-daemon", "daytona", "e2b")
        },
    }
    attempts = _artifact()["profiles"]
    config = ModalOptimizationConfig(region="us-west", image_revision="a" * 40)
    raw = build_modal_optimization_artifact(
        config,
        source_sha="a" * 40,
        dependency_sha="b" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration_sha256="d" * 64,
        provider_default_payload=provider,
        cold_attempts=attempts[PROFILE_MODAL_ON_DEMAND]["cold_attempts"],
        warm_action_attempts=attempts[PROFILE_MODAL_ON_DEMAND]["warm_action_attempts"],
        warm_action_metadata={
            "target_resource_duration_seconds": 1.0,
            "target_cleanup": {"attempted": True, "succeeded": True, "error_type": None},
        },
        claim_attempts=attempts[PROFILE_MODAL_WARM_AVAILABILITY]["claim_attempts"],
        claim_metadata={"pool_target_size": 3},
        region_selection={"selected": "us-west", "artifact_sha256": "e" * 64},
    )

    sanitized = sanitize_modal_optimization_benchmark(
        raw,
        raw_bytes=json.dumps(raw).encode(),
        raw_artifact_path="benchmark-results/modal-optimization/raw.json",
        harness_commit="a" * 40,
    )

    assert sanitized["profiles"][PROFILE_MODAL_WARM_AVAILABILITY]["pool_hit_count"] == 30
    assert sanitized["provenance"]["raw_artifact_tracked"] is False
