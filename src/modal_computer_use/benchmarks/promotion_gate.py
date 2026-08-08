"""Offline evidence gate for the optimized SDK default.

The gate compares two sanitized arms that used one preregistered topology.  It does not
create a Modal resource and it does not know how to execute a benchmark.  A separate
runner may write the artifact; this module only validates and compares its evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PRIOR_PUBLIC_ARM = "prior-public"
CANDIDATE_ARM = "candidate-default"
PROMOTION_BENCHMARK = "optimized-default-promotion"
PROMOTION_SCHEMA_VERSION = 1
MINIMUM_SAMPLES_PER_ARM = 30
DEFAULT_BOOTSTRAP_RESAMPLES = 2_000
REGRESSION_RELATIVE_THRESHOLD_PERCENT = 5.0
REGRESSION_ABSOLUTE_THRESHOLD_MS = 0.25
PROMOTION_METRICS = (
    "cold_start_ms",
    "startup_ms",
    "dispatch_ms",
    "borrow_ms",
    "warm_operation_ms",
)
_TIMING_ALIASES = {
    "cold_ms": "cold_start_ms",
    "cold_allocation_ms": "cold_start_ms",
    "warm_ms": "warm_operation_ms",
}
_REQUIRED_CONFIGURATION = (
    "caller_topology",
    "target_identity",
    "requested_placement",
    "observed_placement",
    "resources",
    "image_identity",
    "ingress",
    "http_version",
    "input_backend",
    "screenshot",
    "action_payload_sha256",
    "warmup_iterations",
    "connection_reuse",
    "warm_capacity",
)
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "bearer",
    "base_url",
    "clipboard",
    "cookie",
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
_EXACT_REGION = re.compile(r"^[a-z][a-z0-9]*-[a-z][a-z0-9]*-[0-9][a-z0-9]*$")
_SAFE_FAILURE_PHASES = {
    "borrow",
    "cleanup",
    "cold_start",
    "dispatch",
    "measure",
    "release",
    "setup",
    "startup",
    "warmup",
}
_SAFE_ERROR_CATEGORIES = {
    "cancellation",
    "cleanup",
    "operation",
    "protocol",
    "timeout",
    "transport",
    "unknown",
    "validation",
}


class PromotionGateError(ValueError):
    """Raised when promotion evidence is malformed or unsafe."""


def build_interleaved_schedule(
    *,
    samples_per_arm: int = MINIMUM_SAMPLES_PER_ARM,
    warmup_iterations: int = 1,
    seed: int = 20260808,
) -> list[dict[str, Any]]:
    """Return a deterministic, paired schedule for one offline benchmark run."""

    if isinstance(samples_per_arm, bool) or samples_per_arm < MINIMUM_SAMPLES_PER_ARM:
        raise ValueError(f"samples_per_arm must be at least {MINIMUM_SAMPLES_PER_ARM}")
    if isinstance(warmup_iterations, bool) or warmup_iterations < 0:
        raise ValueError("warmup_iterations must be nonnegative")
    rng = random.Random(seed)  # noqa: S311 - deterministic benchmark scheduling only.
    schedule: list[dict[str, Any]] = []
    sequence = 0
    for phase, count in (("warmup", warmup_iterations), ("measure", samples_per_arm)):
        for pair_index in range(count):
            arms = [PRIOR_PUBLIC_ARM, CANDIDATE_ARM]
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


def validate_promotion_artifact(
    payload: Mapping[str, Any], *, expected_arm: str | None = None
) -> None:
    """Validate one complete or failed, secret-free arm artifact.

    Failed attempts remain valid evidence so the report can include them.  The comparison
    function applies the stricter promotion rule and rejects incomplete or failed arms.
    """

    if not isinstance(payload, Mapping):
        raise PromotionGateError("promotion artifact must be a JSON object")
    _validate_safe_payload(payload)
    if payload.get("schema_version") != PROMOTION_SCHEMA_VERSION:
        raise PromotionGateError("promotion artifact schema version is unsupported")
    if payload.get("benchmark") != PROMOTION_BENCHMARK:
        raise PromotionGateError("promotion artifact benchmark name is invalid")
    arm = payload.get("arm")
    if arm not in {PRIOR_PUBLIC_ARM, CANDIDATE_ARM}:
        raise PromotionGateError("promotion artifact arm is invalid")
    if expected_arm is not None and arm != expected_arm:
        raise PromotionGateError("promotion artifact arm does not match the requested arm")
    if payload.get("status") not in {"candidate", "complete", "failed", "rejected"}:
        raise PromotionGateError("promotion artifact status is invalid")

    configuration = _mapping(payload.get("configuration"), "configuration")
    _validate_configuration(configuration, expected_arm=arm)
    preregistration = _mapping(payload.get("preregistration"), "preregistration")
    samples_per_arm = _positive_int(
        preregistration.get("samples_per_arm"), "preregistration samples_per_arm"
    )
    minimum = preregistration.get("minimum_samples_per_arm", MINIMUM_SAMPLES_PER_ARM)
    if _positive_int(minimum, "preregistration minimum_samples_per_arm") < MINIMUM_SAMPLES_PER_ARM:
        raise PromotionGateError(
            f"preregistration minimum_samples_per_arm must be at least {MINIMUM_SAMPLES_PER_ARM}"
        )
    if samples_per_arm < MINIMUM_SAMPLES_PER_ARM:
        raise PromotionGateError(
            f"promotion requires at least {MINIMUM_SAMPLES_PER_ARM} measured samples per arm"
        )
    warmup_iterations = _nonnegative_int(
        preregistration.get("warmup_iterations"), "preregistration warmup_iterations"
    )
    bootstrap_resamples = preregistration.get("bootstrap_resamples", DEFAULT_BOOTSTRAP_RESAMPLES)
    if _positive_int(bootstrap_resamples, "preregistration bootstrap_resamples") < 100:
        raise PromotionGateError("bootstrap_resamples must be at least 100")

    schedule = payload.get("schedule")
    _validate_schedule(
        schedule,
        samples_per_arm=samples_per_arm,
        warmup_iterations=warmup_iterations,
        seed=preregistration.get("schedule_seed", 20260808),
    )
    observations = _observations(payload)
    if len(observations) != samples_per_arm:
        raise PromotionGateError("observations do not match the preregistered sample count")
    seen: set[int] = set()
    for observation in observations:
        _validate_observation(
            observation,
            samples_per_arm=samples_per_arm,
            seen=seen,
            configuration=configuration,
        )
    if seen != set(range(samples_per_arm)):
        raise PromotionGateError("observations must contain every measured sample index once")

    failures = payload.get("failures", [])
    if not isinstance(failures, list):
        raise PromotionGateError("failures must be a list")
    for failure in failures:
        _validate_failure(failure)
    _validate_cleanup(payload.get("cleanup"), "run cleanup")
    for key, value in (
        ("replacement_samples", payload.get("replacement_samples", 0)),
        ("retries", payload.get("retries", 0)),
    ):
        if value != 0:
            raise PromotionGateError(f"{key} must be zero; replacement or replay is not allowed")
    if payload.get("status") == "complete":
        if failures or any(observation.get("status") != "ok" for observation in observations):
            raise PromotionGateError("complete promotion artifact contains failed observations")
        if payload["cleanup"].get("succeeded") is not True:
            raise PromotionGateError("complete promotion artifact requires successful cleanup")


def sanitize_promotion_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated copy that contains only safe, numeric raw observations."""

    normalized = _canonicalize_payload(payload)
    validate_promotion_artifact(normalized)
    return copy.deepcopy(dict(normalized))


