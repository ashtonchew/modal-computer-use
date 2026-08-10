#!/usr/bin/env python3
"""Non-gating public SDK vertical slice for the stock-zlib X11-SHM build.

This compares MSS with one separately built Rust X11-SHM artifact whose PNG
codec is system zlib at level 1 with NoFilter. It uses the literal public
screenshots.full() call, ten excluded warmups, and 100 measured pairs in
fixed AB/BA order. It does not change an SDK default or promotion gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import statistics
import subprocess
import sys
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, suppress
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import modal
from PIL import Image

from modal_computer_use import AsyncComputerSandbox, ComputerConfig
from modal_computer_use.config import (
    ActionConfig,
    BrowserConfig,
    DesktopConfig,
    ResourceConfig,
    RuntimeConfig,
)
from modal_computer_use.image import (
    BROWSER_APT_PACKAGES,
    DESKTOP_APT_PACKAGES,
    _add_x11_shared_memory_capture,
)
from modal_computer_use.latency import SessionStartupTiming

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NATIVE_SOURCE = PROJECT_ROOT / "src" / "modal_computer_use" / "_native" / "x11_shm"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "x11_shm_chromium_fixture.html"
APP_NAME = "mcu-x11-shm-stock-zlib-vertical-slice"
REGION = "us-west-2"
ENVIRONMENT = "main"
WIDTH = 1024
HEIGHT = 768
DEPTH = 24
CPU = 1.0
MEMORY_MIB = 2048
SAMPLES_PER_ARM = 100
WARMUPS_PER_ARM = 10
PAYLOAD_LIMIT_BYTES = 57_546
MSS_REPORTED_PAYLOAD_BYTES = 52_315
SDK_P50_LIMIT_MS = 16.40
SDK_P95_MAX_REGRESSION_PERCENT = 5.0
PAYLOAD_P50_MAX_GROWTH_PERCENT = 10.0
TAIL_THRESHOLDS_MS = (100, 500)
STOCK_ZLIB_CODEC = "png-deflate-level1-no-filter-stock-zlib"
MINIZ_CODEC = "png-deflate-level1-no-filter"
BROWSER_LAUNCH_ARGS = (
    "--kiosk",
    "--window-position=0,0",
    "--window-size=1024,768",
    "--force-device-scale-factor=1",
    "--no-first-run",
    "--disable-session-crashed-bubble",
    "--disable-infobars",
)


def _fixture_data_url() -> str:
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    return "data:text/html;charset=utf-8," + quote(html, safe="")


FIXTURE_DATA_URL = _fixture_data_url()
BENCHMARK_RUN_TAG = (
    "stock-zlib-vertical-slice-"
    + hashlib.sha256(FIXTURE_DATA_URL.encode("utf-8")).hexdigest()[:12]
)


def _stock_zlib_image() -> object:
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install(*DESKTOP_APT_PACKAGES, *BROWSER_APT_PACKAGES)
        .pip_install_from_pyproject("pyproject.toml")
    )
    image = _add_x11_shared_memory_capture(
        image, cargo_features=("extension-module", "stock-zlib")
    )
    return (
        image.env(
            {
                "COMPUTER_USE_WINDOW_MANAGER": "xfce",
                "COMPUTER_USE_IMAGE_PROFILE": "browser",
                "COMPUTER_USE_BROWSER_PREWARM": "true",
                "COMPUTER_USE_BROWSER": "chromium",
            }
        )
        .add_local_python_source("modal_computer_use", copy=True)
        .add_local_dir(
            str(PROJECT_ROOT / "scripts"),
            remote_path="/opt/mcu-scripts",
            copy=False,
            ignore=("__pycache__", "*.pyc"),
        )
    )


app = modal.App(APP_NAME)
image = _stock_zlib_image()


class _ArmContext(AbstractAsyncContextManager[Any]):
    def __init__(self, arm: str) -> None:
        if arm not in {"mss", "x11-shm"}:
            raise ValueError("the vertical slice has exactly MSS and x11-shm arms")
        self.arm = arm
        self.context: AbstractAsyncContextManager[Any] | None = None
        self.target_identity: dict[str, Any] | None = None
        self.fixture_verified = False

    async def __aenter__(self) -> Any:
        config = ComputerConfig(
            desktop=DesktopConfig(
                resolution=(WIDTH, HEIGHT),
                window_manager="xfce",
                display_depth=DEPTH,
            ),
            browser=BrowserConfig(
                kind="chromium",
                prewarm=True,
                launch_args=list(BROWSER_LAUNCH_ARGS),
                open_url_on_start=FIXTURE_DATA_URL,
                gpu_mode="off",
            ),
            runtime=RuntimeConfig(
                modal_environment=ENVIRONMENT,
                modal_region=REGION,
                timeout_seconds=900,
                readiness_timeout_seconds=180,
            ),
            resources=ResourceConfig(
                profile="browser",
                cpu=CPU,
                memory_mib=MEMORY_MIB,
            ),
            actions=ActionConfig(
                input_backend="xtest",
                screenshot_capture_source=self.arm,
            ),
            ingress="attested-tunnel",
            expose_vnc="off",
        )
        self.context = AsyncComputerSandbox.create(
            config=config,
            app_name=APP_NAME,
            image=image,
            owner=f"stock-zlib-{self.arm}",
            tags={"benchmark_run": BENCHMARK_RUN_TAG},
            cpu=(CPU, CPU),
            memory=(MEMORY_MIB, MEMORY_MIB),
            timing=SessionStartupTiming(),
        )
        try:
            computer = await self.context.__aenter__()
            self.target_identity = await _target_runtime_identity(computer)
            status = await computer.browser.status()
            prewarm_result = status.get("prewarm_result")
            if (
                status.get("configured_browser") != "chromium"
                or status.get("prewarm") is not True
                or not isinstance(prewarm_result, Mapping)
                or prewarm_result.get("ok") is not True
                or status.get("open_url_on_start") != FIXTURE_DATA_URL
                or not isinstance(status.get("windows"), int)
                or int(status["windows"]) < 1
            ):
                raise RuntimeError("managed Chromium fixture did not become ready")
            self.fixture_verified = True
            return computer
        except BaseException as exc:
            context, self.context = self.context, None
            if context is not None:
                with suppress(Exception):
                    await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.context is not None:
            await self.context.__aexit__(exc_type, exc, traceback)
        self.context = None


async def _process_stdout_text(process: Any) -> str:
    raw = await process.stdout.read.aio()
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    if isinstance(raw, str):
        return raw.strip()
    raise RuntimeError("runtime identity returned invalid output")


async def _target_runtime_identity(computer: Any) -> dict[str, Any]:
    sandbox = getattr(computer, "_sandbox", None)
    if sandbox is None or not hasattr(sandbox, "exec"):
        raise RuntimeError("sandbox handle unavailable for runtime identity")
    script = """
