from __future__ import annotations

import copy
import json

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks.promotion_gate import (
    CANDIDATE_ARM,
    PRIOR_PUBLIC_ARM,
    PromotionGateError,
    build_interleaved_schedule,
    compare_promotion_artifacts,
    sanitize_promotion_artifact,
    validate_promotion_artifact,
)


def _configuration() -> dict[str, object]:
    return {
        "caller_topology": "one-application-owned-modal-function",
        "target_identity": "target-sha-abc123",
        "requested_placement": {"cloud": "aws", "region": "us-west-2"},
        "observed_placement": {
            "function": {"cloud": "aws", "region": "us-west-2"},
            "target": {"cloud": "aws", "region": "us-west-2"},
        },
        "resources": {"cpu": 1, "memory_mib": 2048},
        "image_identity": "image-sha-def456",
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": "xtest",
        "screenshot": {
            "format": "png",
            "show_cursor": False,
            "transport": "raw-binary",
        },
        "action_payload_sha256": "a" * 64,
        "warmup_iterations": 1,
        "connection_reuse": "one-pooled-async-client",
        "warm_capacity": {
            "function_min_containers": 0,
            "sandbox_pool_capacity": 0,
        },
        "timeout_ms": 5000,
    }


def _artifact(
    arm: str,
    *,
    warm_values: list[float] | None = None,
    failures: list[dict[str, object]] | None = None,
    config: dict[str, object] | None = None,
) -> dict[str, object]:
    values = list(warm_values or ([10.0, 11.0, 12.0, 13.0] * 8)[:30])
    resolved_config = copy.deepcopy(config or _configuration())
    screenshot = resolved_config["screenshot"]
    assert isinstance(screenshot, dict)
    screenshot["transport"] = "json-base64" if arm == PRIOR_PUBLIC_ARM else "raw-binary"
    observations = [
        {
            "sample_index": index,
            "status": "ok",
            "frame_valid": True,
            "connection_reused": True,
            "borrow_count": 1,
            "timings_ms": {
                "cold_start_ms": 0.0,
                "startup_ms": 0.0,
                "dispatch_ms": 1.0,
                "borrow_ms": 0.5,
                "warm_operation_ms": value,
            },
            "cleanup": {"attempted": True, "succeeded": True, "survivors": 0},
            "attribution": {
                "daemon_ms": value - 1.0,
                "transport_ms": 1.0,
                "input_backend": "xtest",
                "screenshot_transport": screenshot["transport"],
            },
        }
        for index, value in enumerate(values)
    ]
    return {
        "schema_version": 1,
        "benchmark": "optimized-default-promotion",
        "arm": arm,
        "status": "complete" if not failures else "failed",
        "configuration": resolved_config,
        "preregistration": {
            "samples_per_arm": len(values),
            "warmup_iterations": 1,
            "schedule_seed": 42,
            "bootstrap_seed": 7,
            "bootstrap_resamples": 500,
            "minimum_samples_per_arm": 30,
        },
        "schedule": build_interleaved_schedule(
            samples_per_arm=len(values), warmup_iterations=1, seed=42
        ),
        "observations": observations,
        "failures": failures or [],
        "cleanup": {"attempted": True, "succeeded": True, "survivors": 0},
        "replacement_samples": 0,
        "retries": 0,
    }


def test_interleaved_schedule_has_both_arms_per_pair_and_changes_order() -> None:
    schedule = build_interleaved_schedule(samples_per_arm=30, warmup_iterations=1, seed=42)

    measured = [row for row in schedule if row["phase"] == "measure"]
    assert len(measured) == 60
    for pair_index in range(30):
        pair = [row for row in measured if row["pair_index"] == pair_index]
        assert [row["arm"] for row in pair] in [
            [PRIOR_PUBLIC_ARM, CANDIDATE_ARM],
            [CANDIDATE_ARM, PRIOR_PUBLIC_ARM],
        ]
    assert {tuple(row["arm"] for row in measured[i : i + 2]) for i in range(0, 60, 2)} == {
        (PRIOR_PUBLIC_ARM, CANDIDATE_ARM),
        (CANDIDATE_ARM, PRIOR_PUBLIC_ARM),
    }


def test_validator_accepts_complete_sanitized_artifact() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM, warm_values=[10.0] * 30)

    validate_promotion_artifact(payload, expected_arm=PRIOR_PUBLIC_ARM)


