"""Offline evidence gate for the canonical Computer Step Interface."""

from __future__ import annotations

import copy
import math
import random
import re
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

PRIOR_PUBLIC_ARM = "prior-public"
CANDIDATE_ARM = "candidate-default"
STEP_PROMOTION_BENCHMARK = "computer-step-promotion"
STEP_PROMOTION_SCHEMA_VERSION = 1
MINIMUM_SAMPLES_PER_ARM = 100
DEFAULT_BOOTSTRAP_RESAMPLES = 2_000

_ARMS = (PRIOR_PUBLIC_ARM, CANDIDATE_ARM)
_TIMINGS = (
    "cold_start_ms",
    "startup_ms",
    "dispatch_ms",
    "borrow_ms",
    "action_to_frame_ms",
    "action_phase_ms",
    "screenshot_phase_ms",
    "daemon_total_ms",
    "transport_and_decode_ms",
)
_CONFIGURATION_FIELDS = (
    "caller_topology",
    "target_identity",
    "requested_placement",
    "observed_placement",
    "resources",
    "image_identity",
    "ingress",
    "http_version",
    "input_backend",
    "input_rate_limit_per_sec",
    "operation_pacing_ms",
    "screenshot",
    "action_scenario",
    "action_payload_sha256",
    "connection_reuse",
    "warm_capacity",
    "operation_transport",
)
_ARM_TRANSPORT = {
    PRIOR_PUBLIC_ARM: "actions-run-then-screenshots-full",
    CANDIDATE_ARM: "computer-step-envelope-v1",
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "benchmark",
    "arm",
    "status",
    "configuration",
    "preregistration",
    "schedule",
    "observations",
    "failures",
    "cleanup",
    "replacement_samples",
    "retries",
}
_PREREGISTRATION_FIELDS = {
    "samples_per_arm",
    "minimum_samples_per_arm",
    "warmup_iterations",
    "schedule_seed",
    "bootstrap_seed",
    "bootstrap_resamples",
}
_SCHEDULE_FIELDS = {
    "sequence",
    "phase",
    "pair_index",
    "sample_index",
    "position",
    "arm",
}
_OBSERVATION_FIELDS = {
    "sample_index",
    "status",
    "frame_valid",
    "freshness_verified",
    "cursor_position_verified",
    "capture_after_baseline",
    "connection_reused",
    "borrow_count",
    "timings_ms",
    "attribution",
    "cleanup",
}
_FAILURE_FIELDS = {"phase", "sample_index", "status", "error_category"}
_ERROR_CATEGORIES = {
    "action",
    "frame",
    "freshness",
    "attribution",
    "timeout",
    "operation",
    "configuration",
    "borrow",
    "cleanup",
}
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "base_url",
    "bearer",
    "clipboard",
    "cookie",
    "daemon_url",
    "endpoint",
    "frame_bytes",
    "password",
    "private_key",
    "screenshot_bytes",
    "secret",
    "token",
    "typed_text",
)
_FULL_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StepPromotionGateError(ValueError):
    """Raised when step-promotion evidence is incomplete or unsafe."""


def build_step_interleaved_schedule(
    *,
    samples_per_arm: int = MINIMUM_SAMPLES_PER_ARM,
    warmup_iterations: int = 2,
    seed: int = 20260808,
) -> list[dict[str, Any]]:
    """Build a deterministic paired schedule with randomized within-pair order."""

    if isinstance(samples_per_arm, bool) or samples_per_arm < MINIMUM_SAMPLES_PER_ARM:
        raise ValueError(f"samples_per_arm must be at least {MINIMUM_SAMPLES_PER_ARM}")
    if isinstance(warmup_iterations, bool) or warmup_iterations < 0:
        raise ValueError("warmup_iterations must be nonnegative")
    rng = random.Random(seed)  # noqa: S311 - deterministic benchmark schedule.
    schedule: list[dict[str, Any]] = []
    sequence = 0
    for phase, count in (("warmup", warmup_iterations), ("measure", samples_per_arm)):
        for pair_index in range(count):
            arms = list(_ARMS)
            rng.shuffle(arms)
            for position, arm in enumerate(arms):
                schedule.append(
                    {
                        "sequence": sequence,
                        "phase": phase,
                        "pair_index": pair_index,
                        "sample_index": pair_index if phase == "measure" else None,
                        "position": position,
                        "arm": arm,
                    }
                )
                sequence += 1
    return schedule