import hashlib, json, os, platform
from pathlib import Path
import _modal_computer_use_x11_shm as native

def read(path):
    return Path(path).read_text(encoding="utf-8").strip()

cpu_max = Path("/sys/fs/cgroup/cpu.max")
if cpu_max.is_file():
    quota, period = read(cpu_max).split()
else:
    quota = read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
memory_path = Path("/sys/fs/cgroup/memory.max")
memory_limit = (
    read(memory_path)
    if memory_path.is_file()
    else read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
)
print(json.dumps({
    "backend": native.backend,
    "codec": native.codec,
    "codec_runtime": native.codec_runtime,
    "codec_library": native.codec_library,
    "module_sha256": hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
    "image_object_id": os.environ.get("MODAL_IMAGE_ID"),
    "cpu": None if quota in {"max", "-1"} else int(quota) / int(period),
    "memory_bytes": None if memory_limit in {"max", "-1"} else int(memory_limit),
    "machine": platform.machine(),
}, sort_keys=True))
"""
    process = await sandbox.exec.aio("python", "-c", script, timeout=30)
    output = await _process_stdout_text(process)
    payload = json.loads(output)
    runtime_version = payload.get("codec_runtime") if isinstance(payload, dict) else None
    module_sha256 = payload.get("module_sha256") if isinstance(payload, dict) else None
    runtime_version_valid = (
        isinstance(runtime_version, str)
        and len(runtime_version.split(".")) >= 3
        and all(part.isdigit() for part in runtime_version.split("."))
    )
    module_sha256_valid = (
        isinstance(module_sha256, str)
        and len(module_sha256) == 64
        and all(character in "0123456789abcdef" for character in module_sha256)
    )
    if (
        not isinstance(payload, dict)
        or payload.get("backend") != "x11-shm"
        or payload.get("codec") != STOCK_ZLIB_CODEC
        or not runtime_version_valid
        or payload.get("codec_library") != "system-libz"
        or not module_sha256_valid
        or not str(payload.get("image_object_id", "")).startswith("im-")
        or payload.get("cpu") != CPU
        or payload.get("memory_bytes") != MEMORY_MIB * 1024 * 1024
    ):
        raise RuntimeError("stock-zlib native build identity is invalid")
    return payload


async def _final_tagged_cleanup() -> dict[str, Any]:
    """Sweep this run's tag and prove that no tagged Sandboxes remain."""

    app_id = app.app_id
    if not isinstance(app_id, str) or not app_id.startswith("ap-"):
        return {
            "succeeded": False,
            "tag": {"benchmark_run": BENCHMARK_RUN_TAG},
            "survivors_before_sweep": None,
            "remaining_sandboxes": None,
            "cleanup_error_types": ["MissingAppIdentity"],
            "terminal_zero_survivors": False,
        }

    async def live_sandboxes() -> list[Any]:
        live: list[Any] = []
        async for sandbox in modal.Sandbox.list.aio(
            app_id=app_id,
            tags={"benchmark_run": BENCHMARK_RUN_TAG},
        ):
            if await sandbox.poll.aio() is None:
                live.append(sandbox)
        return live

    errors: list[str] = []
    try:
        survivors = await live_sandboxes()
    except Exception as exc:
        return {
            "succeeded": False,
            "tag": {"benchmark_run": BENCHMARK_RUN_TAG},
            "survivors_before_sweep": None,
            "remaining_sandboxes": None,
            "cleanup_error_types": [type(exc).__name__],
            "terminal_zero_survivors": False,
        }
    for sandbox in survivors:
        try:
            await sandbox.terminate.aio(wait=True)
        except Exception as exc:
            errors.append(type(exc).__name__)
    try:
        remaining = await live_sandboxes()
    except Exception as exc:
        errors.append(type(exc).__name__)
        remaining = []
        remaining_known = False
    else:
        remaining_known = True
    remaining_count = len(remaining) if remaining_known else None
    terminal_zero_survivors = remaining_known and remaining_count == 0
    return {
        "succeeded": not survivors and not errors and terminal_zero_survivors,
        "tag": {"benchmark_run": BENCHMARK_RUN_TAG},
        "survivors_before_sweep": len(survivors),
        "remaining_sandboxes": remaining_count,
        "cleanup_error_types": errors,
        "terminal_zero_survivors": terminal_zero_survivors,
    }


