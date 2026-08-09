"""Evidence gate for X11 shared-memory screenshot capture.

This module is deliberately offline.  It validates a secret-free artifact from
the exact public ``await computer.screenshots.full()`` call and applies the
pre-agreed promotion thresholds without creating Modal resources.
"""

from __future__ import annotations

import math
import random
import re
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = 1
BENCHMARK = "x11-shm-screenshot-promotion"
BASELINE_ARM = "mss"
CANDIDATE_ARM = "x11-shm"
MINIMUM_SAMPLES_PER_ARM = 100
FIXED_GATES = {
    "minimum_p50_improvement_percent": 20.0,
    "maximum_p95_regression_percent": 5.0,
    "maximum_payload_growth_percent": 10.0,
    "minimum_daemon_saving_ms": 5.0,
}

_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_FULL_SHA256 = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_KEY_PARTS = (
    "app_url",
    "authorization",
    "base_url",
    "bearer",
    "clipboard",
    "endpoint",
    "password",
    "private_key",
    "screenshot_bytes",
    "secret",
    "token",
    "typed_text",
)


def validate_x11_shm_screenshot_artifact(payload: Mapping[str, Any]) -> None:
    """Validate complete, publishable promotion evidence.

    Rejected or partial attempts belong in untracked raw results.  A tracked
    artifact accepted here must be sufficient to make the default decision.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("screenshot promotion artifact must be an object")
    _validate_safe_value(payload)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported screenshot promotion schema")
    if payload.get("benchmark") != BENCHMARK:
        raise ValueError("unexpected screenshot promotion benchmark")
    if payload.get("status") != "complete":
        raise ValueError("publishable screenshot evidence must be complete")
    if payload.get("public_call") != "await computer.screenshots.full()":
        raise ValueError("promotion boundary must be the exact public SDK call")
    if payload.get("retries") != 0 or payload.get("replacement_samples") != 0:
        raise ValueError("retries and replacement samples must be zero")
    if payload.get("failures") != []:
        raise ValueError("publishable screenshot evidence cannot hide failures")

    preregistration = _mapping(payload.get("preregistration"), "preregistration")
    samples = _integer(preregistration.get("samples_per_arm"), "samples_per_arm")
    if samples < MINIMUM_SAMPLES_PER_ARM:
        raise ValueError("promotion requires at least 100 samples per arm")
    if preregistration.get("gates") != FIXED_GATES:
        raise ValueError("artifact must preserve the fixed promotion gates")
    _nonnegative_integer(preregistration.get("warmup_iterations"), "warmup_iterations")
    _positive_integer(preregistration.get("schedule_seed"), "schedule_seed")
    _positive_integer(preregistration.get("bootstrap_seed"), "bootstrap_seed")
    if _positive_integer(
        preregistration.get("bootstrap_resamples"), "bootstrap_resamples"
    ) < 100:
        raise ValueError("bootstrap_resamples must be at least 100")

    _validate_configuration(_mapping(payload.get("configuration"), "configuration"))
    _validate_schedule(payload.get("schedule"), samples=samples)

    arms = _mapping(payload.get("arms"), "arms")
    if set(arms) != {BASELINE_ARM, CANDIDATE_ARM}:
        raise ValueError("artifact must contain exactly MSS and x11-shm arms")
    for arm in (BASELINE_ARM, CANDIDATE_ARM):
        _validate_arm(_mapping(arms.get(arm), f"arms.{arm}"), arm=arm, samples=samples)

    fallback_counts = _mapping(payload.get("fallback_counts"), "fallback_counts")
    if fallback_counts != {BASELINE_ARM: 0, CANDIDATE_ARM: 0}:
        raise ValueError("publishable arms must have zero fallback")
    cleanup = _mapping(payload.get("cleanup"), "cleanup")
    if cleanup.get("succeeded") is not True or cleanup.get("remaining_sandboxes") != 0:
        raise ValueError("terminal screenshot benchmark cleanup must succeed")
    _validate_operational_gates(
        _mapping(payload.get("operational_gates"), "operational_gates")
    )


def evaluate_x11_shm_screenshot_promotion(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable gate decision for a validated artifact."""

    validate_x11_shm_screenshot_artifact(payload)
    arms = _mapping(payload["arms"], "arms")
    baseline = _observations(_mapping(arms[BASELINE_ARM], BASELINE_ARM))
    candidate = _observations(_mapping(arms[CANDIDATE_ARM], CANDIDATE_ARM))

    baseline_sdk = [float(item["complete_sdk_ms"]) for item in baseline]
    candidate_sdk = [float(item["complete_sdk_ms"]) for item in candidate]
    baseline_daemon = [float(item["daemon_total_ms"]) for item in baseline]
    candidate_daemon = [float(item["daemon_total_ms"]) for item in candidate]
    baseline_payload = [float(item["payload_bytes"]) for item in baseline]
    candidate_payload = [float(item["payload_bytes"]) for item in candidate]

    baseline_p50 = statistics.median(baseline_sdk)
    candidate_p50 = statistics.median(candidate_sdk)
    baseline_p95 = _percentile(baseline_sdk, 0.95)
    candidate_p95 = _percentile(candidate_sdk, 0.95)
    p50_improvement = _percent_change(baseline_p50, candidate_p50, improvement=True)
    p95_regression = _percent_change(baseline_p95, candidate_p95, improvement=False)
    payload_growth = _percent_change(
        statistics.median(baseline_payload),
        statistics.median(candidate_payload),
        improvement=False,
    )
    daemon_saving = statistics.median(baseline_daemon) - statistics.median(candidate_daemon)

    reasons: list[str] = []
    if p50_improvement < FIXED_GATES["minimum_p50_improvement_percent"]:
        reasons.append("complete public-SDK p50 improvement is below 20%")
    if p95_regression > FIXED_GATES["maximum_p95_regression_percent"]:
        reasons.append("complete public-SDK p95 regresses by more than 5%")
    if payload_growth > FIXED_GATES["maximum_payload_growth_percent"]:
        reasons.append("median PNG payload growth exceeds 10%")
    if daemon_saving < FIXED_GATES["minimum_daemon_saving_ms"]:
        reasons.append("daemon-side absolute saving is below 5 ms")

    preregistration = _mapping(payload["preregistration"], "preregistration")
    bootstrap_ci = _paired_bootstrap_median_difference(
        baseline_sdk,
        candidate_sdk,
        seed=int(preregistration["bootstrap_seed"]),
        resamples=int(preregistration["bootstrap_resamples"]),
    )
    return {
        "eligible": not reasons,
        "decision": "promote" if not reasons else "reject",
        "reasons": reasons,
        "paired_samples": len(baseline),
        "metrics": {
            "complete_sdk_ms": {
                "baseline_p50": baseline_p50,
                "candidate_p50": candidate_p50,
                "p50_improvement_percent": p50_improvement,
                "baseline_p95": baseline_p95,
                "candidate_p95": candidate_p95,
                "p95_regression_percent": p95_regression,
                "paired_median_saving_bootstrap_95_ci_ms": bootstrap_ci,
            },
            "daemon_total_ms": {
                "baseline_median": statistics.median(baseline_daemon),
                "candidate_median": statistics.median(candidate_daemon),
                "absolute_saving_ms": daemon_saving,
            },
            "payload_bytes": {
                "baseline_median": statistics.median(baseline_payload),
                "candidate_median": statistics.median(candidate_payload),
                "growth_percent": payload_growth,
            },
        },
    }


