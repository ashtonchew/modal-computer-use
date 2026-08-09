from __future__ import annotations

import copy

import pytest

from modal_computer_use.benchmarks.step_promotion_gate import (
    CANDIDATE_ARM,
    MINIMUM_SAMPLES_PER_ARM,
    PRIOR_PUBLIC_ARM,
    StepPromotionGateError,
    build_step_interleaved_schedule,
    compare_step_promotion_artifacts,
    validate_step_promotion_artifact,
)


def _configuration(arm: str) -> dict[str, object]:
    return {
        "caller_topology": "one-application-owned-modal-function",
        "target_identity": "deterministic-color-target-v1",
        "requested_placement": {"cloud": "aws", "region": "us-west-2"},
        "observed_placement": {
            "function": {"cloud": "aws", "region": "us-west-2"},
            "target": {"cloud": "aws", "region": "us-west-2"},
        },
        "resources": {
            "function": {"cpu": 1, "memory_mib": 2048},
            "sandbox": {"cpu": 1, "memory_mib": 2048},
        },
        "image_identity": "image-sha-abc123",
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": "xtest",
        "input_rate_limit_per_sec": 20,
        "operation_pacing_ms": 125,
        "screenshot": {
            "format": "png",
            "quality": 90,
            "scale": 1.0,
            "show_cursor": False,
            "processing": "daemon",
            "storage": "inline",
            "transport": "raw-binary",
        },
        "action_scenario": "reset-then-click-color-target-v1",
        "action_payload_sha256": "a" * 64,
        "connection_reuse": "one-pooled-async-client",
        "warm_capacity": {"function_min_containers": 0, "sandbox_pool_capacity": 0},
        "operation_transport": (
            "actions-run-then-screenshots-full"
            if arm == PRIOR_PUBLIC_ARM
            else "computer-step-envelope-v1"
        ),
    }


def _artifact(arm: str, values: list[float]) -> dict[str, object]:
    observations = [
        {
            "sample_index": index,
            "status": "ok",
            "frame_valid": True,
            "freshness_verified": True,
            "cursor_position_verified": True,
            "capture_after_baseline": True,
            "connection_reused": True,
            "borrow_count": 1,
            "timings_ms": {
                "cold_start_ms": 0.0,
                "startup_ms": 0.0,
                "dispatch_ms": 1.0,
                "borrow_ms": 0.5,
                "action_to_frame_ms": value,
                "action_phase_ms": value * 0.25,
                "screenshot_phase_ms": value * 0.5,
                "daemon_total_ms": value * 0.75,
                "transport_and_decode_ms": value * 0.25,
            },
            "attribution": {
                "input_backend": "xtest",
                "screenshot_transport": "raw-binary",
                "operation_transport": _configuration(arm)["operation_transport"],
            },
            "cleanup": {"attempted": True, "succeeded": True, "survivors": 0},
        }
        for index, value in enumerate(values)
    ]
    return {
        "schema_version": 1,
        "benchmark": "computer-step-promotion",
        "arm": arm,
        "status": "complete",
        "configuration": _configuration(arm),
        "preregistration": {
            "samples_per_arm": len(values),
            "minimum_samples_per_arm": MINIMUM_SAMPLES_PER_ARM,
            "warmup_iterations": 2,
            "schedule_seed": 42,
            "bootstrap_seed": 7,
            "bootstrap_resamples": 500,
        },
        "schedule": build_step_interleaved_schedule(
            samples_per_arm=len(values), warmup_iterations=2, seed=42
        ),
        "observations": observations,
        "failures": [],
        "cleanup": {"attempted": True, "succeeded": True, "survivors": 0},
        "replacement_samples": 0,
        "retries": 0,
    }


def test_step_schedule_preregisters_at_least_one_hundred_pairs() -> None:
    schedule = build_step_interleaved_schedule(
        samples_per_arm=MINIMUM_SAMPLES_PER_ARM,
        warmup_iterations=2,
        seed=42,
    )

    measured = [row for row in schedule if row["phase"] == "measure"]
    assert MINIMUM_SAMPLES_PER_ARM == 100
    assert len(measured) == 200
    assert all(
        {row["arm"] for row in measured[index : index + 2]}
        == {PRIOR_PUBLIC_ARM, CANDIDATE_ARM}
        for index in range(0, len(measured), 2)
    )