def validate_step_promotion_artifact(
    payload: Mapping[str, Any], *, expected_arm: str | None = None
) -> None:
    """Validate one complete or failed, sanitized step-promotion artifact."""

    if not isinstance(payload, Mapping):
        raise StepPromotionGateError("promotion artifact must be an object")
    _validate_safe_payload(payload)
    _require_exact_fields(payload, _TOP_LEVEL_FIELDS, "promotion artifact")
    if payload.get("schema_version") != STEP_PROMOTION_SCHEMA_VERSION:
        raise StepPromotionGateError("promotion artifact schema version is unsupported")
    if payload.get("benchmark") != STEP_PROMOTION_BENCHMARK:
        raise StepPromotionGateError("promotion artifact benchmark is invalid")
    arm = payload.get("arm")
    if arm not in _ARMS or (expected_arm is not None and arm != expected_arm):
        raise StepPromotionGateError("promotion artifact arm is invalid")
    if payload.get("status") not in {"complete", "failed"}:
        raise StepPromotionGateError("promotion artifact status is invalid")

    configuration = _mapping(payload.get("configuration"), "configuration")
    _validate_configuration(configuration, arm=str(arm))
    preregistration = _mapping(payload.get("preregistration"), "preregistration")
    _require_exact_fields(preregistration, _PREREGISTRATION_FIELDS, "preregistration")
    samples = _positive_int(preregistration.get("samples_per_arm"), "samples_per_arm")
    minimum = _positive_int(
        preregistration.get("minimum_samples_per_arm"), "minimum_samples_per_arm"
    )
    if minimum < MINIMUM_SAMPLES_PER_ARM or samples < MINIMUM_SAMPLES_PER_ARM:
        raise StepPromotionGateError(
            f"promotion requires at least {MINIMUM_SAMPLES_PER_ARM} samples per arm"
        )
    warmups = _nonnegative_int(
        preregistration.get("warmup_iterations"), "warmup_iterations"
    )
    schedule_seed = _positive_int(preregistration.get("schedule_seed"), "schedule_seed")
    _positive_int(preregistration.get("bootstrap_seed"), "bootstrap_seed")
    if _positive_int(preregistration.get("bootstrap_resamples"), "bootstrap_resamples") < 100:
        raise StepPromotionGateError("bootstrap_resamples must be at least 100")
    expected_schedule = build_step_interleaved_schedule(
        samples_per_arm=samples,
        warmup_iterations=warmups,
        seed=schedule_seed,
    )
    if payload.get("schedule") != expected_schedule:
        raise StepPromotionGateError("schedule differs from preregistration")
    for row in expected_schedule:
        _require_exact_fields(row, _SCHEDULE_FIELDS, "schedule row")

    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise StepPromotionGateError("observations must be a list")
    seen: set[int] = set()
    for observation in observations:
        _validate_observation(observation, arm=str(arm), samples=samples, seen=seen)
    if payload.get("status") == "complete" and seen != set(range(samples)):
        raise StepPromotionGateError("complete evidence must contain every sample exactly once")

    failures = payload.get("failures")
    if not isinstance(failures, list):
        raise StepPromotionGateError("failures must be a list")
    for failure in failures:
        _validate_failure(failure, samples=samples)
    if payload.get("replacement_samples") != 0:
        raise StepPromotionGateError("replacement samples are not allowed")
    if payload.get("retries") != 0:
        raise StepPromotionGateError("retries are not allowed")
    _validate_cleanup(
        payload.get("cleanup"),
        "run cleanup",
        require_success=payload.get("status") == "complete",
    )
    if payload.get("status") == "complete":
        if failures:
            raise StepPromotionGateError("complete evidence contains failures")
        cleanup = _mapping(payload.get("cleanup"), "run cleanup")
        if cleanup.get("succeeded") is not True or cleanup.get("survivors") != 0:
            raise StepPromotionGateError("complete evidence requires successful cleanup")