def _validate_configuration(configuration: Mapping[str, Any]) -> None:
    if _FULL_COMMIT.fullmatch(str(configuration.get("source_revision", ""))) is None:
        raise ValueError("source_revision must be a full Git commit")
    if configuration.get("worktree_clean") is not True:
        raise ValueError("publishable evidence requires a clean worktree")
    for key in ("native_source_sha256", "cargo_lock_sha256"):
        if _FULL_SHA256.fullmatch(str(configuration.get(key, ""))) is None:
            raise ValueError(f"{key} must be a SHA-256 digest")
    if configuration.get("rust_toolchain") != "rustc 1.91.0":
        raise ValueError("Rust toolchain must be pinned to rustc 1.91.0")
    if not str(configuration.get("python_version", "")).startswith("3.12."):
        raise ValueError("benchmark must use Python 3.12")
    if configuration.get("target") != "x86_64-unknown-linux-gnu":
        raise ValueError("benchmark target must be x86_64 Linux")
    if not str(configuration.get("image_identity", "")).strip():
        raise ValueError("image identity is required")
    if configuration.get("browser") != "chromium":
        raise ValueError("publishable evidence requires a real Chromium fixture")
    if configuration.get("display") != {"width": 1024, "height": 768, "depth": 24}:
        raise ValueError("display must be 1024x768x24")
    if configuration.get("screenshot") != {
        "format": "png",
        "lossless": True,
        "show_cursor": False,
        "scale": 1.0,
        "storage": "inline",
    }:
        raise ValueError("screenshot configuration changed")
    if configuration.get("connection_reuse") != "one-pooled-async-client-per-arm":
        raise ValueError("benchmark must reuse one pooled client per arm")
    requested = _mapping(configuration.get("requested_placement"), "requested_placement")
    observed = _mapping(configuration.get("observed_placement"), "observed_placement")
    for owner in ("runner", "target"):
        if _mapping(observed.get(owner), f"observed_placement.{owner}") != requested:
            raise ValueError("requested and observed placement must match")
    resources = _mapping(configuration.get("resources"), "resources")
    if resources.get("cpu") != 1.0 or resources.get("memory_mib") != 2048:
        raise ValueError("benchmark resources must stay fixed")