def compare_promotion_artifacts(
    prior_public: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare two artifacts and return a machine-readable promotion decision."""

    reasons: list[str] = []
    try:
        prior = _canonicalize_payload(prior_public)
        candidate_payload = _canonicalize_payload(candidate)
        validate_promotion_artifact(prior, expected_arm=PRIOR_PUBLIC_ARM)
        validate_promotion_artifact(candidate_payload, expected_arm=CANDIDATE_ARM)
    except Exception:
        return {
            "eligible": False,
            "decision": "reject",
            "paired_samples": 0,
            "reasons": ["promotion artifact validation failed"],
            "metrics": {},
            "failures": [],
        }

    reasons.extend(_configuration_mismatch_reasons(prior, candidate_payload))
    reasons.extend(_quality_reasons(prior, arm_label=PRIOR_PUBLIC_ARM))
    reasons.extend(_quality_reasons(candidate_payload, arm_label=CANDIDATE_ARM))
    if reasons:
        return {
            "eligible": False,
            "decision": "reject",
            "paired_samples": 0,
            "reasons": _deduplicate(reasons),
            "metrics": {},
            "failures": _safe_failure_report(prior, candidate_payload),
        }

    prior_observations = {item["sample_index"]: item for item in _observations(prior)}
    candidate_observations = {
        item["sample_index"]: item for item in _observations(candidate_payload)
    }
    if set(prior_observations) != set(candidate_observations):
        reasons.append("prior and candidate sample indexes do not form complete pairs")
        return {
            "eligible": False,
            "decision": "reject",
            "paired_samples": 0,
            "reasons": reasons,
            "metrics": {},
            "failures": _safe_failure_report(prior, candidate_payload),
        }

    preregistration = _mapping(prior.get("preregistration"), "preregistration")
    seed = _positive_int(preregistration.get("bootstrap_seed", 20260808), "bootstrap_seed")
    resamples = _positive_int(
        preregistration.get("bootstrap_resamples", DEFAULT_BOOTSTRAP_RESAMPLES),
        "bootstrap_resamples",
    )
    metrics: dict[str, Any] = {}
    for metric_index, metric in enumerate(PROMOTION_METRICS):
        prior_values = [
            float(_timings(prior_observations[index])[metric])
            for index in sorted(prior_observations)
        ]
        candidate_values = [
            float(_timings(candidate_observations[index])[metric])
            for index in sorted(candidate_observations)
        ]
        metrics[metric] = _paired_metric_summary(
            prior_values,
            candidate_values,
            seed=seed + metric_index,
            resamples=resamples,
        )

    warm_metric = metrics["warm_operation_ms"]
    if (
        warm_metric["bootstrap_95_ci_relative_percent"][0] > REGRESSION_RELATIVE_THRESHOLD_PERCENT
        and warm_metric["bootstrap_95_ci_ms"][0] > REGRESSION_ABSOLUTE_THRESHOLD_MS
    ):
        reasons.append(
            "candidate warm-operation regression exceeds both 5% and 0.25 ms at the "
            "95% confidence lower bound"
        )
    eligible = not reasons
    return {
        "eligible": eligible,
        "decision": "promote" if eligible else "reject",
        "paired_samples": len(prior_observations),
        "minimum_samples_per_arm": MINIMUM_SAMPLES_PER_ARM,
        "gate_metric": "warm_operation_ms",
        "thresholds": {
            "relative_regression_percent": REGRESSION_RELATIVE_THRESHOLD_PERCENT,
            "absolute_regression_ms": REGRESSION_ABSOLUTE_THRESHOLD_MS,
        },
        "reasons": _deduplicate(reasons),
        "metrics": metrics,
        "failures": _safe_failure_report(prior, candidate_payload),
        "configuration": copy.deepcopy(prior["configuration"]),
    }


def load_promotion_artifact(path: Path) -> dict[str, Any]:
    """Load one JSON artifact without exposing file contents in an exception."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionGateError(
            f"could not read promotion artifact: {type(exc).__name__}"
        ) from exc
    if not isinstance(raw, dict):
        raise PromotionGateError("promotion artifact must be a JSON object")
    return raw


