from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import statistics
import subprocess
from pathlib import Path

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks.provider_results import (
    MINIMUM_ELIGIBLE_SOURCE_SHA,
    OPAQUE_TZAFON_SETTLE_SENTENCE,
    SCHEMA_VERSION,
    ProviderResultsError,
    build_provider_results,
    render_provider_results_json,
    render_provider_results_markdown,
    sanitize_modal_observation_input,
    sanitize_modal_optimized_input,
    validate_provider_results,
    validate_sanitized_modal_optimized_input,
)

HARNESS_SHA = "e57ea35f04efdec4100ffa44196ee8599e9811b2"


def _case(value: float, *, iterations: int) -> dict[str, object]:
    samples = [value] * (iterations - 1) + [value + 2]
    ordered = sorted(samples)
    rank = 0.95 * (len(ordered) - 1)
    lower = math.floor(rank)
    fraction = rank - lower
    p95 = ordered[lower] + fraction * (ordered[math.ceil(rank)] - ordered[lower])
    return {
        "status": "ok",
        "iterations": iterations,
        "successful_iterations": iterations,
        "samples_ms": samples,
        "summary_ms": {"p50": statistics.median(ordered), "p95": p95},
        "failures": [],
    }


def _provider_artifact() -> dict[str, object]:
    providers = {}
    for index, name in enumerate(("modal-daemon", "daytona", "e2b", "tzafon"), 1):
        cases = {
            "product_create_to_first_screenshot": _case(index * 1000.0, iterations=3),
            "screenshot_full": {
                **_case(index * 10.0, iterations=3),
                "last_result": (
                    {"format": "png", "width": 1024, "height": 768}
                    if name == "modal-daemon"
                    else {
                        "payload": {
                            "format": "jpeg" if name == "tzafon" else "png",
                            "width": 1280 if name == "tzafon" else 1024,
                            "height": 720 if name == "tzafon" else 768,
                        }
                    }
                ),
            },
            "coordinate_click": {
                **_case(index * 11.0, iterations=3),
                "benchmark_semantics": "coordinate-click-v1",
            },
            "coordinate_click_sequence": {
                **_case(index * 12.0, iterations=3),
                "benchmark_semantics": "coordinate-click-v1",
            },
            "type_100_chars": _case(index * 13.0, iterations=3),
            "type_1000_chars": _case(index * 14.0, iterations=3),
            "command_nonlogin_shell_echo": {
                **_case(index * 15.0, iterations=3),
                "benchmark_semantics": "shell-command-echo-v2",
                "shell_mode": "non_login",
            },
        }
        sdk_calls, transport_requests, batching = {
            "modal-daemon": (1, 1, "single_request"),
            "daytona": (4, 4, "sequential_requests"),
            "e2b": (4, 8, "sequential_requests"),
            "tzafon": (1, 1, "single_request"),
        }[name]
        cases["coordinate_click_sequence"].update(
            provider_sdk_call_count=sdk_calls,
            transport_request_count=transport_requests,
            batching=batching,
        )
        if name == "modal-daemon":
            cases["type_100_chars"]["resolved_methods"] = ["clipboard"]
            cases["type_100_chars"]["request"] = {
                "character_count": 100,
                "method": "auto",
                "delay_ms": 10,
            }
            cases["type_1000_chars"]["request"] = {
                "character_count": 1000,
                "method": "auto",
                "delay_ms": 10,
            }
            cases["type_1000_chars"]["resolved_methods"] = ["clipboard"]
        providers[name] = {
            "status": "ok",
            "provider": name,
            "failures": [],
            "cases": cases,
            "verification": {
                "cursor_position": {"status": "ok"},
                "type_text": {"status": "ok"},
            },
            "metadata": {
                "daytona": {
                    "api_url": None,
                    "snapshot": None,
                    "target": None,
                    "sandbox_source": "default_snapshot",
                    "sdk_package": "daytona",
                    "sdk_version": "0.175.0",
                },
                "e2b": {
                    "template": None,
                    "template_source": "default_desktop",
                    "sdk_package": "e2b-desktop",
                    "sdk_version": "2.3.1",
                },
                "tzafon": {
                    "api_origin": None,
                    "computer_kind": "desktop",
                    "persistent": False,
                    "sdk_retry_policy": "provider_default",
                    "sdk_max_retries": 2,
                    "sdk_package": "tzafon",
                    "sdk_version": "2.44.1",
                },
            }.get(name, {}),
        }
    return {
        "ok": True,
        "benchmark": "provider-compare",
        "iterations": 3,
        "failures": [],
        "metadata": {
            "providers": ["modal-daemon", "daytona", "e2b", "tzafon"],
            "environment": {
                "browser": None,
                "modal_ingress": "attested-tunnel",
                "daemon_http_version": "1.1",
                "resource_profile": "standard",
                "input_rate_limit_per_sec": 20,
                "action_case_pacing_ms": 1050,
                "subprocess_backend": "isolated-asyncio",
                "provenance": {
                    "git_revision": HARNESS_SHA,
                    "git_worktree_clean": True,
                    "region": "provider-default",
                    "resolved_resources": {
                        "cpu": {
                            "requested": None,
                            "resolved": None,
                            "status": "provider_default_unavailable",
                        },
                        "memory": {
                            "requested": None,
                            "resolved": None,
                            "status": "provider_default_unavailable",
                            "unit": "GiB",
                        },
                        "gpu": {
                            "requested": None,
                            "resolved": None,
                            "status": "provider_default_unavailable",
                        },
                    },
                },
            },
        },
        "providers": providers,
        "provenance": {
            "harness_commit": HARNESS_SHA,
            "harness_state": "clean",
            "status": "current_reference",
        },
    }


