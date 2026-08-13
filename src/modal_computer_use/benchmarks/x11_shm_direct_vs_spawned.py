"""Private same-display discriminator for direct and spawned X11-SHM calls.

This module is intentionally a benchmark seam, not a screenshot backend.  It
opens one native session directly and one through the normal spawned-worker
adapter, then drives both persistent sessions through the same deterministic
region schedule.  A terminal failure never starts a replacement: the other
session continues to its fixed target so a paired prefix can be separated from
unpaired evidence.

Only bounded, non-secret metadata crosses the child/artifact boundary.  PNG
bytes and exception text are consumed in memory and are never returned.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import platform
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from statistics import median
from time import perf_counter_ns
from typing import Any, Literal, cast

from PIL import Image, UnidentifiedImageError

from modal_computer_use.daemon.desktop.screenshot_capture import (
    SCREENSHOT_CAPTURE_TIMEOUT_ORIGINS,
    NativeCaptureTiming,
    ScreenshotCaptureError,
    ScreenshotCaptureFailed,
    ScreenshotCaptureTimedOut,
    X11SharedMemoryScreenshotSession,
    _probe_x11_setup,
    validate_png_dimensions,
)

ArmName = Literal["direct_native", "spawned_worker"]
MODULE_NAME = "_modal_computer_use_x11_shm"
WIDTH = 1024
HEIGHT = 768
REGION = {"x": 7, "y": 9, "width": 511, "height": 383}
WARMUPS = 20
# The existing tail artifact reached 531 captures before its first terminal
# region failure.  Keep enough paired samples to observe that tail without
# changing the public/runtime capture path.
PAIRS = 1_000
OPERATION_TIMEOUT_SECONDS = 2.5
CLEANUP_TIMEOUT_SECONDS = 2.5
ARMS: tuple[ArmName, ArmName] = ("direct_native", "spawned_worker")
DIRECT_ARM: ArmName = "direct_native"
SPAWNED_ARM: ArmName = "spawned_worker"
SCHEMA_VERSION = "x11-shm-direct-vs-spawned.v3"
BENCHMARK_NAME = "x11-shm-direct-vs-spawned"
CONFIGURED_RESOURCES = {
    "cpu": 1.0,
    "memory_bytes": 2048 * 1024**2,
}
EXPECTED_MODULE_IDENTITY = {
    "backend": "x11-shm",
    "codec": "png-deflate-level1-no-filter",
    "codec_runtime": "in-process-miniz_oxide",
    "codec_library": "in-process",
}
SAFE_FAILURE_PHASES = frozenset(
    {
        "workload_contract",
        "native_import",
        "module_identity",
        "target_module_hash",
        "target_image_identity",
        "target_cgroup_limits",
        "target_cgroup_mapping",
        "target_cpu_limit",
        "target_memory_limit",
        "target_limit_contract",
        "sandbox_handle",
        "child_result",
        "session_start",
        "direct_session_start",
        "spawned_session_start",
        "warmup_capture",
        "measured_capture",
        "identity_after",
        "session_close",
        "summary",
    }
)
SAFE_ARM_FAILURE_TYPES = frozenset(
    {
        "X11ScreenshotTimeoutError",
        "ScreenshotCaptureTimedOut",
        "ScreenshotCaptureFailed",
        "ScreenshotCaptureUnavailable",
        "RuntimeError",
        "ValueError",
        "ImportError",
        "ModuleNotFoundError",
        "TimeoutError",
        "PixelParityMismatch",
    }
)
SAFE_CLEANUP_ERROR_TYPES = frozenset(
    {
        "CleanupError",
        "ScreenshotCaptureTimedOut",
        "ScreenshotCaptureFailed",
        "TimeoutError",
        "RuntimeError",
    }
)
SAFE_TIMEOUT_ORIGINS = frozenset(SCREENSHOT_CAPTURE_TIMEOUT_ORIGINS) | {
    "benchmark_call_deadline",
}
_GEOMETRY_KEYS = ("x", "y", "width", "height")
_TIMING_FIELDS = (
    "executor_queue_ms",
    "x11_reply_ms",
    "rgb_convert_ms",
    "png_encode_ms",
    "native_total_ms",
    "worker_dispatch_ms",
    "worker_response_prep_ms",
    "parent_lock_wait_ms",
    "parent_send_ms",
    "parent_header_wait_ms",
    "parent_payload_read_ms",
    "parent_total_ms",
)
SCOPE_CONTRACT = {
    "same_sandbox": True,
    "same_xvfb_display": True,
    "http_transport_excluded": True,
    "daemon_route_excluded": True,
    "non_gating": True,
    "instrumentation_intrusive": True,
    "lane_order_confounded": True,
    "daemon_requested_source": "mss",
    "diagnostic_source": "x11-shm",
    "cgroup_scope": "configured-resource-only",
    # The current worker protocol does not expose a module hash handshake;
    # bind the run to the image/source identity and record that limitation.
    "worker_module_hash_handshake": False,
    "worker_module_identity_scope": "parent_extension_only",
    "direct_constructor_liveness": "bounded-x11-preflight-then-libxcb",
}


class _ProbeFailure(RuntimeError):
    """Internal bounded failure carrying only already-safe classification."""

    def __init__(
        self,
        failure_type: str,
        *,
        timeout_origin: str | None = None,
    ) -> None:
        self.failure_type = (
            failure_type if failure_type in SAFE_ARM_FAILURE_TYPES else "RuntimeError"
        )
        self.timeout_origin = timeout_origin if timeout_origin in SAFE_TIMEOUT_ORIGINS else None
        super().__init__("direct/spawned discriminator failed")


class _DirectNativeTimeout(ScreenshotCaptureTimedOut):
    """Typed timeout from the direct native ABI, with no native error text."""


class _TargetPreflightFailure(RuntimeError):
    """Bounded target-preflight failure with no retained exception detail."""

    def __init__(self, phase: str) -> None:
        self.phase = phase if phase in SAFE_FAILURE_PHASES else "target_cgroup_limits"
        super().__init__("target preflight failed")


class _CgroupEvidenceUnavailable(RuntimeError):
    """Cgroup mapping/files are unavailable, so evidence is explicitly null."""


class _CgroupEvidenceMalformed(RuntimeError):
    """Readable cgroup metadata is malformed and must fail closed."""


@dataclass(frozen=True, slots=True)
class _RuntimeLimits:
    cgroup_available: bool
    quota_usec: int | None
    period_usec: int | None
    memory_bytes: int | None
    cgroup_version: str | None
    cgroup_resolution: str | None


@dataclass(frozen=True, slots=True)
class _CaptureSample:
    payload_bytes: int
    elapsed_ms: float
    timing: NativeCaptureTiming
    pixel_hash: str


class _SessionExecutor:
    """Pin one session's Python/native object graph to one worker thread."""

    def __init__(self, arm: str) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"x11-shm-{arm}",
        )

    def shutdown(self) -> None:
        # A shielded native call may still be unwinding after its outer
        # diagnostic deadline.  Do not block the event-loop thread waiting for
        # it; the Rust watchdog/worker process bound owns that cleanup path.
        self.executor.shutdown(wait=False, cancel_futures=True)


class _Progress:
    def __init__(self) -> None:
        self.phase = "workload_contract"
        self.warmups_completed = {arm: 0 for arm in ARMS}
        self.captures_completed = {arm: 0 for arm in ARMS}
        self.paired_prefix_samples = 0
        self.first_unpaired_pair: int | None = None


def empty_rejected_observation(failure_phase: str) -> dict[str, Any]:
    """Return the exact bounded envelope for a failure before child evidence."""

    phase = failure_phase if failure_phase in SAFE_FAILURE_PHASES else "summary"
    return {
        "passed": False,
        **SCOPE_CONTRACT,
        "configured_resources": dict(CONFIGURED_RESOURCES),
        "worker_cgroup_same": None,
        "warmups_completed": {arm: 0 for arm in ARMS},
        "captures_completed": {arm: 0 for arm in ARMS},
        "paired_prefix_samples": 0,
        "unpaired_after_failure_samples": 0,
        "first_unpaired_pair": None,
        "pixel_hash_parity": False,
        "arms": {},
        "retries": 0,
        "replacement_samples": 0,
        "failure_type": "RuntimeError",
        "failure_phase": phase,
    }


def configure_resources(cpu: float) -> dict[str, float | int]:
    """Select the private child resource contract for one CPU ablation arm."""

    if isinstance(cpu, bool) or not isinstance(cpu, float) or cpu not in {1.0, 2.0}:
        raise ValueError("CPU ablation supports exactly 1.0 or 2.0 CPUs")
    CONFIGURED_RESOURCES["cpu"] = cpu
    return dict(CONFIGURED_RESOURCES)