def serialize_promotion_result(payload: Mapping[str, Any]) -> str:
    """Serialize a result deterministically for CI and review."""

    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n"


def promotion_artifact_sha256(payload: Mapping[str, Any]) -> str:
    """Return a stable digest for a sanitized artifact."""

    return hashlib.sha256(serialize_promotion_result(payload).encode()).hexdigest()


def _canonicalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(payload))
    if "observations" not in result and "samples" in result:
        result["observations"] = result.pop("samples")
    schedule = result.get("schedule")
    if schedule is None:
        preregistration = result.get("preregistration")
        if isinstance(preregistration, Mapping):
            try:
                result["schedule"] = build_interleaved_schedule(
                    samples_per_arm=int(preregistration.get("samples_per_arm", 0)),
                    warmup_iterations=int(preregistration.get("warmup_iterations", 0)),
                    seed=int(preregistration.get("schedule_seed", 20260808)),
                )
            except (TypeError, ValueError) as exc:
                raise PromotionGateError("preregistration schedule is malformed") from exc
    observations = result.get("observations")
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            timings = observation.get("timings_ms")
            if isinstance(timings, dict):
                for source, target in _TIMING_ALIASES.items():
                    if target not in timings and source in timings:
                        timings[target] = timings[source]
            frame = observation.get("frame")
            if isinstance(frame, Mapping) and "frame_valid" not in observation:
                observation["frame_valid"] = frame.get("valid")
    return result