def test_comparator_passes_candidate_with_paired_bootstrap_interval() -> None:
    prior = _artifact(PRIOR_PUBLIC_ARM, warm_values=([20.0, 21.0, 20.5, 19.5] * 7) + [20.0, 21.0])
    candidate = _artifact(CANDIDATE_ARM, warm_values=([10.0, 11.0, 10.5, 9.5] * 7) + [10.0, 11.0])

    result = compare_promotion_artifacts(prior, candidate)

    assert result["eligible"] is True
    assert result["decision"] == "promote"
    assert result["paired_samples"] == 30
    assert (
        result["metrics"]["warm_operation_ms"]["candidate_p50_ms"]
        < result["metrics"]["warm_operation_ms"]["prior_p50_ms"]
    )
    assert result["metrics"]["warm_operation_ms"]["bootstrap_95_ci_ms"]


def test_comparator_rejects_material_regression() -> None:
    prior = _artifact(PRIOR_PUBLIC_ARM, warm_values=[10.0] * 30)
    candidate = _artifact(CANDIDATE_ARM, warm_values=[20.0] * 30)

    result = compare_promotion_artifacts(prior, candidate)

    assert result["eligible"] is False
    assert result["decision"] == "reject"
    assert any("regression" in reason for reason in result["reasons"])


def test_comparator_rejects_configuration_drift_before_latency() -> None:
    prior = _artifact(PRIOR_PUBLIC_ARM)
    drifted = _configuration()
    drifted["http_version"] = "2"
    candidate = _artifact(CANDIDATE_ARM, config=drifted)

    result = compare_promotion_artifacts(prior, candidate)

    assert result["eligible"] is False
    assert result["reasons"] == ["promotion artifact validation failed"]
    assert result["metrics"] == {}


def test_comparator_rejects_missing_frame_and_cleanup() -> None:
    prior = _artifact(PRIOR_PUBLIC_ARM)
    candidate = _artifact(CANDIDATE_ARM)
    candidate_observation = candidate["observations"][1]
    assert isinstance(candidate_observation, dict)
    candidate_observation["frame_valid"] = False
    candidate_observation["cleanup"] = {"attempted": True, "succeeded": False, "survivors": 1}

    result = compare_promotion_artifacts(prior, candidate)

    assert result["eligible"] is False
    assert any("frame" in reason for reason in result["reasons"])
    assert any("cleanup" in reason for reason in result["reasons"])


def test_validator_rejects_secrets_and_invalid_samples() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["configuration"]["base_url"] = "https://example.invalid/?token=secret"
    with pytest.raises(PromotionGateError, match=r"secret|URL|unsafe"):
        validate_promotion_artifact(payload)


def test_comparator_does_not_echo_unvalidated_failure_fields_or_credentials() -> None:
    prior = _artifact(PRIOR_PUBLIC_ARM)
    candidate = _artifact(CANDIDATE_ARM)
    candidate["failures"] = [
        {"exception_type": "Authorization: Bearer leaked-secret", "phase": "warm"}
    ]

    result = compare_promotion_artifacts(prior, candidate)
    rendered = json.dumps(result)

    assert result["eligible"] is False
    assert result["reasons"] == ["promotion artifact validation failed"]
    assert result["failures"] == []
    assert "Bearer" not in rendered
    assert "leaked-secret" not in rendered


def test_validated_failure_records_use_only_fixed_safe_categories() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["failures"] = [
        {
            "phase": "measure",
            "error_category": "transport",
            "sample_index": 3,
            "status": "failed",
            "elapsed_ms": 1.5,
        }
    ]
    payload["status"] = "failed"
    validate_promotion_artifact(payload, expected_arm=PRIOR_PUBLIC_ARM)

    payload["failures"][0]["error_category"] = "customer-secret-value"
    with pytest.raises(PromotionGateError, match=r"category|failure"):
        validate_promotion_artifact(payload, expected_arm=PRIOR_PUBLIC_ARM)

    payload["failures"] = [{"phase": "private-task-text", "error_category": "unknown"}]
    with pytest.raises(PromotionGateError, match=r"phase|failure"):
        validate_promotion_artifact(payload, expected_arm=PRIOR_PUBLIC_ARM)


def test_validator_requires_non_null_nonnegative_timing_fields() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["observations"][0]["timings_ms"]["borrow_ms"] = None
    with pytest.raises(PromotionGateError, match=r"timing|borrow"):
        validate_promotion_artifact(payload)

    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["observations"][0]["timings_ms"]["dispatch_ms"] = -0.1
    with pytest.raises(PromotionGateError, match=r"timing|dispatch"):
        validate_promotion_artifact(payload)