def pair_order(pair_index: int) -> tuple[ArmName, ArmName]:
    """Return deterministic AB/BA order for one measured pair."""

    if isinstance(pair_index, bool) or not isinstance(pair_index, int) or pair_index < 0:
        raise ValueError("pair index must be a non-negative integer")
    return (DIRECT_ARM, SPAWNED_ARM) if pair_index % 2 == 0 else (SPAWNED_ARM, DIRECT_ARM)


def build_schedule(*, pairs: int = PAIRS) -> list[dict[str, Any]]:
    """Build the fixed region-only schedule used by both persistent sessions."""

    if isinstance(pairs, bool) or not isinstance(pairs, int) or not 1 <= pairs <= PAIRS:
        raise ValueError("paired discriminator workload is fixed")
    schedule: list[dict[str, Any]] = []
    for pair_index in range(pairs):
        for position, arm in enumerate(pair_order(pair_index)):
            schedule.append(
                {
                    "pair_index": pair_index,
                    "sequence": pair_index * 2 + position,
                    "position": position,
                    "arm": arm,
                    "geometry": dict(REGION),
                }
            )
    return schedule


def _safe_label(value: object, *, allowed: frozenset[str] | None = None) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if not all(character.isalnum() or character in "_.-" for character in value):
        return None
    if allowed is not None and value not in allowed:
        return None
    return value