def _local_provenance() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is unavailable")
    revision = subprocess.run(  # noqa: S603 - fixed git executable and arguments.
        (git, "rev-parse", "HEAD"),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 - fixed git executable and arguments.
        (git, "status", "--porcelain"),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    digest = hashlib.sha256()
    for child in sorted(NATIVE_SOURCE.rglob("*")):
        relative = child.relative_to(NATIVE_SOURCE)
        if (
            not child.is_file()
            or "target" in relative.parts
            or "__pycache__" in relative.parts
            or child.name.endswith(".pyc")
        ):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
    return {
        "source_revision": revision,
        "worktree_clean": not bool(status),
        "working_tree_status_entries": len(status.splitlines()),
        "x11_shm_source_sha256": digest.hexdigest(),
        "cargo_lock_sha256": hashlib.sha256(
            (NATIVE_SOURCE / "Cargo.lock").read_bytes()
        ).hexdigest(),
        "native_feature": "stock-zlib",
        "target_cpu": CPU,
        "target_memory_mib": MEMORY_MIB,
        "requested_region": REGION,
    }


def _pixel_parity_callback() -> Any:
    expected: bytes | None = None

    def parity(data: bytes) -> bool:
        nonlocal expected
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.size != (WIDTH, HEIGHT):
                return False
            pixels = image.convert("RGB").tobytes()
        if expected is None:
            expected = pixels
            return True
        return pixels == expected

    return parity


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _metrics(measurement: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for arm in ("mss", "x11-shm"):
        observations = measurement["arms"][arm]["observations"]
        sdk_values = [float(item["complete_sdk_ms"]) for item in observations]
        result[arm] = {
            "samples": len(observations),
            "sdk_p50_ms": statistics.median(sdk_values),
            "sdk_p95_ms": _percentile(sdk_values, 0.95),
            "sdk_over_100ms_count": sum(value > 100.0 for value in sdk_values),
            "sdk_over_500ms_count": sum(value > 500.0 for value in sdk_values),
            "daemon_p50_ms": statistics.median(
                float(item["daemon_total_ms"]) for item in observations
            ),
            "payload_p50_bytes": statistics.median(
                int(item["payload_bytes"]) for item in observations
            ),
            "payload_min_bytes": min(int(item["payload_bytes"]) for item in observations),
            "payload_max_bytes": max(int(item["payload_bytes"]) for item in observations),
            "pixel_parity_all": all(item["decoded_pixel_parity"] for item in observations),
            "metadata_parity_all": all(item["metadata_parity"] for item in observations),
            "capture_backend_counts": {
                backend: sum(item["capture_backend"] == backend for item in observations)
                for backend in {item["capture_backend"] for item in observations}
            },
        }
    baseline = result["mss"]
    candidate = result["x11-shm"]
    candidate["sdk_p50_improvement_percent_vs_mss"] = (
        (baseline["sdk_p50_ms"] - candidate["sdk_p50_ms"])
        / baseline["sdk_p50_ms"]
        * 100.0
    )
    candidate["payload_growth_percent_vs_mss"] = (
        (candidate["payload_p50_bytes"] - baseline["payload_p50_bytes"])
        / baseline["payload_p50_bytes"]
        * 100.0
    )
    return result


async def _measure() -> dict[str, Any]:
    sys.path.insert(0, "/opt/mcu-scripts")
    from benchmarks.full_screenshot_sdk_harness import measure_full_screenshot_arms

    contexts: dict[str, _ArmContext] = {}

    def borrow(arm: str) -> _ArmContext:
        context = _ArmContext(arm)
        contexts[arm] = context
        return context

    try:
        measurement = await measure_full_screenshot_arms(
            {"mss": lambda: borrow("mss"), "x11-shm": lambda: borrow("x11-shm")},
            sample_count=SAMPLES_PER_ARM,
            warmup_iterations=WARMUPS_PER_ARM,
            schedule_seed=20260810,
            schedule_order="alternating",
            decode_parity=_pixel_parity_callback(),
            expected_capture_backends={"mss": "mss", "x11-shm": "x11-shm"},
            retain_partial_evidence=True,
        )
    except Exception as exc:
        measurement = {
            "status": "rejected",
            "failure": {
                "phase": "measurement",
                "exception_type": type(exc).__name__,
                "arm": None,
                "sample_index": None,
            },
            "arms": {
                "mss": {"observations": [], "transport_traces": []},
                "x11-shm": {"observations": [], "transport_traces": []},
            },
            "fallback_counts": {"mss": 0, "x11-shm": 0},
            "cleanup": {"errors": [], "succeeded": False},
            "schedule": [],
            "warmup_schedule": [],
            "warmup_completed_per_arm": {"mss": 0, "x11-shm": 0},
        }
    terminal_cleanup = await _final_tagged_cleanup()
    identities = {
        arm: context.target_identity or {} for arm, context in contexts.items()
    }
    identity_fields = (
        "module_sha256",
        "image_object_id",
        "codec",
        "codec_runtime",
        "codec_library",
        "cpu",
        "memory_bytes",
    )
    identity_match = all(
        identities.get("mss", {}).get(field)
        == identities.get("x11-shm", {}).get(field)
        for field in identity_fields
    )
    if measurement.get("status") == "complete" and terminal_cleanup.get("succeeded") is not True:
        measurement["status"] = "rejected"
        measurement["failure"] = {
            "phase": "terminal_cleanup",
            "exception_type": "TerminalCleanupError",
            "arm": None,
            "sample_index": None,
        }
    elif measurement.get("status") == "complete" and not identity_match:
        measurement["status"] = "rejected"
        measurement["failure"] = {
            "phase": "identity_validation",
            "exception_type": "IdentityMismatch",
            "arm": None,
            "sample_index": None,
        }
    return {
        "measurement": measurement,
        "target_identities": identities,
        "target_identity_match": identity_match,
        "fixture_verified": all(context.fixture_verified for context in contexts.values()),
        "terminal_cleanup": terminal_cleanup,
    }

class _CodecProofFailure(RuntimeError):
    def __init__(self, partial: Mapping[str, Any]) -> None:
        super().__init__()
        self.partial = dict(partial)


def _run_local_codec_proof() -> dict[str, Any]:
    commands = [
        (
            "miniz",
            (
                "cargo", "run", "--manifest-path", str(NATIVE_SOURCE / "Cargo.toml"),
                "--locked", "--release", "--bin", "codec_proof",
            ),
            MINIZ_CODEC,
        ),
        (
            "stock-zlib",
            (
                "cargo", "run", "--manifest-path", str(NATIVE_SOURCE / "Cargo.toml"),
                "--locked", "--release", "--features", "stock-zlib", "--bin", "codec_proof",
            ),
            STOCK_ZLIB_CODEC,
        ),
    ]
    results: dict[str, Any] = {}
    for name, command, expected_codec in commands:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed local Cargo command.
                command, cwd=PROJECT_ROOT, check=True, capture_output=True, text=True
            )
        except Exception as exc:
            raise _CodecProofFailure(results) from exc
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        if not lines:
            raise _CodecProofFailure(results)
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise _CodecProofFailure(results) from exc
        if not isinstance(result, dict):
            raise _CodecProofFailure(results)
        if (
            result.get("codec") != expected_codec
            or result.get("width") != WIDTH
            or result.get("height") != HEIGHT
            or result.get("pixel_parity") is not True
            or result.get("decoded_pixel_bytes") != WIDTH * HEIGHT * 3
        ):
            raise _CodecProofFailure(results)
        results[name] = result
    if results["miniz"]["pixel_hash"] != results["stock-zlib"]["pixel_hash"]:
        raise _CodecProofFailure(results)
    return results


_FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {
        "body",
        "data",
        "raw",
        "screenshot_bytes",
        "token",
        "bearer",
        "url",
        "no_vnc_url",
    }
)


