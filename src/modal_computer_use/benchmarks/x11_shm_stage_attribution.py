"""Non-gating stage attribution for the private Rust X11-SHM source."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import perf_counter_ns
from typing import Any

from modal_computer_use.daemon.desktop.screenshot_capture import (
    NativeCaptureTiming,
    X11SharedMemoryScreenshotSession,
)

CAPTURES = 1_000
WARMUPS = 20
TAIL_THRESHOLDS_MS = (50, 100, 500)
CGROUP_FIELDS = ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")
STAGE_METRICS = (
    "controller_total_ms",
    "executor_queue_ms",
    "controller_boundary_residual_ms",
    "parent_total_ms",
    "parent_lock_wait_ms",
    "parent_send_ms",
    "parent_header_wait_ms",
    "parent_payload_read_ms",
    "parent_outside_io_ms",
    "worker_dispatch_ms",
    "worker_response_prep_ms",
    "native_total_ms",
    "x11_reply_ms",
    "rgb_convert_ms",
    "png_encode_ms",
    "native_residual_ms",
)
EXPECTED_MODULE_IDENTITY = {
    "backend": "x11-shm",
    "codec": "png-deflate-level1-no-filter",
    "codec_runtime": "in-process-miniz_oxide",
    "codec_library": "in-process",
}
_STAGE_FAILURE_PHASES = frozenset(
    {
        "workload_contract",
        "cgroup_directory",
        "cpu_limit",
        "native_import",
        "module_identity",
        "session_start",
        "worker_identity",
        "worker_cgroup",
        "cgroup_before",
        "warmup_full_capture",
        "warmup_region_capture",
        "measured_full_capture",
        "measured_region_capture",
        "frame_stability",
        "request_cgroup",
        "worker_identity_after",
        "session_close",
        "cgroup_after",
        "summary",
        "unknown",
    }
)


class _StageProgress:
    def __init__(self) -> None:
        self.phase = "workload_contract"


class _StageAttributionFailure(RuntimeError):
    def __init__(self, phase: str, failure_type: str) -> None:
        self.safe_phase = phase if phase in _STAGE_FAILURE_PHASES else "unknown"
        self.safe_failure_type = (
            failure_type
            if failure_type.isidentifier() and len(failure_type) <= 64
            else "RuntimeError"
        )
        super().__init__("stage attribution diagnostic failed")


@dataclass(frozen=True)
class _CpuCgroupSource:
    version: str
    directory: Path
    usage_file: Path | None


def _finite_nonnegative(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {label}")
    try:
        retained = float(value)
    except OverflowError as exc:
        raise ValueError(f"invalid {label}") from exc
    if not math.isfinite(retained) or retained < 0:
        raise ValueError(f"invalid {label}")
    return retained


def _bounded_count(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty timing sample")
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _metric_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": max(values),
        **{
            f"over_{threshold}_count": sum(value > threshold for value in values)
            for threshold in TAIL_THRESHOLDS_MS
        },
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sample_count": len(rows),
        "metrics": {
            metric: _metric_summary([float(row[metric]) for row in rows])
            for metric in STAGE_METRICS
        },
    }


def _parse_cpu_cgroup_paths(value: str) -> tuple[tuple[str, str], ...]:
    paths: list[tuple[str, str]] = []
    for line in value.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, path = parts
        if not path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise RuntimeError("CPU cgroup path is invalid")
        if not controllers:
            paths.append(("v2", path))
        elif "cpu" in controllers.split(","):
            paths.append(("v1", path))
        elif "cpuacct" in controllers.split(","):
            paths.append(("v1_cpuacct", path))
    if not paths:
        raise RuntimeError("CPU cgroup path is unavailable")
    return tuple(paths)


def _cpu_cgroup_paths(process: str | int = "self") -> tuple[tuple[str, str], ...]:
    value = Path(f"/proc/{process}/cgroup").read_text(encoding="utf-8")
    return _parse_cpu_cgroup_paths(value)


def _select_cpu_cgroup_source(
    root: Path,
    memberships: tuple[tuple[str, str], ...],
) -> _CpuCgroupSource:
    cpuacct_relatives = tuple(
        relative for version, relative in memberships if version == "v1_cpuacct"
    )
    for version, relative in memberships:
        if version == "v1_cpuacct":
            continue
        bases = (root,) if version == "v2" else (root / "cpu,cpuacct", root / "cpu")
        for base in bases:
            relative_path = relative.lstrip("/")
            candidates = (base / relative_path,) if relative_path else (base,)
            for candidate in candidates:
                quota_file = "cpu.max" if version == "v2" else "cpu.cfs_quota_us"
                if not (
                    (candidate / quota_file).is_file()
                    and (candidate / "cpu.stat").is_file()
                ):
                    continue
                if version == "v2":
                    return _CpuCgroupSource(version, candidate, None)
                usage_relatives = cpuacct_relatives or (relative,)
                usage_candidates = [candidate / "cpuacct.usage"]
                for usage_relative in usage_relatives:
                    usage_path = usage_relative.lstrip("/")
                    usage_directory = (
                        root / "cpuacct" / usage_path
                        if usage_path
                        else root / "cpuacct"
                    )
                    usage_candidates.append(usage_directory / "cpuacct.usage")
                usage_file = next(
                    (path for path in usage_candidates if path.is_file()), None
                )
                if usage_file is not None:
                    return _CpuCgroupSource(version, candidate, usage_file)
    raise RuntimeError("CPU cgroup directory is unavailable")


def _cgroup_source() -> _CpuCgroupSource:
    return _select_cpu_cgroup_source(
        Path("/sys/fs/cgroup"),
        _cpu_cgroup_paths(),
    )


def _cpu_stat(source: _CpuCgroupSource) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in (source.directory / "cpu.stat").read_text(encoding="utf-8").splitlines():
        key, raw = line.split()
        if key in CGROUP_FIELDS or key == "throttled_time":
            values[key] = int(raw)
    if "usage_usec" not in values and source.usage_file is not None:
        values["usage_usec"] = int(source.usage_file.read_text(encoding="utf-8")) // 1_000
    if "throttled_usec" not in values and "throttled_time" in values:
        values["throttled_usec"] = values.pop("throttled_time") // 1_000
    if set(values) != set(CGROUP_FIELDS):
        raise RuntimeError("cgroup cpu.stat is incomplete")
    return values


def _cpu_max(source: _CpuCgroupSource) -> dict[str, int]:
    if source.version == "v2":
        quota, period = (source.directory / "cpu.max").read_text(
            encoding="utf-8"
        ).split()
        if quota == "max":
            raise RuntimeError("stage diagnostic requires a fixed CPU quota")
    else:
        quota = (source.directory / "cpu.cfs_quota_us").read_text(
            encoding="utf-8"
        ).strip()
        period = (source.directory / "cpu.cfs_period_us").read_text(
            encoding="utf-8"
        ).strip()
        if quota == "-1":
            raise RuntimeError("stage diagnostic requires a fixed CPU quota")
    result = {"quota_usec": int(quota), "period_usec": int(period)}
    if result["quota_usec"] != result["period_usec"] or result["quota_usec"] < 1:
        raise RuntimeError("stage diagnostic requires exactly one CPU")
    return result


def _process_identity(pid: int) -> dict[str, int]:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
    return {"pid": pid, "starttime_ticks": int(fields[19])}


def _stage_row(
    timing: NativeCaptureTiming,
    *,
    controller_total_ns: int,
    executor_queue_ns: int,
) -> dict[str, float]:
    native_components = (
        timing.x11_reply_ns + timing.rgb_convert_ns + timing.png_encode_ns
    )
    if timing.native_total_ns < native_components:
        raise RuntimeError("native stage algebra is invalid")
    parent_components = (
        timing.parent_lock_wait_ns
        + timing.parent_send_ns
        + timing.parent_header_wait_ns
        + timing.parent_payload_read_ns
    )
    if timing.parent_total_ns < parent_components:
        raise RuntimeError("parent stage algebra is invalid")
    if controller_total_ns < executor_queue_ns + timing.parent_total_ns:
        raise RuntimeError("controller stage algebra is invalid")
    ns = {
        "controller_total_ms": controller_total_ns,
        "executor_queue_ms": executor_queue_ns,
        "controller_boundary_residual_ms": (
            controller_total_ns - executor_queue_ns - timing.parent_total_ns
        ),
        "parent_total_ms": timing.parent_total_ns,
        "parent_lock_wait_ms": timing.parent_lock_wait_ns,
        "parent_send_ms": timing.parent_send_ns,
        "parent_header_wait_ms": timing.parent_header_wait_ns,
        "parent_payload_read_ms": timing.parent_payload_read_ns,
        "parent_outside_io_ms": timing.parent_total_ns - parent_components,
        "worker_dispatch_ms": timing.worker_dispatch_ns,
        "worker_response_prep_ms": timing.worker_response_prep_ns,
        "native_total_ms": timing.native_total_ns,
        "x11_reply_ms": timing.x11_reply_ns,
        "rgb_convert_ms": timing.rgb_convert_ns,
        "png_encode_ms": timing.png_encode_ns,
        "native_residual_ms": timing.native_total_ns - native_components,
    }
    return {key: value / 1_000_000 for key, value in ns.items()}


def _tail_owner(row: Mapping[str, float], threshold: int) -> str:
    if row["executor_queue_ms"] > threshold:
        return "executor_queue"
    if row["parent_total_ms"] > threshold:
        if row["native_total_ms"] > threshold:
            for metric, owner in (
                ("x11_reply_ms", "x11_reply"),
                ("rgb_convert_ms", "rgb_convert"),
                ("png_encode_ms", "png_encode"),
                ("native_residual_ms", "native_residual"),
            ):
                if row[metric] > threshold:
                    return owner
            return "native_unresolved"
        for metric, owner in (
            ("worker_dispatch_ms", "worker_dispatch"),
            ("worker_response_prep_ms", "worker_response_prep"),
            ("parent_lock_wait_ms", "parent_lock_wait"),
            ("parent_send_ms", "parent_send"),
            ("parent_payload_read_ms", "parent_payload_read"),
            ("parent_header_wait_ms", "worker_or_ipc_wait"),
            ("parent_outside_io_ms", "parent_residual"),
        ):
            if row[metric] > threshold:
                return owner
        return "parent_unresolved"
    if row["controller_boundary_residual_ms"] > threshold:
        return "executor_resume_or_boundary"
    return "unattributed"


async def _capture_once(
    session: X11SharedMemoryScreenshotSession,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[bytes, dict[str, float]]:
    loop = asyncio.get_running_loop()
    submitted_ns = perf_counter_ns()

    def capture() -> tuple[int, bytes, NativeCaptureTiming]:
        entered_ns = perf_counter_ns()
        data, timing = session.capture_png_with_timing(
            x=x, y=y, width=width, height=height
        )
        return entered_ns, data, timing

    future = loop.run_in_executor(None, capture)
    entered_ns, data, timing = await future
    resumed_ns = perf_counter_ns()
    return data, _stage_row(
        timing,
        controller_total_ns=resumed_ns - submitted_ns,
        executor_queue_ns=entered_ns - submitted_ns,
    )


async def _run_child_inner(
    *, captures: int, warmups: int, progress: _StageProgress
) -> dict[str, Any]:
    if captures != CAPTURES or warmups != WARMUPS:
        raise ValueError("stage diagnostic workload is fixed")
    progress.phase = "cgroup_directory"
    cgroup_source = _cgroup_source()
    progress.phase = "cpu_limit"
    cpu_limit = _cpu_max(cgroup_source)
    progress.phase = "native_import"
    native = importlib.import_module("_modal_computer_use_x11_shm")
    progress.phase = "module_identity"
    module_identity = {
        key: getattr(native, key, None) for key in EXPECTED_MODULE_IDENTITY
    }
    if module_identity != EXPECTED_MODULE_IDENTITY:
        raise RuntimeError("native module identity differs from the fixed diagnostic")
    progress.phase = "session_start"
    session = X11SharedMemoryScreenshotSession(
        display=os.environ.get("DISPLAY", ":99"), width=1024, height=768
    )
    progress.phase = "worker_identity"
    owner = session._session
    process = getattr(owner, "_process", None)
    if process is None or not isinstance(process.pid, int):
        raise RuntimeError("timed worker identity is unavailable")
    worker_before = _process_identity(process.pid)
    progress.phase = "worker_cgroup"
    worker_cgroup_same = set(_cpu_cgroup_paths(process.pid)) == set(
        _cpu_cgroup_paths()
    )
    if not worker_cgroup_same:
        raise RuntimeError("timed worker does not share the diagnostic cgroup")
    rows: list[dict[str, Any]] = []
    lane_hashes: dict[str, bytes] = {}
    payload_sizes: dict[str, list[int]] = {"full": [], "region": []}
    progress.phase = "cgroup_before"
    cgroup_before = _cpu_stat(cgroup_source)
    worker_after = None
    try:
        for index in range(warmups + captures):
            lane = "full" if index % 2 == 0 else "region"
            geometry = (0, 0, 1024, 768) if lane == "full" else (7, 9, 511, 383)
            measured = index >= warmups
            if measured:
                progress.phase = "request_cgroup"
            cpu_before_request = _cpu_stat(cgroup_source) if measured else None
            progress.phase = (
                f"measured_{lane}_capture" if measured else f"warmup_{lane}_capture"
            )
            data, stages = await _capture_once(
                session,
                x=geometry[0],
                y=geometry[1],
                width=geometry[2],
                height=geometry[3],
            )
            if measured:
                progress.phase = "request_cgroup"
            cpu_after_request = _cpu_stat(cgroup_source) if measured else None
            progress.phase = "frame_stability"
            digest = hashlib.sha256(data).digest()
            expected_digest = lane_hashes.setdefault(lane, digest)
            if digest != expected_digest:
                raise RuntimeError("captured frame changed during the fixed diagnostic")
            if not measured:
                continue
            if cpu_before_request is None or cpu_after_request is None:
                raise RuntimeError("per-request cgroup evidence is unavailable")
            progress.phase = "request_cgroup"
            cgroup_delta = {
                field: cpu_after_request[field] - cpu_before_request[field]
                for field in CGROUP_FIELDS
            }
            if any(value < 0 for value in cgroup_delta.values()):
                raise RuntimeError("cgroup counters regressed")
            rows.append(
                {
                    "schedule_index": index - warmups,
                    "lane": lane,
                    **stages,
                    **{f"cgroup_{key}_delta": value for key, value in cgroup_delta.items()},
                }
            )
            payload_sizes[lane].append(len(data))
        progress.phase = "worker_identity_after"
        worker_after = _process_identity(worker_before["pid"])
    finally:
        previous_phase = progress.phase
        primary_failure_active = sys.exc_info()[0] is not None
        try:
            session.close()
        except Exception:
            if primary_failure_active:
                progress.phase = previous_phase
            else:
                progress.phase = "session_close"
                raise
        else:
            progress.phase = previous_phase
    if worker_after is None:
        raise RuntimeError("terminal timed worker identity is unavailable")
    progress.phase = "cgroup_after"
    cgroup_after = _cpu_stat(cgroup_source)
    cgroup_delta = {
        field: cgroup_after[field] - cgroup_before[field] for field in CGROUP_FIELDS
    }
    if any(value < 0 for value in cgroup_delta.values()):
        raise RuntimeError("aggregate cgroup counters regressed")
    progress.phase = "summary"
    tail_schedule = [
        {
            "schedule_index": row["schedule_index"],
            "lane": row["lane"],
            "owner_over_50": _tail_owner(row, 50),
            "owner_over_500": _tail_owner(row, 500)
            if row["controller_total_ms"] > 500
            else None,
            **{metric: row[metric] for metric in STAGE_METRICS},
            **{
                f"cgroup_{field}_delta": row[f"cgroup_{field}_delta"]
                for field in CGROUP_FIELDS
            },
        }
        for row in rows
        if row["controller_total_ms"] > 50
    ]
    return {
        "passed": True,
        "warmups_completed": warmups,
        "captures_completed": len(rows),
        "full_captures": sum(row["lane"] == "full" for row in rows),
        "region_captures": sum(row["lane"] == "region" for row in rows),
        "frame_stable_by_lane": len(lane_hashes) == 2,
        "module_identity": module_identity,
        "worker_identity_before": worker_before,
        "worker_identity_after": worker_after,
        "worker_cgroup_same": worker_cgroup_same,
        "cgroup_version": cgroup_source.version,
        "cpu_max": cpu_limit,
        "cgroup_cpu_stat_before": cgroup_before,
        "cgroup_cpu_stat_after": cgroup_after,
        "cgroup_cpu_stat_delta": cgroup_delta,
        "payload_bytes": {
            lane: {"min": min(values), "max": max(values)}
            for lane, values in payload_sizes.items()
        },
        "summaries": {
            "combined": _summarize(rows),
            "full": _summarize([row for row in rows if row["lane"] == "full"]),
            "region": _summarize([row for row in rows if row["lane"] == "region"]),
        },
        "tail_schedule": tail_schedule,
        "failure_type": None,
        "failure_phase": None,
    }


async def _run_child(*, captures: int, warmups: int) -> dict[str, Any]:
    progress = _StageProgress()
    try:
        return await _run_child_inner(
            captures=captures,
            warmups=warmups,
            progress=progress,
        )
    except Exception as exc:
        raise _StageAttributionFailure(progress.phase, type(exc).__name__) from None


def run_child(*, captures: int = CAPTURES, warmups: int = WARMUPS) -> dict[str, Any]:
    try:
        return asyncio.run(_run_child(captures=captures, warmups=warmups))
    except _StageAttributionFailure as exc:
        return {
            "passed": False,
            "warmups_completed": 0,
            "captures_completed": 0,
            "full_captures": 0,
            "region_captures": 0,
            "failure_type": exc.safe_failure_type,
            "failure_phase": exc.safe_phase,
        }


def _validate_identity(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"pid", "starttime_ticks"}:
        raise ValueError("invalid worker identity")
    return {
        "pid": _bounded_count(value["pid"], "worker pid", maximum=2**31 - 1),
        "starttime_ticks": _bounded_count(
            value["starttime_ticks"], "worker starttime", maximum=2**63 - 1
        ),
    }


def _validate_cpu_max(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"quota_usec", "period_usec"}:
        raise ValueError("invalid cpu.max evidence")
    retained = {
        key: _bounded_count(value[key], f"cpu.max {key}", maximum=2**31 - 1)
        for key in ("quota_usec", "period_usec")
    }
    if retained["quota_usec"] < 1 or retained["quota_usec"] != retained["period_usec"]:
        raise ValueError("stage diagnostic did not use exactly one CPU")
    return retained


def _validate_cgroup(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(CGROUP_FIELDS):
        raise ValueError(f"invalid {label}")
    return {
        field: _bounded_count(value[field], f"{label}.{field}", maximum=2**63 - 1)
        for field in CGROUP_FIELDS
    }


def _validate_payload_bytes(value: object) -> dict[str, dict[str, int]]:
    if not isinstance(value, Mapping) or set(value) != {"full", "region"}:
        raise ValueError("invalid payload evidence")
    retained: dict[str, dict[str, int]] = {}
    for lane in ("full", "region"):
        cell = value[lane]
        if not isinstance(cell, Mapping) or set(cell) != {"min", "max"}:
            raise ValueError("invalid payload evidence")
        minimum = _bounded_count(
            cell["min"], f"{lane} payload min", maximum=64 * 1024 * 1024
        )
        maximum = _bounded_count(
            cell["max"], f"{lane} payload max", maximum=64 * 1024 * 1024
        )
        if minimum < 1 or maximum < minimum:
            raise ValueError("invalid payload evidence")
        retained[lane] = {"min": minimum, "max": maximum}
    return retained


def _validate_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    revision = value.get("source_revision")
    source_sha = value.get("x11_shm_source_sha256")
    lock_sha = value.get("cargo_lock_sha256")
    image_identity = value.get("image_identity")
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
        or image_identity != "inline:browser-chromium-x11-shm"
    ):
        raise ValueError("invalid stage diagnostic provenance")
    return {
        "source_revision": revision,
        "worktree_clean": True,
        "x11_shm_source_sha256": source_sha,
        "cargo_lock_sha256": lock_sha,
        "image_identity": image_identity,
    }


def _validate_target_identity(value: object) -> dict[str, Any]:
    expected_keys = {
        "backend",
        "codec",
        "module_sha256",
        "image_object_id",
        "cpu",
        "memory_bytes",
        "machine",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise ValueError("invalid target runtime identity")
    module_sha = value["module_sha256"]
    image_id = value["image_object_id"]
    machine = value["machine"]
    cpu = _finite_nonnegative(value["cpu"], "target cpu")
    memory_bytes = _bounded_count(
        value["memory_bytes"], "target memory", maximum=16 * 1024**3
    )
    if (
        value["backend"] != "x11-shm"
        or value["codec"] != "png-deflate-level1-no-filter"
        or not isinstance(module_sha, str)
        or len(module_sha) != 64
        or any(character not in "0123456789abcdef" for character in module_sha)
        or not isinstance(image_id, str)
        or not image_id.startswith("im-")
        or len(image_id) > 64
        or cpu != 1.0
        or memory_bytes != 2048 * 1024**2
        or not isinstance(machine, str)
        or not 1 <= len(machine) <= 32
        or not all(character.isalnum() or character in "_-" for character in machine)
    ):
        raise ValueError("target runtime identity differs from the fixed diagnostic")
    return {
        "backend": "x11-shm",
        "codec": "png-deflate-level1-no-filter",
        "module_sha256": module_sha,
        "image_object_id": image_id,
        "cpu": cpu,
        "memory_bytes": memory_bytes,
        "machine": machine,
    }


def _safe_label(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if not all(character.isalnum() or character in "_.-" for character in value):
        return None
    return value


def _validate_summary(value: object, expected_count: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"sample_count", "metrics"}:
        raise ValueError("invalid stage summary")
    if value["sample_count"] != expected_count or not isinstance(value["metrics"], Mapping):
        raise ValueError("invalid stage summary count")
    if set(value["metrics"]) != set(STAGE_METRICS):
        raise ValueError("invalid stage metric set")
    retained: dict[str, Any] = {"sample_count": expected_count, "metrics": {}}
    for metric in STAGE_METRICS:
        cell = value["metrics"][metric]
        expected_keys = {"p50_ms", "p95_ms", "p99_ms", "max_ms"} | {
            f"over_{threshold}_count" for threshold in TAIL_THRESHOLDS_MS
        }
        if not isinstance(cell, Mapping) or set(cell) != expected_keys:
            raise ValueError("invalid stage metric summary")
        retained_cell: dict[str, Any] = {
            key: _finite_nonnegative(cell[key], f"{metric}.{key}")
            for key in ("p50_ms", "p95_ms", "p99_ms", "max_ms")
        }
        if not (
            retained_cell["p50_ms"]
            <= retained_cell["p95_ms"]
            <= retained_cell["p99_ms"]
            <= retained_cell["max_ms"]
        ):
            raise ValueError("stage summary percentiles are inconsistent")
        for threshold in TAIL_THRESHOLDS_MS:
            key = f"over_{threshold}_count"
            retained_cell[key] = _bounded_count(cell[key], key, maximum=expected_count)
        retained["metrics"][metric] = retained_cell
    return retained


def build_artifact(
    observation: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    validation_failed = False
    retained: dict[str, Any] = {}
    try:
        warmups = _bounded_count(
            observation.get("warmups_completed"), "warmups", maximum=WARMUPS
        )
        captures = _bounded_count(
            observation.get("captures_completed"), "captures", maximum=CAPTURES
        )
        full = _bounded_count(observation.get("full_captures"), "full", maximum=CAPTURES)
        region = _bounded_count(
            observation.get("region_captures"), "region", maximum=CAPTURES
        )
        retained.update(
            {
                "warmups_completed": warmups,
                "captures_completed": captures,
                "full_captures": full,
                "region_captures": region,
            }
        )
        if observation.get("passed") is True:
            if (warmups, captures, full, region) != (
                WARMUPS,
                CAPTURES,
                CAPTURES // 2,
                CAPTURES // 2,
            ):
                raise ValueError("stage diagnostic counts differ from the fixed workload")
            if observation.get("frame_stable_by_lane") is not True:
                raise ValueError("stage diagnostic frame changed")
            module_identity = observation.get("module_identity")
            if module_identity != EXPECTED_MODULE_IDENTITY:
                raise ValueError("stage diagnostic module identity changed")
            target_identity = _validate_target_identity(
                observation.get("target_identity")
            )
            worker_before = _validate_identity(observation.get("worker_identity_before"))
            worker_after = _validate_identity(observation.get("worker_identity_after"))
            if worker_before != worker_after:
                raise ValueError("stage diagnostic worker identity changed")
            if observation.get("worker_cgroup_same") is not True:
                raise ValueError("stage diagnostic worker cgroup changed")
            cgroup_version = observation.get("cgroup_version")
            if cgroup_version not in {"v1", "v2"}:
                raise ValueError("stage diagnostic cgroup version is invalid")
            cpu_max = _validate_cpu_max(observation.get("cpu_max"))
            cgroup_before = _validate_cgroup(
                observation.get("cgroup_cpu_stat_before"), "cgroup before"
            )
            cgroup_after = _validate_cgroup(
                observation.get("cgroup_cpu_stat_after"), "cgroup after"
            )
            cgroup_delta = _validate_cgroup(
                observation.get("cgroup_cpu_stat_delta"), "cgroup delta"
            )
            if any(
                cgroup_after[field] - cgroup_before[field] != cgroup_delta[field]
                for field in CGROUP_FIELDS
            ):
                raise ValueError("aggregate cgroup evidence is inconsistent")
            payload_bytes = _validate_payload_bytes(observation.get("payload_bytes"))
            summaries_value = observation.get("summaries")
            if not isinstance(summaries_value, Mapping) or set(summaries_value) != {
                "combined",
                "full",
                "region",
            }:
                raise ValueError("invalid stage diagnostic summaries")
            summaries = {
                "combined": _validate_summary(summaries_value["combined"], CAPTURES),
                "full": _validate_summary(summaries_value["full"], CAPTURES // 2),
                "region": _validate_summary(summaries_value["region"], CAPTURES // 2),
            }
            tail_value = observation.get("tail_schedule")
            if not isinstance(tail_value, list) or len(tail_value) > CAPTURES:
                raise ValueError("invalid stage tail schedule")
            tail_schedule: list[dict[str, Any]] = []
            seen: set[int] = set()
            allowed_owners = {
                "executor_queue",
                "x11_reply",
                "rgb_convert",
                "png_encode",
                "native_residual",
                "native_unresolved",
                "worker_dispatch",
                "worker_response_prep",
                "parent_lock_wait",
                "parent_send",
                "parent_payload_read",
                "worker_or_ipc_wait",
                "parent_residual",
                "parent_unresolved",
                "executor_resume_or_boundary",
                "unattributed",
            }
            for row in tail_value:
                if not isinstance(row, Mapping):
                    raise ValueError("invalid stage tail row")
                index = _bounded_count(
                    row.get("schedule_index"),
                    "tail index",
                    maximum=CAPTURES - 1,
                )
                if index in seen or row.get("lane") not in {"full", "region"}:
                    raise ValueError("invalid stage tail schedule")
                seen.add(index)
                retained_row: dict[str, Any] = {
                    "schedule_index": index,
                    "lane": row["lane"],
                    "owner_over_50": row.get("owner_over_50"),
                    "owner_over_500": row.get("owner_over_500"),
                }
                if retained_row["owner_over_50"] not in allowed_owners:
                    raise ValueError("invalid stage tail owner")
                if retained_row["owner_over_500"] not in allowed_owners | {None}:
                    raise ValueError("invalid stage tail owner")
                for metric in STAGE_METRICS:
                    retained_row[metric] = _finite_nonnegative(row.get(metric), metric)
                if retained_row["controller_total_ms"] <= 50:
                    raise ValueError("stage tail row is below retention threshold")
                epsilon_ms = 1e-6
                if retained_row["native_total_ms"] + epsilon_ms < (
                    retained_row["x11_reply_ms"]
                    + retained_row["rgb_convert_ms"]
                    + retained_row["png_encode_ms"]
                ):
                    raise ValueError("stage tail native algebra is invalid")
                if retained_row["parent_total_ms"] + epsilon_ms < (
                    retained_row["parent_lock_wait_ms"]
                    + retained_row["parent_send_ms"]
                    + retained_row["parent_header_wait_ms"]
                    + retained_row["parent_payload_read_ms"]
                ):
                    raise ValueError("stage tail parent algebra is invalid")
                if retained_row["controller_total_ms"] + epsilon_ms < (
                    retained_row["executor_queue_ms"]
                    + retained_row["parent_total_ms"]
                ):
                    raise ValueError("stage tail controller algebra is invalid")
                expected_native_residual = retained_row["native_total_ms"] - (
                    retained_row["x11_reply_ms"]
                    + retained_row["rgb_convert_ms"]
                    + retained_row["png_encode_ms"]
                )
                expected_parent_residual = retained_row["parent_total_ms"] - (
                    retained_row["parent_lock_wait_ms"]
                    + retained_row["parent_send_ms"]
                    + retained_row["parent_header_wait_ms"]
                    + retained_row["parent_payload_read_ms"]
                )
                expected_boundary_residual = retained_row["controller_total_ms"] - (
                    retained_row["executor_queue_ms"]
                    + retained_row["parent_total_ms"]
                )
                for actual, expected in (
                    (retained_row["native_residual_ms"], expected_native_residual),
                    (retained_row["parent_outside_io_ms"], expected_parent_residual),
                    (
                        retained_row["controller_boundary_residual_ms"],
                        expected_boundary_residual,
                    ),
                ):
                    if abs(actual - expected) > epsilon_ms:
                        raise ValueError("stage tail residual algebra is invalid")
                metric_view = {
                    metric: float(retained_row[metric]) for metric in STAGE_METRICS
                }
                if retained_row["owner_over_50"] != _tail_owner(metric_view, 50):
                    raise ValueError("stage tail owner is inconsistent")
                if (retained_row["controller_total_ms"] > 500) != (
                    retained_row["owner_over_500"] is not None
                ):
                    raise ValueError("stage tail owner does not match threshold")
                if retained_row["controller_total_ms"] > 500 and (
                    retained_row["owner_over_500"] != _tail_owner(metric_view, 500)
                ):
                    raise ValueError("stage tail owner is inconsistent")
                expected_lane = "full" if index % 2 == 0 else "region"
                if retained_row["lane"] != expected_lane:
                    raise ValueError("stage tail lane differs from fixed schedule")
                for field in CGROUP_FIELDS:
                    key = f"cgroup_{field}_delta"
                    retained_row[key] = _bounded_count(
                        row.get(key), key, maximum=2**63 - 1
                    )
                tail_schedule.append(retained_row)
            controller_summary = summaries["combined"]["metrics"]["controller_total_ms"]
            if len(tail_schedule) != controller_summary["over_50_count"]:
                raise ValueError("stage tail schedule is incomplete")
            for threshold in (100, 500):
                if sum(row["controller_total_ms"] > threshold for row in tail_schedule) != (
                    controller_summary[f"over_{threshold}_count"]
                ):
                    raise ValueError("stage tail schedule count is inconsistent")
            retained.update(
                {
                    "frame_stable_by_lane": True,
                    "module_identity": dict(EXPECTED_MODULE_IDENTITY),
                    "target_identity": target_identity,
                    "worker_identity_before": worker_before,
                    "worker_identity_after": worker_after,
                    "worker_cgroup_same": True,
                    "cgroup_version": cgroup_version,
                    "summaries": summaries,
                    "tail_schedule": tail_schedule,
                    "payload_bytes": payload_bytes,
                    "cpu_max": cpu_max,
                    "cgroup_cpu_stat_before": cgroup_before,
                    "cgroup_cpu_stat_after": cgroup_after,
                    "cgroup_cpu_stat_delta": cgroup_delta,
                }
            )
    except (KeyError, TypeError, ValueError):
        validation_failed = True
    try:
        cleanup_remaining = _bounded_count(
            cleanup.get("remaining_sandboxes"), "remaining sandboxes", maximum=100
        )
        cleanup_survivors = _bounded_count(
            cleanup.get("survivors_before_sweep"), "cleanup survivors", maximum=100
        )
    except ValueError:
        cleanup_remaining = None
        cleanup_survivors = None
    cleanup_ok = (
        cleanup.get("succeeded") is True
        and cleanup_remaining == 0
        and cleanup_survivors == 0
        and cleanup.get("cleanup_error_types") == []
    )
    try:
        retained_provenance = _validate_provenance(provenance)
    except ValueError:
        retained_provenance = None
    provenance_failed = retained_provenance is None
    passed = bool(
        observation.get("passed") is True
        and not validation_failed
        and cleanup_ok
        and retained_provenance is not None
    )
    return {
        "schema_version": "x11-shm-stage-attribution.v1",
        "benchmark": "x11-shm-stage-attribution",
        "status": "complete" if passed else "rejected",
        "passed": passed,
        "scope": "same-sandbox-private-source-mechanism-only",
        "non_gating": True,
        "promotion_proxy": False,
        "same_sandbox": True,
        "http_transport_excluded": True,
        "daemon_route_excluded": True,
        "instrumentation_intrusive": True,
        "lane_order_confounded": True,
        "cgroup_scope": "sandbox-all-processes",
        "requested_source": "mss",
        "diagnostic_source": "x11-shm",
        "warmups_requested": WARMUPS,
        "captures_requested": CAPTURES,
        **retained,
        "failure_type": (
            "EvidenceValidationError"
            if validation_failed or provenance_failed
            else "CleanupError"
            if observation.get("passed") is True and not cleanup_ok
            else _safe_label(observation.get("failure_type"))
        ),
        "failure_phase": (
            "artifact_validation"
            if validation_failed or provenance_failed
            else "terminal_cleanup"
            if observation.get("passed") is True and not cleanup_ok
            else _safe_label(observation.get("failure_phase"))
        ),
        "retries": 0,
        "replacement_samples": 0,
        "provenance": retained_provenance,
        "terminal_cleanup": {
            "succeeded": cleanup.get("succeeded") is True,
            "remaining_sandboxes": cleanup_remaining,
            "survivors_before_sweep": cleanup_survivors,
            "cleanup_error_count": 0 if cleanup.get("cleanup_error_types") == [] else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--captures", type=int, default=CAPTURES)
    parser.add_argument("--warmups", type=int, default=WARMUPS)
    arguments = parser.parse_args()
    print(json.dumps(run_child(captures=arguments.captures, warmups=arguments.warmups)))


if __name__ == "__main__":
    main()