def compare_step_promotion_artifacts(
    prior_public: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply the preregistered paired-improvement and p95 gates."""

    try:
        validate_step_promotion_artifact(prior_public, expected_arm=PRIOR_PUBLIC_ARM)
        validate_step_promotion_artifact(candidate, expected_arm=CANDIDATE_ARM)
        if prior_public.get("status") != "complete" or candidate.get("status") != "complete":
            raise StepPromotionGateError("promotion requires complete evidence")
        if _comparable_configuration(prior_public["configuration"]) != _comparable_configuration(
            candidate["configuration"]
        ):
            raise StepPromotionGateError("configuration mismatch")
    except Exception:
        return {
            "eligible": False,
            "decision": "reject",
            "paired_samples": 0,
            "gate_metric": "action_to_frame_ms",
            "reasons": ["promotion artifact validation failed"],
            "metrics": {},
        }

    prior = {item["sample_index"]: item for item in prior_public["observations"]}
    proposed = {item["sample_index"]: item for item in candidate["observations"]}
    if set(prior) != set(proposed):
        return {
            "eligible": False,
            "decision": "reject",
            "paired_samples": 0,
            "gate_metric": "action_to_frame_ms",
            "reasons": ["prior and candidate observations are not complete pairs"],
            "metrics": {},
        }
    indexes = sorted(prior)
    prior_values = [float(prior[index]["timings_ms"]["action_to_frame_ms"]) for index in indexes]
    candidate_values = [
        float(proposed[index]["timings_ms"]["action_to_frame_ms"]) for index in indexes
    ]
    preregistration = prior_public["preregistration"]
    interval = _paired_bootstrap_interval(
        prior_values,
        candidate_values,
        seed=int(preregistration["bootstrap_seed"]),
        resamples=int(preregistration["bootstrap_resamples"]),
    )
    prior_p95 = _percentile(prior_values, 0.95)
    candidate_p95 = _percentile(candidate_values, 0.95)
    reasons: list[str] = []
    if interval[1] >= 0:
        reasons.append("paired bootstrap 95% confidence interval does not prove improvement")
    if candidate_p95 > prior_p95:
        reasons.append("candidate p95 regresses")
    metrics = {
        "prior_p50_ms": statistics.median(prior_values),
        "candidate_p50_ms": statistics.median(candidate_values),
        "prior_p95_ms": prior_p95,
        "candidate_p95_ms": candidate_p95,
        "paired_median_difference_ms": statistics.median(
            [candidate - old for old, candidate in zip(prior_values, candidate_values, strict=True)]
        ),
        "bootstrap_95_ci_ms": list(interval),
    }
    return {
        "eligible": not reasons,
        "decision": "promote" if not reasons else "reject",
        "paired_samples": len(indexes),
        "minimum_samples_per_arm": MINIMUM_SAMPLES_PER_ARM,
        "gate_metric": "action_to_frame_ms",
        "reasons": reasons,
        "metrics": metrics,
        "configuration": copy.deepcopy(_comparable_configuration(prior_public["configuration"])),
    }


def _validate_configuration(configuration: Mapping[str, Any], *, arm: str) -> None:
    _require_exact_fields(configuration, set(_CONFIGURATION_FIELDS), "configuration")
    missing = [name for name in _CONFIGURATION_FIELDS if name not in configuration]
    if missing:
        raise StepPromotionGateError(f"configuration is missing: {', '.join(missing)}")
    if configuration["caller_topology"] != "one-application-owned-modal-function":
        raise StepPromotionGateError("caller topology is invalid")
    requested = _mapping(configuration["requested_placement"], "requested placement")
    observed = _mapping(configuration["observed_placement"], "observed placement")
    _require_exact_fields(requested, {"cloud", "region"}, "requested placement")
    _require_exact_fields(observed, {"function", "target"}, "observed placement")
    for name, placement in observed.items():
        _require_exact_fields(
            _mapping(placement, f"observed {name} placement"),
            {"cloud", "region"},
            f"observed {name} placement",
        )
    if not observed or any(placement != requested for placement in observed.values()):
        raise StepPromotionGateError("observed placement does not match requested placement")
    if configuration["ingress"] != "attested-tunnel":
        raise StepPromotionGateError("ingress must be attested-tunnel")
    if configuration["http_version"] != "1.1":
        raise StepPromotionGateError("HTTP version must be 1.1")
    if configuration["input_backend"] != "xtest":
        raise StepPromotionGateError("input backend must be xtest")
    if configuration["input_rate_limit_per_sec"] != 20:
        raise StepPromotionGateError("input rate limit must retain the product default")
    if configuration["operation_pacing_ms"] != 125:
        raise StepPromotionGateError("operation pacing must protect the input rate limit")
    screenshot = _mapping(configuration["screenshot"], "screenshot")
    _require_exact_fields(
        screenshot,
        {
            "format",
            "quality",
            "scale",
            "show_cursor",
            "processing",
            "storage",
            "transport",
        },
        "screenshot",
    )
    if screenshot != {
        "format": "png",
        "quality": 90,
        "scale": 1.0,
        "show_cursor": False,
        "processing": "daemon",
        "storage": "inline",
        "transport": "raw-binary",
    }:
        raise StepPromotionGateError("screenshot configuration is invalid")
    if configuration["connection_reuse"] != "one-pooled-async-client":
        raise StepPromotionGateError("connection reuse is invalid")
    if configuration["operation_transport"] != _ARM_TRANSPORT[arm]:
        raise StepPromotionGateError("operation transport does not match arm")
    if not _FULL_SHA256.fullmatch(str(configuration["action_payload_sha256"])):
        raise StepPromotionGateError("action payload digest is invalid")
    for name in ("target_identity", "image_identity", "action_scenario"):
        if not isinstance(configuration[name], str) or not configuration[name].strip():
            raise StepPromotionGateError(f"{name} must be explicit")
    if not isinstance(configuration["resources"], Mapping) or not configuration["resources"]:
        raise StepPromotionGateError("resources must be explicit")
    capacity = _mapping(configuration["warm_capacity"], "warm capacity")
    if capacity != {"function_min_containers": 0, "sandbox_pool_capacity": 0}:
        raise StepPromotionGateError("warm capacity must be zero for this experiment")


def _validate_observation(
    observation: object, *, arm: str, samples: int, seen: set[int]
) -> None:
    item = _mapping(observation, "observation")
    _require_exact_fields(item, _OBSERVATION_FIELDS, "observation")
    index = _nonnegative_int(item.get("sample_index"), "sample_index")
    if index >= samples or index in seen:
        raise StepPromotionGateError("sample indexes must be unique and in range")
    seen.add(index)
    status = item.get("status")
    if status not in {"ok", "failed"}:
        raise StepPromotionGateError("observation status is invalid")
    if status == "ok":
        if item.get("frame_valid") is not True:
            raise StepPromotionGateError("frame validation failed")
        if item.get("freshness_verified") is not True:
            raise StepPromotionGateError("freshness verification failed")
        if item.get("cursor_position_verified") is not True:
            raise StepPromotionGateError("cursor-position verification failed")
        if item.get("capture_after_baseline") is not True:
            raise StepPromotionGateError("capture-time verification failed")
    elif any(
        item.get(name) is not False
        for name in (
            "frame_valid",
            "freshness_verified",
            "cursor_position_verified",
            "capture_after_baseline",
        )
    ):
        raise StepPromotionGateError("failed observation cannot claim a verified frame")
    if item.get("connection_reused") is not True or item.get("borrow_count") != 1:
        raise StepPromotionGateError("one reused connection and one borrow are required")
    timings = _mapping(item.get("timings_ms"), "timings")
    _require_exact_fields(timings, set(_TIMINGS), "timings")
    for name in _TIMINGS[:-2]:
        _nonnegative_number(timings.get(name), name)
    for name in _TIMINGS[-2:]:
        value = timings.get(name)
        if arm == CANDIDATE_ARM or value is not None:
            _nonnegative_number(value, name)
    attribution = _mapping(item.get("attribution"), "attribution")
    if attribution != {
        "input_backend": "xtest",
        "screenshot_transport": "raw-binary",
        "operation_transport": _ARM_TRANSPORT[arm],
    }:
        raise StepPromotionGateError("observation attribution is invalid")
    _validate_cleanup(item.get("cleanup"), "observation cleanup", require_success=False)


def _validate_failure(value: object, *, samples: int) -> None:
    failure = _mapping(value, "failure")
    _require_exact_fields(failure, _FAILURE_FIELDS, "failure")
    if failure["phase"] not in {"warmup", "measure", "borrow", "cleanup"}:
        raise StepPromotionGateError("failure phase is invalid")
    sample_index = failure["sample_index"]
    if sample_index is not None:
        index = _nonnegative_int(sample_index, "failure sample_index")
        if index >= samples:
            raise StepPromotionGateError("failure sample_index is out of range")
    if failure["phase"] in {"borrow", "cleanup"} and sample_index is not None:
        raise StepPromotionGateError("lifecycle failure cannot have a sample_index")
    if failure["phase"] == "measure" and sample_index is None:
        raise StepPromotionGateError("operation failure requires a sample_index")
    if failure["status"] != "failed":
        raise StepPromotionGateError("failure status is invalid")
    if failure["error_category"] not in _ERROR_CATEGORIES:
        raise StepPromotionGateError("failure error category is invalid")


def _validate_cleanup(value: object, name: str, *, require_success: bool) -> None:
    cleanup = _mapping(value, name)
    if set(cleanup) != {"attempted", "succeeded", "survivors"}:
        raise StepPromotionGateError(f"{name} is invalid")
    if cleanup["attempted"] is not True:
        raise StepPromotionGateError(f"{name} was not attempted")
    if not isinstance(cleanup["succeeded"], bool):
        raise StepPromotionGateError(f"{name} success flag is invalid")
    _nonnegative_int(cleanup["survivors"], f"{name} survivors")
    if require_success and cleanup["succeeded"] is not True:
        raise StepPromotionGateError(f"{name} did not succeed")
    if require_success and cleanup["survivors"] != 0:
        raise StepPromotionGateError(f"{name} has survivors")


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], name: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise StepPromotionGateError(
            f"{name} fields are invalid: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _comparable_configuration(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_mapping(value, "configuration")))
    result.pop("operation_transport", None)
    return result


def _paired_bootstrap_interval(
    prior: Sequence[float], candidate: Sequence[float], *, seed: int, resamples: int
) -> tuple[float, float]:
    differences = [new - old for old, new in zip(prior, candidate, strict=True)]
    rng = random.Random(seed)  # noqa: S311 - deterministic statistical resampling.
    medians = [
        statistics.median(rng.choice(differences) for _ in differences)
        for _ in range(resamples)
    ]
    return _percentile(medians, 0.025), _percentile(medians, 0.975)


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1 - fraction) + ordered[upper] * fraction)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StepPromotionGateError(f"{name} must be an object")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StepPromotionGateError(f"{name} must be positive")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StepPromotionGateError(f"{name} must be nonnegative")
    return value


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StepPromotionGateError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise StepPromotionGateError(f"{name} must be finite and nonnegative")
    return number


def _validate_safe_payload(value: object, *, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                raise StepPromotionGateError(f"unsafe secret-bearing field at {path}")
            _validate_safe_payload(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_safe_payload(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if "http://" in lowered or "https://" in lowered or "bearer " in lowered:
            raise StepPromotionGateError(f"unsafe secret-bearing value at {path}")