def _assert_safe_artifact_value(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_ARTIFACT_KEYS:
                raise ValueError("unsafe artifact field")
            _assert_safe_artifact_value(child)
    elif isinstance(value, list):
        for child in value:
            _assert_safe_artifact_value(child)


def _safe_identity(identity: object) -> dict[str, Any]:
    if not isinstance(identity, Mapping):
        return {}
    fields = (
        "backend",
        "codec",
        "codec_runtime",
        "codec_library",
        "module_sha256",
        "image_object_id",
        "cpu",
        "memory_bytes",
        "machine",
    )
    return {
        field: identity[field]
        for field in fields
        if field in identity
        and isinstance(identity[field], (str, int, float, type(None)))
    }


def _safe_provenance(provenance: object) -> dict[str, Any]:
    if not isinstance(provenance, Mapping):
        return {}
    fields = (
        "source_revision",
        "worktree_clean",
        "working_tree_status_entries",
        "x11_shm_source_sha256",
        "cargo_lock_sha256",
        "native_feature",
        "target_cpu",
        "target_memory_mib",
        "requested_region",
    )
    return {
        field: provenance[field]
        for field in fields
        if field in provenance
        and isinstance(provenance[field], (str, int, float, bool, type(None)))
    }


def _safe_partial_live(live: object) -> dict[str, Any]:
    if not isinstance(live, Mapping):
        return {}
    measurement = live.get("measurement")
    partial: dict[str, Any] = {
        "arms": {},
        "fallback_counts": {},
        "warmup_completed_per_arm": {},
    }
    if isinstance(measurement, Mapping):
        arms = measurement.get("arms")
        if isinstance(arms, Mapping):
            for arm in ("mss", "x11-shm"):
                item = arms.get(arm)
                observations = item.get("observations") if isinstance(item, Mapping) else []
                traces = item.get("transport_traces") if isinstance(item, Mapping) else []
                partial["arms"][arm] = {
                    "observations_completed": (
                        len(observations) if isinstance(observations, list) else 0
                    ),
                    "transport_traces_completed": (
                        len(traces) if isinstance(traces, list) else 0
                    ),
                }
        for field in ("fallback_counts", "warmup_completed_per_arm"):
            value = measurement.get(field)
            if isinstance(value, Mapping):
                partial[field] = {
                    arm: int(value.get(arm, 0)) if isinstance(value.get(arm, 0), int) else 0
                    for arm in ("mss", "x11-shm")
                }
        status = measurement.get("status")
        partial["status"] = status if isinstance(status, str) and status in {
            "complete",
            "rejected",
        } else "unknown"
        cleanup = measurement.get("cleanup")
        if isinstance(cleanup, Mapping):
            partial["cleanup"] = {
                "succeeded": cleanup.get("succeeded") is True,
                "error_types": [
                    item.get("exception_type")
                    for item in cleanup.get("errors", [])
                    if isinstance(item, Mapping) and isinstance(item.get("exception_type"), str)
                ],
            }
    identities = live.get("target_identities")
    if isinstance(identities, Mapping):
        partial["target_identities"] = {
            arm: _safe_identity(identities.get(arm)) for arm in ("mss", "x11-shm")
        }
    terminal = live.get("terminal_cleanup")
    if isinstance(terminal, Mapping):
        partial["terminal_cleanup"] = {
            "succeeded": terminal.get("succeeded") is True,
            "survivors_before_sweep": terminal.get("survivors_before_sweep")
            if isinstance(terminal.get("survivors_before_sweep"), int)
            else None,
            "remaining_sandboxes": terminal.get("remaining_sandboxes")
            if isinstance(terminal.get("remaining_sandboxes"), int)
            else None,
            "terminal_zero_survivors": terminal.get("terminal_zero_survivors") is True,
        }
    return partial


def _safe_codec_proof(proof: object) -> dict[str, Any]:
    if not isinstance(proof, Mapping):
        return {}
    allowed = {
        "codec",
        "width",
        "height",
        "payload_bytes",
        "encode_ms",
        "decoded_pixel_bytes",
        "pixel_parity",
        "pixel_hash",
    }
    result: dict[str, Any] = {}
    for name in ("miniz", "stock-zlib"):
        item = proof.get(name)
        if not isinstance(item, Mapping):
            continue
        result[name] = {
            key: item[key]
            for key in allowed
            if key in item and isinstance(item[key], (str, int, float, bool, type(None)))
        }
    return result


def _validate_fixed_schedule(measurement: Mapping[str, Any]) -> bool:
    """Require the exact preregistered AB/BA rows, not only a label."""

    try:
        from benchmarks.full_screenshot_sdk_harness import build_paired_schedule
    except ModuleNotFoundError:
        from scripts.benchmarks.full_screenshot_sdk_harness import build_paired_schedule

    measured = measurement.get("schedule")
    warmups = measurement.get("warmup_schedule")
    if not isinstance(measured, list) or not isinstance(warmups, list):
        return False
    expected_measured = build_paired_schedule(
        ("mss", "x11-shm"),
        sample_count=SAMPLES_PER_ARM,
        seed=20260810,
        order="alternating",
    )
    expected_warmups = build_paired_schedule(
        ("mss", "x11-shm"),
        sample_count=WARMUPS_PER_ARM,
        seed=20260810,
        order="alternating",
        minimum_sample_count=1,
    )
    return measured == expected_measured and warmups == expected_warmups


def _build_rejected_artifact(
    *,
    phase: str,
    exception_type: str,
    provenance: object = None,
    codec_proof: object = None,
    live: object = None,
) -> dict[str, Any]:
    safe_phase = phase if phase.replace("_", "").isalnum() else "unknown"
    safe_type = exception_type if exception_type.replace("_", "").isalnum() else "unknown"
    return {
        "schema_version": 1,
        "benchmark": "x11-shm-stock-zlib-vertical-slice",
        "status": "rejected",
        "non_gating": True,
        "promotion_proxy": False,
        "public_call": "await computer.screenshots.full()",
        "preregistration": {
            "samples_per_arm": SAMPLES_PER_ARM,
            "warmup_iterations": WARMUPS_PER_ARM,
            "schedule_order": "alternating",
            "schedule_seed": 20260810,
            "retries": 0,
            "replacement_samples": 0,
        },
        "failure": {"phase": safe_phase, "exception_type": safe_type},
        "deterministic_codec_proof": _safe_codec_proof(codec_proof),
        "provenance": _safe_provenance(provenance),
        "partial_evidence": _safe_partial_live(live),
        "pass_evaluation": {"all_minimal_checks": False},
    }


def _write_artifact(path: Path, artifact: Mapping[str, Any]) -> None:
    _preflight_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite a prior proof. The caller can choose a fresh explicit
    # path, preserving the first failure artifact for audit.
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(artifact, indent=2, sort_keys=True) + "\n")