def _raw_optimized_artifact() -> dict[str, object]:
    cases = {
        name: {
            **_case(float(index), iterations=30),
            **(
                {"input_backends": ["xtest"]}
                if name
                in {
                    "coordinate_click",
                    "coordinate_click_sequence",
                    "type_100_chars",
                    "type_1000_chars",
                }
                else {}
            ),
            **(
                {"benchmark_semantics": "coordinate-click-v1"}
                if name in {"coordinate_click", "coordinate_click_sequence"}
                else {}
            ),
            **(
                {"benchmark_semantics": "shell-command-echo-v2", "shell_mode": "non_login"}
                if name == "command_nonlogin_shell_echo"
                else {}
            ),
        }
        for index, name in enumerate(
            (
                "screenshot_full",
                "coordinate_click",
                "coordinate_click_sequence",
                "type_100_chars",
                "type_1000_chars",
                "command_nonlogin_shell_echo",
            ),
            1,
        )
    }
    cases["type_100_chars"]["request"] = {
        "character_count": 100,
        "method": "keystrokes",
        "delay_ms": 0,
    }
    cases["type_100_chars"]["resolved_methods"] = ["keystrokes"]
    cases["type_1000_chars"]["request"] = {
        "character_count": 1000,
        "method": "keystrokes",
        "delay_ms": 0,
        "timeout_ms": 30000,
    }
    cases["type_1000_chars"]["resolved_methods"] = ["keystrokes"]
    product_create = _case(100.0, iterations=30)
    product_create.update(
        {
            "name": "product_create_to_first_screenshot",
            "definition": "create through validated frame",
            "warmup_iterations": 1,
            "successful_warmup_iterations": 1,
            "replacement_samples": 0,
            "fresh_target_per_attempt": True,
            "targets_created": 31,
            "target_attempts": 31,
            "targets_reused": 0,
            "target_placements_verified": 31,
            "cleanup": {"attempted": 31, "succeeded": 31, "failures": []},
        }
    )
    environment = {
        "modal_region": "us-west-2",
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
        "browser": "chromium",
        "browser_prewarm": False,
        "image_revision": HARNESS_SHA,
        "runner_cpu": 4.0,
        "runner_memory_mib": 8192,
        "target_cpu": 4.0,
        "target_memory_mib": 8192,
        "input_rate_limit_per_sec": 0,
        "subprocess_backend": "isolated-asyncio",
        "provenance": {
            "git_revision": HARNESS_SHA,
            "git_worktree_clean": True,
            "image_identity": f"named:{HARNESS_SHA}",
        },
    }
    run = {
        "ok": True,
        "failures": [],
        "product_create": product_create,
        "runner_placement": {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
        "warm_target_cleanup": {"attempted": True, "succeeded": True, "error_type": None},
        "warm_target_placement_verified": True,
        "surfaces": {
            "daemon-http": {
                "status": "ok",
                "failures": [],
                "cases": cases,
                "verification": {
                    "cursor_position": {"status": "ok"},
                    "type_text": {"status": "ok"},
                },
            }
        },
    }
    return {
        "schema_version": 1,
        "ok": True,
        "benchmark": "modal-optimized-provider",
        "eligibility": "publishable",
        "iterations": 30,
        "warmup_iterations": 1,
        "replacement_samples": 0,
        "failures": [],
        "metadata": {
            **environment,
            "caller_topology": "single-modal-function-same-requested-modal-region",
            "runner_kind": "modal-function",
            "runner_invocations": 1,
            "runner_startup_in_product_create_boundary": False,
            "external_caller_included": False,
        },
        "runs": {"modal_optimized_runner": run},
        "runner_dispatch": {
            "elapsed_ms": 100.0,
            "included_in_product_create_samples": False,
        },
        "final_cleanup": {"cleanup_succeeded": True, "remaining_sandboxes": 0},
    }


def _raw_observation_artifact() -> dict[str, object]:
    case = {
        **_case(7.5, iterations=30),
        "benchmark_semantics": "first-hash-confirmed-change-v1",
        "metric": "action_to_first_hash_confirmed_change_ms",
        "experimental": True,
        "change_timeout_ms": 200,
        "replacement_samples": 0,
        "last_result": {
            "input_backend": "xtest",
            "action_result": {"ok": True},
            "change_result": {
                "detected": True,
                "timeout_reached": False,
                "baseline_source_sha256": "b" * 64,
                "source_sha256": "c" * 64,
            },
        },
    }
    return {
        "ok": True,
        "benchmark": "modal-colocated-client",
        "iterations": 30,
        "failures": [],
        "metadata": {
            "primary_runner_path": "connect",
            "runner_paths": ["connect"],
            "surfaces": ["daemon-observation-stream"],
            "external_caller_included": False,
            "modal_region": "us-west-2",
            "modal_ingress": "connect",
            "daemon_http_version": "1.1",
        },
        "runs": {
            "modal_colocated_runner": {
                "ok": True,
                "iterations": 30,
                "failures": [],
                "metadata": {
                    "environment": {
                        "modal_region": "us-west-2",
                        "modal_runner_path": "connect",
                        "modal_runner_region": "us-west-2",
                        "modal_ingress": "connect",
                        "daemon_http_version": "1.1",
                        "browser": "chromium",
                        "input_rate_limit_per_sec": 0,
                        "subprocess_backend": "isolated-asyncio",
                        "provenance": {
                            "git_revision": HARNESS_SHA,
                            "git_worktree_clean": True,
                        },
                    }
                },
                "surfaces": {
                    "daemon-observation-stream": {
                        "status": "ok",
                        "failures": [],
                        "cases": {"observation_action_click_observe_change_http_raw": case},
                    }
                },
            }
        },
    }


def _optimized_artifact() -> dict[str, object]:
    return sanitize_modal_optimized_input(
        _raw_optimized_artifact(),
        raw_sha256="2" * 64,
        evidence_harness_sha=HARNESS_SHA,
    )


def _observation_artifact() -> dict[str, object]:
    return sanitize_modal_observation_input(
        _raw_observation_artifact(),
        raw_sha256="3" * 64,
        evidence_harness_sha=HARNESS_SHA,
    )


def _build() -> dict[str, object]:
    inputs = (_provider_artifact(), _optimized_artifact(), _observation_artifact())
    raw = tuple(json.dumps(item, sort_keys=True).encode() for item in inputs)
    return build_provider_results(
        *inputs,
        input_sha256=tuple(hashlib.sha256(item).hexdigest() for item in raw),
        report_source_sha=HARNESS_SHA,
        evidence_harness_sha=HARNESS_SHA,
    )


def test_builder_renders_exact_headline_order_and_one_modal_experiment() -> None:
    result = _build()
    markdown = render_provider_results_markdown(result)

    assert list(result["headline"]["columns"]) == [
        "Modal optimized",
        "Modal default",
        "Daytona default",
        "E2B default",
        "Tzafon default",
    ]
    assert [row["label"] for row in result["headline"]["rows"]] == [
        "Product create to validated screenshot",
        "Full screenshot native/default",
        "One coordinate click",
        "Four coordinate clicks",
        "Type 100",
        "Type 1000",
        "Non-login shell command",
    ]
    assert markdown.count("| Case | Modal optimized |") == 1
    assert markdown.count("## Modal-only experimental result") == 1
    assert OPAQUE_TZAFON_SETTLE_SENTENCE in markdown
    assert "Tzafon 1280x720 JPEG" in markdown
    assert "Modal, Daytona, and E2B 1024x768 PNG" in markdown
    assert "maximum wait for a hash-confirmed first visual change" in markdown
    assert "not a fixed wait, settle period, or application-readiness signal" in markdown
    assert "transport, authentication, request handling and admission" in markdown
    assert (
        "process spawn, output collection, process wait, cleanup, and exact-output validation"
        in markdown
    )
    assert (
        "isolated-asyncio affects only subprocess-backed command and compatibility paths"
        in markdown
    )
    assert "20-actions-per-second input limit" in markdown
    assert "auto resolves to clipboard" in markdown
    assert "1.05 seconds of untimed pacing" in markdown
    assert "Tzafon experimental" not in markdown
    assert "not measured" not in markdown
    assert result["headline"]["rows"][0]["values"]["modal_optimized"]["sample_count"] == 30
    for banned in (
        "canoni" + "cal protocol",
        "porta" + "ble",
        "fastest" + "_setup",
        "same" + "_task",
        "sdk" + "_default",
        "claim" + "_check",
    ):
        assert banned not in markdown.lower()
    assert [item["sha256"] for item in result["provenance"]["inputs"]] == [
        hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        for item in (_provider_artifact(), _optimized_artifact(), _observation_artifact())
    ]
    assert render_provider_results_json(result).endswith("\n")


def test_reporting_policy_keeps_small_sample_p95_machine_only() -> None:
    result = _build()
    policy = result["reporting_policy"]
    assert policy == {
        "small_sample_threshold": 20,
        "small_sample_display": "median [observed min–max]",  # noqa: RUF001
        "large_sample_display": "p50 / p95",
        "p50_method": "statistics.median",
        "p95_method": (
            "linear interpolation on sorted values at rank 0.95*(n-1)"
        ),
    }
    default_value = result["headline"]["rows"][1]["values"]["modal-daemon"]
    assert set(default_value) == {
        "status",
        "sample_count",
        "min_ms",
        "max_ms",
        "p50_ms",
        "p95_ms",
    }
    markdown = render_provider_results_markdown(result)
    assert "10.00 [10.00–12.00]" in markdown  # noqa: RUF001
    assert "10.00 / 11.00" not in markdown
    assert default_value["p95_ms"] == pytest.approx(11.8)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda p, _o, _x: p.update(ok=False), "provider artifact"),
        (
            lambda p, _o, _x: p["provenance"].update(harness_commit="a" * 40),
            "harness commit",
        ),
        (
            lambda p, _o, _x: p["providers"]["e2b"]["cases"]["coordinate_click"].update(
                successful_iterations=2
            ),
            "3/3",
        ),
        (
            lambda p, _o, _x: p["providers"]["daytona"]["verification"]["type_text"].update(
                status="failed"
            ),
            "readback",
        ),
        (
            lambda _p, o, _x: o["configuration"].update(modal_runner_kind="modal-sandbox"),
            "configuration",
        ),
        (
            lambda _p, o, _x: o["configuration"].update(external_caller_included=True),
            "configuration",
        ),
        (
            lambda _p, _o, x: x["configuration"].update(external_caller_included=True),
            "configuration",
        ),
        (
            lambda _p, _o, x: x["case"].update(replacement_samples=1),
            "semantics",
        ),
        (
            lambda _p, _o, x: x["success_attestation"].update(last_timeout_reached=True),
            "attestation",
        ),
        (
            lambda _p, _o, x: x["success_attestation"].update(last_action_ok=False),
            "attestation",
        ),
        (
            lambda _p, _o, x: x["case"].update(input_backend="xdotool"),
            "semantics",
        ),
        (
            lambda p, _o, _x: p["providers"]["modal-daemon"]["cases"]["screenshot_full"][
                "samples_ms"
            ].__setitem__(0, float("nan")),
            "sample",
        ),
        (
            lambda p, _o, _x: p["providers"]["modal-daemon"]["cases"]["screenshot_full"][
                "summary_ms"
            ].update(p95=1.0),
            "p95",
        ),
        (
            lambda _p, o, _x: o["provenance"].update(evidence_harness_sha="a" * 40),
            "configuration",
        ),
        (
            lambda _p, o, _x: o["configuration"].update(image_revision="d" * 40),
            "configuration",
        ),
        (
            lambda _p, o, _x: o["configuration"].update(target_cpu=99.0),
            "configuration",
        ),
        (
            lambda _p, o, _x: o["configuration"].update(
                modal_cloud_provider="CLOUD_PROVIDER_GCP"
            ),
            "configuration",
        ),
        (
            lambda _p, o, _x: o["placement"]["runner"].update(
                cloud="CLOUD_PROVIDER_GCP"
            ),
            "placement",
        ),
        (
            lambda _p, _o, x: x["provenance"].update(evidence_harness_sha="a" * 40),
            "observation source",
        ),
    ],
)
def test_builder_rejects_ineligible_inputs(mutator, match: str) -> None:
    provider, optimized, observation = (
        _provider_artifact(),
        _optimized_artifact(),
        _observation_artifact(),
    )
    mutator(provider, optimized, observation)
    with pytest.raises(ProviderResultsError, match=match):
        build_provider_results(
            provider,
            optimized,
            observation,
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


def test_builder_allows_report_source_to_follow_evidence_commit() -> None:
    result = build_provider_results(
        _provider_artifact(),
        _optimized_artifact(),
        _observation_artifact(),
        input_sha256=("1" * 64, "2" * 64, "3" * 64),
        report_source_sha="a" * 40,
        evidence_harness_sha=HARNESS_SHA,
    )
    assert result["provenance"]["report_source_sha"] == "a" * 40
    assert result["provenance"]["evidence_harness_sha"] == HARNESS_SHA


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda p: p["provenance"].update(status="historical"), "current_reference"),
        (lambda p: p["provenance"].update(harness_state="dirty"), "clean"),
        (lambda p: p["metadata"].update(providers=["modal-daemon"]), "provider list"),
        (
            lambda p: p["metadata"]["environment"].update(modal_ingress="connect"),
            "attested-tunnel",
        ),
        (
            lambda p: p["metadata"]["environment"]["provenance"].update(git_worktree_clean=False),
            "clean",
        ),
        (
            lambda p: p["metadata"]["environment"]["provenance"].update(region="us-west-2"),
            "provider-default",
        ),
        (
            lambda p: p["metadata"]["environment"]["provenance"]["resolved_resources"][
                "cpu"
            ].update(requested=4.0, resolved=4.0, status="explicit"),
            "resources",
        ),
        (
            lambda p: p["providers"]["daytona"]["metadata"].update(snapshot="custom"),
            "Daytona",
        ),
        (
            lambda p: p["providers"]["e2b"]["metadata"].update(template="custom"),
            "E2B",
        ),
        (
            lambda p: p["providers"]["tzafon"]["metadata"].update(sdk_max_retries=3),
            "Tzafon",
        ),
    ],
)
def test_builder_rejects_nondefault_provider_evidence(mutator, match: str) -> None:
    provider = _provider_artifact()
    mutator(provider)
    with pytest.raises(ProviderResultsError, match=match):
        build_provider_results(
            provider,
            _optimized_artifact(),
            _observation_artifact(),
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


def test_builder_rejects_observation_outside_exact_topology() -> None:
    observation = _observation_artifact()
    observation["configuration"]["modal_region"] = "us-east"
    with pytest.raises(ProviderResultsError, match="configuration"):
        build_provider_results(
            _provider_artifact(),
            _optimized_artifact(),
            observation,
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


@pytest.mark.parametrize("artifact_name", ["optimized", "observation"])
@pytest.mark.parametrize("contradiction", ["external_run", "comparison", "metadata_comparison"])
def test_builder_rejects_structurally_external_runner_only_evidence(
    artifact_name: str,
    contradiction: str,
) -> None:
    optimized = _optimized_artifact()
    observation = _observation_artifact()
    artifact = optimized if artifact_name == "optimized" else observation
    if contradiction == "external_run":
        artifact["external_run"] = {"ok": True}
    elif contradiction == "comparison":
        artifact["comparison"] = {"ratio": 1.0}
    else:
        artifact["metadata_comparison"] = "external versus runner"

    with pytest.raises(ProviderResultsError, match="unexpected or missing fields"):
        build_provider_results(
            _provider_artifact(),
            optimized,
            observation,
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


@pytest.mark.parametrize(
    "bad", [{"token": "secret"}, {"endpoint": "https://x.test"}, {"resource_id": "sb-1"}]
)
def test_combined_artifact_rejects_secret_or_resource_fields(bad: dict[str, str]) -> None:
    result = _build()
    result["provenance"].update(bad)
    with pytest.raises(ProviderResultsError):
        validate_provider_results(result)


def test_combined_artifact_binds_image_revision_to_evidence_commit() -> None:
    result = _build()
    result["configuration"]["modal_optimized"]["image_revision"] = "d" * 40
    result["provenance"]["safe_configuration_sha256"] = hashlib.sha256(
        json.dumps(
            result["configuration"], separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()

    with pytest.raises(ProviderResultsError, match="configuration is not exact"):
        validate_provider_results(result)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda r: r.update(status="draft"),
        lambda r: r.update(extra="value"),
        lambda r: r["sample_counts"].update(experiment=29),
        lambda r: r["headline"]["rows"][1]["values"]["modal_optimized"].update(p50_ms=float("nan")),
        lambda r: r["headline"]["rows"][1]["values"]["modal_optimized"].update(p95_ms=float("inf")),
        lambda r: r["headline"]["rows"][1]["values"]["modal_optimized"].update(p50_ms=-1),
        lambda r: r["headline"]["rows"][1]["values"]["modal_optimized"].update(p50_ms=10, p95_ms=9),
        lambda r: r["experiment"].update(iterations=29, successful_iterations=29),
        lambda r: r["experiment"].update(replacement_samples=1),
        lambda r: r["boundaries"].update(extra_url="https://unsafe.example"),
    ],
)
def test_combined_validator_rejects_contradictions(mutator) -> None:
    result = _build()
    mutator(result)
    with pytest.raises(ProviderResultsError):
        validate_provider_results(result)


def test_sanitizer_source_verification_rejects_unrelated_commit(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location("sanitize_provider_results_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[tuple[str, ...]] = []

    def fake_git_output(*args: str) -> str:
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return "b" * 40
        raise subprocess.CalledProcessError(1, ["git", *args])

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    with pytest.raises(RuntimeError, match="minimum eligible"):
        module._verify_source_revisions("b" * 40, "a" * 40)
    assert calls[-1] == (
        "merge-base",
        "--is-ancestor",
        MINIMUM_ELIGIBLE_SOURCE_SHA,
        "a" * 40,
    )


def test_sanitizer_source_verification_rejects_evidence_after_report(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location("sanitize_provider_results_ancestry_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return "b" * 40
        if args == ("merge-base", "--is-ancestor", HARNESS_SHA, HARNESS_SHA):
            return ""
        raise subprocess.CalledProcessError(1, ["git", *args])

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    with pytest.raises(RuntimeError, match="ancestor of the report source"):
        module._verify_source_revisions("b" * 40, HARNESS_SHA)


def test_sanitizer_source_verification_rejects_dirty_tracked_worktree(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location("sanitize_provider_results_dirty_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return HARNESS_SHA
        if args[:2] == ("merge-base", "--is-ancestor"):
            return ""
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return " M tracked.py"
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    with pytest.raises(RuntimeError, match="clean tracked worktree"):
        module._verify_source_revisions(HARNESS_SHA, HARNESS_SHA)


def test_sanitizer_check_accepts_clean_descendant_of_report_source(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location("sanitize_provider_results_check_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report_sha = "b" * 40
    head_sha = "c" * 40
    calls: list[tuple[str, ...]] = []

    def fake_git_output(*args: str) -> str:
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return head_sha
        if args[:2] == ("merge-base", "--is-ancestor"):
            return ""
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    module._verify_source_revisions(report_sha, HARNESS_SHA, check=True)
    assert ("merge-base", "--is-ancestor", report_sha, head_sha) in calls


def test_sanitizer_check_rejects_report_source_not_ancestor_of_head(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location(
        "sanitize_provider_results_check_ancestry_test", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report_sha = "b" * 40
    head_sha = "c" * 40

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return head_sha
        if args in {
            ("merge-base", "--is-ancestor", HARNESS_SHA, HARNESS_SHA),
            ("merge-base", "--is-ancestor", HARNESS_SHA, report_sha),
        }:
            return ""
        raise subprocess.CalledProcessError(1, ["git", *args])

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    with pytest.raises(RuntimeError, match="report source must be an ancestor of HEAD"):
        module._verify_source_revisions(report_sha, HARNESS_SHA, check=True)


def test_offline_cli_renders_markdown_json_and_output(tmp_path: Path, capsys) -> None:
    artifact = tmp_path / "combined.json"
    artifact.write_text(render_provider_results_json(_build()), encoding="utf-8")
    assert cli.main(["benchmark", "provider-results", str(artifact), "--format", "markdown"]) == 0
    assert "Modal optimized" in capsys.readouterr().out
    output = tmp_path / "out.json"
    assert (
        cli.main(
            [
                "benchmark",
                "provider-results",
                str(artifact),
                "--format",
                "json",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["schema_version"] == SCHEMA_VERSION
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises((ProviderResultsError, SystemExit)):
        cli.main(["benchmark", "provider-results", str(artifact)])


def test_pre_fix_experiment_is_rejected() -> None:
    observation = _observation_artifact()
    case = observation["case"]
    del case["benchmark_semantics"]
    with pytest.raises(ProviderResultsError, match="unexpected or missing fields"):
        build_provider_results(
            _provider_artifact(),
            _optimized_artifact(),
            observation,
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


def test_sanitizer_generation_is_deterministic_and_check_detects_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = [tmp_path / name for name in ("provider.json", "optimized.json", "observation.json")]
    for path, payload in zip(
        paths,
        (_provider_artifact(), _optimized_artifact(), _observation_artifact()),
        strict=True,
    ):
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    output = tmp_path / "combined.json"
    argv = [
        *(str(path) for path in paths),
        str(output),
        "--report-source-sha",
        HARNESS_SHA,
        "--evidence-harness-sha",
        HARNESS_SHA,
    ]

    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location(
        "sanitize_provider_results_generation_test", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_verify_source_revisions",
        lambda _report_sha, _evidence_sha, **_kwargs: None,
    )

    assert module.main(argv) == 0
    first = output.read_bytes()
    assert module.main(argv) == 0
    assert output.read_bytes() == first
    assert module.main([*argv, "--check"]) == 0
    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        module.main([*argv, "--check"])


def test_optimized_sanitizer_keeps_run_wall_clock_and_drops_the_rest_of_dispatch() -> None:
    raw = _raw_optimized_artifact()
    raw["runner_dispatch"]["elapsed_ms"] = 520874.78650012054

    result = sanitize_modal_optimized_input(
        raw, raw_sha256="2" * 64, evidence_harness_sha=HARNESS_SHA
    )

    assert result["run_wall_clock_ms"] == 520874.78650012054
    rendered = json.dumps(result, sort_keys=True)
    assert "runner_dispatch" not in rendered
    assert "included_in_product_create_samples" not in rendered


def test_sanitized_optimized_run_wall_clock_must_be_a_finite_nonnegative_number() -> None:
    for bad in (-1.0, math.inf, math.nan, "520874", True, None):
        payload = _optimized_artifact()
        payload["run_wall_clock_ms"] = bad
        with pytest.raises(ProviderResultsError, match="run wall clock"):
            validate_sanitized_modal_optimized_input(payload)


def test_sanitized_optimized_input_still_rejects_unknown_top_level_keys() -> None:
    payload = _optimized_artifact()
    payload["modal_sandbox_id"] = "sb-123"
    with pytest.raises(ProviderResultsError, match="unexpected or missing fields"):
        validate_sanitized_modal_optimized_input(payload)


def test_sanitized_optimized_input_accepts_artifacts_predating_the_wall_clock() -> None:
    payload = _optimized_artifact()
    del payload["run_wall_clock_ms"]
    validate_sanitized_modal_optimized_input(payload)


@pytest.mark.parametrize("raw_factory", [_raw_optimized_artifact, _raw_observation_artifact])
def test_modal_input_sanitizers_reject_dirty_raw_evidence(raw_factory) -> None:
    raw = raw_factory()
    if raw["benchmark"] == "modal-optimized-provider":
        raw["metadata"]["provenance"]["git_worktree_clean"] = False
        sanitizer = sanitize_modal_optimized_input
    else:
        raw["runs"]["modal_colocated_runner"]["metadata"]["environment"]["provenance"][
            "git_worktree_clean"
        ] = False
        sanitizer = sanitize_modal_observation_input
    with pytest.raises(ProviderResultsError, match="clean"):
        sanitizer(raw, raw_sha256="4" * 64, evidence_harness_sha=HARNESS_SHA)


def test_modal_input_sanitizer_is_deterministic_allowlisted_and_checkable(
    tmp_path: Path,
) -> None:
    raw_paths = [tmp_path / "optimized-raw.json", tmp_path / "observation-raw.json"]
    out_paths = [tmp_path / "optimized.json", tmp_path / "observation.json"]
    for path, payload in zip(
        raw_paths, (_raw_optimized_artifact(), _raw_observation_artifact()), strict=True
    ):
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "sanitize_modal_provider_inputs_test", Path("scripts/sanitize_modal_provider_inputs.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    argv = [
        *map(str, raw_paths),
        *map(str, out_paths),
        "--evidence-harness-sha",
        HARNESS_SHA,
    ]
    assert module.main(argv) == 0
    first = [path.read_bytes() for path in out_paths]
    assert module.main(argv) == 0
    assert [path.read_bytes() for path in out_paths] == first
    assert module.main([*argv, "--check"]) == 0
    rendered = b"".join(first).lower()
    for forbidden in (
        b"access_token",
        b"api_key",
        b"endpoint",
        b"resource_id",
        b"last_result",
        b"source_sha256",
        b"failure_text",
        b"stdout",
        b"stderr",
        b"runner_dispatch",
        b"included_in_product_create_samples",
    ):
        assert forbidden not in rendered
    out_paths[0].write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        module.main([*argv, "--check"])


def test_minimum_eligible_revision_is_pinned_to_evidence_harness() -> None:
    assert MINIMUM_ELIGIBLE_SOURCE_SHA == HARNESS_SHA