def _validate_configuration(
    configuration: Mapping[str, Any], *, expected_arm: str | None = None
) -> None:
    missing = [key for key in _REQUIRED_CONFIGURATION if key not in configuration]
    if missing:
        raise PromotionGateError(f"configuration is missing required fields: {', '.join(missing)}")
    if configuration["caller_topology"] != "one-application-owned-modal-function":
        raise PromotionGateError(
            "caller_topology must be one application-owned Modal Function"
        )
    if (
        not isinstance(configuration["target_identity"], str)
        or not configuration["target_identity"].strip()
    ):
        raise PromotionGateError("target_identity must be a stable non-secret identity")
    requested = _placement(configuration["requested_placement"], "requested placement")
    observed = configuration["observed_placement"]
    if not isinstance(observed, Mapping) or not observed:
        raise PromotionGateError("observed placement is required")
    for role, placement in observed.items():
        _placement(placement, f"observed placement {role}")
    for role, placement in observed.items():
        if placement != requested:
            raise PromotionGateError(
                f"observed placement {role} does not match requested placement"
            )
    resources = configuration["resources"]
    if not isinstance(resources, Mapping) or not resources:
        raise PromotionGateError("resources must be explicit")
    if (
        not isinstance(configuration["image_identity"], str)
        or not configuration["image_identity"].strip()
    ):
        raise PromotionGateError("image_identity must be explicit")
    required_profile = {
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": "xtest",
        "connection_reuse": "one-pooled-async-client",
    }
    for key, expected in required_profile.items():
        if configuration[key] != expected:
            raise PromotionGateError(
                f"article-parity {key} must be {expected}"
            )
    screenshot = configuration["screenshot"]
    if not isinstance(screenshot, Mapping) or not screenshot:
        raise PromotionGateError("screenshot configuration must be explicit")
    if screenshot.get("format") != "png":
        raise PromotionGateError("promotion screenshot format must be png")
    transport = screenshot.get("transport")
    if transport not in {"raw-binary", "json-base64"}:
        raise PromotionGateError("promotion screenshot transport is unsupported")
    if expected_arm == CANDIDATE_ARM and transport != "raw-binary":
        raise PromotionGateError("candidate screenshot transport must be raw-binary")
    if expected_arm == PRIOR_PUBLIC_ARM and transport != "json-base64":
        raise PromotionGateError("prior screenshot transport must be json-base64")
    if screenshot.get("show_cursor") is not False:
        raise PromotionGateError("promotion screenshot must hide the cursor")
    if not _FULL_SHA256.fullmatch(str(configuration["action_payload_sha256"])):
        raise PromotionGateError("action_payload_sha256 must be a SHA-256 digest")
    _nonnegative_int(configuration["warmup_iterations"], "warmup_iterations")
    warm_capacity = configuration["warm_capacity"]
    if not isinstance(warm_capacity, Mapping):
        raise PromotionGateError("warm_capacity must be explicit")
    for name in ("function_min_containers", "sandbox_pool_capacity"):
        value = warm_capacity.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PromotionGateError(f"warm_capacity {name} must be a nonnegative integer")
        if value != 0:
            raise PromotionGateError(
                f"warm_capacity {name} must be zero for the article-parity gate"
            )