def _preflight_output(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("artifact output path must be absolute and explicit")
    if path.exists():
        raise FileExistsError(f"artifact output already exists: {path}")


def _build_artifact(
    *,
    codec_proof: Mapping[str, Any],
    provenance: Mapping[str, Any],
    live: Mapping[str, Any],
) -> dict[str, Any]:
    _assert_safe_artifact_value(codec_proof)
    _assert_safe_artifact_value(provenance)
    _assert_safe_artifact_value(live)
    measurement = live["measurement"]
    if not isinstance(measurement, Mapping) or measurement.get("status") != "complete":
        raise ValueError("remote measurement did not complete")
    if not _validate_fixed_schedule(measurement):
        raise ValueError("remote measurement schedule differs from preregistration")
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "x11-shm-stock-zlib-vertical-slice",
        "status": "complete",
        "non_gating": True,
        "promotion_proxy": False,
        "public_call": "await computer.screenshots.full()",
        "preregistration": {
            "samples_per_arm": SAMPLES_PER_ARM,
            "warmup_iterations": WARMUPS_PER_ARM,
            "schedule_order": "alternating",
            "schedule_seed": 20260810,
            "retries": 0,
            "replacement_samples": 0,
        },
        "pass_criteria": {
            "candidate_backend": "x11-shm",
            "candidate_codec": STOCK_ZLIB_CODEC,
            "payload_max_bytes": PAYLOAD_LIMIT_BYTES,
            "literal_mss_reported_payload_bytes": MSS_REPORTED_PAYLOAD_BYTES,
            "candidate_payload_p50_max_growth_percent_vs_live_mss": PAYLOAD_P50_MAX_GROWTH_PERCENT,
            "candidate_sdk_p50_max_ms": SDK_P50_LIMIT_MS,
            "candidate_sdk_p95_max_regression_percent_vs_live_mss": SDK_P95_MAX_REGRESSION_PERCENT,
            "tail_thresholds_ms": list(TAIL_THRESHOLDS_MS),
            "tail_counts": "candidate count at each threshold must be <= live MSS count",
            "existing_p95_gate_semantics": "unchanged; artifact is non-gating characterization",
            "zero_fallback": True,
            "pixel_and_dimension_parity": True,
            "provenance_clean": True,
            "terminal_cleanup": "tagged sweep with zero survivors before and after",
        },
        "deterministic_codec_proof": dict(codec_proof),
        "provenance": dict(provenance),
        "fixture": {
            "kind": "local deterministic data document",
            "width": WIDTH,
            "height": HEIGHT,
            "depth": DEPTH,
            "browser": "chromium",
            "cursor": "hidden",
        },
        "schedule": measurement["schedule"],
        "arms": measurement["arms"],
        "fallback_counts": measurement["fallback_counts"],
        "cleanup": measurement["cleanup"],
        "terminal_cleanup": live["terminal_cleanup"],
        "metrics": _metrics(measurement),
        "target_identities": live["target_identities"],
        "target_identity_match": live["target_identity_match"],
        "fixture_verified": live["fixture_verified"],
    }
    for arm in ("mss", "x11-shm"):
        arm_identity = live["target_identities"].get(arm, {})
        if isinstance(arm_identity, Mapping):
            artifact["arms"][arm]["observed_codec"] = arm_identity.get("codec")
            artifact["arms"][arm]["codec_runtime"] = arm_identity.get("codec_runtime")
    observations = measurement["arms"]["x11-shm"]["observations"]
    identity = live["target_identities"].get("x11-shm", {})
    checks = {
        "exact_samples": all(
            len(measurement["arms"][arm]["observations"]) == SAMPLES_PER_ARM
            for arm in ("mss", "x11-shm")
        ),
        "exact_warmups": all(
            measurement.get("warmup_completed_per_arm", {}).get(arm) == WARMUPS_PER_ARM
            for arm in ("mss", "x11-shm")
        ),
        "backend_every_call": all(
            item.get("capture_backend") == "x11-shm" for item in observations
        ),
        "codec_identity": identity.get("codec") == STOCK_ZLIB_CODEC,
        "codec_runtime_identity": identity.get("codec_library") == "system-libz"
        and isinstance(identity.get("codec_runtime"), str)
        and identity["codec_runtime"].count(".") >= 2,
        "fixed_schedule": _validate_fixed_schedule(measurement),
        "fallback_zero": measurement["fallback_counts"].get("x11-shm") == 0,
        "fallback_zero_all_arms": all(
            measurement["fallback_counts"].get(arm) == 0 for arm in ("mss", "x11-shm")
        ),
        "pixel_parity": all(
            item.get("decoded_pixel_parity") is True
            for arm in ("mss", "x11-shm")
            for item in measurement["arms"][arm]["observations"]
        ),
        "metadata_parity": all(
            item.get("metadata_parity") is True
            for arm in ("mss", "x11-shm")
            for item in measurement["arms"][arm]["observations"]
        ),
        "payload_limit": all(
            int(item["payload_bytes"]) <= PAYLOAD_LIMIT_BYTES for item in observations
        ),
        "payload_p50_growth_limit": artifact["metrics"]["x11-shm"][
            "payload_growth_percent_vs_mss"
        ]
        <= PAYLOAD_P50_MAX_GROWTH_PERCENT,
        "sdk_p50_limit": artifact["metrics"]["x11-shm"]["sdk_p50_ms"] <= SDK_P50_LIMIT_MS,
        "beats_mss_p50": artifact["metrics"]["x11-shm"]["sdk_p50_ms"]
        < artifact["metrics"]["mss"]["sdk_p50_ms"],
        "sdk_p95_regression_limit": artifact["metrics"]["x11-shm"]["sdk_p95_ms"]
        <= artifact["metrics"]["mss"]["sdk_p95_ms"]
        * (1.0 + SDK_P95_MAX_REGRESSION_PERCENT / 100.0),
        "tail_counts_no_worse": all(
            artifact["metrics"]["x11-shm"][f"sdk_over_{threshold}ms_count"]
            <= artifact["metrics"]["mss"][f"sdk_over_{threshold}ms_count"]
            for threshold in TAIL_THRESHOLDS_MS
        ),
        "cleanup": measurement["cleanup"].get("succeeded") is True,
        "terminal_cleanup": live["terminal_cleanup"].get("succeeded") is True
        and live["terminal_cleanup"].get("survivors_before_sweep") == 0
        and live["terminal_cleanup"].get("remaining_sandboxes") == 0
        and live["terminal_cleanup"].get("terminal_zero_survivors") is True,
        "target_identity_match": live["target_identity_match"] is True,
        "provenance_clean": provenance.get("worktree_clean") is True,
        "fixture_verified": live["fixture_verified"] is True,
    }
    checks["all_minimal_checks"] = all(bool(value) for value in checks.values())
    artifact["pass_evaluation"] = checks
    return artifact