def _validate_schedule(value: Any, *, samples: int) -> None:
    if not isinstance(value, list) or len(value) != samples * 2:
        raise ValueError("schedule must contain one sample per arm in every pair")
    for pair_index in range(samples):
        pair = value[pair_index * 2 : pair_index * 2 + 2]
        if {item.get("arm") for item in pair if isinstance(item, Mapping)} != {
            BASELINE_ARM,
            CANDIDATE_ARM,
        }:
            raise ValueError("schedule pair must contain both arms")
        for position, item in enumerate(pair):
            if not isinstance(item, Mapping):
                raise ValueError("schedule entry must be an object")
            if item.get("sequence") != pair_index * 2 + position:
                raise ValueError("schedule sequence is not contiguous")
            if item.get("sample_index") != pair_index or item.get("position") != position:
                raise ValueError("schedule pair metadata is invalid")


def _validate_arm(arm_payload: Mapping[str, Any], *, arm: str, samples: int) -> None:
    if arm_payload.get("requested_source") != arm or arm_payload.get("expected_backend") != arm:
        raise ValueError(f"{arm} source/backend contract is invalid")
    observations = _observations(arm_payload)
    if len(observations) != samples:
        raise ValueError(f"{arm} observations do not match sample count")
    for index, observation in enumerate(observations):
        if observation.get("sample_index") != index or observation.get("status") != "ok":
            raise ValueError(f"{arm} observations are incomplete")
        if observation.get("capture_backend") != arm:
            raise ValueError(f"{arm} backend attribution mismatch")
        if observation.get("decoded_pixel_parity") is not True:
            raise ValueError(f"{arm} decoded pixel parity failed")
        if observation.get("metadata_parity") is not True:
            raise ValueError(f"{arm} metadata parity failed")
        for key in ("complete_sdk_ms", "daemon_total_ms", "hash_ms", "payload_bytes"):
            _nonnegative_number(observation.get(key), f"{arm}.{key}")


def _validate_operational_gates(gates: Mapping[str, Any]) -> None:
    for key in (
        "chromium_fixture",
        "failure_matrix",
        "concurrency_matrix",
        "cleanup_succeeded",
    ):
        if gates.get(key) is not True:
            raise ValueError(f"operational gate {key} did not pass")
    if gates.get("captures") != 10_000:
        raise ValueError("operational soak must contain 10000 captures")
    if gates.get("fd_delta") != 0 or gates.get("mapping_delta") != 0:
        raise ValueError("resource counts changed during the operational soak")
    if _nonnegative_integer(gates.get("rss_growth_bytes"), "rss_growth_bytes") > 16 * 1024 * 1024:
        raise ValueError("resource RSS growth exceeds the fixed 16 MiB ceiling")


def _paired_bootstrap_median_difference(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    seed: int,
    resamples: int,
) -> list[float]:
    rng = random.Random(seed)  # noqa: S311 - reproducible benchmark statistics.
    differences: list[float] = []
    sample_count = len(baseline)
    for _ in range(resamples):
        indexes = [rng.randrange(sample_count) for _ in range(sample_count)]
        differences.append(
            statistics.median(baseline[index] for index in indexes)
            - statistics.median(candidate[index] for index in indexes)
        )
    return [_percentile(differences, 0.025), _percentile(differences, 0.975)]


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction)


def _percent_change(baseline: float, candidate: float, *, improvement: bool) -> float:
    if baseline <= 0:
        raise ValueError("baseline metrics must be positive")
    delta = baseline - candidate if improvement else candidate - baseline
    return delta / baseline * 100.0


def _validate_safe_value(value: Any, *, key: str = "") -> None:
    normalized = key.lower().replace("-", "_")
    if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
        raise ValueError(f"unsafe benchmark key: {key}")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _validate_safe_value(child, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            _validate_safe_value(child)
    elif isinstance(value, str) and (
        value.startswith(("http://", "https://", "/")) or "Bearer " in value
    ):
        raise ValueError("unsafe benchmark string")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _observations(arm: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = arm.get("observations")
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError("arm observations must be a list of objects")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _positive_integer(value: Any, label: str) -> int:
    result = _integer(value, label)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _nonnegative_integer(value: Any, label: str) -> int:
    result = _integer(value, label)
    if result < 0:
        raise ValueError(f"{label} must be nonnegative")
    return result


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return result