def test_step_gate_promotes_strict_paired_improvement_without_p95_regression() -> None:
    prior = _artifact(PRIOR_PUBLIC_ARM, [20.0 + (index % 4) for index in range(100)])
    candidate = _artifact(CANDIDATE_ARM, [10.0 + (index % 4) for index in range(100)])

    result = compare_step_promotion_artifacts(prior, candidate)

    assert result["eligible"] is True
    assert result["decision"] == "promote"
    assert result["paired_samples"] == 100
    assert result["gate_metric"] == "action_to_frame_ms"
    assert result["metrics"]["bootstrap_95_ci_ms"][1] < 0
    assert result["metrics"]["candidate_p95_ms"] <= result["metrics"]["prior_p95_ms"]


def test_step_gate_rejects_missing_freshness_and_configuration_drift() -> None:
    prior = _artifact(PRIOR_PUBLIC_ARM, [20.0] * 100)
    candidate = _artifact(CANDIDATE_ARM, [10.0] * 100)
    candidate["observations"][0]["freshness_verified"] = False

    with pytest.raises(StepPromotionGateError, match="freshness"):
        validate_step_promotion_artifact(candidate, expected_arm=CANDIDATE_ARM)

    candidate = _artifact(CANDIDATE_ARM, [10.0] * 100)
    candidate["configuration"]["http_version"] = "2"
    result = compare_step_promotion_artifacts(prior, candidate)
    assert result["eligible"] is False
    assert result["reasons"] == ["promotion artifact validation failed"]


def test_step_gate_requires_strict_ci_improvement_and_no_p95_regression() -> None:
    prior = _artifact(PRIOR_PUBLIC_ARM, [20.0] * 100)
    same = _artifact(CANDIDATE_ARM, [20.0] * 100)

    result = compare_step_promotion_artifacts(prior, same)
    assert result["eligible"] is False
    assert any("confidence interval" in reason for reason in result["reasons"])

    tail = _artifact(CANDIDATE_ARM, [10.0] * 94 + [40.0] * 6)
    result = compare_step_promotion_artifacts(prior, tail)
    assert result["eligible"] is False
    assert any("p95" in reason for reason in result["reasons"])


def test_step_gate_rejects_retries_replacements_and_secret_bearing_fields() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM, [20.0] * 100)
    payload["retries"] = 1
    with pytest.raises(StepPromotionGateError, match="retries"):
        validate_step_promotion_artifact(payload)

    payload = _artifact(PRIOR_PUBLIC_ARM, [20.0] * 100)
    payload["configuration"]["daemon_url"] = "https://private.invalid/?token=secret"
    with pytest.raises(StepPromotionGateError, match=r"secret|unsafe"):
        validate_step_promotion_artifact(payload)

    payload = copy.deepcopy(_artifact(PRIOR_PUBLIC_ARM, [20.0] * 100))
    payload["replacement_samples"] = 1
    with pytest.raises(StepPromotionGateError, match="replacement"):
        validate_step_promotion_artifact(payload)


def test_step_gate_rejects_unknown_fields_and_non_allowlisted_failure_categories() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM, [20.0] * 100)
    payload["unexpected"] = "secret-value"
    with pytest.raises(StepPromotionGateError, match="fields"):
        validate_step_promotion_artifact(payload)

    payload = _artifact(PRIOR_PUBLIC_ARM, [20.0] * 100)
    payload["status"] = "failed"
    payload["observations"] = []
    payload["failures"] = [
        {
            "phase": "measure",
            "sample_index": 0,
            "status": "failed",
            "error_category": "https://secret.invalid/?token=private",
        }
    ]
    with pytest.raises(StepPromotionGateError, match=r"error category|secret"):
        validate_step_promotion_artifact(payload)


def test_step_gate_accepts_a_sanitized_failed_artifact() -> None:
    payload = _artifact(PRIOR_PUBLIC_ARM, [20.0] * 100)
    payload["status"] = "failed"
    payload["observations"] = []
    payload["failures"] = [
        {
            "phase": "measure",
            "sample_index": 0,
            "status": "failed",
            "error_category": "freshness",
        }
    ]

    validate_step_promotion_artifact(payload)
