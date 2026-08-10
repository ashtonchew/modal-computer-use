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
OPERATIONAL_CEILINGS = {
    "maximum_readiness_p95_regression_percent": 5.0,
    "maximum_concurrency_p95_regression_percent": 5.0,
    "maximum_rss_growth_bytes": 16 * 1024 * 1024,
    "maximum_fd_delta": 0,
    "maximum_mapping_delta": 0,
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
_FAILURE_CHECKS = {
    "close_idempotent",
    "closed_capture_rejected",
    "constructor_geometry_failure",
    "invalid_region_rejected",
    "attach_failure_falls_back_once",
    "encode_failure_falls_back_once",
    "invalid_result_falls_back_once",
    "extension_load_failure_selects_mss",
    "close_failure_reported",
}


def validate_x11_shm_screenshot_artifact(
    payload: Mapping[str, Any], *, require_publishable: bool = True
) -> None:
    """Validate publishable or explicitly rejected promotion evidence.

    The default requires a complete publishable artifact.  A caller may opt
    into the rejected status to retain a secret-free operational failure and
    its promotion decision without making it eligible for the default.
    """

    _validate_artifact_structure(payload, require_publishable=require_publishable)
    actual = _mapping(payload.get("promotion"), "promotion")
    expected = _evaluate_validated_artifact(payload)
    if actual != expected:
        raise ValueError("promotion decision does not match the retained observations")


def _validate_artifact_structure(
    payload: Mapping[str, Any], *, require_publishable: bool = True
) -> None:
    if not isinstance(payload, Mapping):
        raise ValueError("screenshot promotion artifact must be an object")
    _validate_safe_value(payload)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported screenshot promotion schema")
    if payload.get("benchmark") != BENCHMARK:
        raise ValueError("unexpected screenshot promotion benchmark")
    status = payload.get("status")
    if require_publishable and status != "complete":
        raise ValueError("publishable screenshot evidence must be complete")
    if not require_publishable and status not in {"complete", "rejected"}:
        raise ValueError("screenshot evidence must be complete or rejected")
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
    if preregistration.get("operational_ceilings") != OPERATIONAL_CEILINGS:
        raise ValueError("artifact must preserve the fixed operational ceilings")
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
    operational_gates = _mapping(payload.get("operational_gates"), "operational_gates")
    operational_details = _mapping(
        payload.get("operational_details"), "operational_details"
    )
    _validate_operational_gates(
        operational_gates, require_publishable=require_publishable
    )
    _validate_operational_details(
        operational_details, require_publishable=require_publishable
    )
    _validate_soak_resource_consistency(
        operational_gates,
        operational_details,
        require_publishable=require_publishable,
    )
    for gate, detail in (
        ("concurrency_matrix", "concurrency"),
        ("readiness_parity", "readiness"),
    ):
        detail_payload = _mapping(operational_details.get(detail), detail)
        if operational_gates.get(gate) is not detail_payload.get("passed"):
            raise ValueError(f"operational gate {gate} disagrees with retained detail")


def evaluate_x11_shm_screenshot_promotion(
    payload: Mapping[str, Any], *, require_publishable: bool = True
) -> dict[str, Any]:
    """Return the immutable gate decision for a validated artifact."""

    _validate_artifact_structure(payload, require_publishable=require_publishable)
    return _evaluate_validated_artifact(payload)


def _evaluate_validated_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
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
    operational_gates = _mapping(payload["operational_gates"], "operational_gates")
    for gate, label in (
        ("chromium_fixture", "Chromium fixture"),
        ("failure_matrix", "failure matrix"),
        ("x_server_restart", "X server restart"),
        ("bounded_x_server_failure", "bounded X server failure"),
        ("region_parity", "region parity"),
        ("cleanup_succeeded", "cleanup"),
    ):
        if operational_gates.get(gate) is not True:
            reasons.append(f"{label} gate did not pass")
    if operational_gates.get("readiness_parity") is not True:
        reasons.append("fresh readiness p95 regresses by more than 5%")
    if operational_gates.get("concurrency_matrix") is not True:
        reasons.append("screenshot concurrency p95 regresses by more than 5%")
    if operational_gates.get("captures") != 10_000:
        reasons.append("operational soak did not complete 10000 captures")
    if (
        operational_gates.get("full_captures") != 5_000
        or operational_gates.get("region_captures") != 5_000
    ):
        reasons.append("operational soak did not complete balanced full and regional captures")
    missing_resource_deltas = [
        name
        for name in ("fd_delta", "mapping_delta")
        if operational_gates.get(name) is None
    ]
    changed_resource_deltas = [
        name
        for name in ("fd_delta", "mapping_delta")
        if operational_gates.get(name) is not None
        and operational_gates.get(name) != 0
    ]
    if missing_resource_deltas:
        reasons.append(
            "operational soak resource counts unavailable: "
            + ", ".join(missing_resource_deltas)
        )
    if changed_resource_deltas:
        reasons.append(
            "operational soak resource counts changed: "
            + ", ".join(changed_resource_deltas)
        )
    if (
        _nonnegative_integer(operational_gates.get("rss_growth_bytes"), "rss_growth_bytes")
        > 16 * 1024 * 1024
        or _nonnegative_integer(
            operational_gates.get("peak_rss_growth_bytes"), "peak_rss_growth_bytes"
        )
        > 16 * 1024 * 1024
    ):
        reasons.append("operational soak RSS growth exceeds 16 MiB")

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
    for key in ("x11_shm_source_sha256", "cargo_lock_sha256"):
        if _FULL_SHA256.fullmatch(str(configuration.get(key, ""))) is None:
            raise ValueError(f"{key} must be a SHA-256 digest")
    if configuration.get("rust_toolchain") != "rustc 1.91.0":
        raise ValueError("Rust toolchain must be pinned to rustc 1.91.0")
    if not str(configuration.get("python_version", "")).startswith("3.12."):
        raise ValueError("benchmark must use Python 3.12")
    target = configuration.get("target")
    target_machines = {
        "x86_64-unknown-linux-gnu": "x86_64",
        "aarch64-unknown-linux-gnu": "aarch64",
    }
    if target not in target_machines:
        raise ValueError("benchmark target must be a supported Linux architecture")
    if not str(configuration.get("image_identity", "")).strip():
        raise ValueError("image identity is required")
    if not str(configuration.get("image_object_id", "")).startswith("im-"):
        raise ValueError("hydrated Modal Image object identity is required")
    native_builds = _mapping(configuration.get("native_builds"), "native_builds")
    if set(native_builds) != {BASELINE_ARM, CANDIDATE_ARM}:
        raise ValueError("both arms must report the native build identity")
    normalized_builds: list[dict[str, Any]] = []
    for arm in (BASELINE_ARM, CANDIDATE_ARM):
        build = _mapping(native_builds.get(arm), f"native_builds.{arm}")
        if set(build) != {
            "backend",
            "codec",
            "module_sha256",
            "image_object_id",
            "machine",
        }:
            raise ValueError("native build identity fields changed")
        if build.get("backend") != CANDIDATE_ARM:
            raise ValueError("native build backend marker changed")
        if build.get("codec") != "png-deflate-level2-no-filter":
            raise ValueError("native build codec marker changed")
        if _FULL_SHA256.fullmatch(str(build.get("module_sha256", ""))) is None:
            raise ValueError("native module digest is invalid")
        if build.get("image_object_id") != configuration.get("image_object_id"):
            raise ValueError("target native build does not match the Modal Image object")
        if build.get("machine") != target_machines[target]:
            raise ValueError("native build architecture does not match the benchmark target")
        normalized_builds.append(dict(build))
    if normalized_builds[0] != normalized_builds[1]:
        raise ValueError("benchmark arms used different native Image builds")
    if configuration.get("browser") != "chromium":
        raise ValueError("publishable evidence requires a real Chromium fixture")
    if configuration.get("browser_launch_args") != [
        "--kiosk",
        "--window-position=0,0",
        "--window-size=1024,768",
        "--force-device-scale-factor=1",
        "--no-first-run",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
    ] or configuration.get("browser_gpu_mode") != "off":
        raise ValueError("Chromium fixture launch configuration changed")
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
    if _mapping(observed.get("runner"), "observed_placement.runner") != requested:
        raise ValueError("requested and observed runner placement must match")
    targets = _mapping(observed.get("targets"), "observed_placement.targets")
    if set(targets) != {BASELINE_ARM, CANDIDATE_ARM}:
        raise ValueError("both benchmark arms must report observed placement")
    for arm in (BASELINE_ARM, CANDIDATE_ARM):
        if _mapping(targets.get(arm), f"observed_placement.targets.{arm}") != requested:
            raise ValueError("requested and observed target placement must match")
    resources = _mapping(configuration.get("resources"), "resources")
    if resources.get("cpu") != 1.0 or resources.get("memory_mib") != 2048:
        raise ValueError("benchmark resources must stay fixed")
    observed_resources = _mapping(
        configuration.get("observed_resources"), "observed_resources"
    )
    if set(observed_resources) != {BASELINE_ARM, CANDIDATE_ARM}:
        raise ValueError("both arms must report observed resources")
    for arm in (BASELINE_ARM, CANDIDATE_ARM):
        observed = _mapping(observed_resources.get(arm), f"observed_resources.{arm}")
        if observed.get("cpu") != 1.0 or observed.get("memory_bytes") != 2048 * 1024 * 1024:
            raise ValueError("observed resources differ from the requested resources")


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
    expected_source = "auto" if arm == CANDIDATE_ARM else arm
    if (
        arm_payload.get("requested_source") != expected_source
        or arm_payload.get("expected_backend") != arm
    ):
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
        if arm == BASELINE_ARM:
            _nonnegative_number(observation.get("capture_ms"), f"{arm}.capture_ms")
            _nonnegative_number(observation.get("encode_ms"), f"{arm}.encode_ms")
            if observation.get("x11_shm_capture_encode_ms") is not None:
                raise ValueError("MSS observation reported fused native timing")
        else:
            _nonnegative_number(
                observation.get("x11_shm_capture_encode_ms"),
                f"{arm}.x11_shm_capture_encode_ms",
            )
            if (
                observation.get("capture_ms") is not None
                or observation.get("encode_ms") is not None
            ):
                raise ValueError("X11 shared-memory observation reported split timing")


def _validate_operational_gates(
    gates: Mapping[str, Any], *, require_publishable: bool
) -> None:
    for key in (
        "chromium_fixture",
        "failure_matrix",
        "concurrency_matrix",
        "readiness_parity",
        "x_server_restart",
        "bounded_x_server_failure",
        "region_parity",
        "cleanup_succeeded",
    ):
        if not isinstance(gates.get(key), bool):
            raise ValueError(f"operational gate {key} must be boolean")
        if (
            require_publishable
            and key not in {"concurrency_matrix", "readiness_parity"}
            and gates.get(key) is not True
        ):
            raise ValueError(f"operational gate {key} did not pass")
    captures = _nonnegative_integer(gates.get("captures"), "captures")
    full_captures = _nonnegative_integer(gates.get("full_captures"), "full_captures")
    region_captures = _nonnegative_integer(
        gates.get("region_captures"), "region_captures"
    )
    if require_publishable and captures != 10_000:
        raise ValueError("operational soak must contain 10000 captures")
    if require_publishable and (full_captures != 5_000 or region_captures != 5_000):
        raise ValueError("operational soak must alternate 5000 full and regional captures")
    if require_publishable:
        fd_delta = _nonnegative_integer(gates.get("fd_delta"), "fd_delta")
        mapping_delta = _nonnegative_integer(gates.get("mapping_delta"), "mapping_delta")
    else:
        fd_delta = _optional_integer(gates.get("fd_delta"), "fd_delta")
        mapping_delta = _optional_integer(gates.get("mapping_delta"), "mapping_delta")
    if require_publishable and (fd_delta != 0 or mapping_delta != 0):
        raise ValueError("resource counts changed during the operational soak")
    rss_growth = _nonnegative_integer(gates.get("rss_growth_bytes"), "rss_growth_bytes")
    if require_publishable and rss_growth > 16 * 1024 * 1024:
        raise ValueError("resource RSS growth exceeds the fixed 16 MiB ceiling")
    peak_rss_growth = _nonnegative_integer(
        gates.get("peak_rss_growth_bytes"), "peak_rss_growth_bytes"
    )
    if require_publishable and peak_rss_growth > 16 * 1024 * 1024:
        raise ValueError("resource peak RSS growth exceeds the fixed 16 MiB ceiling")


def _validate_operational_details(
    details: Mapping[str, Any], *, require_publishable: bool
) -> None:
    if not require_publishable:
        required_details = (
            "concurrency",
            "readiness",
            "failure_matrix",
            "soak",
            "x_server_restart",
            "x_server_timeout",
            "region_parity",
            "terminal_cleanup",
        )
        for name in required_details:
            if not isinstance(details.get(name), Mapping):
                raise ValueError(f"rejected evidence detail {name} is incomplete")
        for name in ("concurrency", "readiness"):
            if not isinstance(details[name].get("passed"), bool):
                raise ValueError(f"rejected evidence detail {name} has no decision")
        return

    concurrency = _mapping(details.get("concurrency"), "concurrency")
    concurrency_arms = _mapping(concurrency.get("arms"), "concurrency.arms")
    if not isinstance(concurrency.get("passed"), bool) or set(concurrency_arms) != {
        BASELINE_ARM,
        CANDIDATE_ARM,
    }:
        raise ValueError("concurrency detail is incomplete")
    for arm in (BASELINE_ARM, CANDIDATE_ARM):
        arm_detail = _mapping(concurrency_arms.get(arm), f"concurrency.arms.{arm}")
        levels = arm_detail.get("levels")
        if arm_detail.get("passed") is not True or not isinstance(levels, list):
            raise ValueError("concurrency arm did not pass")
        if [row.get("concurrency") for row in levels if isinstance(row, Mapping)] != [1, 2, 4, 8]:
            raise ValueError("concurrency detail must cover levels 1, 2, 4, and 8")
        if any(
            not isinstance(row, Mapping)
            or row.get("capture_backend") != arm
            or row.get("trials") != 5
            for row in levels
        ):
            raise ValueError("concurrency detail contains unexpected source attribution")
    baseline_levels = _mapping(
        concurrency_arms.get(BASELINE_ARM), "concurrency baseline"
    )["levels"]
    candidate_levels = _mapping(
        concurrency_arms.get(CANDIDATE_ARM), "concurrency candidate"
    )["levels"]
    concurrency_passed = True
    for baseline, candidate in zip(baseline_levels, candidate_levels, strict=True):
        baseline_p95 = _nonnegative_number(
            baseline.get("elapsed_p95_ms"), "baseline concurrency p95"
        )
        candidate_p95 = _nonnegative_number(
            candidate.get("elapsed_p95_ms"), "candidate concurrency p95"
        )
        concurrency_passed = concurrency_passed and (
            baseline_p95 > 0 and candidate_p95 <= baseline_p95 * 1.05
        )
    if concurrency.get("passed") is not concurrency_passed:
        raise ValueError("concurrency gate disagrees with retained p95 values")

    readiness = _mapping(details.get("readiness"), "readiness")
    readiness_arms = _mapping(readiness.get("arms"), "readiness.arms")
    if not isinstance(readiness.get("passed"), bool) or set(readiness_arms) != {
        BASELINE_ARM,
        CANDIDATE_ARM,
    }:
        raise ValueError("readiness parity detail is incomplete")
    for arm in (BASELINE_ARM, CANDIDATE_ARM):
        arm_detail = _mapping(readiness_arms.get(arm), f"readiness.arms.{arm}")
        if (
            arm_detail.get("passed") is not True
            or arm_detail.get("samples") != 20
            or arm_detail.get("capture_backend") != arm
        ):
            raise ValueError("readiness arm detail is incomplete")
        _nonnegative_number(arm_detail.get("startup_p95_ms"), "startup_p95_ms")
    baseline_readiness_p95 = _nonnegative_number(
        _mapping(readiness_arms[BASELINE_ARM], "baseline readiness").get(
            "startup_p95_ms"
        ),
        "baseline readiness p95",
    )
    candidate_readiness_p95 = _nonnegative_number(
        _mapping(readiness_arms[CANDIDATE_ARM], "candidate readiness").get(
            "startup_p95_ms"
        ),
        "candidate readiness p95",
    )
    readiness_passed = (
        baseline_readiness_p95 > 0
        and candidate_readiness_p95 <= baseline_readiness_p95 * 1.05
    )
    if readiness.get("passed") is not readiness_passed:
        raise ValueError("readiness gate disagrees with retained p95 values")

    failure_matrix = _mapping(details.get("failure_matrix"), "failure_matrix")
    checks = _mapping(failure_matrix.get("checks"), "failure_matrix.checks")
    if (
        failure_matrix.get("passed") is not True
        or set(checks) != _FAILURE_CHECKS
        or any(value is not True for value in checks.values())
    ):
        raise ValueError("failure matrix is incomplete")

    soak = _mapping(details.get("soak"), "soak")
    if (
        soak.get("passed") is not True
        or soak.get("captures") != 10_000
        or soak.get("full_captures") != 5_000
        or soak.get("region_captures") != 5_000
        or _nonnegative_integer(
            soak.get("peak_rss_growth_bytes"), "soak.peak_rss_growth_bytes"
        )
        > 16 * 1024 * 1024
    ):
        raise ValueError("daemon-local soak detail is incomplete")
    restart = _mapping(details.get("x_server_restart"), "x_server_restart")
    if (
        restart.get("passed") is not True
        or restart.get("ready_after_restart") is not True
        or restart.get("backend_before") != CANDIDATE_ARM
        or restart.get("backend_after") != CANDIDATE_ARM
    ):
        raise ValueError("X server restart detail did not pass")
    timeout = _mapping(details.get("x_server_timeout"), "x_server_timeout")
    if (
        timeout.get("passed") is not True
        or timeout.get("failed_bounded") is not True
        or timeout.get("public_error_type") != "DaemonHTTPError"
        or timeout.get("public_error_code") != "internal_error"
        or timeout.get("public_error_detail_type") != "ScreenshotCaptureTimedOut"
        or timeout.get("no_fallback_observed") is not True
        or timeout.get("constructor_bounded") is not True
        or timeout.get("constructor_error_type") != "ScreenshotCaptureTimedOut"
        or timeout.get("backend_after_restart") != CANDIDATE_ARM
        or _nonnegative_number(timeout.get("elapsed_ms"), "x_server_timeout.elapsed_ms")
        >= 2_500.0
        or _nonnegative_number(
            timeout.get("constructor_elapsed_ms"),
            "x_server_timeout.constructor_elapsed_ms",
        )
        >= 2_500.0
    ):
        raise ValueError("bounded X server failure detail did not pass")
    region_parity = _mapping(details.get("region_parity"), "region_parity")
    region_arms = _mapping(region_parity.get("arms"), "region_parity.arms")
    if (
        region_parity.get("passed") is not True
        or region_parity.get("case_count") != 4
        or region_parity.get("decoded_pixel_and_metadata_parity") is not True
        or set(region_arms) != {BASELINE_ARM, CANDIDATE_ARM}
    ):
        raise ValueError("regional screenshot parity detail did not pass")
    for arm in (BASELINE_ARM, CANDIDATE_ARM):
        rows = region_arms.get(arm)
        if not isinstance(rows, list) or len(rows) != 4:
            raise ValueError("regional screenshot parity cases are incomplete")
        for row in rows:
            item = _mapping(row, f"region_parity.arms.{arm}")
            region = _mapping(item.get("region"), "region_parity.region")
            coordinate_space = _mapping(
                item.get("coordinate_space"), "region_parity.coordinate_space"
            )
            if (
                item.get("capture_backend") != arm
                or item.get("width") != region.get("width")
                or item.get("height") != region.get("height")
                or item.get("cursor_visible") is not False
                or item.get("cursor_position_is_null") is not True
                or _FULL_SHA256.fullmatch(str(item.get("pixels_sha256", ""))) is None
                or coordinate_space.get("desktop_width") != 1024
                or coordinate_space.get("desktop_height") != 768
                or coordinate_space.get("image_width") != region.get("width")
                or coordinate_space.get("image_height") != region.get("height")
                or coordinate_space.get("scale_x") != 1.0
                or coordinate_space.get("scale_y") != 1.0
                or coordinate_space.get("source_region") != region
            ):
                raise ValueError("regional screenshot parity case is invalid")
    terminal_cleanup = _mapping(details.get("terminal_cleanup"), "terminal_cleanup")
    if (
        terminal_cleanup.get("succeeded") is not True
        or terminal_cleanup.get("survivors_before_sweep") != 0
        or terminal_cleanup.get("remaining_sandboxes") != 0
        or terminal_cleanup.get("cleanup_error_types") != []
    ):
        raise ValueError("terminal Sandbox cleanup detail did not pass")


def _validate_soak_resource_consistency(
    gates: Mapping[str, Any],
    details: Mapping[str, Any],
    *,
    require_publishable: bool,
) -> None:
    """Keep retained resource gate values tied to the daemon-local soak."""

    soak = _mapping(details.get("soak"), "soak")
    if not isinstance(soak.get("passed"), bool):
        raise ValueError("soak detail must retain a boolean decision")
    for key in ("fd_delta", "mapping_delta"):
        gate_value = (
            _nonnegative_integer(gates.get(key), f"operational_gates.{key}")
            if require_publishable
            else _optional_integer(gates.get(key), f"operational_gates.{key}")
        )
        detail_value = (
            _nonnegative_integer(soak.get(key), f"operational_details.soak.{key}")
            if require_publishable
            else _optional_integer(soak.get(key), f"operational_details.soak.{key}")
        )
        if gate_value != detail_value:
            raise ValueError(f"soak detail disagrees with operational gate {key}")
    if soak.get("passed") and (
        gates.get("fd_delta") != 0 or gates.get("mapping_delta") != 0
    ):
        raise ValueError(
            "soak detail passed despite incomplete or changed resource counts"
        )


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


def _optional_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


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