def _placement(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PromotionGateError(f"{name} must include cloud and exact region")
    cloud = value.get("cloud")
    region = value.get("region")
    if not isinstance(cloud, str) or not cloud.strip():
        raise PromotionGateError(f"{name} cloud is missing")
    if not isinstance(region, str) or not _EXACT_REGION.fullmatch(region):
        raise PromotionGateError(f"{name} must use an exact region")
    return {"cloud": cloud, "region": region}


def _validate_schedule(
    schedule_value: Any, *, samples_per_arm: int, warmup_iterations: int, seed: Any
) -> None:
    if not isinstance(schedule_value, list):
        raise PromotionGateError("interleaved schedule is required")
    expected = build_interleaved_schedule(
        samples_per_arm=samples_per_arm,
        warmup_iterations=warmup_iterations,
        seed=_positive_int(seed, "schedule_seed"),
    )
    if schedule_value != expected:
        raise PromotionGateError("schedule does not match the preregistered interleaved order")


def _observations(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("observations")
    if not isinstance(value, list):
        raise PromotionGateError("observations must be a list")
    if any(not isinstance(item, dict) for item in value):
        raise PromotionGateError("observations must contain JSON objects")
    return value


def _validate_observation(
    observation: Mapping[str, Any],
    *,
    samples_per_arm: int,
    seen: set[int],
    configuration: Mapping[str, Any],
) -> None:
    index = observation.get("sample_index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < samples_per_arm:
        raise PromotionGateError("observation sample_index is invalid")
    if index in seen:
        raise PromotionGateError("observation sample indexes must be unique")
    seen.add(index)
    status = observation.get("status")
    if status not in {"ok", "failed", "timeout"}:
        raise PromotionGateError("observation status is invalid")
    timings = _timings(observation)
    for metric in PROMOTION_METRICS:
        value = timings.get(metric)
        if not _nonnegative_number(value):
            raise PromotionGateError(
                f"observation timing {metric} must be non-null and nonnegative"
            )
    frame_valid = observation.get("frame_valid")
    if not isinstance(frame_valid, bool):
        raise PromotionGateError("observation frame_valid must be boolean")
    connection_reused = observation.get("connection_reused")
    if not isinstance(connection_reused, bool):
        raise PromotionGateError("observation connection_reused must be boolean")
    if observation.get("borrow_count") != 1:
        raise PromotionGateError("each measured trajectory must enter borrow exactly once")
    cleanup = observation.get("cleanup")
    _validate_cleanup(cleanup, "observation cleanup")
    attribution = observation.get("attribution")
    if not isinstance(attribution, Mapping):
        raise PromotionGateError("observation attribution is required")
    if attribution.get("input_backend") != configuration["input_backend"]:
        raise PromotionGateError("observed input backend attribution does not match configuration")
    screenshot = configuration["screenshot"]
    if not isinstance(screenshot, Mapping):
        raise PromotionGateError("screenshot configuration is invalid")
    if attribution.get("screenshot_transport") != screenshot["transport"]:
        raise PromotionGateError(
            "observed screenshot transport attribution does not match configuration"
        )
    if status == "ok" and not _nonnegative_number(attribution.get("daemon_ms")):
        raise PromotionGateError("successful observation lacks daemon timing attribution")


def _timings(observation: Mapping[str, Any]) -> dict[str, Any]:
    value = observation.get("timings_ms")
    if not isinstance(value, Mapping):
        raise PromotionGateError("observation timings_ms are required")
    result = dict(value)
    for source, target in _TIMING_ALIASES.items():
        if target not in result and source in result:
            result[target] = result[source]
    missing = [metric for metric in PROMOTION_METRICS if metric not in result]
    if missing:
        raise PromotionGateError(f"observation timings are missing: {', '.join(missing)}")
    return result


def _validate_cleanup(value: Any, name: str) -> None:
    if not isinstance(value, Mapping):
        raise PromotionGateError(f"{name} is required")
    if value.get("attempted") is not True or not isinstance(value.get("succeeded"), bool):
        raise PromotionGateError(f"{name} must record attempted and succeeded state")
    survivors = value.get("survivors", 0)
    if isinstance(survivors, bool) or not isinstance(survivors, int) or survivors < 0:
        raise PromotionGateError(f"{name} survivors must be a nonnegative integer")


def _validate_failure(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise PromotionGateError("failures must contain objects")
    allowed = {"phase", "sample_index", "error_category", "status", "elapsed_ms"}
    if set(value) - allowed:
        raise PromotionGateError(
            "failure records may not contain messages or secret-bearing fields"
        )
    if value.get("phase") not in _SAFE_FAILURE_PHASES:
        raise PromotionGateError("failure phase is invalid")
    if value.get("error_category") not in _SAFE_ERROR_CATEGORIES:
        raise PromotionGateError("failure error category is invalid")
    sample_index = value.get("sample_index")
    if sample_index is not None and (
        isinstance(sample_index, bool) or not isinstance(sample_index, int) or sample_index < 0
    ):
        raise PromotionGateError("failure sample_index is invalid")
    if value.get("status") is not None and value["status"] not in {
        "cancelled",
        "failed",
        "timeout",
    }:
        raise PromotionGateError("failure status is invalid")
    if value.get("elapsed_ms") is not None and not _nonnegative_number(
        value["elapsed_ms"]
    ):
        raise PromotionGateError("failure elapsed_ms is invalid")


def _validate_safe_payload(value: Any, *, key: str | None = None) -> None:
    if key is not None:
        normalized = key.lower().replace("-", "_")
        if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
            raise PromotionGateError("promotion artifact contains a secret-bearing field")
    if isinstance(value, Mapping):
        for item_key, item_value in value.items():
            if not isinstance(item_key, str):
                raise PromotionGateError("promotion artifact keys must be strings")
            _validate_safe_payload(item_value, key=item_key)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_safe_payload(item)
        return
    if isinstance(value, (bytes, bytearray)):
        raise PromotionGateError("promotion artifact may not contain binary payloads")
    if isinstance(value, str):
        lowered = value.lower()
        if "authorization: bearer " in lowered or "bearer " in lowered:
            raise PromotionGateError("promotion artifact contains an unsafe credential")
        if lowered.startswith(("http://", "https://")):
            raise PromotionGateError("promotion artifact may not contain URLs")


def _configuration_mismatch_reasons(
    prior: Mapping[str, Any], candidate: Mapping[str, Any]
) -> list[str]:
    prior_config = _mapping(prior.get("configuration"), "prior configuration")
    candidate_config = _mapping(candidate.get("configuration"), "candidate configuration")
    reasons: list[str] = []
    for key in _REQUIRED_CONFIGURATION:
        if key == "screenshot":
            prior_screenshot = dict(_mapping(prior_config.get(key), "prior screenshot"))
            candidate_screenshot = dict(
                _mapping(candidate_config.get(key), "candidate screenshot")
            )
            # The transport is the measured adapter difference. Request format and cursor
            # policy remain fixed between arms.
            prior_screenshot.pop("transport", None)
            candidate_screenshot.pop("transport", None)
            if prior_screenshot != candidate_screenshot:
                reasons.append("configuration mismatch for screenshot")
            continue
        if prior_config.get(key) != candidate_config.get(key):
            reasons.append(f"configuration mismatch for {key}")
    return reasons


def _quality_reasons(payload: Mapping[str, Any], *, arm_label: str) -> list[str]:
    reasons: list[str] = []
    if payload.get("status") != "complete":
        reasons.append(f"{arm_label} artifact is not complete")
    failures = payload.get("failures")
    if isinstance(failures, list) and failures:
        reasons.append(f"{arm_label} reported benchmark failures")
    cleanup = payload.get("cleanup")
    if isinstance(cleanup, Mapping) and cleanup.get("succeeded") is not True:
        reasons.append(f"{arm_label} cleanup did not succeed")
    for observation in _observations(payload):
        index = observation.get("sample_index", "?")
        if observation.get("status") != "ok":
            reasons.append(f"{arm_label} sample {index} did not complete")
        if observation.get("frame_valid") is not True:
            reasons.append(f"{arm_label} sample {index} frame validation failed")
        if observation.get("connection_reused") is not True:
            reasons.append(f"{arm_label} sample {index} did not reuse its connection")
        cleanup_value = observation.get("cleanup")
        if isinstance(cleanup_value, Mapping) and cleanup_value.get("succeeded") is not True:
            reasons.append(f"{arm_label} sample {index} cleanup failed")
        attribution = observation.get("attribution")
        if not isinstance(attribution, Mapping) or not _nonnegative_number(
            attribution.get("daemon_ms")
        ):
            reasons.append(f"{arm_label} sample {index} lacks timing attribution")
    return reasons


def _paired_metric_summary(
    prior_values: Sequence[float],
    candidate_values: Sequence[float],
    *,
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    prior_p50 = statistics.median(prior_values)
    candidate_p50 = statistics.median(candidate_values)
    deltas = [
        candidate - prior for prior, candidate in zip(prior_values, candidate_values, strict=True)
    ]
    delta_p50 = statistics.median(deltas)
    relative = (candidate_p50 - prior_p50) / prior_p50 * 100 if prior_p50 else None
    rng = random.Random(seed)  # noqa: S311 - deterministic evidence summary.
    bootstrap_deltas: list[float] = []
    bootstrap_relative: list[float] = []
    indexes = range(len(deltas))
    for _ in range(resamples):
        selected = [rng.choice(tuple(indexes)) for _ in deltas]
        prior_sample = [prior_values[index] for index in selected]
        candidate_sample = [candidate_values[index] for index in selected]
        bootstrap_deltas.append(
            statistics.median(
                candidate - prior
                for prior, candidate in zip(prior_sample, candidate_sample, strict=True)
            )
        )
        prior_sample_p50 = statistics.median(prior_sample)
        candidate_sample_p50 = statistics.median(candidate_sample)
        bootstrap_relative.append(
            (candidate_sample_p50 - prior_sample_p50) / prior_sample_p50 * 100
            if prior_sample_p50
            else 0.0
        )
    return {
        "prior_p50_ms": prior_p50,
        "candidate_p50_ms": candidate_p50,
        "prior_p95_ms": _percentile(prior_values, 95),
        "candidate_p95_ms": _percentile(candidate_values, 95),
        "paired_delta_p50_ms": delta_p50,
        "paired_delta_relative_percent": relative,
        "bootstrap_95_ci_ms": [
            _percentile(bootstrap_deltas, 2.5),
            _percentile(bootstrap_deltas, 97.5),
        ],
        "bootstrap_95_ci_relative_percent": [
            _percentile(bootstrap_relative, 2.5),
            _percentile(bootstrap_relative, 97.5),
        ],
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise PromotionGateError("cannot summarize an empty sample")
    if len(ordered) == 1:
        return ordered[0]
    rank = percentile / 100 * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _safe_failure_report(*payloads: Mapping[str, Any]) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for payload in payloads:
        arm = payload.get("arm")
        failures = payload.get("failures")
        if isinstance(failures, list):
            for item in failures:
                if not isinstance(item, Mapping):
                    continue
                report.append(
                    {
                        "arm": arm,
                        "phase": item.get("phase"),
                        "sample_index": item.get("sample_index"),
                        "error_category": item.get("error_category"),
                        **(
                            {"status": item["status"]}
                            if item.get("status") is not None
                            else {}
                        ),
                        **(
                            {"elapsed_ms": item["elapsed_ms"]}
                            if item.get("elapsed_ms") is not None
                            else {}
                        ),
                    }
                )
    return report


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotionGateError(f"{name} must be an object")
    return dict(value)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PromotionGateError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PromotionGateError(f"{name} must be a nonnegative integer")
    return value


def _nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _positive_number(value: Any) -> bool:
    return _nonnegative_number(value) and value > 0


def _deduplicate(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