def _bounded_count(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


def _finite_nonnegative(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {label}")
    retained = float(value)
    if not retained >= 0 or retained != retained or retained in {float("inf"), float("-inf")}:
        raise ValueError(f"invalid {label}")
    return retained


def _validate_identity(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"pid", "starttime_ticks"}:
        raise ValueError("invalid process identity")
    pid = _bounded_count(value["pid"], "process pid", maximum=2**31 - 1)
    starttime_ticks = _bounded_count(
        value["starttime_ticks"], "process starttime", maximum=2**63 - 1
    )
    if pid < 1 or starttime_ticks < 1:
        raise ValueError("process identity must be positive")
    return {"pid": pid, "starttime_ticks": starttime_ticks}


def _validate_geometry(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(_GEOMETRY_KEYS):
        raise ValueError("invalid discriminator geometry")
    geometry = {
        key: _bounded_count(value[key], f"geometry {key}", maximum=WIDTH * HEIGHT)
        for key in _GEOMETRY_KEYS
    }
    if geometry != REGION:
        raise ValueError("discriminator geometry differs from the fixed failing region")
    return geometry


def _validate_module_identity(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(EXPECTED_MODULE_IDENTITY):
        raise ValueError("invalid native module identity")
    retained = {key: _safe_label(value[key]) for key in EXPECTED_MODULE_IDENTITY}
    if any(item is None for item in retained.values()) or retained != EXPECTED_MODULE_IDENTITY:
        raise ValueError("native module identity differs from the fixed diagnostic")
    return cast(dict[str, str], retained)


def _validate_target_identity(value: object) -> dict[str, Any]:
    expected = {
        "backend",
        "codec",
        "codec_runtime",
        "codec_library",
        "module_sha256",
        "image_object_id",
        "cpu",
        "quota_usec",
        "period_usec",
        "memory_bytes",
        "cgroup_available",
        "cgroup_version",
        "cgroup_resolution",
        "machine",
        "display",
        "width",
        "height",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("invalid target identity")
    module_sha = value["module_sha256"]
    image_id = value["image_object_id"]
    display = value["display"]
    cgroup_available = value["cgroup_available"]
    if not isinstance(cgroup_available, bool):
        raise ValueError("invalid cgroup availability state")
    if (
        value["backend"] != EXPECTED_MODULE_IDENTITY["backend"]
        or value["codec"] != EXPECTED_MODULE_IDENTITY["codec"]
        or value["codec_runtime"] != EXPECTED_MODULE_IDENTITY["codec_runtime"]
        or value["codec_library"] != EXPECTED_MODULE_IDENTITY["codec_library"]
        or not isinstance(module_sha, str)
        or len(module_sha) != 64
        or any(character not in "0123456789abcdef" for character in module_sha)
        or not isinstance(image_id, str)
        or not image_id.startswith("im-")
        or len(image_id) > 128
        or not all(character.isalnum() or character in "_.-" for character in image_id)
        or not isinstance(display, str)
        or not 1 <= len(display) <= 128
        or not all(character.isalnum() or character in "_.-:" for character in display)
        or value["width"] != WIDTH
        or value["height"] != HEIGHT
        or not isinstance(value["machine"], str)
        or not 1 <= len(value["machine"]) <= 64
        or not all(character.isalnum() or character in "_.-" for character in value["machine"])
    ):
        raise ValueError("target identity differs from the fixed runtime")
    if cgroup_available:
        if (
            value["cgroup_version"] != "v2"
            or value["cgroup_resolution"] not in {"namespace-root", "namespace-relative"}
            or isinstance(value["cpu"], bool)
            or not isinstance(value["cpu"], float)
            or value["cpu"] != CONFIGURED_RESOURCES["cpu"]
        ):
            raise ValueError("available cgroup evidence has no safe resolution")
        quota = _bounded_count(value["quota_usec"], "target quota", maximum=2**31 - 1)
        period = _bounded_count(value["period_usec"], "target period", maximum=2**31 - 1)
        memory = _bounded_count(value["memory_bytes"], "target memory", maximum=16 * 1024**3)
        expected_quota = int(period * CONFIGURED_RESOURCES["cpu"])
        if (
            quota != expected_quota
            or quota < 1
            or memory != CONFIGURED_RESOURCES["memory_bytes"]
        ):
            raise ValueError("available cgroup limits differ from the fixed runtime")
        cgroup_version = "v2"
        cgroup_resolution = value["cgroup_resolution"]
    else:
        if any(
            value[field] is not None
            for field in (
                "quota_usec",
                "period_usec",
                "memory_bytes",
                "cgroup_version",
                "cgroup_resolution",
            )
        ):
            raise ValueError("unavailable cgroup evidence must be explicitly null")
        if value["cpu"] is not None:
            raise ValueError("unavailable cgroup evidence must have null CPU")
        quota = period = memory = None
        cgroup_version = cgroup_resolution = None
    return {
        "backend": EXPECTED_MODULE_IDENTITY["backend"],
        "codec": EXPECTED_MODULE_IDENTITY["codec"],
        "codec_runtime": EXPECTED_MODULE_IDENTITY["codec_runtime"],
        "codec_library": EXPECTED_MODULE_IDENTITY["codec_library"],
        "module_sha256": module_sha,
        "image_object_id": image_id,
        "cpu": CONFIGURED_RESOURCES["cpu"] if cgroup_available else None,
        "quota_usec": quota,
        "period_usec": period,
        "memory_bytes": memory,
        "cgroup_available": cgroup_available,
        "cgroup_version": cgroup_version,
        "cgroup_resolution": cgroup_resolution,
        "machine": value["machine"],
        "display": display,
        "width": WIDTH,
        "height": HEIGHT,
    }


def _validate_configured_resources(
    value: object, *, expected_cpu: float | None = None
) -> dict[str, float | int]:
    if not isinstance(value, Mapping) or set(value) != set(CONFIGURED_RESOURCES):
        raise ValueError("invalid authoritative configured resources")
    cpu = value["cpu"]
    memory = value["memory_bytes"]
    configured_cpu = CONFIGURED_RESOURCES["cpu"] if expected_cpu is None else expected_cpu
    if (
        isinstance(cpu, bool)
        or not isinstance(cpu, float)
        or cpu not in {1.0, 2.0}
        or cpu != configured_cpu
    ):
        raise ValueError("invalid authoritative configured CPU")
    memory_bytes = _bounded_count(memory, "configured memory", maximum=16 * 1024**3)
    if memory_bytes != CONFIGURED_RESOURCES["memory_bytes"]:
        raise ValueError("invalid authoritative configured memory")
    return {"cpu": cpu, "memory_bytes": memory_bytes}


def _validate_timing(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(_TIMING_FIELDS):
        raise ValueError("invalid capture timing")
    retained = {
        key: _finite_nonnegative(value[key], f"capture timing {key}") for key in _TIMING_FIELDS
    }
    if retained["native_total_ms"] < sum(
        retained[key] for key in ("x11_reply_ms", "rgb_convert_ms", "png_encode_ms")
    ):
        raise ValueError("native capture timing algebra is invalid")
    if retained["parent_total_ms"] < sum(
        retained[key]
        for key in (
            "parent_lock_wait_ms",
            "parent_send_ms",
            "parent_header_wait_ms",
            "parent_payload_read_ms",
        )
    ):
        raise ValueError("parent capture timing algebra is invalid")
    return retained


def _validate_sample(value: object, *, expected_arm: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid capture observation")
    required = {
        "pair_index",
        "sequence",
        "position",
        "arm",
        "status",
        "payload_bytes",
        "elapsed_ms",
        "png_width",
        "png_height",
        "pixel_hash",
        "timing",
    }
    if set(value) != required:
        raise ValueError("invalid capture observation")
    if value["arm"] != expected_arm or value["status"] != "ok":
        raise ValueError("invalid capture observation status")
    pair_index = _bounded_count(value["pair_index"], "pair index", maximum=PAIRS - 1)
    sequence = _bounded_count(value["sequence"], "schedule sequence", maximum=2 * PAIRS - 1)
    position = _bounded_count(value["position"], "schedule position", maximum=1)
    expected = build_schedule()[sequence]
    if (
        expected["pair_index"] != pair_index
        or expected["sequence"] != sequence
        or expected["position"] != position
        or expected["arm"] != expected_arm
    ):
        raise ValueError("capture observation is not bound to the fixed schedule")
    payload_bytes = _bounded_count(value["payload_bytes"], "payload bytes", maximum=64 * 1024**2)
    if payload_bytes < 1:
        raise ValueError("capture payload is empty")
    elapsed_ms = _finite_nonnegative(value["elapsed_ms"], "capture elapsed")
    if value["png_width"] != REGION["width"] or value["png_height"] != REGION["height"]:
        raise ValueError("capture PNG dimensions differ from the fixed region")
    pixel_hash = value["pixel_hash"]
    if (
        not isinstance(pixel_hash, str)
        or len(pixel_hash) != 64
        or any(character not in "0123456789abcdef" for character in pixel_hash)
    ):
        raise ValueError("invalid decoded pixel hash")
    timing = _validate_timing(value["timing"])
    if elapsed_ms + 1e-6 < timing["executor_queue_ms"] + timing["parent_total_ms"]:
        raise ValueError("capture elapsed time is shorter than its retained stages")
    if expected_arm == DIRECT_ARM:
        for field in (
            "worker_dispatch_ms",
            "worker_response_prep_ms",
            "parent_lock_wait_ms",
            "parent_send_ms",
            "parent_header_wait_ms",
            "parent_payload_read_ms",
        ):
            if timing[field] != 0:
                raise ValueError("direct native sample retained spawned transport stages")
        if abs(timing["parent_total_ms"] - timing["native_total_ms"]) > 1e-6:
            raise ValueError("direct native parent/native timing differs")
    else:
        worker_before_header_ms = sum(
            timing[field]
            for field in (
                "worker_dispatch_ms",
                "native_total_ms",
                "worker_response_prep_ms",
            )
        )
        # The worker can receive and start the request before the parent's
        # send call returns.  Its dispatch/native/preparation stages therefore
        # fit inside the combined parent send + response-header interval, not
        # response-header wait alone.
        if (
            timing["parent_send_ms"] + timing["parent_header_wait_ms"] + 1e-6
            < worker_before_header_ms
        ):
            raise ValueError("spawned nested timing algebra is invalid")
    return {
        "pair_index": pair_index,
        "sequence": sequence,
        "position": position,
        "arm": expected_arm,
        "status": "ok",
        "payload_bytes": payload_bytes,
        "elapsed_ms": elapsed_ms,
        "png_width": REGION["width"],
        "png_height": REGION["height"],
        "pixel_hash": pixel_hash,
        "timing": timing,
    }


def _validate_failure(value: object, *, expected_arm: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("invalid arm failure")
    allowed = {"pair_index", "phase", "failure_type", "timeout_origin"}
    if set(value) != allowed:
        raise ValueError("invalid arm failure")
    pair_index = _bounded_count(value["pair_index"], "failure pair index", maximum=PAIRS - 1)
    phase = _safe_label(value["phase"], allowed=SAFE_FAILURE_PHASES)
    failure_type = _safe_label(value["failure_type"], allowed=SAFE_ARM_FAILURE_TYPES)
    origin = value["timeout_origin"]
    if phase is None or failure_type is None:
        raise ValueError("invalid arm failure classification")
    if failure_type in {"X11ScreenshotTimeoutError", "ScreenshotCaptureTimedOut"}:
        if origin not in SAFE_TIMEOUT_ORIGINS:
            raise ValueError("invalid timeout origin")
        if expected_arm == DIRECT_ARM and origin not in {
            "native_x11_setup_deadline",
            "native_x11_reply_deadline",
            "benchmark_call_deadline",
        }:
            raise ValueError("direct arm retained a worker timeout origin")
        if expected_arm == SPAWNED_ARM and origin not in SAFE_TIMEOUT_ORIGINS:
            raise ValueError("spawned arm retained an invalid timeout origin")
    elif origin is not None:
        raise ValueError("non-timeout arm failure retained timeout origin")
    return {
        "pair_index": pair_index,
        "phase": phase,
        "failure_type": failure_type,
        "timeout_origin": origin,
    }


def _validate_cleanup(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"succeeded", "error_types"}:
        raise ValueError(f"invalid {label} cleanup")
    if not isinstance(value["succeeded"], bool) or not isinstance(value["error_types"], list):
        raise ValueError(f"invalid {label} cleanup")
    error_types = []
    for item in value["error_types"]:
        safe = _safe_label(item, allowed=SAFE_CLEANUP_ERROR_TYPES)
        if safe is None:
            raise ValueError(f"invalid {label} cleanup error")
        error_types.append(safe)
    if value["succeeded"] and error_types:
        raise ValueError(f"invalid {label} cleanup state")
    return {"succeeded": value["succeeded"], "error_types": error_types}


def _validate_rejected_envelope(observation: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "passed",
        *SCOPE_CONTRACT,
        "warmups_completed",
        "captures_completed",
        "configured_resources",
        "worker_cgroup_same",
        "paired_prefix_samples",
        "unpaired_after_failure_samples",
        "first_unpaired_pair",
        "pixel_hash_parity",
        "arms",
        "retries",
        "replacement_samples",
        "failure_type",
        "failure_phase",
    }
    allowed = required | {"failure_timeout_origin"}
    if set(observation) not in {frozenset(required), frozenset(allowed)}:
        raise ValueError("invalid rejected observation envelope")
    if observation.get("passed") is not False or observation.get("arms") != {}:
        raise ValueError("invalid rejected observation state")
    retained: dict[str, Any] = {"passed": False, "arms": {}}
    retained["configured_resources"] = _validate_configured_resources(
        observation.get("configured_resources")
    )
    if observation.get("worker_cgroup_same") is not None:
        raise ValueError("rejected envelope cannot claim worker cgroup equality")
    retained["worker_cgroup_same"] = None
    for key, expected in SCOPE_CONTRACT.items():
        if observation.get(key) != expected:
            raise ValueError(f"scope contract differs for {key}")
        retained[key] = expected
    for key in ("warmups_completed", "captures_completed"):
        counts = observation.get(key)
        if not isinstance(counts, Mapping) or set(counts) != set(ARMS):
            raise ValueError(f"invalid rejected {key}")
        retained[key] = {
            arm: _bounded_count(
                counts[arm],
                f"{key}.{arm}",
                maximum=WARMUPS if key == "warmups_completed" else PAIRS,
            )
            for arm in ARMS
        }
    retained["paired_prefix_samples"] = _bounded_count(
        observation.get("paired_prefix_samples"), "paired prefix", maximum=PAIRS
    )
    retained["unpaired_after_failure_samples"] = _bounded_count(
        observation.get("unpaired_after_failure_samples"),
        "unpaired samples",
        maximum=2 * PAIRS,
    )
    first_unpaired = observation.get("first_unpaired_pair")
    retained["first_unpaired_pair"] = (
        None
        if first_unpaired is None
        else _bounded_count(first_unpaired, "first unpaired pair", maximum=PAIRS - 1)
    )
    if observation.get("pixel_hash_parity") is not False:
        raise ValueError("rejected envelope cannot claim pixel parity")
    retained["pixel_hash_parity"] = False
    retained["retries"] = _bounded_count(observation.get("retries"), "retries", maximum=0)
    retained["replacement_samples"] = _bounded_count(
        observation.get("replacement_samples"), "replacement samples", maximum=0
    )
    failure_type = _safe_label(observation.get("failure_type"), allowed=SAFE_ARM_FAILURE_TYPES)
    failure_phase = _safe_label(observation.get("failure_phase"), allowed=SAFE_FAILURE_PHASES)
    if failure_type is None or failure_phase is None:
        raise ValueError("invalid rejected failure classification")
    origin = observation.get("failure_timeout_origin")
    if origin is not None and origin not in SAFE_TIMEOUT_ORIGINS:
        raise ValueError("invalid rejected timeout origin")
    if origin is not None and failure_type not in {
        "X11ScreenshotTimeoutError",
        "ScreenshotCaptureTimedOut",
        "TimeoutError",
    }:
        raise ValueError("non-timeout rejected envelope retained timeout origin")
    retained["failure_type"] = failure_type
    retained["failure_phase"] = failure_phase
    retained["failure_timeout_origin"] = origin
    return retained


def _timing_to_row(
    sample: _CaptureSample, *, pair_index: int, sequence: int, position: int, arm: str
) -> dict[str, Any]:
    timing = sample.timing
    values = {
        "x11_reply_ms": timing.x11_reply_ns / 1_000_000,
        "rgb_convert_ms": timing.rgb_convert_ns / 1_000_000,
        "png_encode_ms": timing.png_encode_ns / 1_000_000,
        "native_total_ms": timing.native_total_ns / 1_000_000,
        "worker_dispatch_ms": timing.worker_dispatch_ns / 1_000_000,
        "worker_response_prep_ms": timing.worker_response_prep_ns / 1_000_000,
        "parent_lock_wait_ms": timing.parent_lock_wait_ns / 1_000_000,
        "parent_send_ms": timing.parent_send_ns / 1_000_000,
        "parent_header_wait_ms": timing.parent_header_wait_ns / 1_000_000,
        "parent_payload_read_ms": timing.parent_payload_read_ns / 1_000_000,
        "parent_total_ms": timing.parent_total_ns / 1_000_000,
    }
    return {
        "pair_index": pair_index,
        "sequence": sequence,
        "position": position,
        "arm": arm,
        "status": "ok",
        "payload_bytes": sample.payload_bytes,
        "elapsed_ms": sample.elapsed_ms,
        "png_width": REGION["width"],
        "png_height": REGION["height"],
        "pixel_hash": sample.pixel_hash,
        "timing": values,
    }


def _decode_rgb_pixel_hash(data: bytes) -> str:
    """Validate dimensions and hash decoded RGB pixels without retaining PNG."""

    if not validate_png_dimensions(
        data,
        width=REGION["width"],
        height=REGION["height"],
    ):
        raise ScreenshotCaptureFailed("capture PNG dimensions are invalid")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.size != (REGION["width"], REGION["height"]):
                raise ScreenshotCaptureFailed("capture PNG dimensions are invalid")
            rgb = image.convert("RGB")
            rgb.load()
            return hashlib.sha256(rgb.tobytes()).hexdigest()
    except (ScreenshotCaptureFailed, UnidentifiedImageError, OSError, ValueError):
        raise ScreenshotCaptureFailed("capture PNG could not be decoded") from None


def _native_result(value: object) -> tuple[bytes, tuple[int, int, int, int]]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("invalid direct native timing result")
    data, raw_timing = value
    if not isinstance(data, bytes) or not data:
        raise ValueError("invalid direct native payload")
    if not isinstance(raw_timing, tuple) or len(raw_timing) != 4:
        raise ValueError("invalid direct native timing")
    stages: list[int] = []
    for stage in raw_timing:
        if isinstance(stage, bool) or not isinstance(stage, int) or stage < 0:
            raise ValueError("invalid direct native timing")
        stages.append(stage)
    if stages[3] < sum(stages[:3]):
        raise ValueError("invalid direct native timing algebra")
    return data, cast(tuple[int, int, int, int], tuple(stages))


class DirectNativeSession:
    """Thin private adapter around the imported native direct ABI."""

    arm = DIRECT_ARM

    def __init__(self, native: Any, *, display: str, width: int, height: int) -> None:
        constructor = getattr(native, "X11SharedMemoryScreenshotSession", None)
        timeout_error = getattr(native, "X11ScreenshotTimeoutError", None)
        if not callable(constructor) or not isinstance(timeout_error, type):
            raise ScreenshotCaptureFailed("direct native ABI is unavailable")
        self._timeout_error = timeout_error
        self._closed = False
        _probe_x11_setup(display)
        try:
            self._session = constructor(display, width, height)
        except Exception as exc:
            if isinstance(exc, timeout_error):
                raise _DirectNativeTimeout(
                    "direct native startup exceeded its deadline",
                    timeout_origin="native_x11_setup_deadline",
                ) from None
            raise ScreenshotCaptureFailed("direct native session could not start") from None

    def capture_png_with_timing(
        self, *, x: int, y: int, width: int, height: int
    ) -> tuple[bytes, NativeCaptureTiming]:
        if self._closed:
            raise ScreenshotCaptureFailed("direct native session is closed")
        capture = getattr(self._session, "capture_png_timed", None)
        if not callable(capture):
            raise ScreenshotCaptureFailed("direct native timing ABI is unavailable")
        try:
            data, values = _native_result(capture(x, y, width, height))
        except _DirectNativeTimeout:
            raise
        except Exception as exc:
            if isinstance(exc, self._timeout_error):
                raise _DirectNativeTimeout(
                    "direct native capture exceeded its deadline",
                    timeout_origin="native_x11_reply_deadline",
                ) from None
            if isinstance(exc, ScreenshotCaptureError):
                raise
            raise ScreenshotCaptureFailed("direct native capture failed") from None
        return data, NativeCaptureTiming(
            x11_reply_ns=values[0],
            rgb_convert_ns=values[1],
            png_encode_ns=values[2],
            native_total_ns=values[3],
            worker_dispatch_ns=0,
            worker_response_prep_ns=0,
            parent_lock_wait_ns=0,
            parent_send_ns=0,
            parent_header_wait_ns=0,
            parent_payload_read_ns=0,
            parent_total_ns=values[3],
        )

    def close(self) -> None:
        if self._closed:
            return
        session = self._session
        try:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            if isinstance(exc, self._timeout_error):
                raise _DirectNativeTimeout(
                    "direct native cleanup exceeded its deadline",
                    timeout_origin="native_x11_reply_deadline",
                ) from None
            raise ScreenshotCaptureFailed("direct native cleanup failed") from None
        finally:
            # Drop the unsendable PyO3 object on this pinned executor thread;
            # retaining it in the event-loop owner would decref it there.
            self._session = None
            self._closed = True


class SpawnedWorkerSession:
    """Benchmark seam for the normal Python-owned spawned worker adapter."""

    arm = SPAWNED_ARM

    def __init__(self, *, display: str, width: int, height: int) -> None:
        self._session = X11SharedMemoryScreenshotSession(
            display=display,
            width=width,
            height=height,
        )

    def capture_png_with_timing(
        self, *, x: int, y: int, width: int, height: int
    ) -> tuple[bytes, NativeCaptureTiming]:
        return self._session.capture_png_with_timing(
            x=x,
            y=y,
            width=width,
            height=height,
        )

    def close(self) -> None:
        session = self._session
        try:
            session.close()
        finally:
            self._session = None


def _process_identity(pid: int) -> dict[str, int]:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
    return {"pid": pid, "starttime_ticks": int(fields[19])}


def _same_cgroup(pid: int) -> bool:
    current = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    worker = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    return current == worker


def _spawned_worker_pid(session: Any) -> int:
    owner = getattr(session, "_session", None)
    process = getattr(owner, "_session", owner)
    process_obj = getattr(process, "_process", None)
    pid = getattr(process_obj, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("spawned worker identity is unavailable")
    return pid


def _spawned_worker_identity_and_cgroup(
    session: Any, *, verify_cgroup: bool = True
) -> tuple[dict[str, int], bool | None]:
    pid = _spawned_worker_pid(session)
    try:
        cgroup_same = _same_cgroup(pid)
    except OSError:
        if verify_cgroup:
            raise RuntimeError("spawned worker cgroup is unavailable") from None
        cgroup_same = None
    else:
        if not cgroup_same:
            raise RuntimeError("spawned worker cgroup differs from the target")
    return _process_identity(pid), cgroup_same


def _spawned_worker_identity(session: Any, *, verify_cgroup: bool = True) -> dict[str, int]:
    identity, _ = _spawned_worker_identity_and_cgroup(session, verify_cgroup=verify_cgroup)
    return identity


def _module_identity(native: Any) -> dict[str, str]:
    identity = {key: getattr(native, key, None) for key in EXPECTED_MODULE_IDENTITY}
    return _validate_module_identity(identity)


def _v2_cgroup_directory(
    membership_text: str | None = None,
    *,
    mountinfo_text: str | None = None,
    root: Path = Path("/sys/fs/cgroup"),
) -> tuple[Path, str]:
    if membership_text is None:
        try:
            membership_text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        except OSError:
            raise _CgroupEvidenceUnavailable(
                "target cgroup membership file is unavailable"
            ) from None
    if mountinfo_text is None:
        try:
            mountinfo_text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            raise _CgroupEvidenceUnavailable("target cgroup mount mapping is unavailable") from None
    memberships = membership_text.splitlines()
    paths = []
    for line in memberships:
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1] == "":
            paths.append(parts[2])
    if not paths:
        raise _CgroupEvidenceUnavailable("target cgroup v2 membership is unavailable")
    if len(paths) != 1 or not paths[0].startswith("/"):
        raise _CgroupEvidenceMalformed("target cgroup v2 membership is malformed")
    relative = PurePosixPath(paths[0])
    if ".." in relative.parts:
        raise _CgroupEvidenceMalformed("target cgroup v2 membership is unsafe")
    mount_roots = []
    for line in mountinfo_text.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        filesystem = after.split()
        if (
            separator
            and len(fields) >= 6
            and len(filesystem) >= 1
            and filesystem[0] == "cgroup2"
            and fields[4] == str(root)
        ):
            mount_roots.append(fields[3])
    if not mount_roots:
        raise _CgroupEvidenceUnavailable("target cgroup v2 mount mapping is unavailable")
    if len(mount_roots) != 1 or not mount_roots[0].startswith("/"):
        raise _CgroupEvidenceMalformed("target cgroup v2 mount mapping is malformed")
    mount_root = PurePosixPath(mount_roots[0])
    if ".." in mount_root.parts:
        raise _CgroupEvidenceMalformed("target cgroup v2 mount mapping is unsafe")
    # The membership path is relative to the process's cgroup namespace.  The
    # mountinfo root can be a host-coordinate path, so only use it to verify
    # the mounted filesystem type and mountpoint.
    if str(relative) == "/":
        return root, "namespace-root"
    return root.joinpath(*relative.parts[1:]), "namespace-relative"


def _runtime_limits() -> _RuntimeLimits:
    """Read optional cgroup evidence without weakening configured resources.

    A missing/unmappable cgroup file is represented as an explicit unavailable
    state.  Readable malformed contents or a readable wrong limit remain hard
    failures, so the probe never treats bad evidence as an unconstrained
    target.
    """

    try:
        directory, resolution = _v2_cgroup_directory()
    except (OSError, _CgroupEvidenceUnavailable):
        return _RuntimeLimits(False, None, None, None, None, None)
    except _CgroupEvidenceMalformed:
        raise _TargetPreflightFailure("target_cgroup_mapping") from None
    try:
        cpu_text = (directory / "cpu.max").read_text(encoding="utf-8")
    except OSError:
        raise _TargetPreflightFailure("target_cpu_limit") from None
    try:
        memory_text = (directory / "memory.max").read_text(encoding="utf-8").strip()
    except OSError:
        raise _TargetPreflightFailure("target_memory_limit") from None
    cpu_parts = cpu_text.split()
    if len(cpu_parts) != 2:
        raise _TargetPreflightFailure("target_cpu_limit")
    quota_text, period_text = cpu_parts
    if quota_text == "max" or period_text == "max":
        raise _TargetPreflightFailure("target_cpu_limit")
    if memory_text in {"max", "-1"}:
        raise _TargetPreflightFailure("target_memory_limit")
    try:
        cpu_quota, cpu_period = int(quota_text), int(period_text)
    except ValueError:
        raise _TargetPreflightFailure("target_cpu_limit") from None
    try:
        memory_bytes = int(memory_text)
    except ValueError:
        raise _TargetPreflightFailure("target_memory_limit") from None
    expected_quota = int(cpu_period * CONFIGURED_RESOURCES["cpu"])
    if (
        cpu_quota != expected_quota
        or cpu_quota < 1
        or memory_bytes != CONFIGURED_RESOURCES["memory_bytes"]
    ):
        raise _TargetPreflightFailure("target_limit_contract")
    return _RuntimeLimits(True, cpu_quota, cpu_period, memory_bytes, "v2", resolution)


def _target_identity(
    native: Any,
    *,
    display: str,
    progress: _Progress | None = None,
) -> dict[str, Any]:
    if progress is not None:
        progress.phase = "target_module_hash"
    module_path = getattr(native, "__file__", None)
    if not isinstance(module_path, str):
        raise RuntimeError("native module path is unavailable")
    module_sha = hashlib.sha256(Path(module_path).read_bytes()).hexdigest()
    if progress is not None:
        progress.phase = "target_image_identity"
    image_id = os.environ.get("MODAL_IMAGE_ID")
    if not isinstance(image_id, str) or not image_id.startswith("im-"):
        raise RuntimeError("target image identity is unavailable")
    if progress is not None:
        progress.phase = "target_cgroup_limits"
    limits = _runtime_limits()
    return {
        **_module_identity(native),
        "module_sha256": module_sha,
        "image_object_id": image_id,
        "cpu": CONFIGURED_RESOURCES["cpu"] if limits.cgroup_available else None,
        "quota_usec": limits.quota_usec,
        "period_usec": limits.period_usec,
        "memory_bytes": limits.memory_bytes,
        "cgroup_available": limits.cgroup_available,
        "cgroup_version": limits.cgroup_version,
        "cgroup_resolution": limits.cgroup_resolution,
        "machine": platform.machine(),
        "display": display,
        "width": WIDTH,
        "height": HEIGHT,
    }


async def _bounded_call(
    call: Callable[[], Any],
    *,
    executor: Executor | None = None,
    timeout_seconds: float | None = None,
) -> Any:
    """Run one native call away from the event loop under an absolute bound."""

    # ``run_in_executor`` keeps the event loop free.  The direct PyO3 call can
    # hold the GIL, so its independent bound is the Rust X11 750 ms watchdog;
    # this outer wait is a transport/worker guard and is not a cancellation
    # claim about a running native call.
    # A constructor that reaches this outer deadline is terminal: its native
    # setup watchdog owns the underlying bound, and the child emits no usable
    # arm evidence or survivor continuation.
    if timeout_seconds is None:
        timeout_seconds = OPERATION_TIMEOUT_SECONDS
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(executor, call)
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_seconds)
    except TimeoutError:
        raise _ProbeFailure("TimeoutError", timeout_origin="benchmark_call_deadline") from None


async def _capture_call(
    session: Any,
    *,
    executor: Executor,
    pair_index: int,
    sequence: int,
    position: int,
    arm: str,
) -> dict[str, Any]:
    submitted_ns = perf_counter_ns()
    entered_ns = 0

    def call() -> tuple[int, bytes, NativeCaptureTiming]:
        nonlocal entered_ns
        entered_ns = perf_counter_ns()
        data, timing = session.capture_png_with_timing(**REGION)
        if not isinstance(data, bytes) or not 0 < len(data) <= 64 * 1024**2:
            raise ScreenshotCaptureFailed("capture returned an invalid payload")
        if not isinstance(timing, NativeCaptureTiming):
            raise ScreenshotCaptureFailed("capture returned invalid timing")
        return entered_ns, data, timing

    resumed_ns = 0
    entered_ns, data, timing = await _bounded_call(call, executor=executor)
    resumed_ns = perf_counter_ns()
    pixel_hash = _decode_rgb_pixel_hash(data)
    sample = _CaptureSample(
        payload_bytes=len(data),
        elapsed_ms=(resumed_ns - submitted_ns) / 1_000_000,
        timing=timing,
        pixel_hash=pixel_hash,
    )
    row = _timing_to_row(
        sample,
        pair_index=pair_index,
        sequence=sequence,
        position=position,
        arm=arm,
    )
    row["timing"]["executor_queue_ms"] = (entered_ns - submitted_ns) / 1_000_000
    return row


def _classify_failure(exc: BaseException, *, arm: str) -> tuple[str, str | None]:
    if isinstance(exc, _DirectNativeTimeout):
        return "X11ScreenshotTimeoutError", exc.timeout_origin
    if isinstance(exc, ScreenshotCaptureTimedOut):
        return "ScreenshotCaptureTimedOut", exc.timeout_origin
    if isinstance(exc, _ProbeFailure):
        return exc.failure_type, exc.timeout_origin
    if isinstance(exc, ScreenshotCaptureFailed):
        return "ScreenshotCaptureFailed", None
    return "RuntimeError", None


def _is_terminal_deadline(exc: BaseException) -> bool:
    return isinstance(exc, _ProbeFailure) and exc.timeout_origin == "benchmark_call_deadline"


def _failure_record(
    exc: BaseException,
    *,
    arm: str,
    pair_index: int,
    phase: str,
) -> dict[str, Any]:
    failure_type, timeout_origin = _classify_failure(exc, arm=arm)
    return {
        "pair_index": pair_index,
        "phase": phase,
        "failure_type": failure_type,
        "timeout_origin": timeout_origin,
    }


async def _close_one(session: Any, *, executor: Executor) -> None:
    await _bounded_call(
        session.close,
        executor=executor,
        timeout_seconds=CLEANUP_TIMEOUT_SECONDS,
    )


async def _run_child_inner(
    *,
    pairs: int,
    warmups: int,
    progress: _Progress,
) -> dict[str, Any]:
    if pairs != PAIRS or warmups != WARMUPS:
        raise ValueError("paired discriminator workload is fixed")
    display = os.environ.get("DISPLAY", ":99")
    progress.phase = "native_import"
    native = importlib.import_module(MODULE_NAME)
    progress.phase = "module_identity"
    module_identity = _module_identity(native)
    target_identity = _target_identity(native, display=display, progress=progress)
    sessions: dict[str, Any] = {}
    identities_before: dict[str, dict[str, int]] = {}
    failures: dict[str, dict[str, Any] | None] = {arm: None for arm in ARMS}
    active = {arm: True for arm in ARMS}
    observations: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARMS}
    warmups_completed = {arm: 0 for arm in ARMS}
    captures_completed = {arm: 0 for arm in ARMS}
    session_cleanup_errors: dict[str, list[str]] = {arm: [] for arm in ARMS}
    identities_after: dict[str, dict[str, int] | None] = {}
    # ``None`` is intentional when target cgroup discovery is unavailable;
    # otherwise a successful spawned identity handshake must prove equality.
    worker_cgroup_same: bool | None = None
    pixel_hashes_seen: set[str] = set()
    executors = {arm: _SessionExecutor(arm) for arm in ARMS}
    verify_worker_cgroup = target_identity["cgroup_available"] is True
    try:
        progress.phase = "direct_session_start"
        try:
            sessions[DIRECT_ARM] = await _bounded_call(
                lambda: DirectNativeSession(native, display=display, width=WIDTH, height=HEIGHT),
                executor=executors[DIRECT_ARM].executor,
            )
            identities_before[DIRECT_ARM] = await _bounded_call(
                lambda: _process_identity(os.getpid()),
                executor=executors[DIRECT_ARM].executor,
            )
        except BaseException as exc:
            if _is_terminal_deadline(exc):
                raise
            failures[DIRECT_ARM] = _failure_record(
                exc, arm=DIRECT_ARM, pair_index=0, phase="direct_session_start"
            )
            active[DIRECT_ARM] = False
        progress.phase = "spawned_session_start"
        try:
            sessions[SPAWNED_ARM] = await _bounded_call(
                lambda: SpawnedWorkerSession(display=display, width=WIDTH, height=HEIGHT),
                executor=executors[SPAWNED_ARM].executor,
            )
            spawned_before, observed_cgroup_same = await _bounded_call(
                lambda: _spawned_worker_identity_and_cgroup(
                    sessions[SPAWNED_ARM], verify_cgroup=verify_worker_cgroup
                ),
                executor=executors[SPAWNED_ARM].executor,
            )
            worker_cgroup_same = observed_cgroup_same if verify_worker_cgroup else None
            identities_before[SPAWNED_ARM] = spawned_before
        except BaseException as exc:
            if _is_terminal_deadline(exc):
                raise
            failures[SPAWNED_ARM] = _failure_record(
                exc, arm=SPAWNED_ARM, pair_index=0, phase="spawned_session_start"
            )
            active[SPAWNED_ARM] = False

        for warmup_index in range(WARMUPS):
            for position, arm in enumerate(pair_order(warmup_index)):
                if not active[arm]:
                    continue
                progress.phase = "warmup_capture"
                try:
                    await _capture_call(
                        sessions[arm],
                        executor=executors[arm].executor,
                        pair_index=warmup_index,
                        sequence=warmup_index * 2 + position,
                        position=position,
                        arm=arm,
                    )
                except BaseException as exc:
                    if _is_terminal_deadline(exc):
                        raise
                    failures[arm] = _failure_record(
                        exc,
                        arm=arm,
                        pair_index=warmup_index,
                        phase="warmup_capture",
                    )
                    active[arm] = False
                else:
                    warmups_completed[arm] += 1
                    progress.warmups_completed[arm] += 1

        paired_prefix_samples = 0
        first_unpaired_pair: int | None = None
        for pair_index in range(PAIRS):
            pair_rows: dict[str, dict[str, Any]] = {}
            for position, arm in enumerate(pair_order(pair_index)):
                if not active[arm]:
                    continue
                progress.phase = "measured_capture"
                try:
                    row = await _capture_call(
                        sessions[arm],
                        executor=executors[arm].executor,
                        pair_index=pair_index,
                        sequence=pair_index * 2 + position,
                        position=position,
                        arm=arm,
                    )
                except BaseException as exc:
                    if _is_terminal_deadline(exc):
                        raise
                    failures[arm] = _failure_record(
                        exc,
                        arm=arm,
                        pair_index=pair_index,
                        phase="measured_capture",
                    )
                    active[arm] = False
                    first_unpaired_pair = (
                        pair_index if first_unpaired_pair is None else first_unpaired_pair
                    )
                else:
                    observations[arm].append(row)
                    pixel_hashes_seen.add(row["pixel_hash"])
                    captures_completed[arm] += 1
                    progress.captures_completed[arm] += 1
                    pair_rows[arm] = row
            if set(pair_rows) == set(ARMS) and first_unpaired_pair is None:
                paired_prefix_samples += 1
            elif first_unpaired_pair is None:
                first_unpaired_pair = pair_index
        progress.paired_prefix_samples = paired_prefix_samples
        progress.first_unpaired_pair = first_unpaired_pair
    finally:
        # Sample identity while each persistent session still exists.  A
        # terminal worker timeout may already have reaped its worker; retain
        # ``None`` for that failed arm rather than copying the before sample.
        progress.phase = "identity_after"
        if DIRECT_ARM in identities_before:
            try:
                identities_after[DIRECT_ARM] = await _bounded_call(
                    lambda: _process_identity(identities_before[DIRECT_ARM]["pid"]),
                    executor=executors[DIRECT_ARM].executor,
                )
            except BaseException:
                identities_after[DIRECT_ARM] = None
        if SPAWNED_ARM in identities_before:
            try:
                spawned_after, cgroup_after = await _bounded_call(
                    lambda: _spawned_worker_identity_and_cgroup(
                        sessions[SPAWNED_ARM], verify_cgroup=verify_worker_cgroup
                    ),
                    executor=executors[SPAWNED_ARM].executor,
                )
                identities_after[SPAWNED_ARM] = spawned_after
                worker_cgroup_same = cgroup_after if verify_worker_cgroup else None
            except BaseException:
                identities_after[SPAWNED_ARM] = None
        progress.phase = "session_close"
        for arm in ARMS:
            session = sessions.get(arm)
            if session is None:
                continue
            try:
                await _close_one(session, executor=executors[arm].executor)
            except BaseException as exc:
                failure_type, _ = _classify_failure(exc, arm=arm)
                session_cleanup_errors[arm].append(
                    failure_type if failure_type in SAFE_CLEANUP_ERROR_TYPES else "CleanupError"
                )
        for executor in executors.values():
            executor.shutdown()
    session_cleanup = {
        arm: {
            "succeeded": not session_cleanup_errors[arm],
            "error_types": session_cleanup_errors[arm],
        }
        for arm in ARMS
    }
    passed = all(
        active[arm]
        and failures[arm] is None
        and warmups_completed[arm] == WARMUPS
        and captures_completed[arm] == PAIRS
        and identities_after.get(arm) == identities_before.get(arm)
        and session_cleanup[arm]["succeeded"]
        for arm in ARMS
    )
    unpaired_after_failure = sum(len(observations[arm]) for arm in ARMS) - (
        paired_prefix_samples * len(ARMS)
    )
    return {
        "passed": passed,
        "display": display,
        "geometry": dict(REGION),
        **SCOPE_CONTRACT,
        "configured_resources": dict(CONFIGURED_RESOURCES),
        "module_identity": module_identity,
        "target_identity": target_identity,
        "worker_cgroup_same": worker_cgroup_same,
        "schedule": build_schedule(pairs=pairs),
        "warmups_completed": warmups_completed,
        "captures_completed": captures_completed,
        "paired_prefix_samples": paired_prefix_samples,
        "unpaired_after_failure_samples": max(unpaired_after_failure, 0),
        "first_unpaired_pair": first_unpaired_pair,
        "pixel_hash_parity": len(pixel_hashes_seen) <= 1,
        "arms": {
            arm: {
                "identity_before": identities_before.get(arm),
                "identity_after": identities_after.get(arm),
                "observations": observations[arm],
                "failure": failures[arm],
                "session_cleanup": session_cleanup[arm],
            }
            for arm in ARMS
        },
        "retries": 0,
        "replacement_samples": 0,
        "session_cleanup": {
            "succeeded": all(item["succeeded"] for item in session_cleanup.values()),
            "error_types": sorted(
                {error for item in session_cleanup.values() for error in item["error_types"]}
            ),
        },
    }


async def _run_child(*, pairs: int, warmups: int, progress: _Progress) -> dict[str, Any]:
    try:
        return await _run_child_inner(pairs=pairs, warmups=warmups, progress=progress)
    except BaseException as exc:
        if isinstance(exc, _TargetPreflightFailure):
            progress.phase = exc.phase
        failure_type, timeout_origin = _classify_failure(exc, arm=DIRECT_ARM)
        result: dict[str, Any] = {
            "passed": False,
            **SCOPE_CONTRACT,
            "configured_resources": dict(CONFIGURED_RESOURCES),
            "worker_cgroup_same": None,
            "warmups_completed": dict(progress.warmups_completed),
            "captures_completed": dict(progress.captures_completed),
            "paired_prefix_samples": progress.paired_prefix_samples,
            "unpaired_after_failure_samples": 0,
            "first_unpaired_pair": progress.first_unpaired_pair,
            "pixel_hash_parity": False,
            "arms": {},
            "retries": 0,
            "replacement_samples": 0,
            "failure_type": failure_type,
            "failure_phase": progress.phase if progress.phase in SAFE_FAILURE_PHASES else "summary",
        }
        if timeout_origin is not None:
            result["failure_timeout_origin"] = timeout_origin
        return result


def run_child(*, pairs: int = PAIRS, warmups: int = WARMUPS, cpu: float = 1.0) -> dict[str, Any]:
    configure_resources(cpu)
    progress = _Progress()
    return asyncio.run(_run_child(pairs=pairs, warmups=warmups, progress=progress))


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


def _summary(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    values = [float(row["elapsed_ms"]) for row in rows]
    return {
        "sample_count": len(values),
        "p50_ms": median(values) if values else 0.0,
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": max(values) if values else 0.0,
    }


def _validate_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    target_identity = _validate_target_identity(observation.get("target_identity"))
    worker_cgroup_same = observation.get("worker_cgroup_same")
    if target_identity["cgroup_available"]:
        if worker_cgroup_same is not True:
            raise ValueError("available cgroup evidence requires worker equality")
    elif worker_cgroup_same is not None:
        raise ValueError("unavailable cgroup evidence requires null worker equality")
    retained: dict[str, Any] = {
        "display": observation.get("display"),
        "geometry": _validate_geometry(observation.get("geometry")),
        "module_identity": _validate_module_identity(observation.get("module_identity")),
        "target_identity": target_identity,
        "worker_cgroup_same": worker_cgroup_same,
        "configured_resources": _validate_configured_resources(
            observation.get("configured_resources")
        ),
        "schedule": observation.get("schedule"),
    }
    for key, expected in SCOPE_CONTRACT.items():
        if observation.get(key) != expected:
            raise ValueError(f"scope contract differs for {key}")
        retained[key] = expected
    if retained["display"] != retained["target_identity"]["display"]:
        raise ValueError("display identity differs")
    schedule = observation.get("schedule")
    if schedule != build_schedule():
        raise ValueError("paired schedule differs from fixed schedule")
    for key in ("warmups_completed", "captures_completed"):
        counts = observation.get(key)
        if not isinstance(counts, Mapping) or set(counts) != set(ARMS):
            raise ValueError(f"invalid {key}")
        retained[key] = {
            arm: _bounded_count(
                counts[arm],
                f"{key}.{arm}",
                maximum=(WARMUPS if key == "warmups_completed" else PAIRS),
            )
            for arm in ARMS
        }
    paired = _bounded_count(
        observation.get("paired_prefix_samples"),
        "paired prefix",
        maximum=PAIRS,
    )
    unpaired = _bounded_count(
        observation.get("unpaired_after_failure_samples"),
        "unpaired samples",
        maximum=2 * PAIRS,
    )
    retained["paired_prefix_samples"] = paired
    retained["unpaired_after_failure_samples"] = unpaired
    first_unpaired = observation.get("first_unpaired_pair")
    if first_unpaired is not None:
        first_unpaired = _bounded_count(first_unpaired, "first unpaired pair", maximum=PAIRS - 1)
    retained["first_unpaired_pair"] = first_unpaired
    arms_value = observation.get("arms")
    if not isinstance(arms_value, Mapping) or set(arms_value) != set(ARMS):
        raise ValueError("invalid arm evidence")
    arms: dict[str, Any] = {}
    for arm in ARMS:
        cell = arms_value[arm]
        if not isinstance(cell, Mapping) or set(cell) != {
            "identity_before",
            "identity_after",
            "observations",
            "failure",
            "session_cleanup",
        }:
            raise ValueError("invalid arm evidence")
        before_value = cell["identity_before"]
        before = None if before_value is None else _validate_identity(before_value)
        after_value = cell["identity_after"]
        after = None if after_value is None else _validate_identity(after_value)
        rows_value = cell["observations"]
        if not isinstance(rows_value, list) or len(rows_value) > PAIRS:
            raise ValueError("invalid arm observations")
        rows = [_validate_sample(row, expected_arm=arm) for row in rows_value]
        failure = _validate_failure(cell["failure"], expected_arm=arm)
        cleanup = _validate_cleanup(cell["session_cleanup"], f"{arm} session")
        expected_count = retained["captures_completed"][arm]
        if expected_count != len(rows):
            raise ValueError("arm capture count does not match observations")
        row_pairs = [row["pair_index"] for row in rows]
        if row_pairs != list(range(len(rows))):
            raise ValueError("arm observations are not a contiguous measured prefix")
        completed_warmups = retained["warmups_completed"][arm]
        if failure is None:
            if completed_warmups != WARMUPS or len(rows) != PAIRS:
                raise ValueError("arm without a failure did not complete the fixed workload")
        elif failure["phase"] == "measured_capture":
            if completed_warmups != WARMUPS or failure["pair_index"] != len(rows):
                raise ValueError("measured failure does not follow the retained prefix")
        elif failure["phase"] == "warmup_capture":
            if len(rows) != 0 or failure["pair_index"] != completed_warmups:
                raise ValueError("warmup failure does not follow completed warmups")
        elif failure["phase"] in {"direct_session_start", "spawned_session_start"}:
            if completed_warmups != 0 or len(rows) != 0 or failure["pair_index"] != 0:
                raise ValueError("session-start failure retained measured evidence")
        else:
            raise ValueError("arm failure phase cannot be bound to the measured schedule")
        if (before is None or before != after) and failure is None:
            raise ValueError("arm identity changed")
        arms[arm] = {
            "identity_before": before,
            "identity_after": after,
            "captures_completed": expected_count,
            "observations": rows,
            "summary": _summary(rows),
            "failure": failure,
            "session_cleanup": cleanup,
        }
    rows_by_pair: dict[int, set[str]] = {}
    for arm in ARMS:
        for row in arms[arm]["observations"]:
            rows_by_pair.setdefault(row["pair_index"], set()).add(arm)
    for pair_index in range(paired):
        if rows_by_pair.get(pair_index) != set(ARMS):
            raise ValueError("paired prefix is not contiguous")
    for pair_index in range(paired, PAIRS):
        if rows_by_pair.get(pair_index) == set(ARMS):
            raise ValueError("paired observations continue after the failure prefix")
    expected_unpaired = sum(len(arms[arm]["observations"]) for arm in ARMS) - 2 * paired
    if unpaired != expected_unpaired:
        raise ValueError("unpaired observation count is inconsistent")
    if first_unpaired is None:
        if unpaired != 0:
            raise ValueError("unpaired observations require a first failure pair")
    elif first_unpaired != paired:
        raise ValueError("first failure pair differs from the paired prefix")
    pixel_hashes = {row["pixel_hash"] for arm in ARMS for row in arms[arm]["observations"]}
    pixel_hash_parity = len(pixel_hashes) <= 1
    if observation.get("pixel_hash_parity") is not pixel_hash_parity:
        raise ValueError("decoded RGB pixel hash parity is inconsistent")
    retained["pixel_hash_parity"] = pixel_hash_parity
    retained["arms"] = arms
    retained["retries"] = _bounded_count(observation.get("retries"), "retries", maximum=0)
    retained["replacement_samples"] = _bounded_count(
        observation.get("replacement_samples"), "replacement samples", maximum=0
    )
    retained["session_cleanup"] = _validate_cleanup(
        observation.get("session_cleanup"), "overall session"
    )
    retained["passed"] = observation.get("passed") is True
    return retained


def _validate_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    revision = value.get("source_revision")
    source_sha = value.get("x11_shm_source_sha256")
    lock_sha = value.get("cargo_lock_sha256")
    configured_cpu = value.get("configured_cpu")
    configured_memory = value.get("configured_memory_bytes")
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision)
        or value.get("worktree_clean") is not True
        or not isinstance(source_sha, str)
        or len(source_sha) != 64
        or any(character not in "0123456789abcdef" for character in source_sha)
        or not isinstance(lock_sha, str)
        or len(lock_sha) != 64
        or any(character not in "0123456789abcdef" for character in lock_sha)
        or value.get("image_identity") != "inline:browser-chromium-x11-shm"
        or isinstance(configured_cpu, bool)
        or not isinstance(configured_cpu, float)
        or configured_cpu != CONFIGURED_RESOURCES["cpu"]
        or configured_memory != CONFIGURED_RESOURCES["memory_bytes"]
    ):
        raise ValueError("invalid discriminator provenance")
    return {
        "source_revision": revision,
        "worktree_clean": True,
        "x11_shm_source_sha256": source_sha,
        "cargo_lock_sha256": lock_sha,
        "image_identity": "inline:browser-chromium-x11-shm",
        "configured_cpu": configured_cpu,
        "configured_memory_bytes": CONFIGURED_RESOURCES["memory_bytes"],
    }


def _safe_failure_summary(observation: Mapping[str, Any]) -> tuple[str | None, str | None]:
    failure_type = _safe_label(observation.get("failure_type"))
    origin = observation.get("failure_timeout_origin")
    if origin not in SAFE_TIMEOUT_ORIGINS:
        origin = None
    if failure_type not in SAFE_ARM_FAILURE_TYPES:
        failure_type = None
    return failure_type, origin


def _build_artifact_impl(
    observation: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and sanitize one private child result into a non-gating artifact."""

    retained: dict[str, Any] = {}
    validation_failed = False
    failure_type: str | None = None
    failure_origin: str | None = None
    try:
        retained = _validate_observation(observation)
    except (TypeError, ValueError, OverflowError):
        try:
            retained = _validate_rejected_envelope(observation)
        except (TypeError, ValueError, OverflowError):
            validation_failed = True
    try:
        retained_provenance = _validate_provenance(provenance)
    except (TypeError, ValueError):
        retained_provenance = None
        validation_failed = True
    if (
        not validation_failed
        and retained_provenance is not None
        and retained.get("configured_resources")
        != {
            "cpu": retained_provenance["configured_cpu"],
            "memory_bytes": retained_provenance["configured_memory_bytes"],
        }
    ):
        validation_failed = True
    try:
        retained_cleanup = _validate_cleanup(
            {
                "succeeded": cleanup.get("succeeded"),
                "error_types": cleanup.get("cleanup_error_types", cleanup.get("error_types", [])),
            },
            "sandbox",
        )
        remaining = _bounded_count(
            cleanup.get("remaining_sandboxes", 0),
            "remaining sandboxes",
            maximum=1_000_000,
        )
        survivors = _bounded_count(
            cleanup.get("survivors_before_sweep", 0),
            "survivors before sweep",
            maximum=1_000_000,
        )
        retained_cleanup = {
            **retained_cleanup,
            "remaining_sandboxes": remaining,
            "survivors_before_sweep": survivors,
        }
        cleanup_ok = retained_cleanup["succeeded"] and remaining == 0 and survivors == 0
    except (TypeError, ValueError, OverflowError):
        retained_cleanup = None
        cleanup_ok = False
        validation_failed = True
    if not validation_failed:
        failure_type, failure_origin = _safe_failure_summary(observation)
        arm_failures = [
            retained["arms"][arm]["failure"]
            for arm in ARMS
            if arm in retained["arms"] and retained["arms"][arm]["failure"] is not None
        ]
        cleanup_only = (
            not cleanup_ok
            or ("session_cleanup" in retained and not retained["session_cleanup"]["succeeded"])
            or any(
                arm in retained["arms"]
                and not retained["arms"][arm]["session_cleanup"]["succeeded"]
                for arm in ARMS
            )
        ) and not arm_failures
        if failure_type is None:
            if arm_failures:
                failure_type = arm_failures[0]["failure_type"]
                failure_origin = arm_failures[0]["timeout_origin"]
            elif cleanup_only:
                failure_type = "CleanupError"
            elif not retained.get("pixel_hash_parity", False):
                failure_type = "PixelParityMismatch"
        if retained.get("passed") is True and not cleanup_ok:
            failure_type = "CleanupError"
        elif retained.get("passed") is True:
            if cleanup_only:
                failure_type = "CleanupError"
            else:
                failure_type = (
                    None if retained.get("pixel_hash_parity") is True else "PixelParityMismatch"
                )
        elif failure_type is None:
            failure_type = "EvidenceValidationError"
    else:
        failure_type = "EvidenceValidationError"
        failure_origin = None
        retained = {}
    passed = (
        not validation_failed
        and retained.get("passed") is True
        and retained_cleanup is not None
        and cleanup_ok
        and retained.get("pixel_hash_parity") is True
        and retained["session_cleanup"]["succeeded"]
        and all(retained["arms"][arm]["session_cleanup"]["succeeded"] for arm in ARMS)
        and all(
            retained["arms"][arm]["failure"] is None
            and retained["arms"][arm]["captures_completed"] == PAIRS
            and retained["warmups_completed"][arm] == WARMUPS
            for arm in ARMS
        )
    )
    if passed:
        failure_type = None
        failure_origin = None
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "status": "complete" if passed else "rejected",
        "passed": passed,
        "geometry": dict(REGION),
        **{key: retained.get(key, expected) for key, expected in SCOPE_CONTRACT.items()},
        "configured_resources": retained.get("configured_resources"),
        "pixel_hash_parity": retained.get("pixel_hash_parity", False),
        "arms": retained.get("arms", {}),
        "warmups_completed": retained.get("warmups_completed", {arm: 0 for arm in ARMS}),
        "captures_completed": retained.get("captures_completed", {arm: 0 for arm in ARMS}),
        "paired_prefix_samples": retained.get("paired_prefix_samples", 0),
        "unpaired_after_failure_samples": retained.get("unpaired_after_failure_samples", 0),
        "first_unpaired_pair": retained.get("first_unpaired_pair"),
        "retries": 0,
        "replacement_samples": 0,
        "failure_type": failure_type,
        "failure_timeout_origin": failure_origin,
        "failure_phase": (
            "artifact_validation"
            if validation_failed
            else None
            if passed
            else retained.get("failure_phase")
            or _safe_label(
                observation.get("failure_phase"),
                allowed=SAFE_FAILURE_PHASES,
            )
            or next(
                (
                    retained["arms"][arm]["failure"]["phase"]
                    for arm in ARMS
                    if retained.get("arms", {}).get(arm, {}).get("failure") is not None
                ),
                None,
            )
        ),
        "target_identity": retained.get("target_identity"),
        "worker_cgroup_same": retained.get("worker_cgroup_same"),
        "module_identity": retained.get("module_identity"),
        "display": retained.get("display"),
        "session_cleanup": retained_cleanup,
        "provenance": retained_provenance,
    }
    return result


def build_artifact(
    observation: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    configured_resources: Mapping[str, float | int] | None = None,
) -> dict[str, Any]:
    """Validate one child result, optionally under an ablation CPU contract.

    The optional override is private benchmark plumbing.  It lets the CPU
    ablation validate the same child contract independently for 1 and 2 CPU
    Sandboxes while leaving all public/runtime capture defaults untouched.
    """

    if configured_resources is None:
        return _build_artifact_impl(observation, cleanup, provenance)
    if not isinstance(configured_resources, Mapping):
        raise ValueError("invalid configured resources override")
    requested_cpu = configured_resources.get("cpu")
    if isinstance(requested_cpu, bool) or not isinstance(requested_cpu, float):
        raise ValueError("invalid configured CPU override")
    requested = _validate_configured_resources(
        configured_resources,
        expected_cpu=requested_cpu,
    )
    previous = dict(CONFIGURED_RESOURCES)
    CONFIGURED_RESOURCES.clear()
    CONFIGURED_RESOURCES.update(requested)
    try:
        return _build_artifact_impl(observation, cleanup, provenance)
    finally:
        CONFIGURED_RESOURCES.clear()
        CONFIGURED_RESOURCES.update(previous)


def _read_child_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("child input must be an object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--pairs", type=int, default=PAIRS)
    parser.add_argument("--warmups", type=int, default=WARMUPS)
    parser.add_argument("--cpu", type=float, default=1.0, choices=(1.0, 2.0))
    args = parser.parse_args()
    if not args.child:
        parser.error("this private module is invoked with --child")
    result = run_child(pairs=args.pairs, warmups=args.warmups, cpu=args.cpu)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