@app.function(
    image=image,
    cpu=CPU,
    memory=MEMORY_MIB,
    timeout=3_600,
    region=REGION,
    retries=0,
)
def run() -> dict[str, Any]:
    return asyncio.run(_measure())


@app.local_entrypoint()
def main(output: str | None = None) -> None:
    if not output:
        raise SystemExit("an explicit absolute --output artifact path is required")
    path = Path(output)
    _preflight_output(path)
    codec_proof: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    live: Mapping[str, Any] | None = None

    try:
        codec_proof = _run_local_codec_proof()
    except Exception as exc:
        partial = getattr(exc, "partial", {})
        artifact = _build_rejected_artifact(
            phase="codec_proof",
            exception_type=type(exc).__name__,
            codec_proof=partial,
        )
        _write_artifact(path, artifact)
        print(json.dumps(artifact, indent=2, sort_keys=True))
        raise SystemExit(1) from None

    try:
        provenance = _local_provenance()
    except Exception as exc:
        artifact = _build_rejected_artifact(
            phase="provenance",
            exception_type=type(exc).__name__,
            codec_proof=codec_proof,
        )
        _write_artifact(path, artifact)
        print(json.dumps(artifact, indent=2, sort_keys=True))
        raise SystemExit(1) from None

    try:
        live_result = run.remote()
        if not isinstance(live_result, Mapping):
            raise TypeError("remote proof returned no mapping")
        live = live_result
        artifact = _build_artifact(
            codec_proof=codec_proof,
            provenance=provenance,
            live=live,
        )
    except Exception as exc:
        artifact = _build_rejected_artifact(
            phase="live_vertical_slice",
            exception_type=type(exc).__name__,
            provenance=provenance,
            codec_proof=codec_proof,
            live=live,
        )
        _write_artifact(path, artifact)
        print(json.dumps(artifact, indent=2, sort_keys=True))
        raise SystemExit(1) from None

    _write_artifact(path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(
        "Use modal run scripts/benchmarks/x11_shm_stock_zlib_vertical_slice.py"
    )