def test_validator_requires_exactly_one_borrow_per_measured_trajectory() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["observations"][0]["borrow_count"] = 2

    with pytest.raises(PromotionGateError, match=r"borrow"):
        validate_promotion_artifact(payload)


def test_validator_requires_observed_backends_to_match_configuration() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["observations"][0]["attribution"]["input_backend"] = "xdotool"
    with pytest.raises(PromotionGateError, match=r"input backend|attribution"):
        validate_promotion_artifact(payload)

    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["observations"][0]["attribution"]["screenshot_transport"] = "raw-binary"
    with pytest.raises(PromotionGateError, match=r"screenshot|transport|attribution"):
        validate_promotion_artifact(payload)


def test_validator_requires_article_parity_warm_capacity_values() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["configuration"]["warm_capacity"]["function_min_containers"] = 1
    with pytest.raises(PromotionGateError, match=r"warm|capacity|min_containers"):
        validate_promotion_artifact(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caller_topology", "external-laptop"),
        ("ingress", "connect"),
        ("http_version", "2"),
        ("input_backend", "xdotool"),
        ("connection_reuse", "new-client-per-request"),
    ],
)
def test_validator_rejects_non_article_parity_execution_profiles(
    field: str, value: str
) -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["configuration"][field] = value

    with pytest.raises(PromotionGateError, match=r"article|topology|ingress|HTTP|input|connection"):
        validate_promotion_artifact(payload, expected_arm=PRIOR_PUBLIC_ARM)

    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["configuration"]["warm_capacity"]["sandbox_pool_capacity"] = 1
    with pytest.raises(PromotionGateError, match=r"warm|capacity|pool"):
        validate_promotion_artifact(payload)

    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["observations"] = payload["observations"][:-1]
    with pytest.raises(PromotionGateError, match=r"sample|observation|schedule"):
        validate_promotion_artifact(payload)


def test_sanitizer_keeps_raw_numeric_observations_and_rejects_secret_values() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    sanitized = sanitize_promotion_artifact(payload)

    assert sanitized["observations"] == payload["observations"]
    assert "base_url" not in json.dumps(sanitized)

    secret_payload = copy.deepcopy(payload)
    secret_payload["failures"] = [{"exception_type": "Authorization: Bearer abc"}]
    with pytest.raises(PromotionGateError, match=r"secret|unsafe"):
        sanitize_promotion_artifact(secret_payload)


def test_sanitizer_retains_partial_failed_attempt_without_promoting_it() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["status"] = "failed"
    payload["observations"] = payload["observations"][:1]
    payload["observations"][0]["status"] = "failed"
    payload["failures"] = [
        {"phase": "measure", "sample_index": 0, "error_category": "validation"}
    ]

    sanitized = sanitize_promotion_artifact(payload)

    assert sanitized["status"] == "failed"
    assert len(sanitized["observations"]) == 1
    result = compare_promotion_artifacts(sanitized, _artifact(CANDIDATE_ARM))
    assert result["decision"] == "reject"


def test_validator_requires_exact_placement_and_explicit_configuration() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM)
    payload["configuration"]["requested_placement"]["region"] = "us-west"

    with pytest.raises(PromotionGateError, match=r"region|placement"):
        validate_promotion_artifact(payload)


def test_promotion_gate_cli_compares_files_without_provider_calls(tmp_path, capsys) -> None:
    prior_path = tmp_path / "prior.json"
    candidate_path = tmp_path / "candidate.json"
    output_path = tmp_path / "result.json"
    prior_path.write_text(json.dumps(_artifact(PRIOR_PUBLIC_ARM)), encoding="utf-8")
    candidate_path.write_text(
        json.dumps(_artifact(CANDIDATE_ARM, warm_values=[8.0] * 30)), encoding="utf-8"
    )

    exit_code = cli.main(
        [
            "benchmark",
            "promotion-gate",
            "--prior-public",
            str(prior_path),
            "--candidate",
            str(candidate_path),
            "--output",
            str(output_path),
        ]
    )

    rendered = capsys.readouterr().out
    result = json.loads(rendered)
    assert exit_code == 0
    assert result["decision"] == "promote"
    assert json.loads(output_path.read_text(encoding="utf-8")) == result
    assert "https://" not in rendered
    assert "Bearer" not in rendered
