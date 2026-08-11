#!/usr/bin/env python3
"""Modal promotion runner for X11 shared-memory screenshot capture.

The runner compares the existing MSS source with the ``x11-shm`` source on
the same managed Chromium image and the same public SDK path:

    await computer.screenshots.full()

Both arms use 1024x768x24, one CPU, 2048 MiB, attested ingress, and one warm
pooled async SDK client.  The page is a local deterministic data document, so
the measured frame has no external URL or network dependency.  This file is a
benchmark tool; it never changes a production default.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import platform
import random
import statistics
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from textwrap import dedent, indent
from typing import Any
from urllib.parse import quote

import modal

import modal_computer_use
from modal_computer_use import AsyncComputerSandbox, ComputerConfig
from modal_computer_use.benchmarks.x11_shm_screenshot import (
    FIXED_GATES,
    evaluate_x11_shm_screenshot_promotion,
    validate_x11_shm_screenshot_artifact,
)
from modal_computer_use.benchmarks.x11_shm_stage_attribution import (
    CAPTURES as STAGE_ATTRIBUTION_CAPTURES,
)
from modal_computer_use.benchmarks.x11_shm_stage_attribution import (
    WARMUPS as STAGE_ATTRIBUTION_WARMUPS,
)
from modal_computer_use.benchmarks.x11_shm_stage_attribution import (
    build_artifact as build_stage_attribution_artifact,
)
from modal_computer_use.config import (
    ActionConfig,
    BrowserConfig,
    DesktopConfig,
    ResourceConfig,
    RuntimeConfig,
)
from modal_computer_use.errors import DaemonHTTPError
from modal_computer_use.image import _named_image_recipe
from modal_computer_use.latency import SessionStartupTiming

_RUNNER_PATH = Path(__file__).resolve()
PROJECT_ROOT = _RUNNER_PATH.parents[2] if len(_RUNNER_PATH.parents) > 2 else Path("/root")
FIXTURE_PATH = _RUNNER_PATH.parent / "fixtures" / "x11_shm_chromium_fixture.html"


def _load_fixture_html() -> str:
    candidates = (
        FIXTURE_PATH,
        Path("/opt/mcu-scripts/benchmarks/fixtures/x11_shm_chromium_fixture.html"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise RuntimeError("Chromium screenshot fixture is not available in the benchmark image")


FIXTURE_HTML = _load_fixture_html()
FIXTURE_DATA_URL = "data:text/html;charset=utf-8," + quote(FIXTURE_HTML, safe="")

APP_NAME = "mcu-x11-shm-screenshot-promotion"
REGION = "us-west-2"
ENVIRONMENT = "main"
CLOUD = "aws"
WIDTH = 1024
HEIGHT = 768
DEPTH = 24

_TARGET_RUNTIME_IDENTITY_PHASES = frozenset(
    {
        "exec_launch",
        "process_wait",
        "process_exit",
        "stdout_read",
        "empty_output",
        "json_decode",
        "envelope",
        "sandbox_handle",
        "native_import",
        "cpu_limit",
        "memory_limit",
        "module_hash",
        "image_object_id",
        "backend_marker",
        "codec_marker",
        "module_sha256",
    }
)


class _TargetRuntimeIdentityError(RuntimeError):
    """Carry one bounded preflight phase without retaining target error text."""

    def __init__(self, safe_phase: str) -> None:
        if safe_phase not in _TARGET_RUNTIME_IDENTITY_PHASES:
            safe_phase = "envelope"
        self.safe_phase = safe_phase
        super().__init__("target runtime identity preflight failed")
CPU = 1.0
MEMORY_MIB = 2048
BROWSER_LAUNCH_ARGS = (
    "--kiosk",
    "--window-position=0,0",
    "--window-size=1024,768",
    "--force-device-scale-factor=1",
    "--no-first-run",
    "--disable-session-crashed-bubble",
    "--disable-infobars",
)
SCHEDULE_SEED = 20260808
BOOTSTRAP_SEED = 20260808
BOOTSTRAP_RESAMPLES = 1_000
RUST_TOOLCHAIN = "rustc 1.91.0"
CONCURRENCY_LEVELS = (1, 2, 4, 8)
CONCURRENCY_TRIALS = 5
READINESS_SAMPLES = 20
MAX_OPERATIONAL_REGRESSION_PERCENT = 5.0
# The fixed campaign creates 100 fresh paired contexts, then runs the
# preregistered operational probes and 10,000-capture soak. Keep enough
# bounded wall-clock budget for the full run to finish without partial data.
PROMOTION_RUN_TIMEOUT_SECONDS = 7_200
BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLES = 10
BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLE_COUNTS = frozenset((10, 30))
SOAK_DIAGNOSTIC_CAPTURES = 10_000
# Sample VmRSS at baseline, every 100 captures, and one explicit terminal point.
SOAK_RSS_SAMPLE_INTERVAL = 100
SOAK_RSS_SAMPLE_COUNT = SOAK_DIAGNOSTIC_CAPTURES // SOAK_RSS_SAMPLE_INTERVAL + 2
SOAK_RESOURCE_METRICS = ("maps", "fd", "VmRSS", "sampled_vm_rss")
MAX_RSS_GROWTH_BYTES = 16 * 1024 * 1024
TRANSPORT_THRESHOLD_BYTES = 65_536
TRANSPORT_THRESHOLD_SWEEP_TRIALS = 30
DAEMON_LOCAL_TAIL_CAPTURES = 1_000
DAEMON_LOCAL_TAIL_WARMUPS = 2
DAEMON_LOCAL_TAIL_THRESHOLDS_MS = (50, 100, 500)
DAEMON_LOCAL_TAIL_METRICS = (
    "local_wall_ms",
    "daemon_total_ms",
    "x11_shm_capture_encode_ms",
    "hash_ms",
    "cursor_position_ms",
    "daemon_unattributed_ms",
    "local_residual_ms",
)
X11_SCHEDULING_DIAGNOSTIC_CAPTURES = 1_000
X11_SCHEDULING_DIAGNOSTIC_WARMUPS = 2
X11_SCHEDULING_TAIL_THRESHOLDS_MS = (50, 100, 500)
X11_SCHEDULING_TIMING_METRICS = (
    "local_wall_ms",
    "request_write_ms",
    "response_headers_ms",
    "body_read_ms",
    "controller_total_ms",
    "x11_shm_capture_encode_ms",
    "cursor_position_ms",
    "hash_ms",
    "controller_unattributed_ms",
    "route_ready_ms",
    "route_lock_wait_ms",
    "route_operation_ms",
    "route_total_ms",
    "route_outside_controller_residual_ms",
    "local_outside_route_residual_ms",
)
X11_SCHEDULING_CPU_METRICS = (
    "cgroup_usage_usec_delta",
    "cgroup_nr_periods_delta",
    "cgroup_nr_throttled_delta",
    "cgroup_throttled_usec_delta",
)
X11_SCHEDULING_CGROUP_FIELDS = (
    "usage_usec",
    "nr_periods",
    "nr_throttled",
    "throttled_usec",
)
X11_SCHEDULING_SCHEDSTAT_FIELDS = (
    "cpu_runtime_ns",
    "runqueue_wait_ns",
    "timeslices",
)
TRANSPORT_THRESHOLD_SWEEP_SPECS: tuple[dict[str, int | float | str], ...] = (
    {
        "case": "full-control",
        "route": "/v1/screenshots/full/raw",
        "x": 0,
        "y": 0,
        "width": WIDTH,
        "height": HEIGHT,
        "scale": 1.0,
        "expected_payload_relation": "around",
        "expected_backend": "x11-shm",
    },
    {
        "case": "region-below",
        "route": "/v1/screenshots/region/raw",
        "x": 0,
        "y": 0,
        "width": WIDTH,
        "height": 736,
        "scale": 1.0,
        "expected_payload_relation": "below",
        "expected_backend": "x11-shm",
    },
    {
        "case": "region-around",
        "route": "/v1/screenshots/region/raw",
        "x": 0,
        "y": 0,
        "width": WIDTH,
        "height": HEIGHT,
        "scale": 1.0,
        "expected_payload_relation": "around",
        "expected_backend": "x11-shm",
    },
    {
        "case": "region-above",
        "route": "/v1/screenshots/region/raw",
        "x": 0,
        "y": 0,
        "width": WIDTH,
        "height": HEIGHT,
        "scale": 1.05,
        "expected_payload_relation": "above",
        "expected_backend": "mss",
    },
)
SAFE_TIMEOUT_ORIGINS = frozenset(
    {
        "native_x11_setup_deadline",
        "native_x11_reply_deadline",
        "worker_startup_deadline",
        "worker_process_deadline",
    }
)
BENCHMARK_RUN_TAG = f"x11-shm-{uuid.uuid4().hex}"
REGION_PARITY_CASES = (
    (0, 0, 1, 1),
    (7, 9, 511, 383),
    (WIDTH - 257, HEIGHT - 193, 257, 193),
    (WIDTH - 1, HEIGHT - 1, 1, 1),
)


def _git_output(*args: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed local Git metadata command.
            ("git", *args),  # noqa: S607 - fixed executable name for local metadata.
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path)
        if (
            not child.is_file()
            or child.name.endswith(".pyc")
            or "target" in relative.parts
            or "__pycache__" in relative.parts
        ):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
    return digest.hexdigest()


def _native_source_path() -> Path:
    candidates = (
        PROJECT_ROOT / "src" / "modal_computer_use" / "_native" / "x11_shm",
        Path(modal_computer_use.__file__).resolve().parent / "_native" / "x11_shm",
        Path("/opt/modal-computer-use/native/x11_shm"),
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "Cargo.lock").is_file():
            return candidate
    raise RuntimeError("packaged X11 shared-memory Cargo source is unavailable")


_NATIVE_SOURCE_PATH = _native_source_path()


def _local_provenance() -> dict[str, str | bool]:
    """Bind the remote run to one clean local source tree before dispatch."""

    revision = _git_output("rev-parse", "HEAD")
    if revision is None or len(revision) != 40:
        raise RuntimeError("benchmark source revision is unavailable")
    if _git_output("status", "--porcelain") != "":
        raise RuntimeError("promotion benchmark requires a clean worktree")
    source_sha256 = _tree_sha256(_NATIVE_SOURCE_PATH)
    cargo_lock_sha256 = hashlib.sha256(
        (_NATIVE_SOURCE_PATH / "Cargo.lock").read_bytes()
    ).hexdigest()
    return {
        "source_revision": revision,
        "worktree_clean": True,
        "x11_shm_source_sha256": source_sha256,
        "cargo_lock_sha256": cargo_lock_sha256,
        "image_identity": "inline:browser-chromium-x11-shm",
    }


app = modal.App(APP_NAME)

# Reuse the managed browser Image recipe.  It installs Chromium, starts the
# normal Xvfb/desktop stack, and builds the packaged x11-shm extension with
# the pinned Rust toolchain and bakes the Python package for nested Sandbox
# creation. Benchmark scripts are the final runtime mount, so no build step
# follows a local startup mount.
image = (
    _named_image_recipe(variant="chromium", window_manager="xfce")
    .add_local_dir(
        str(PROJECT_ROOT / "scripts"),
        remote_path="/opt/mcu-scripts",
        copy=False,
        ignore=("__pycache__", "*.pyc"),
    )
)


class _ArmContext(AbstractAsyncContextManager[Any]):
    def __init__(self, source: str) -> None:
        if source not in {"auto", "mss", "x11-shm"}:
            raise ValueError("screenshot source must be auto, mss, or x11-shm")
        self.source = source
        self._context: AbstractAsyncContextManager[Any] | None = None
        self._computer: Any | None = None
        self.target_placement: dict[str, str | None] | None = None
        self.target_identity: dict[str, Any] | None = None
        self.fixture_verified = False
        self.startup_timing: SessionStartupTiming | None = None
        self.enter_phase = "not_started"

    async def __aenter__(self) -> Any:
        self.startup_timing = SessionStartupTiming()
        self.enter_phase = "create_sandbox"
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
                screenshot_capture_source=self.source,
            ),
            ingress="attested-tunnel",
            expose_vnc="off",
        )
        self._context = AsyncComputerSandbox.create(
            config=config,
            app_name=APP_NAME,
            image=image,
            owner=f"x11-shm-screenshot-{self.source}",
            tags={"benchmark_run": BENCHMARK_RUN_TAG},
            cpu=(CPU, CPU),
            memory=(MEMORY_MIB, MEMORY_MIB),
            timing=self.startup_timing,
        )
        try:
            self._computer = await self._context.__aenter__()
            self.enter_phase = "runtime_placement"
            self.target_placement = _normalize_placement(
                await self._computer.runtime_placement()
            )
            self.enter_phase = "runtime_identity"
            self.target_identity = await _target_runtime_identity(self._computer)
            self.enter_phase = "browser_status"
            status = await self._computer.browser.status()
            self.enter_phase = "fixture_validation"
            prewarm_result = status.get("prewarm_result")
            if (
                status.get("configured_browser") != "chromium"
                or status.get("prewarm") is not True
                or not isinstance(prewarm_result, Mapping)
                or prewarm_result.get("ok") is not True
                or status.get("open_url_on_start") != FIXTURE_DATA_URL
                or not isinstance(status.get("windows"), int)
                or status["windows"] < 1
            ):
                raise RuntimeError("managed Chromium fixture did not become ready")
            self.fixture_verified = True
            self.enter_phase = "ready"
            return self._computer
        except BaseException as exc:
            context, self._context = self._context, None
            self._computer = None
            if context is not None:
                with suppress(Exception):
                    await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._context is not None:
            await self._context.__aexit__(exc_type, exc, traceback)
        self._context = None
        self._computer = None


async def _target_runtime_identity(computer: Any) -> dict[str, Any]:
    """Observe the built extension and cgroup limits inside the target Sandbox."""

    sandbox = getattr(computer, "_sandbox", None)
    if sandbox is None or not hasattr(sandbox, "exec"):
        raise _TargetRuntimeIdentityError("sandbox_handle")
    script = dedent(
        """
        import hashlib
        import json
        import os
        import platform
        from pathlib import Path

        def read_text(path):
            return Path(path).read_text(encoding="utf-8").strip()

        def first_text(paths):
            for path in paths:
                if Path(path).is_file():
                    return read_text(path)
            raise RuntimeError(f"target cgroup limit is unavailable: {paths}")

        phase = "native_import"
        try:
            import _modal_computer_use_x11_shm as native

            phase = "cpu_limit"
            cpu_max = Path("/sys/fs/cgroup/cpu.max")
            if cpu_max.is_file():
                cpu_quota, cpu_period = read_text(cpu_max).split()
            else:
                cpu_quota = first_text((
                    "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
                    "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us",
                ))
                cpu_period = first_text((
                    "/sys/fs/cgroup/cpu/cpu.cfs_period_us",
                    "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us",
                ))
            if cpu_quota in {"max", "-1"}:
                raise RuntimeError("target CPU quota is unbounded")
            cpu = int(cpu_quota) / int(cpu_period)

            phase = "memory_limit"
            memory_limit = first_text((
                "/sys/fs/cgroup/memory.max",
                "/sys/fs/cgroup/memory/memory.limit_in_bytes",
            ))
            if memory_limit in {"max", "-1"}:
                raise RuntimeError("target memory limit is unbounded")
            memory_bytes = int(memory_limit)

            phase = "module_hash"
            module_bytes = Path(native.__file__).read_bytes()
            module_sha256 = hashlib.sha256(module_bytes).hexdigest()

            phase = "image_object_id"
            image_object_id = os.environ.get("MODAL_IMAGE_ID")
            if not isinstance(image_object_id, str) or not image_object_id.startswith("im-"):
                raise RuntimeError("target image identity is unavailable")

            phase = "backend_marker"
            backend = native.backend
            phase = "codec_marker"
            codec = native.codec
            identity = {
                "backend": backend,
                "codec": codec,
                "module_sha256": module_sha256,
                "image_object_id": image_object_id,
                "cpu": cpu,
                "memory_bytes": memory_bytes,
                "machine": platform.machine(),
            }
        except Exception as exc:
            print(json.dumps({
                "ok": False,
                "failure_phase": phase,
                "failure_type": type(exc).__name__,
            }, sort_keys=True))
        else:
            print(json.dumps({"ok": True, "identity": identity}, sort_keys=True))
        """
    )
    try:
        process = await sandbox.exec.aio("python", "-c", script, timeout=30)
    except Exception:
        raise _TargetRuntimeIdentityError("exec_launch") from None
    try:
        exit_code = await process.wait.aio()
    except Exception:
        raise _TargetRuntimeIdentityError("process_wait") from None
    try:
        raw = await _process_stdout_text(process)
    except Exception:
        raise _TargetRuntimeIdentityError("stdout_read") from None
    if exit_code != 0:
        raise _TargetRuntimeIdentityError("process_exit")
    if not raw:
        raise _TargetRuntimeIdentityError("empty_output")
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        raise _TargetRuntimeIdentityError("json_decode") from None
    if not isinstance(envelope, dict):
        raise _TargetRuntimeIdentityError("envelope")
    if envelope.get("ok") is False:
        phase = envelope.get("failure_phase")
        if not isinstance(phase, str):
            phase = "envelope"
        raise _TargetRuntimeIdentityError(phase)
    payload = envelope.get("identity")
    if envelope.get("ok") is not True or not isinstance(payload, dict):
        raise _TargetRuntimeIdentityError("envelope")
    if payload.get("backend") != "x11-shm":
        raise _TargetRuntimeIdentityError("backend_marker")
    if payload.get("codec") != "png-deflate-level1-no-filter":
        raise _TargetRuntimeIdentityError("codec_marker")
    module_sha256 = payload.get("module_sha256")
    if (
        not isinstance(module_sha256, str)
        or len(module_sha256) != 64
        or any(character not in "0123456789abcdef" for character in module_sha256)
    ):
        raise _TargetRuntimeIdentityError("module_sha256")
    if not str(payload.get("image_object_id", "")).startswith("im-"):
        raise _TargetRuntimeIdentityError("image_object_id")
    return payload


async def _process_stdout_text(process: Any) -> str:
    raw = await process.stdout.read.aio()
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    if isinstance(raw, str):
        return raw.strip()
    raise RuntimeError("sandbox process stdout has an unexpected type")


def _safe_diagnostic_label(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    if not all(character.isalnum() or character in "._-" for character in value):
        return None
    return value


def _safe_daemon_failure(exc: Exception) -> dict[str, Any]:
    """Retain only bounded daemon error attribution, never response text."""

    if not isinstance(exc, DaemonHTTPError):
        return {}
    result: dict[str, Any] = {"failure_status_code": exc.status_code}
    code = _safe_diagnostic_label(exc.code)
    if code is not None:
        result["failure_code"] = code
    details = exc.details
    if isinstance(details, Mapping):
        detail_type = _safe_diagnostic_label(details.get("type"))
        if detail_type is not None:
            result["failure_detail_type"] = detail_type
        timeout_origin = _safe_diagnostic_label(details.get("timeout_origin"))
        if timeout_origin in SAFE_TIMEOUT_ORIGINS:
            result["failure_timeout_origin"] = timeout_origin
        errors = details.get("errors")
        if isinstance(errors, list):
            categories: list[str] = []
            for error in errors:
                if not isinstance(error, str):
                    category = "unknown"
                elif "screenshot" in error.lower():
                    category = "screenshot"
                elif "input backend" in error.lower() or "xdotool" in error.lower():
                    category = "input"
                elif "window" in error.lower():
                    category = "windows"
                elif "missing required tools" in error.lower():
                    category = "tools"
                elif "xdpyinfo" in error.lower() or "xvfb" in error.lower():
                    category = "display_probe"
                elif "display lifecycle" in error.lower():
                    category = "lifecycle"
                else:
                    category = "unknown"
                if category not in categories:
                    categories.append(category)
            if categories:
                result["failure_readiness_categories"] = categories
    return result


def _is_modal_daemon_cmdline(command: bytes) -> bool:
    """Match the daemon's ``-m`` argv pair, excluding helper-script text."""

    argv = command.split(b"\0")
    return any(
        argv[index : index + 2] == [b"-m", b"modal_computer_use.daemon"]
        for index in range(len(argv) - 1)
    )


def _is_x11_shm_worker_cmdline(command: bytes) -> bool:
    """Match the fixed native worker argv, excluding helper-script text."""

    argv = command.split(b"\0")
    if argv and argv[-1] == b"":
        argv.pop()
    return bool(
        len(argv) == 10
        and argv[1].rsplit(b"/", 1)[-1] == b"_x11_shm_worker.py"
        and argv[2] == b"--fd"
        and argv[3].isdigit()
        and argv[4] == b"--display"
        and argv[5] == b":99"
        and argv[6] == b"--width"
        and argv[7] == b"1024"
        and argv[8] == b"--height"
        and argv[9] == b"768"
    )


def _select_daemon_worker_pair(
    daemon_matches: list[dict[str, int | bool | str]],
    worker_matches: list[dict[str, int | bool | str]],
) -> tuple[
    dict[str, int | bool | str] | None,
    dict[str, int | bool | str] | None,
    int,
    int,
    int,
    int,
]:
    """Select the unique exact daemon/worker pair joined by worker PPID."""

    pairs = [
        (daemon, worker)
        for daemon in daemon_matches
        for worker in worker_matches
        if worker.get("parent_pid") == daemon.get("pid")
    ]
    root_count = sum(match.get("parent_pid") == 0 for match in daemon_matches)
    if len(pairs) != 1:
        return (
            None,
            None,
            len(daemon_matches),
            len(worker_matches),
            len(pairs),
            root_count,
        )
    selected_daemon = dict(pairs[0][0])
    selected_daemon.pop("parent_pid", None)
    return (
        selected_daemon,
        dict(pairs[0][1]),
        len(daemon_matches),
        len(worker_matches),
        1,
        root_count,
    )


_DAEMON_ARGV_MATCHER_SOURCE = dedent(
    inspect.getsource(_is_modal_daemon_cmdline)
).strip()
_X11_WORKER_ARGV_MATCHER_SOURCE = dedent(
    inspect.getsource(_is_x11_shm_worker_cmdline)
).strip()
_DAEMON_WORKER_SELECTOR_SOURCE = dedent(
    inspect.getsource(_select_daemon_worker_pair)
).strip()


def _daemon_unattributed_ms(
    total_ms: float,
    fused_ms: float,
    hash_ms: float,
    cursor_position_ms: float,
) -> float:
    return max(0.0, total_ms - fused_ms - hash_ms - cursor_position_ms)


_DAEMON_UNATTRIBUTED_SOURCE = dedent(
    inspect.getsource(_daemon_unattributed_ms)
).strip()


def _validate_bounded_x_server_sample_count(sample_count: int) -> None:
    if sample_count not in BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLE_COUNTS:
        raise ValueError(
            "bounded X server diagnostic requires exactly 10 or 30 samples"
        )


def _startup_failure_phase(timing: SessionStartupTiming | None) -> str:
    """Map the last secret-free startup mark to the operation still pending."""

    if timing is None:
        return "create_sandbox"
    stages = timing.as_dict().get("stages")
    if not isinstance(stages, Mapping):
        return "create_sandbox"
    observed = [
        name
        for name, value in stages.items()
        if isinstance(name, str)
        and isinstance(value, Mapping)
        and value.get("status") == "observed"
    ]
    if not observed:
        return "create_sandbox"
    return {
        "request_received": "modal_app_lookup",
        "sandbox_create_started": "modal_sandbox_allocation",
        "sandbox_registered": "sandbox_tcp_readiness",
        "tcp_ready": "connection_parameters",
        "connection_parameters_ready": "daemon_readiness",
        "connect_ready": "tunnel_attestation",
        "attestation_ready": "attested_tunnel_readiness",
        "tunnel_ready": "context_post_ready",
    }.get(observed[-1], "create_sandbox")


async def _completed_process_stdout_text(process: Any) -> str:
    exit_code = await process.wait.aio()
    raw = await _process_stdout_text(process)
    if exit_code != 0:
        stderr_raw = await process.stderr.read.aio()
        stderr = (
            stderr_raw.decode("utf-8", errors="replace")
            if isinstance(stderr_raw, bytes)
            else str(stderr_raw)
        ).strip()
        detail = stderr[-1_000:] if stderr else "no stderr"
        raise RuntimeError(
            f"benchmark subprocess exited with status {exit_code}: {detail}"
        )
    if not raw:
        raise RuntimeError("benchmark subprocess returned empty stdout")
    return raw


async def _run_concurrency_probe(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    expected_backend: str,
    levels: tuple[int, ...] = CONCURRENCY_LEVELS,
) -> dict[str, Any]:
    """Exercise concurrent public calls; the daemon lock must preserve safety."""

    context = factory()
    computer: Any | None = None
    rows: list[dict[str, Any]] = []
    failure_type: str | None = None
    try:
        computer = await context.__aenter__()
        for level in levels:
            elapsed_samples: list[float] = []
            for _ in range(CONCURRENCY_TRIALS):
                backends: list[str | None] = []
                original = computer.client.post_bytes_with_headers

                async def traced_request(
                    *args: Any,
                    _original: Any = original,
                    _backends: list[str | None] = backends,
                    **kwargs: Any,
                ) -> Any:
                    data, headers = await _original(*args, **kwargs)
                    _backends.append(headers.get("x-computer-use-capture-backend"))
                    return data, headers

                computer.client.post_bytes_with_headers = traced_request
                started = time.perf_counter()
                try:
                    screenshots = await asyncio.gather(
                        *(computer.screenshots.full() for _ in range(level))
                    )
                finally:
                    computer.client.post_bytes_with_headers = original
                elapsed_samples.append((time.perf_counter() - started) * 1000.0)
                if any(
                    shot.width != WIDTH
                    or shot.height != HEIGHT
                    or shot.cursor_visible
                    or shot.format != "png"
                    for shot in screenshots
                ) or backends != [expected_backend] * level:
                    raise RuntimeError("concurrent screenshot contract mismatch")
            rows.append(
                {
                    "concurrency": level,
                    "trials": CONCURRENCY_TRIALS,
                    "captures_per_trial": level,
                    "elapsed_p50_ms": round(statistics.median(elapsed_samples), 4),
                    "elapsed_p95_ms": round(_percentile(elapsed_samples, 0.95), 4),
                    "capture_backend": expected_backend,
                }
            )
    except Exception as exc:
        failure_type = type(exc).__name__
    cleanup_type: str | None = None
    try:
        if computer is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                cleanup_type = type(exc).__name__
    except Exception as exc:
        cleanup_type = type(exc).__name__
    if failure_type is not None or cleanup_type is not None:
        return {
            "passed": False,
            "levels": rows,
            **({"failure_type": failure_type} if failure_type else {}),
            **({"cleanup_failure_type": cleanup_type} if cleanup_type else {}),
        }
    return {"passed": True, "source": expected_backend, "levels": rows}


async def _run_readiness_probe(
    factories: Mapping[str, Callable[[], AbstractAsyncContextManager[Any]]],
    *,
    sample_count: int = READINESS_SAMPLES,
    continue_on_failure: bool = False,
) -> dict[str, Any]:
    """Measure paired fresh create-to-ready samples for both capture sources."""

    samples: dict[str, list[float]] = {"mss": [], "x11-shm": []}
    startup_timings: dict[str, list[dict[str, Any]]] = {"mss": [], "x11-shm": []}
    rng = random.Random(SCHEDULE_SEED)  # noqa: S311 - reproducible benchmark order.
    failure_type: str | None = None
    cleanup_failure_type: str | None = None
    failure_arm: str | None = None
    failure_sample_index: int | None = None
    current_arm: str | None = None
    current_sample_index: int | None = None
    try:
        for sample_index in range(sample_count):
            current_sample_index = sample_index
            pair = ["mss", "x11-shm"]
            rng.shuffle(pair)
            for position, arm in enumerate(pair):
                current_arm = arm
                context = factories[arm]()
                computer: Any | None = None
                observation: dict[str, Any] = {
                    "sample_index": sample_index,
                    "position": position,
                    "status": "failed",
                }
                phase = "context_enter"
                started = time.perf_counter()
                try:
                    computer = await context.__aenter__()
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    samples[arm].append(elapsed_ms)
                    observation["status"] = "ok"
                    observation["startup_total_ms"] = round(elapsed_ms, 4)
                    phase = "public_capture"
                    backends: list[str | None] = []
                    original = computer.client.post_bytes_with_headers

                    async def traced_request(
                        *args: Any,
                        _original: Any = original,
                        _backends: list[str | None] = backends,
                        **kwargs: Any,
                    ) -> Any:
                        data, headers = await _original(*args, **kwargs)
                        _backends.append(headers.get("x-computer-use-capture-backend"))
                        return data, headers

                    computer.client.post_bytes_with_headers = traced_request
                    public_capture_started = time.perf_counter()
                    try:
                        await computer.screenshots.full()
                    finally:
                        observation["public_capture_elapsed_ms"] = round(
                            (time.perf_counter() - public_capture_started) * 1000.0,
                            4,
                        )
                        computer.client.post_bytes_with_headers = original
                    if backends != [arm]:
                        raise RuntimeError("fresh readiness used an unexpected source")
                except Exception as exc:
                    observation["status"] = "failed"
                    observation["failure_type"] = type(exc).__name__
                    if phase == "context_enter" and isinstance(context, _ArmContext):
                        observation["failure_phase"] = (
                            _startup_failure_phase(context.startup_timing)
                            if context.enter_phase == "create_sandbox"
                            else context.enter_phase
                        )
                    else:
                        observation["failure_phase"] = phase
                    observation.update(_safe_daemon_failure(exc))
                    if not continue_on_failure:
                        raise
                finally:
                    timing = getattr(context, "startup_timing", None)
                    if isinstance(timing, SessionStartupTiming):
                        observation["startup_timing"] = timing.as_dict()
                    try:
                        if computer is not None:
                            try:
                                await context.__aexit__(None, None, None)
                            except Exception as exc:
                                cleanup_failure_type = type(exc).__name__
                                observation["status"] = "failed"
                                observation["cleanup_failure_type"] = type(exc).__name__
                                observation["failure_phase"] = "cleanup"
                                raise
                    finally:
                        startup_timings[arm].append(observation)
    except Exception as exc:
        failure_type = type(exc).__name__
        failure_arm = current_arm
        failure_sample_index = current_sample_index

    arms = {
        arm: {
            "passed": len(values) == sample_count
            and all(
                observation.get("status") == "ok"
                for observation in startup_timings[arm]
            ),
            "source": arm,
            "samples": len(values),
            "startup_p50_ms": round(statistics.median(values), 4) if values else 0.0,
            "startup_p95_ms": round(_percentile(values, 0.95), 4) if values else 0.0,
            "capture_backend": arm,
            "startup_timings": startup_timings[arm],
        }
        for arm, values in samples.items()
    }
    failure_count = sum(
        observation.get("status") != "ok"
        for observations in startup_timings.values()
        for observation in observations
    )
    passed = (
        failure_type is None
        and cleanup_failure_type is None
        and failure_count == 0
    )
    if passed:
        passed = float(arms["x11-shm"]["startup_p95_ms"]) <= float(
            arms["mss"]["startup_p95_ms"]
        ) * (1.0 + MAX_OPERATIONAL_REGRESSION_PERCENT / 100.0)
    return {
        "passed": passed,
        "maximum_p95_regression_percent": MAX_OPERATIONAL_REGRESSION_PERCENT,
        "failure_count": failure_count,
        "arms": arms,
        **({"failure_type": failure_type} if failure_type else {}),
        **({"failure_arm": failure_arm} if failure_arm else {}),
        **(
            {"failure_sample_index": failure_sample_index}
            if failure_sample_index is not None
            else {}
        ),
        **(
            {"cleanup_failure_type": cleanup_failure_type}
            if cleanup_failure_type
            else {}
        ),
    }


async def _run_x11_shm_timeout_origin_probe(
    *,
    sample_count: int,
) -> dict[str, Any]:
    """Classify candidate setup and capture timeouts across fresh contexts."""

    observations: list[dict[str, Any]] = []
    timeout_origin_counts: dict[str, int] = {}
    for sample_index in range(sample_count):
        context = _ArmContext("x11-shm")
        computer: Any | None = None
        observation: dict[str, Any] = {
            "sample_index": sample_index,
            "status": "failed",
        }
        phase = "context_enter"
        started = time.perf_counter()
        try:
            computer = await context.__aenter__()
            observation["startup_total_ms"] = round(
                (time.perf_counter() - started) * 1000.0,
                4,
            )
            phase = "public_capture"
            backends: list[str | None] = []
            original = computer.client.post_bytes_with_headers

            async def traced_request(
                *args: Any,
                _original: Any = original,
                _backends: list[str | None] = backends,
                **kwargs: Any,
            ) -> Any:
                data, headers = await _original(*args, **kwargs)
                _backends.append(headers.get("x-computer-use-capture-backend"))
                return data, headers

            computer.client.post_bytes_with_headers = traced_request
            public_capture_started = time.perf_counter()
            try:
                await computer.screenshots.full()
            finally:
                observation["public_capture_elapsed_ms"] = round(
                    (time.perf_counter() - public_capture_started) * 1000.0,
                    4,
                )
                computer.client.post_bytes_with_headers = original
            if backends != ["x11-shm"]:
                raise RuntimeError("timeout-origin probe used an unexpected source")
            observation["status"] = "ok"
            observation["capture_backend"] = "x11-shm"
        except Exception as exc:
            observation["failure_type"] = type(exc).__name__
            if phase == "context_enter":
                observation["failure_phase"] = (
                    _startup_failure_phase(context.startup_timing)
                    if context.enter_phase == "create_sandbox"
                    else context.enter_phase
                )
            else:
                observation["failure_phase"] = phase
            observation.update(_safe_daemon_failure(exc))
            timeout_origin = observation.get("failure_timeout_origin")
            if (
                observation.get("failure_detail_type")
                == "ScreenshotCaptureTimedOut"
                and isinstance(timeout_origin, str)
            ):
                timeout_origin_counts[timeout_origin] = (
                    timeout_origin_counts.get(timeout_origin, 0) + 1
                )
        finally:
            observation["startup_timing"] = context.startup_timing.as_dict()
            if computer is not None:
                try:
                    await context.__aexit__(None, None, None)
                except Exception as exc:
                    observation["status"] = "failed"
                    observation["cleanup_failure_type"] = type(exc).__name__
                    observation["failure_phase"] = "cleanup"
            observations.append(observation)

    timeout_observations = [
        observation
        for observation in observations
        if observation.get("failure_detail_type") == "ScreenshotCaptureTimedOut"
    ]
    timeout_failures = len(timeout_observations)
    unknown_timeout_origins = sum(
        observation.get("failure_timeout_origin") not in SAFE_TIMEOUT_ORIGINS
        for observation in timeout_observations
    )
    return {
        "passed": all(observation.get("status") == "ok" for observation in observations),
        "sample_count": sample_count,
        "failure_count": sum(
            observation.get("status") != "ok" for observation in observations
        ),
        "timeout_failure_count": timeout_failures,
        "unknown_timeout_origin_count": unknown_timeout_origins,
        "timeout_origin_counts": timeout_origin_counts,
        "observations": observations,
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


async def _run_x_server_restart_probe(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
) -> dict[str, Any]:
    """Verify the display lifecycle releases and reopens the XShm session."""

    context = factory()
    computer: Any | None = None
    result: dict[str, Any] | None = None
    failure_type: str | None = None
    failure: dict[str, Any] = {}
    phase = "context_enter"
    try:
        computer = await context.__aenter__()

        async def capture_with_backend() -> tuple[Any, str | None]:
            backend: str | None = None
            original = computer.client.post_bytes_with_headers

            async def traced_request(*args: Any, **kwargs: Any) -> Any:
                nonlocal backend
                data, headers = await original(*args, **kwargs)
                backend = headers.get("x-computer-use-capture-backend")
                return data, headers

            computer.client.post_bytes_with_headers = traced_request
            try:
                screenshot = await computer.screenshots.full()
            finally:
                computer.client.post_bytes_with_headers = original
            return screenshot, backend

        phase = "capture_before_restart"
        before, backend_before = await capture_with_backend()
        phase = "lifecycle_restart"
        restarted = await computer.lifecycle.restart()
        phase = "wait_for_readiness"
        ready = False
        for _ in range(100):
            status = await computer.lifecycle.status()
            if status.ready:
                ready = True
                break
            await asyncio.sleep(0.1)
        phase = "capture_after_restart"
        after, backend_after = await capture_with_backend()
        result = {
            "passed": (
                restarted.ok
                and ready
                and before.width == after.width == WIDTH
                and before.height == after.height == HEIGHT
                and not before.cursor_visible
                and not after.cursor_visible
                and backend_before == "x11-shm"
                and backend_after == "x11-shm"
            ),
            "ready_after_restart": ready,
            "backend_before": backend_before,
            "backend_after": backend_after,
        }
    except Exception as exc:
        failure_type = type(exc).__name__
        failure = {"failure_phase": phase, **_safe_daemon_failure(exc)}
    cleanup_type: str | None = None
    if computer is not None:
        try:
            await context.__aexit__(None, None, None)
        except Exception as exc:
            cleanup_type = type(exc).__name__
    if failure_type is not None or cleanup_type is not None:
        return {
            "passed": False,
            **({"failure_type": failure_type} if failure_type else {}),
            **failure,
            **(
                {"failure_phase": "cleanup"}
                if failure_type is None and cleanup_type is not None
                else {}
            ),
            **({"cleanup_failure_type": cleanup_type} if cleanup_type else {}),
        }
    return result or {"passed": False, "failure_type": "NoResult"}


async def _run_x_server_timeout_probe(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
) -> dict[str, Any]:
    """Pause Xvfb and prove a native public call has a bounded failure policy."""

    context = factory()
    computer: Any | None = None
    failure_type: str | None = None
    failure_phase: str | None = None
    failure_status_code: int | None = None
    failure_code: str | None = None
    failure_detail_type: str | None = None
    failure_readiness_categories: list[str] | None = None
    lifecycle_restart_elapsed_ms: float | None = None
    phase = "context_enter"
    result: dict[str, Any] | None = None
    resumed = False
    try:
        computer = await context.__aenter__()
        phase = "sandbox_handle"
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for X server timeout probe")
        phase = "prime_public_capture"
        await computer.screenshots.full()
        phase = "pause_xvfb"
        stop = await sandbox.exec.aio(
            "sh",
            "-c",
            'set -eu; pid=$(pgrep -xo Xvfb); kill -STOP "$pid"; printf "%s\\n" "$pid"',
            timeout=10,
        )
        stop_exit = await stop.wait.aio()
        xvfb_pid = await _process_stdout_text(stop)
        if stop_exit != 0 or not xvfb_pid.isdigit():
            raise RuntimeError("Xvfb stop command failed")
        phase = "public_capture"
        started = time.perf_counter()
        failed_bounded = False
        public_error_type: str | None = None
        public_error_code: str | None = None
        public_error_detail_type: str | None = None
        try:
            await asyncio.wait_for(computer.screenshots.full(), timeout=10.0)
        except Exception as exc:
            public_error_type = type(exc).__name__
            public_error_code = getattr(exc, "code", None)
            details = getattr(exc, "details", None)
            if isinstance(details, Mapping):
                detail_type = details.get("type")
                if isinstance(detail_type, str):
                    public_error_detail_type = detail_type
            failed_bounded = (
                isinstance(exc, DaemonHTTPError)
                and exc.status_code == 500
                and public_error_code == "internal_error"
                and public_error_detail_type == "ScreenshotCaptureTimedOut"
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        phase = "constructor"
        constructor_probe = dedent(
            """
            import json
            import time
            from modal_computer_use.daemon.desktop.screenshot_capture import (
                ScreenshotCaptureTimedOut,
                X11SharedMemoryScreenshotSession,
            )

            started = time.perf_counter()
            failed = False
            exception_type = None
            try:
                X11SharedMemoryScreenshotSession(
                    display=":99", width=1024, height=768
                )
            except Exception as exc:
                exception_type = type(exc).__name__
                failed = isinstance(exc, ScreenshotCaptureTimedOut)
            print(json.dumps({
                "failed": failed,
                "exception_type": exception_type,
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            }))
            """
        )
        constructor_process = await sandbox.exec.aio(
            "python", "-c", constructor_probe, timeout=10
        )
        constructor_result = json.loads(
            await _completed_process_stdout_text(constructor_process)
        )
        constructor_bounded = (
            isinstance(constructor_result, dict)
            and constructor_result.get("failed") is True
            and constructor_result.get("exception_type") == "ScreenshotCaptureTimedOut"
            and float(constructor_result.get("elapsed_ms", 3_000.0)) < 2_500.0
        )
        phase = "resume_xvfb"
        resume = await sandbox.exec.aio(
            "sh", "-c", 'kill -CONT "$1"', "sh", xvfb_pid, timeout=10
        )
        resume_exit = await resume.wait.aio()
        if resume_exit != 0:
            raise RuntimeError("Xvfb resume command failed")
        resumed = True
        phase = "lifecycle_restart"
        restart_started = time.perf_counter()
        try:
            restarted = await computer.lifecycle.restart()
        finally:
            lifecycle_restart_elapsed_ms = round(
                (time.perf_counter() - restart_started) * 1000.0, 4
            )
        phase = "capture_after_restart"
        backend_after: str | None = None
        original = computer.client.post_bytes_with_headers

        async def traced_request(*args: Any, **kwargs: Any) -> Any:
            nonlocal backend_after
            data, headers = await original(*args, **kwargs)
            backend_after = headers.get("x-computer-use-capture-backend")
            return data, headers

        computer.client.post_bytes_with_headers = traced_request
        try:
            after = await computer.screenshots.full()
        finally:
            computer.client.post_bytes_with_headers = original
        result = {
            "passed": (
                failed_bounded
                and constructor_bounded
                and elapsed_ms < 2_500.0
                and restarted.ok
                and after.width == WIDTH
                and after.height == HEIGHT
                and backend_after == "x11-shm"
            ),
            "failed_bounded": failed_bounded,
            "public_error_type": public_error_type,
            "public_error_code": public_error_code,
            "public_error_detail_type": public_error_detail_type,
            "no_fallback_observed": failed_bounded,
            "constructor_bounded": constructor_bounded,
            "lifecycle_restart_elapsed_ms": lifecycle_restart_elapsed_ms,
            "constructor_error_type": constructor_result.get("exception_type"),
            "xvfb_pid": int(xvfb_pid),
            "constructor_elapsed_ms": round(
                float(constructor_result.get("elapsed_ms", 0.0)), 4
            ),
            "elapsed_ms": round(elapsed_ms, 4),
            "backend_after_restart": backend_after,
        }
    except Exception as exc:
        failure_type = type(exc).__name__
        failure_phase = phase
        safe_failure = _safe_daemon_failure(exc)
        categories = safe_failure.get("failure_readiness_categories")
        if isinstance(categories, list) and all(
            isinstance(category, str) for category in categories
        ):
            failure_readiness_categories = list(categories)
        if isinstance(exc, DaemonHTTPError):
            failure_status_code = exc.status_code
            failure_code = _safe_diagnostic_label(exc.code)
            failure_detail_type = _safe_diagnostic_label(
                exc.details.get("type") or exc.details.get("error")
            )
    finally:
        if computer is not None and not resumed:
            sandbox = getattr(computer, "_sandbox", None)
            if sandbox is not None and hasattr(sandbox, "exec"):
                with suppress(Exception):
                    resume = await sandbox.exec.aio(
                        "sh",
                        "-c",
                        "pid=$(pgrep -x Xvfb); test -z \"$pid\" || kill -CONT $pid",
                        timeout=10,
                    )
                    await resume.wait.aio()
    cleanup_type: str | None = None
    if computer is not None:
        try:
            await context.__aexit__(None, None, None)
        except Exception as exc:
            cleanup_type = type(exc).__name__
            if failure_phase is None:
                failure_phase = "cleanup"
    if failure_type is not None or cleanup_type is not None:
        return {
            "passed": False,
            **({"failure_type": failure_type} if failure_type else {}),
            **({"failure_phase": failure_phase} if failure_phase else {}),
            **(
                {"failure_status_code": failure_status_code}
                if failure_status_code is not None
                else {}
            ),
            **({"failure_code": failure_code} if failure_code else {}),
            **(
                {"failure_detail_type": failure_detail_type}
                if failure_detail_type
                else {}
            ),
            "lifecycle_restart_elapsed_ms": lifecycle_restart_elapsed_ms,
            **(
                {"failure_readiness_categories": failure_readiness_categories}
                if failure_readiness_categories
                else {}
            ),
            **({"cleanup_failure_type": cleanup_type} if cleanup_type else {}),
        }
    return result or {"passed": False, "failure_type": "NoResult"}


async def _run_region_parity_probe(
    factories: Mapping[str, Callable[[], AbstractAsyncContextManager[Any]]],
) -> dict[str, Any]:
    """Compare public regional screenshots at interior and display edges."""

    from io import BytesIO

    from PIL import Image

    arms: dict[str, list[dict[str, Any]]] = {}
    failure_type: str | None = None
    cleanup_failure_type: str | None = None
    for arm in ("mss", "x11-shm"):
        context = factories[arm]()
        computer: Any | None = None
        rows: list[dict[str, Any]] = []
        try:
            computer = await context.__aenter__()
            for x, y, width, height in REGION_PARITY_CASES:
                trace: dict[str, Any] = {}
                original_post_bytes = computer.client.post_bytes
                original_with_headers = computer.client.post_bytes_with_headers

                async def traced_post_bytes(
                    path: str,
                    *,
                    json: Any | None = None,
                    headers: dict[str, str] | None = None,
                    _mutation: bool = False,
                    _original=original_with_headers,
                    _trace=trace,
                ) -> bytes:
                    data, response_headers = await _original(
                        path,
                        json=json,
                        headers=headers,
                        _mutation=_mutation,
                    )
                    _trace.update(
                        {
                            str(key).lower(): value
                            for key, value in response_headers.items()
                        }
                    )
                    return data

                computer.client.post_bytes = traced_post_bytes
                try:
                    data = await computer.screenshots.region_bytes(x, y, width, height)
                finally:
                    computer.client.post_bytes = original_post_bytes
                backend = trace.get("x-computer-use-capture-backend")
                if backend != arm:
                    raise RuntimeError("regional screenshot used an unexpected source")
                with Image.open(BytesIO(data)) as image:
                    image.load()
                    if image.size != (width, height):
                        raise RuntimeError("regional screenshot PNG dimensions changed")
                    pixels_sha256 = hashlib.sha256(
                        image.convert("RGB").tobytes()
                    ).hexdigest()
                coordinate_space = json.loads(
                    str(trace.get("x-computer-use-coordinate-space", "null"))
                )
                cursor_position = json.loads(
                    str(trace.get("x-computer-use-cursor-position", "null"))
                )
                rows.append(
                    {
                        "region": {"x": x, "y": y, "width": width, "height": height},
                        "pixels_sha256": pixels_sha256,
                        "width": int(trace.get("x-computer-use-width", -1)),
                        "height": int(trace.get("x-computer-use-height", -1)),
                        "cursor_visible": trace.get("x-computer-use-cursor-visible") == "true",
                        "cursor_position_is_null": cursor_position is None,
                        "coordinate_space": coordinate_space,
                        "capture_backend": backend,
                    }
                )
        except Exception as exc:
            failure_type = type(exc).__name__
        finally:
            if computer is not None:
                try:
                    await context.__aexit__(None, None, None)
                except Exception as exc:
                    cleanup_failure_type = type(exc).__name__
        arms[arm] = rows
        if failure_type is not None or cleanup_failure_type is not None:
            break
    def comparable(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in row.items() if key != "capture_backend"}
            for row in (rows or [])
        ]

    parity = comparable(arms.get("mss")) == comparable(arms.get("x11-shm"))
    return {
        "passed": (
            failure_type is None
            and cleanup_failure_type is None
            and parity
            and len(arms.get("mss", ())) == len(REGION_PARITY_CASES)
        ),
        "case_count": len(REGION_PARITY_CASES),
        "decoded_pixel_and_metadata_parity": parity,
        "arms": arms,
        **({"failure_type": failure_type} if failure_type else {}),
        **(
            {"cleanup_failure_type": cleanup_failure_type}
            if cleanup_failure_type
            else {}
        ),
    }


async def _final_sandbox_cleanup() -> dict[str, Any]:
    """Observe and terminate any live Sandbox carrying this run's unique tag."""

    async def live_sandboxes() -> list[Any]:
        live: list[Any] = []
        app_id = app.app_id
        if not isinstance(app_id, str) or not app_id.startswith("ap-"):
            raise RuntimeError("benchmark Modal App identity is unavailable")
        async for sandbox in modal.Sandbox.list.aio(
            app_id=app_id,
            tags={"benchmark_run": BENCHMARK_RUN_TAG},
        ):
            if await sandbox.poll.aio() is None:
                live.append(sandbox)
        return live

    cleanup_error_types: list[str] = []
    survivors = await live_sandboxes()
    for sandbox in survivors:
        try:
            await sandbox.terminate.aio(wait=True)
        except Exception as exc:
            cleanup_error_types.append(type(exc).__name__)
    remaining = await live_sandboxes()
    return {
        "succeeded": not survivors and not cleanup_error_types and not remaining,
        "survivors_before_sweep": len(survivors),
        "remaining_sandboxes": len(remaining),
        "cleanup_error_types": cleanup_error_types,
    }


async def _run_x11_shm_failure_matrix(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
) -> dict[str, Any]:
    """Exercise the real extension and controller fallback boundaries in-image."""

    context = factory()
    computer: Any | None = None
    result: dict[str, Any] | None = None
    failure_type: str | None = None
    try:
        computer = await context.__aenter__()
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for failure matrix")
        script = dedent(
            """
            import asyncio
            import base64
            import json
            import os
            from types import SimpleNamespace

            import _modal_computer_use_x11_shm as x11_shm
            from modal_computer_use.daemon.desktop import screenshot_capture
            from modal_computer_use.daemon.desktop.screenshots import X11ScreenshotController
            from modal_computer_use.models import Point, ScreenshotOptions

            checks = {}
            session = x11_shm.X11SharedMemoryScreenshotSession(
                os.environ.get("DISPLAY", ":99"), 1024, 768
            )
            session.close()
            session.close()
            closed = False
            try:
                session.capture_png(0, 0, 1024, 768)
            except Exception:
                closed = True
            checks["close_idempotent"] = True
            checks["closed_capture_rejected"] = closed

            try:
                x11_shm.X11SharedMemoryScreenshotSession(
                    os.environ.get("DISPLAY", ":99"), 1, 1
                )
            except Exception:
                checks["constructor_geometry_failure"] = True
            else:
                checks["constructor_geometry_failure"] = False

            real = x11_shm.X11SharedMemoryScreenshotSession(
                os.environ.get("DISPLAY", ":99"), 1024, 768
            )
            try:
                try:
                    real.capture_png(1024, 0, 1, 1)
                except Exception:
                    checks["invalid_region_rejected"] = True
                else:
                    checks["invalid_region_rejected"] = False
            finally:
                real.close()

            class AttachFailure:
                calls = 0
                def __init__(self, *_args):
                    AttachFailure.calls += 1
                    raise RuntimeError("AttachFd failed")

            class EncodeFailure:
                calls = 0
                def __init__(self, *_args):
                    EncodeFailure.calls += 1
                def capture_png(self, *_args):
                    raise RuntimeError("PNG encode failed")
                def close(self):
                    pass

            class InvalidResult:
                calls = 0
                def __init__(self, *_args):
                    InvalidResult.calls += 1
                def capture_png(self, *_args):
                    return b"\\x89PNG\\r\\n\\x1a\\ninvalid"
                def close(self):
                    pass

            async def cursor_position():
                return Point(x=0, y=0)

            async def unexpected_file_capture(*_args, **_kwargs):
                raise RuntimeError("MSS fallback unexpectedly failed")

            async def fallback_check(constructor):
                screenshot_capture._module = SimpleNamespace(
                    X11SharedMemoryScreenshotSession=constructor
                )
                screenshot_capture._module_checked = True
                controller = X11ScreenshotController(
                    run=unexpected_file_capture,
                    width=1024,
                    height=768,
                    display=os.environ.get("DISPLAY", ":99"),
                    cursor_position=cursor_position,
                    capture_source="auto",
                )
                try:
                    options = ScreenshotOptions(format="png", show_cursor=False)
                    first = await controller.capture_bytes(
                        options, prefer_native_png=True
                    )
                    second = await controller.capture_bytes(
                        options, prefer_native_png=True
                    )
                    return (
                        first.capture_backend == "mss-fallback"
                        and second.capture_backend == "mss-fallback"
                        and constructor.calls == 1
                    )
                finally:
                    controller.close()

            checks["attach_failure_falls_back_once"] = asyncio.run(
                fallback_check(AttachFailure)
            )
            checks["encode_failure_falls_back_once"] = asyncio.run(
                fallback_check(EncodeFailure)
            )
            checks["invalid_result_falls_back_once"] = asyncio.run(
                fallback_check(InvalidResult)
            )

            screenshot_capture._module = None
            screenshot_capture._module_checked = True
            checks["extension_load_failure_selects_mss"] = (
                screenshot_capture.resolve_capture_source("auto").selected == "mss"
            )

            valid_one_pixel_png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nGQAAAAASUVORK5CYII="
            )
            class CloseFailure:
                def __init__(self, *_args):
                    pass
                def capture_png(self, *_args):
                    return valid_one_pixel_png
                def close(self):
                    raise RuntimeError("detach failed")

            screenshot_capture._module = SimpleNamespace(
                X11SharedMemoryScreenshotSession=CloseFailure
            )
            screenshot_capture._module_checked = True
            wrapper = screenshot_capture.X11SharedMemoryScreenshotSession(
                display=":99", width=1, height=1
            )
            try:
                wrapper.close()
            except Exception:
                checks["close_failure_reported"] = True
            else:
                checks["close_failure_reported"] = False

            print(json.dumps(checks))
            """
        )
        process = await sandbox.exec.aio("python", "-c", script, timeout=60)
        raw = await _completed_process_stdout_text(process)
        payload = json.loads(raw)
        required = {
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
        passed = (
            isinstance(payload, dict)
            and set(payload) == required
            and all(payload.values())
        )
        result = {"passed": passed, "checks": payload if isinstance(payload, dict) else {}}
    except Exception as exc:
        failure_type = type(exc).__name__
    cleanup_type: str | None = None
    try:
        if computer is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                cleanup_type = type(exc).__name__
    except Exception as exc:
        cleanup_type = type(exc).__name__
    if failure_type is not None or cleanup_type is not None:
        return {
            "passed": False,
            "checks": {},
            **({"failure_type": failure_type} if failure_type else {}),
            **({"cleanup_failure_type": cleanup_type} if cleanup_type else {}),
        }
    if result is None:
        return {"passed": False, "checks": {}, "failure_type": "NoResult"}
    return result


async def _run_x11_shm_soak(
    factory: Callable[[], AbstractAsyncContextManager[Any]], *, captures: int
) -> dict[str, Any]:
    """Run 10k daemon-local full/region requests and sample daemon resources."""

    if captures != 10_000:
        raise ValueError("the promotion soak is fixed at 10000 captures")
    context = factory()
    computer: Any | None = None
    result: dict[str, Any] | None = None
    failure_type: str | None = None
    try:
        computer = await context.__aenter__()
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for X11 shared-memory soak")
        script = _build_x11_shm_soak_diagnostic_script(captures)
        process = await sandbox.exec.aio("python", "-c", script, timeout=900)
        raw = await _completed_process_stdout_text(process)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("X11 shared-memory soak returned invalid output")
        signed_deltas = payload.get("signed_deltas")
        if not isinstance(signed_deltas, Mapping):
            raise RuntimeError("X11 shared-memory soak returned invalid resource deltas")
        fd_delta = int(signed_deltas["fd"])
        mapping_delta = int(signed_deltas["mappings"])
        current_rss = int(payload["rss_current_bytes"])
        before_rss = int(payload["rss_before_bytes"])
        rss_growth = current_rss - before_rss
        peak_growth = int(payload["rss_peak_growth_bytes"])
        rss_sample_count = int(payload["rss_sample_count"])
        result = {
            "passed": (
                payload.get("passed") is True
                and int(payload["captures_requested"]) == captures
                and int(payload["full_captures"]) == captures // 2
                and int(payload["region_captures"]) == captures // 2
                and int(payload["prime_captures"]) == 2
                and fd_delta == 0
                and mapping_delta == 0
                and payload.get("rss_metric_source") == "sampled_vm_rss"
                and rss_sample_count == SOAK_RSS_SAMPLE_COUNT
                and payload.get("final_included") is True
                and rss_growth <= 16 * 1024 * 1024
                and peak_growth <= 16 * 1024 * 1024
            ),
            "captures": captures,
            "full_captures": int(payload["full_captures"]),
            "region_captures": int(payload["region_captures"]),
            "fd_delta": fd_delta,
            "mapping_delta": mapping_delta,
            "rss_growth_bytes": max(0, rss_growth),
            "peak_rss_growth_bytes": max(0, peak_growth),
            "rss_metric_source": payload.get("rss_metric_source"),
            "rss_sample_count": rss_sample_count,
            "rss_before_bytes": before_rss,
            "rss_current_bytes": current_rss,
            "rss_final_bytes": int(payload["rss_final_bytes"]),
            "rss_observed_peak_bytes": int(payload["rss_observed_peak_bytes"]),
            "rss_peak_growth_bytes": max(0, peak_growth),
            "final_included": payload.get("final_included") is True,
        }
    except Exception as exc:
        failure_type = type(exc).__name__
    cleanup_type: str | None = None
    if computer is not None:
        try:
            await context.__aexit__(None, None, None)
        except Exception as exc:
            cleanup_type = type(exc).__name__
    if failure_type is not None or cleanup_type is not None:
        return {
            "passed": False,
            "captures": 0,
            **({"failure_type": failure_type} if failure_type else {}),
            **({"cleanup_failure_type": cleanup_type} if cleanup_type else {}),
        }
    if result is None:
        return {"passed": False, "captures": 0, "failure_type": "NoResult"}
    return result


def _retain_soak_counts(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    retained: dict[str, int] = {}
    for key in ("fd", "mappings", "rss"):
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            retained[key] = candidate
    return retained if len(retained) == 3 else None


def _retain_resource_metric_availability(value: object) -> dict[str, bool] | None:
    if not isinstance(value, Mapping):
        return None
    retained: dict[str, bool] = {}
    for key in SOAK_RESOURCE_METRICS:
        candidate = value.get(key)
        if isinstance(candidate, bool):
            retained[key] = candidate
    return retained if retained else None


def _retain_failure_resource_metric(value: object) -> str | None:
    return value if isinstance(value, str) and value in SOAK_RESOURCE_METRICS else None


def _retain_resource_bytes(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _retain_daemon_identity(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    pid = value.get("pid")
    starttime_ticks = value.get("starttime_ticks")
    argv_match = value.get("argv_match")
    argv_module = value.get("argv_module")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(starttime_ticks, int)
        or isinstance(starttime_ticks, bool)
        or not isinstance(argv_match, bool)
        or argv_module != "modal_computer_use.daemon"
    ):
        return None
    return {
        "pid": pid,
        "starttime_ticks": starttime_ticks,
        "argv_match": argv_match,
        "argv_module": argv_module,
    }


def _build_x11_shm_soak_diagnostic(
    observation: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, str | bool],
) -> dict[str, Any]:
    before_identity = _retain_daemon_identity(
        observation.get("daemon_identity_before")
    )
    after_identity = _retain_daemon_identity(observation.get("daemon_identity_after"))
    counts_before = _retain_soak_counts(observation.get("counts_before"))
    counts_after = _retain_soak_counts(observation.get("counts_after"))
    signed_deltas = _retain_soak_counts(observation.get("signed_deltas"))
    resource_metrics_before = _retain_resource_metric_availability(
        observation.get("resource_metrics_before")
    )
    resource_metrics_after = _retain_resource_metric_availability(
        observation.get("resource_metrics_after")
    )
    failure_resource_metric = _retain_failure_resource_metric(
        observation.get("failure_resource_metric")
    )
    rss_metric_source = (
        "sampled_vm_rss"
        if observation.get("rss_metric_source") == "sampled_vm_rss"
        else None
    )
    rss_sample_count = _retain_resource_bytes(observation.get("rss_sample_count"))
    rss_before_bytes = _retain_resource_bytes(observation.get("rss_before_bytes"))
    rss_current_bytes = _retain_resource_bytes(observation.get("rss_current_bytes"))
    rss_final_bytes = _retain_resource_bytes(observation.get("rss_final_bytes"))
    rss_observed_peak_bytes = _retain_resource_bytes(
        observation.get("rss_observed_peak_bytes")
    )
    rss_peak_growth_bytes = _retain_resource_bytes(
        observation.get("rss_peak_growth_bytes")
    )
    final_included = observation.get("final_included") is True
    rss_contract = (
        rss_metric_source == "sampled_vm_rss"
        and rss_sample_count == SOAK_RSS_SAMPLE_COUNT
        and rss_before_bytes is not None
        and rss_current_bytes is not None
        and rss_final_bytes is not None
        and rss_observed_peak_bytes is not None
        and rss_peak_growth_bytes is not None
        and final_included
    )
    expected_signed_deltas = None
    if counts_before is not None and counts_after is not None:
        expected_signed_deltas = {
            key: counts_after[key] - counts_before[key]
            for key in ("fd", "mappings", "rss")
        }
    signed_deltas_consistent = (
        expected_signed_deltas is not None
        and signed_deltas == expected_signed_deltas
    )
    identity_same = bool(
        before_identity
        and after_identity
        and before_identity["pid"] == after_identity["pid"]
        and before_identity["starttime_ticks"] == after_identity["starttime_ticks"]
        and before_identity["argv_match"]
        and after_identity["argv_match"]
    )
    resource_delta_zero = bool(signed_deltas) and all(
        signed_deltas.get(key) == 0 for key in ("fd", "mappings")
    )
    metrics_complete = (
        resource_metrics_before == {key: True for key in SOAK_RESOURCE_METRICS}
        and resource_metrics_after == {key: True for key in SOAK_RESOURCE_METRICS}
    )
    rss_growth_ok = (
        rss_current_bytes is not None
        and rss_before_bytes is not None
        and rss_current_bytes - rss_before_bytes <= MAX_RSS_GROWTH_BYTES
        and rss_peak_growth_bytes is not None
        and rss_peak_growth_bytes <= MAX_RSS_GROWTH_BYTES
    )
    captures_completed = observation.get("captures_completed")
    full_captures = observation.get("full_captures")
    region_captures = observation.get("region_captures")
    captures_shape = (
        captures_completed == SOAK_DIAGNOSTIC_CAPTURES
        and full_captures == SOAK_DIAGNOSTIC_CAPTURES // 2
        and region_captures == SOAK_DIAGNOSTIC_CAPTURES // 2
    )
    observed_backend = _safe_diagnostic_label(observation.get("observed_backend"))
    cleanup_succeeded = cleanup.get("succeeded") is True and cleanup.get(
        "remaining_sandboxes"
    ) == 0
    failure_type = _safe_diagnostic_label(observation.get("failure_type"))
    failure_phase = _safe_diagnostic_label(observation.get("failure_phase"))
    passed = bool(
        observation.get("passed") is True
        and observation.get("requested_source") == "auto"
        and observed_backend == "x11-shm"
        and captures_shape
        and identity_same
        and resource_delta_zero
        and signed_deltas_consistent
        and metrics_complete
        and rss_contract
        and rss_growth_ok
        and cleanup_succeeded
    )
    return {
        "schema_version": "x11-shm-soak-diagnostic.v1",
        "benchmark": "x11-shm-soak-diagnostic",
        "status": "complete",
        "passed": passed,
        "sample_count": SOAK_DIAGNOSTIC_CAPTURES,
        "captures_requested": SOAK_DIAGNOSTIC_CAPTURES,
        "captures_completed": captures_completed,
        "full_captures": full_captures,
        "region_captures": region_captures,
        "requested_source": "auto",
        "observed_backend": observed_backend,
        "daemon_identity_before": before_identity,
        "daemon_identity_after": after_identity,
        "daemon_identity_same": identity_same,
        "counts_before": counts_before,
        "counts_after": counts_after,
        "signed_deltas": signed_deltas,
        "resource_metrics_before": resource_metrics_before,
        "resource_metrics_after": resource_metrics_after,
        "failure_resource_metric": failure_resource_metric,
        "rss_metric_source": rss_metric_source,
        "rss_sample_count": rss_sample_count,
        "rss_before_bytes": rss_before_bytes,
        "rss_current_bytes": rss_current_bytes,
        "rss_final_bytes": rss_final_bytes,
        "rss_observed_peak_bytes": rss_observed_peak_bytes,
        "rss_peak_growth_bytes": rss_peak_growth_bytes,
        "final_included": final_included,
        "resource_delta_zero": resource_delta_zero,
        "failure_type": failure_type,
        "failure_phase": failure_phase,
        "retries": 0,
        "replacement_samples": 0,
        "provenance": dict(provenance),
        "terminal_cleanup": dict(cleanup),
    }


def _build_x11_shm_resource_snapshot_diagnostic(
    observation: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, str | bool],
) -> dict[str, Any]:
    before_identity = _retain_daemon_identity(
        observation.get("daemon_identity_before")
    )
    counts_before = _retain_soak_counts(observation.get("counts_before"))
    resource_metrics_before = _retain_resource_metric_availability(
        observation.get("resource_metrics_before")
    )
    failure_resource_metric = _retain_failure_resource_metric(
        observation.get("failure_resource_metric")
    )
    rss_metric_source = (
        "sampled_vm_rss"
        if observation.get("rss_metric_source") == "sampled_vm_rss"
        else None
    )
    rss_sample_count = _retain_resource_bytes(observation.get("rss_sample_count"))
    rss_before_bytes = _retain_resource_bytes(observation.get("rss_before_bytes"))
    rss_current_bytes = _retain_resource_bytes(observation.get("rss_current_bytes"))
    rss_final_bytes = _retain_resource_bytes(observation.get("rss_final_bytes"))
    rss_observed_peak_bytes = _retain_resource_bytes(
        observation.get("rss_observed_peak_bytes")
    )
    rss_peak_growth_bytes = _retain_resource_bytes(
        observation.get("rss_peak_growth_bytes")
    )
    final_included = observation.get("final_included") is True
    observed_backend = _safe_diagnostic_label(observation.get("observed_backend"))
    prime_captures = observation.get("prime_captures")
    if not isinstance(prime_captures, int) or isinstance(prime_captures, bool):
        prime_captures = 0
    cleanup_succeeded = cleanup.get("succeeded") is True and cleanup.get(
        "remaining_sandboxes"
    ) == 0
    metrics_complete = resource_metrics_before == {
        key: True for key in SOAK_RESOURCE_METRICS
    }
    rss_contract = (
        rss_metric_source == "sampled_vm_rss"
        and rss_sample_count == 1
        and rss_before_bytes is not None
        and rss_current_bytes is None
        and rss_final_bytes is None
        and rss_observed_peak_bytes == rss_before_bytes
        and rss_peak_growth_bytes == 0
        and not final_included
    )
    passed = bool(
        observation.get("passed") is True
        and observation.get("requested_source") == "auto"
        and observed_backend == "x11-shm"
        and prime_captures == 2
        and before_identity is not None
        and counts_before is not None
        and metrics_complete
        and rss_contract
        and cleanup_succeeded
    )
    return {
        "schema_version": "x11-shm-resource-snapshot.v1",
        "benchmark": "x11-shm-resource-snapshot",
        "status": "complete",
        "passed": passed,
        "prime_captures": prime_captures,
        "requested_source": "auto",
        "observed_backend": observed_backend,
        "daemon_identity_before": before_identity,
        "counts_before": counts_before,
        "resource_metrics_before": resource_metrics_before,
        "failure_resource_metric": failure_resource_metric,
        "rss_metric_source": rss_metric_source,
        "rss_sample_count": rss_sample_count,
        "rss_before_bytes": rss_before_bytes,
        "rss_current_bytes": rss_current_bytes,
        "rss_final_bytes": rss_final_bytes,
        "rss_observed_peak_bytes": rss_observed_peak_bytes,
        "rss_peak_growth_bytes": rss_peak_growth_bytes,
        "final_included": final_included,
        "failure_type": _safe_diagnostic_label(observation.get("failure_type")),
        "failure_phase": _safe_diagnostic_label(observation.get("failure_phase")),
        "retries": 0,
        "replacement_samples": 0,
        "provenance": dict(provenance),
        "terminal_cleanup": dict(cleanup),
    }


def _daemon_local_nonnegative_float(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("daemon-local timing summary is invalid")
    return float(value)


def _retain_daemon_local_tail_summaries(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"combined", "full", "region"}:
        raise ValueError("daemon-local summaries have invalid lanes")
    expected_counts = {
        "combined": DAEMON_LOCAL_TAIL_CAPTURES,
        "full": DAEMON_LOCAL_TAIL_CAPTURES // 2,
        "region": DAEMON_LOCAL_TAIL_CAPTURES // 2,
    }
    retained: dict[str, Any] = {}
    expected_summary_keys = {
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
        *(f"over_{threshold}_count" for threshold in DAEMON_LOCAL_TAIL_THRESHOLDS_MS),
    }
    for lane, expected_count in expected_counts.items():
        lane_value = value.get(lane)
        if (
            not isinstance(lane_value, Mapping)
            or set(lane_value) != {"sample_count", "metrics"}
            or lane_value.get("sample_count") != expected_count
        ):
            raise ValueError("daemon-local summary sample count is invalid")
        metrics = lane_value.get("metrics")
        if not isinstance(metrics, Mapping) or set(metrics) != set(
            DAEMON_LOCAL_TAIL_METRICS
        ):
            raise ValueError("daemon-local summary metrics are invalid")
        retained_metrics: dict[str, Any] = {}
        for metric in DAEMON_LOCAL_TAIL_METRICS:
            summary = metrics.get(metric)
            if not isinstance(summary, Mapping) or set(summary) != expected_summary_keys:
                raise ValueError("daemon-local metric summary is invalid")
            p50 = _daemon_local_nonnegative_float(summary.get("p50_ms"))
            p95 = _daemon_local_nonnegative_float(summary.get("p95_ms"))
            p99 = _daemon_local_nonnegative_float(summary.get("p99_ms"))
            maximum = _daemon_local_nonnegative_float(summary.get("max_ms"))
            if p50 > p95 or p95 > p99 or p99 > maximum:
                raise ValueError("daemon-local timing percentiles are invalid")
            tail_counts: list[int] = []
            for threshold in DAEMON_LOCAL_TAIL_THRESHOLDS_MS:
                count = summary.get(f"over_{threshold}_count")
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 0
                    or count > expected_count
                ):
                    raise ValueError("daemon-local tail count is invalid")
                tail_counts.append(count)
            if tail_counts != sorted(tail_counts, reverse=True):
                raise ValueError("daemon-local tail counts are not monotonic")
            retained_metrics[metric] = {
                "p50_ms": p50,
                "p95_ms": p95,
                "p99_ms": p99,
                "max_ms": maximum,
                **{
                    f"over_{threshold}_count": count
                    for threshold, count in zip(
                        DAEMON_LOCAL_TAIL_THRESHOLDS_MS,
                        tail_counts,
                        strict=True,
                    )
                },
            }
        retained[lane] = {
            "sample_count": expected_count,
            "metrics": retained_metrics,
        }
    for metric in DAEMON_LOCAL_TAIL_METRICS:
        combined = retained["combined"]["metrics"][metric]
        full = retained["full"]["metrics"][metric]
        region = retained["region"]["metrics"][metric]
        if combined["max_ms"] != max(full["max_ms"], region["max_ms"]):
            raise ValueError("daemon-local lane maxima are inconsistent")
        for threshold in DAEMON_LOCAL_TAIL_THRESHOLDS_MS:
            key = f"over_{threshold}_count"
            if combined[key] != full[key] + region[key]:
                raise ValueError("daemon-local lane tail counts are inconsistent")
    return retained


def _retain_daemon_local_tail_schedule(
    value: object,
    summaries: Mapping[str, Any],
) -> dict[str, list[dict[str, int | float]]]:
    if not isinstance(value, Mapping) or set(value) != set(DAEMON_LOCAL_TAIL_METRICS):
        raise ValueError("daemon-local tail schedule metrics are invalid")
    retained: dict[str, list[dict[str, int | float]]] = {}
    for metric in DAEMON_LOCAL_TAIL_METRICS:
        entries = value.get(metric)
        if not isinstance(entries, list):
            raise ValueError("daemon-local tail schedule is invalid")
        retained_entries: list[dict[str, int | float]] = []
        previous_index = -1
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {
                "schedule_index",
                "timing_ms",
            }:
                raise ValueError("daemon-local tail entry is invalid")
            schedule_index = entry.get("schedule_index")
            if (
                not isinstance(schedule_index, int)
                or isinstance(schedule_index, bool)
                or schedule_index <= previous_index
                or schedule_index >= DAEMON_LOCAL_TAIL_CAPTURES
            ):
                raise ValueError("daemon-local tail schedule index is invalid")
            timing_ms = _daemon_local_nonnegative_float(entry.get("timing_ms"))
            if timing_ms <= DAEMON_LOCAL_TAIL_THRESHOLDS_MS[0]:
                raise ValueError("daemon-local tail entry does not cross the floor")
            retained_entries.append(
                {"schedule_index": schedule_index, "timing_ms": timing_ms}
            )
            previous_index = schedule_index
        combined = summaries["combined"]["metrics"][metric]
        full = summaries["full"]["metrics"][metric]
        region = summaries["region"]["metrics"][metric]
        for threshold in DAEMON_LOCAL_TAIL_THRESHOLDS_MS:
            key = f"over_{threshold}_count"
            threshold_entries = [
                entry for entry in retained_entries if entry["timing_ms"] > threshold
            ]
            if (
                len(threshold_entries) != combined[key]
                or sum(
                    entry["schedule_index"] % 2 == 0
                    for entry in threshold_entries
                )
                != full[key]
                or sum(
                    entry["schedule_index"] % 2 == 1
                    for entry in threshold_entries
                )
                != region[key]
            ):
                raise ValueError("daemon-local tail schedule disagrees with summaries")
        if retained_entries:
            if max(entry["timing_ms"] for entry in retained_entries) != combined[
                "max_ms"
            ]:
                raise ValueError("daemon-local tail maximum disagrees with summary")
        elif combined["max_ms"] > DAEMON_LOCAL_TAIL_THRESHOLDS_MS[0]:
            raise ValueError("daemon-local tail maximum is missing from schedule")
        retained[metric] = retained_entries
    return retained


def _retain_daemon_local_count(value: object, *, maximum: int) -> int | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > maximum
    ):
        return None
    return value


def _retain_daemon_local_provenance(
    value: Mapping[str, str | bool],
) -> dict[str, str | bool] | None:
    source_revision = value.get("source_revision")
    x11_sha = value.get("x11_shm_source_sha256")
    cargo_sha = value.get("cargo_lock_sha256")
    if (
        not isinstance(source_revision, str)
        or len(source_revision) != 40
        or any(character not in "0123456789abcdef" for character in source_revision)
        or value.get("worktree_clean") is not True
        or not isinstance(x11_sha, str)
        or len(x11_sha) != 64
        or any(character not in "0123456789abcdef" for character in x11_sha)
        or not isinstance(cargo_sha, str)
        or len(cargo_sha) != 64
        or any(character not in "0123456789abcdef" for character in cargo_sha)
        or value.get("image_identity") != "inline:browser-chromium-x11-shm"
    ):
        return None
    return {
        "source_revision": source_revision,
        "worktree_clean": True,
        "x11_shm_source_sha256": x11_sha,
        "cargo_lock_sha256": cargo_sha,
        "image_identity": "inline:browser-chromium-x11-shm",
    }


def _build_x11_shm_daemon_local_tail_diagnostic(
    observation: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, str | bool],
) -> dict[str, Any]:
    before_identity = _retain_daemon_identity(
        observation.get("daemon_identity_before")
    )
    after_identity = _retain_daemon_identity(observation.get("daemon_identity_after"))
    identity_same = bool(
        before_identity
        and after_identity
        and before_identity["pid"] == after_identity["pid"]
        and before_identity["starttime_ticks"] == after_identity["starttime_ticks"]
        and before_identity["argv_match"]
        and after_identity["argv_match"]
    )
    validation_failed = False
    summaries: dict[str, Any] | None = None
    tail_schedule: dict[str, list[dict[str, int | float]]] = {}
    if observation.get("passed") is True:
        try:
            summaries = _retain_daemon_local_tail_summaries(
                observation.get("summaries")
            )
            tail_schedule = _retain_daemon_local_tail_schedule(
                observation.get("tail_schedule"),
                summaries,
            )
        except (TypeError, ValueError, OverflowError):
            validation_failed = True
    cleanup_succeeded = cleanup.get("succeeded") is True and cleanup.get(
        "remaining_sandboxes"
    ) == 0
    captures_completed = _retain_daemon_local_count(
        observation.get("captures_completed"),
        maximum=DAEMON_LOCAL_TAIL_CAPTURES,
    )
    full_captures = _retain_daemon_local_count(
        observation.get("full_captures"),
        maximum=DAEMON_LOCAL_TAIL_CAPTURES // 2,
    )
    region_captures = _retain_daemon_local_count(
        observation.get("region_captures"),
        maximum=DAEMON_LOCAL_TAIL_CAPTURES // 2,
    )
    warmups_completed = _retain_daemon_local_count(
        observation.get("warmups_completed"),
        maximum=DAEMON_LOCAL_TAIL_WARMUPS,
    )
    observed_backend = _safe_diagnostic_label(observation.get("observed_backend"))
    retained_provenance = _retain_daemon_local_provenance(provenance)
    contract_matches = bool(
        observation.get("requested_source") == "x11-shm"
        and observed_backend == "x11-shm"
        and observation.get("warmups_requested") == DAEMON_LOCAL_TAIL_WARMUPS
        and warmups_completed == DAEMON_LOCAL_TAIL_WARMUPS
        and observation.get("captures_requested") == DAEMON_LOCAL_TAIL_CAPTURES
        and captures_completed == DAEMON_LOCAL_TAIL_CAPTURES
        and full_captures == DAEMON_LOCAL_TAIL_CAPTURES // 2
        and region_captures == DAEMON_LOCAL_TAIL_CAPTURES // 2
        and identity_same
        and summaries is not None
        and retained_provenance is not None
    )
    if observation.get("passed") is True and not contract_matches:
        validation_failed = True
    passed = bool(
        observation.get("passed") is True
        and not validation_failed
        and contract_matches
        and cleanup_succeeded
    )
    failure_type = (
        "EvidenceValidationError"
        if validation_failed
        else "CleanupError"
        if observation.get("passed") is True and not cleanup_succeeded
        else _safe_diagnostic_label(observation.get("failure_type"))
    )
    failure_phase = (
        "artifact_validation"
        if validation_failed
        else "terminal_cleanup"
        if observation.get("passed") is True and not cleanup_succeeded
        else _safe_diagnostic_label(observation.get("failure_phase"))
    )
    return {
        "schema_version": "x11-shm-daemon-local-tail.v1",
        "benchmark": "x11-shm-daemon-local-tail",
        "status": "complete",
        "passed": passed,
        "scope": "daemon-local-tail-mechanism-only",
        "non_gating": True,
        "promotion_proxy": False,
        "requested_source": "x11-shm",
        "observed_backend": observed_backend,
        "warmups_requested": DAEMON_LOCAL_TAIL_WARMUPS,
        "warmups_completed": warmups_completed,
        "expected_sample_count": DAEMON_LOCAL_TAIL_CAPTURES,
        "sample_count": captures_completed,
        "captures_requested": DAEMON_LOCAL_TAIL_CAPTURES,
        "captures_completed": captures_completed,
        "full_captures": full_captures,
        "region_captures": region_captures,
        "daemon_identity_before": before_identity,
        "daemon_identity_after": after_identity,
        "daemon_identity_same": identity_same,
        "summaries": summaries,
        "tail_schedule": tail_schedule,
        "failure_type": failure_type,
        "failure_phase": failure_phase,
        "retries": 0,
        "replacement_samples": 0,
        "provenance": retained_provenance,
        "terminal_cleanup": dict(cleanup),
    }


def _scheduling_nonnegative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("scheduling diagnostic integer is invalid")
    return value


def _retain_scheduling_identity(
    value: object,
    *,
    expected_module: str,
    expected_parent_pid: int | None = None,
) -> dict[str, int | bool | str] | None:
    if value is None:
        return None
    expected_keys = {"pid", "starttime_ticks", "argv_match", "argv_module"}
    if expected_parent_pid is not None:
        expected_keys.add("parent_pid")
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise ValueError("scheduling diagnostic identity is invalid")
    pid = _scheduling_nonnegative_int(value.get("pid"))
    starttime_ticks = _scheduling_nonnegative_int(value.get("starttime_ticks"))
    if (
        pid < 1
        or starttime_ticks < 1
        or value.get("argv_match") is not True
        or value.get("argv_module") != expected_module
    ):
        raise ValueError("scheduling diagnostic process identity is invalid")
    retained: dict[str, int | bool | str] = {
        "pid": pid,
        "starttime_ticks": starttime_ticks,
        "argv_match": True,
        "argv_module": expected_module,
    }
    if expected_parent_pid is not None:
        parent_pid = _scheduling_nonnegative_int(value.get("parent_pid"))
        if parent_pid != expected_parent_pid:
            raise ValueError("scheduling diagnostic worker parent is invalid")
        retained["parent_pid"] = parent_pid
    return retained


def _retain_scheduling_counter_snapshot(
    value: object,
    fields: tuple[str, ...],
) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise ValueError("scheduling diagnostic counter snapshot is invalid")
    return {field: _scheduling_nonnegative_int(value.get(field)) for field in fields}


def _scheduling_counter_delta(
    before: Mapping[str, int],
    after: Mapping[str, int],
) -> dict[str, int]:
    delta = {field: after[field] - before[field] for field in before}
    if any(value < 0 for value in delta.values()):
        raise ValueError("scheduling diagnostic counters are not monotonic")
    return delta


def _retain_scheduling_summaries(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"combined", "full", "region"}:
        raise ValueError("scheduling diagnostic summary lanes are invalid")
    expected_counts = {
        "combined": X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
        "full": X11_SCHEDULING_DIAGNOSTIC_CAPTURES // 2,
        "region": X11_SCHEDULING_DIAGNOSTIC_CAPTURES // 2,
    }
    expected_metrics = set(X11_SCHEDULING_TIMING_METRICS) | set(
        X11_SCHEDULING_CPU_METRICS
    )
    retained: dict[str, Any] = {}
    for lane, expected_count in expected_counts.items():
        lane_value = value.get(lane)
        if (
            not isinstance(lane_value, Mapping)
            or set(lane_value) != {"sample_count", "metrics"}
            or lane_value.get("sample_count") != expected_count
        ):
            raise ValueError("scheduling diagnostic lane count is invalid")
        metrics = lane_value.get("metrics")
        if not isinstance(metrics, Mapping) or set(metrics) != expected_metrics:
            raise ValueError("scheduling diagnostic summary metrics are invalid")
        retained_metrics: dict[str, Any] = {}
        for metric in (*X11_SCHEDULING_TIMING_METRICS, *X11_SCHEDULING_CPU_METRICS):
            summary = metrics.get(metric)
            percentile_keys = (
                ("p50_ms", "p95_ms", "p99_ms", "max_ms")
                if metric in X11_SCHEDULING_TIMING_METRICS
                else ("p50", "p95", "p99", "max")
            )
            expected_keys = set(percentile_keys)
            if metric in X11_SCHEDULING_TIMING_METRICS:
                expected_keys.update(
                    f"over_{threshold}_count"
                    for threshold in X11_SCHEDULING_TAIL_THRESHOLDS_MS
                )
            if not isinstance(summary, Mapping) or set(summary) != expected_keys:
                raise ValueError("scheduling diagnostic metric summary is invalid")
            p50 = _daemon_local_nonnegative_float(summary.get(percentile_keys[0]))
            p95 = _daemon_local_nonnegative_float(summary.get(percentile_keys[1]))
            p99 = _daemon_local_nonnegative_float(summary.get(percentile_keys[2]))
            maximum = _daemon_local_nonnegative_float(summary.get(percentile_keys[3]))
            if p50 > p95 or p95 > p99 or p99 > maximum:
                raise ValueError("scheduling diagnostic percentiles are invalid")
            retained_summary: dict[str, int | float] = dict(
                zip(percentile_keys, (p50, p95, p99, maximum), strict=True)
            )
            if metric in X11_SCHEDULING_TIMING_METRICS:
                counts: list[int] = []
                for threshold in X11_SCHEDULING_TAIL_THRESHOLDS_MS:
                    count = _scheduling_nonnegative_int(
                        summary.get(f"over_{threshold}_count")
                    )
                    if count > expected_count:
                        raise ValueError("scheduling diagnostic tail count is invalid")
                    counts.append(count)
                if counts != sorted(counts, reverse=True):
                    raise ValueError("scheduling diagnostic tail counts are not monotonic")
                retained_summary.update(
                    {
                        f"over_{threshold}_count": count
                        for threshold, count in zip(
                            X11_SCHEDULING_TAIL_THRESHOLDS_MS,
                            counts,
                            strict=True,
                        )
                    }
                )
            retained_metrics[metric] = retained_summary
        retained[lane] = {"sample_count": expected_count, "metrics": retained_metrics}
    for metric in (*X11_SCHEDULING_TIMING_METRICS, *X11_SCHEDULING_CPU_METRICS):
        combined = retained["combined"]["metrics"][metric]
        full = retained["full"]["metrics"][metric]
        region = retained["region"]["metrics"][metric]
        maximum_key = (
            "max_ms" if metric in X11_SCHEDULING_TIMING_METRICS else "max"
        )
        if combined[maximum_key] != max(full[maximum_key], region[maximum_key]):
            raise ValueError("scheduling diagnostic lane maxima are inconsistent")
        if metric in X11_SCHEDULING_TIMING_METRICS:
            for threshold in X11_SCHEDULING_TAIL_THRESHOLDS_MS:
                key = f"over_{threshold}_count"
                if combined[key] != full[key] + region[key]:
                    raise ValueError("scheduling diagnostic lane tails are inconsistent")
    return retained


def _retain_scheduling_tail_schedule(
    value: object,
    summaries: Mapping[str, Any],
) -> dict[str, list[dict[str, int | float]]]:
    if not isinstance(value, Mapping) or set(value) != set(
        X11_SCHEDULING_TIMING_METRICS
    ):
        raise ValueError("scheduling diagnostic tail schedule is invalid")
    retained: dict[str, list[dict[str, int | float]]] = {}
    for metric in X11_SCHEDULING_TIMING_METRICS:
        entries = value.get(metric)
        if not isinstance(entries, list):
            raise ValueError("scheduling diagnostic tail entries are invalid")
        retained_entries: list[dict[str, int | float]] = []
        previous_index = -1
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {
                "schedule_index",
                "timing_ms",
            }:
                raise ValueError("scheduling diagnostic tail entry is invalid")
            index = _scheduling_nonnegative_int(entry.get("schedule_index"))
            timing = _daemon_local_nonnegative_float(entry.get("timing_ms"))
            if (
                index <= previous_index
                or index >= X11_SCHEDULING_DIAGNOSTIC_CAPTURES
                or timing <= X11_SCHEDULING_TAIL_THRESHOLDS_MS[0]
            ):
                raise ValueError("scheduling diagnostic tail record is invalid")
            retained_entries.append({"schedule_index": index, "timing_ms": timing})
            previous_index = index
        combined = summaries["combined"]["metrics"][metric]
        full = summaries["full"]["metrics"][metric]
        region = summaries["region"]["metrics"][metric]
        for threshold in X11_SCHEDULING_TAIL_THRESHOLDS_MS:
            key = f"over_{threshold}_count"
            above = [entry for entry in retained_entries if entry["timing_ms"] > threshold]
            if (
                len(above) != combined[key]
                or sum(entry["schedule_index"] % 2 == 0 for entry in above)
                != full[key]
                or sum(entry["schedule_index"] % 2 == 1 for entry in above)
                != region[key]
            ):
                raise ValueError("scheduling diagnostic tail records disagree")
        if retained_entries:
            if max(entry["timing_ms"] for entry in retained_entries) != combined["max_ms"]:
                raise ValueError("scheduling diagnostic maximum disagrees")
        elif combined["max_ms"] > X11_SCHEDULING_TAIL_THRESHOLDS_MS[0]:
            raise ValueError("scheduling diagnostic maximum is missing")
        retained[metric] = retained_entries
    return retained


def _retain_scheduling_correlations(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(
        X11_SCHEDULING_TIMING_METRICS
    ):
        raise ValueError("scheduling diagnostic correlations are invalid")
    retained: dict[str, Any] = {}
    for timing_metric in X11_SCHEDULING_TIMING_METRICS:
        row = value.get(timing_metric)
        if not isinstance(row, Mapping) or set(row) != set(X11_SCHEDULING_CPU_METRICS):
            raise ValueError("scheduling diagnostic correlation row is invalid")
        retained_row: dict[str, Any] = {}
        for cpu_metric in X11_SCHEDULING_CPU_METRICS:
            cell = row.get(cpu_metric)
            if not isinstance(cell, Mapping) or set(cell) != {
                "coefficient",
                "sample_count",
            }:
                raise ValueError("scheduling diagnostic correlation cell is invalid")
            coefficient = cell.get("coefficient")
            if coefficient is not None:
                if (
                    isinstance(coefficient, bool)
                    or not isinstance(coefficient, (int, float))
                    or not math.isfinite(coefficient)
                    or not -1 <= coefficient <= 1
                ):
                    raise ValueError("scheduling diagnostic coefficient is invalid")
                coefficient = float(coefficient)
            if cell.get("sample_count") != X11_SCHEDULING_DIAGNOSTIC_CAPTURES:
                raise ValueError("scheduling diagnostic correlation count is invalid")
            retained_row[cpu_metric] = {
                "coefficient": coefficient,
                "sample_count": X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
            }
        retained[timing_metric] = retained_row
    return retained


def _build_x11_shm_scheduling_diagnostic(
    observation: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, str | bool],
) -> dict[str, Any]:
    validation_failed = False
    summaries: dict[str, Any] | None = None
    tail_schedule: dict[str, list[dict[str, int | float]]] = {}
    correlations: dict[str, Any] | None = None
    daemon_before: dict[str, int | bool | str] | None = None
    daemon_after: dict[str, int | bool | str] | None = None
    worker_before: dict[str, int | bool | str] | None = None
    worker_after: dict[str, int | bool | str] | None = None
    cpu_max: dict[str, int | None] | None = None
    cgroup_before: dict[str, int] | None = None
    cgroup_after: dict[str, int] | None = None
    cgroup_delta: dict[str, int] | None = None
    per_request_sums: dict[str, int] | None = None
    daemon_schedstat_delta: dict[str, int] | None = None
    worker_schedstat_delta: dict[str, int] | None = None
    client_schedstat_delta: dict[str, int] | None = None

    def optional_schedstat_delta(prefix: str) -> dict[str, int] | None:
        raw_before = observation.get(f"{prefix}_schedstat_before")
        raw_after = observation.get(f"{prefix}_schedstat_after")
        if raw_before is None and raw_after is None:
            return None
        if raw_before is None or raw_after is None:
            raise ValueError("scheduling diagnostic schedstat availability changed")
        before = _retain_scheduling_counter_snapshot(
            raw_before,
            X11_SCHEDULING_SCHEDSTAT_FIELDS,
        )
        after = _retain_scheduling_counter_snapshot(
            raw_after,
            X11_SCHEDULING_SCHEDSTAT_FIELDS,
        )
        return _scheduling_counter_delta(before, after)

    if observation.get("passed") is True:
        try:
            summaries = _retain_scheduling_summaries(observation.get("summaries"))
            tail_schedule = _retain_scheduling_tail_schedule(
                observation.get("tail_schedule"), summaries
            )
            correlations = _retain_scheduling_correlations(
                observation.get("correlations")
            )
            daemon_before = _retain_scheduling_identity(
                observation.get("daemon_identity_before"),
                expected_module="modal_computer_use.daemon",
            )
            daemon_after = _retain_scheduling_identity(
                observation.get("daemon_identity_after"),
                expected_module="modal_computer_use.daemon",
            )
            worker_before = _retain_scheduling_identity(
                observation.get("worker_identity_before"),
                expected_module="_x11_shm_worker.py",
                expected_parent_pid=int(daemon_before["pid"]) if daemon_before else None,
            )
            worker_after = _retain_scheduling_identity(
                observation.get("worker_identity_after"),
                expected_module="_x11_shm_worker.py",
                expected_parent_pid=int(daemon_after["pid"]) if daemon_after else None,
            )
            raw_cpu_max = observation.get("cpu_max")
            if not isinstance(raw_cpu_max, Mapping) or set(raw_cpu_max) != {
                "quota_usec",
                "period_usec",
            }:
                raise ValueError("scheduling diagnostic cpu.max is invalid")
            quota = _scheduling_nonnegative_int(raw_cpu_max.get("quota_usec"))
            period = _scheduling_nonnegative_int(raw_cpu_max.get("period_usec"))
            if quota < 1 or period < 1 or quota != period:
                raise ValueError("scheduling diagnostic is not fixed to one CPU")
            cpu_max = {"quota_usec": quota, "period_usec": period}
            cgroup_before = _retain_scheduling_counter_snapshot(
                observation.get("cgroup_cpu_stat_before"),
                X11_SCHEDULING_CGROUP_FIELDS,
            )
            cgroup_after = _retain_scheduling_counter_snapshot(
                observation.get("cgroup_cpu_stat_after"),
                X11_SCHEDULING_CGROUP_FIELDS,
            )
            cgroup_delta = _scheduling_counter_delta(cgroup_before, cgroup_after)
            supplied_delta = _retain_scheduling_counter_snapshot(
                observation.get("cgroup_cpu_stat_deltas"),
                X11_SCHEDULING_CGROUP_FIELDS,
            )
            if supplied_delta != cgroup_delta:
                raise ValueError("scheduling diagnostic cgroup delta disagrees")
            per_request_sums = _retain_scheduling_counter_snapshot(
                observation.get("per_request_cgroup_delta_sums"),
                X11_SCHEDULING_CGROUP_FIELDS,
            )
            if any(per_request_sums[key] > cgroup_delta[key] for key in cgroup_delta):
                raise ValueError("scheduling diagnostic sampled cgroup delta is invalid")
            daemon_schedstat_delta = optional_schedstat_delta("daemon")
            if worker_before is None or worker_after is None:
                raise ValueError("scheduling diagnostic worker identity is unavailable")
            worker_schedstat_delta = optional_schedstat_delta("worker")
            client_schedstat_delta = optional_schedstat_delta("client")
        except (TypeError, ValueError, OverflowError):
            validation_failed = True
            summaries = None
            tail_schedule = {}
            correlations = None
            daemon_before = daemon_after = None
            worker_before = worker_after = None
            cpu_max = cgroup_before = cgroup_after = cgroup_delta = None
            per_request_sums = None
            daemon_schedstat_delta = worker_schedstat_delta = None
            client_schedstat_delta = None
    else:
        # Failed diagnostics still retain independently validated numeric process
        # and cgroup evidence. Malformed or unavailable subfields become unknown.
        with suppress(TypeError, ValueError, OverflowError):
            daemon_before = _retain_scheduling_identity(
                observation.get("daemon_identity_before"),
                expected_module="modal_computer_use.daemon",
            )
        with suppress(TypeError, ValueError, OverflowError):
            daemon_after = _retain_scheduling_identity(
                observation.get("daemon_identity_after"),
                expected_module="modal_computer_use.daemon",
            )
        if daemon_before is not None:
            with suppress(TypeError, ValueError, OverflowError):
                worker_before = _retain_scheduling_identity(
                    observation.get("worker_identity_before"),
                    expected_module="_x11_shm_worker.py",
                    expected_parent_pid=int(daemon_before["pid"]),
                )
        if daemon_after is not None:
            with suppress(TypeError, ValueError, OverflowError):
                worker_after = _retain_scheduling_identity(
                    observation.get("worker_identity_after"),
                    expected_module="_x11_shm_worker.py",
                    expected_parent_pid=int(daemon_after["pid"]),
                )
        with suppress(TypeError, ValueError, OverflowError):
            raw_cpu_max = observation.get("cpu_max")
            if isinstance(raw_cpu_max, Mapping) and set(raw_cpu_max) == {
                "quota_usec",
                "period_usec",
            }:
                raw_quota = raw_cpu_max.get("quota_usec")
                quota = (
                    None
                    if raw_quota is None
                    else _scheduling_nonnegative_int(raw_quota)
                )
                period = _scheduling_nonnegative_int(raw_cpu_max.get("period_usec"))
                if period > 0 and (quota is None or quota > 0):
                    cpu_max = {"quota_usec": quota, "period_usec": period}
        with suppress(TypeError, ValueError, OverflowError):
            cgroup_before = _retain_scheduling_counter_snapshot(
                observation.get("cgroup_cpu_stat_before"),
                X11_SCHEDULING_CGROUP_FIELDS,
            )
        with suppress(TypeError, ValueError, OverflowError):
            cgroup_after = _retain_scheduling_counter_snapshot(
                observation.get("cgroup_cpu_stat_after"),
                X11_SCHEDULING_CGROUP_FIELDS,
            )
        if cgroup_before is not None and cgroup_after is not None:
            with suppress(TypeError, ValueError, OverflowError):
                cgroup_delta = _scheduling_counter_delta(cgroup_before, cgroup_after)
        for prefix, destination in (
            ("daemon", "daemon"),
            ("worker", "worker"),
            ("client", "client"),
        ):
            try:
                before = _retain_scheduling_counter_snapshot(
                    observation.get(f"{prefix}_schedstat_before"),
                    X11_SCHEDULING_SCHEDSTAT_FIELDS,
                )
                after = _retain_scheduling_counter_snapshot(
                    observation.get(f"{prefix}_schedstat_after"),
                    X11_SCHEDULING_SCHEDSTAT_FIELDS,
                )
                delta = _scheduling_counter_delta(before, after)
            except (TypeError, ValueError, OverflowError):
                continue
            if destination == "daemon":
                daemon_schedstat_delta = delta
            elif destination == "worker":
                worker_schedstat_delta = delta
            else:
                client_schedstat_delta = delta

    retained_provenance = _retain_daemon_local_provenance(provenance)
    captures_completed = _retain_daemon_local_count(
        observation.get("captures_completed"),
        maximum=X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
    )
    warmups_completed = _retain_daemon_local_count(
        observation.get("warmups_completed"),
        maximum=X11_SCHEDULING_DIAGNOSTIC_WARMUPS,
    )
    full_captures = _retain_daemon_local_count(
        observation.get("full_captures"),
        maximum=X11_SCHEDULING_DIAGNOSTIC_CAPTURES // 2,
    )
    region_captures = _retain_daemon_local_count(
        observation.get("region_captures"),
        maximum=X11_SCHEDULING_DIAGNOSTIC_CAPTURES // 2,
    )
    count_prefix_valid = bool(
        captures_completed is not None
        and full_captures is not None
        and region_captures is not None
        and full_captures + region_captures == captures_completed
        and full_captures == (captures_completed + 1) // 2
        and region_captures == captures_completed // 2
    )
    if not count_prefix_valid:
        if observation.get("passed") is True:
            validation_failed = True
        else:
            captures_completed = full_captures = region_captures = None
    daemon_match_count = _retain_daemon_local_count(
        observation.get("daemon_match_count"), maximum=100
    )
    daemon_root_match_count = _retain_daemon_local_count(
        observation.get("daemon_root_match_count"), maximum=100
    )
    daemon_match_count_after = _retain_daemon_local_count(
        observation.get("daemon_match_count_after"), maximum=100
    )
    daemon_root_match_count_after = _retain_daemon_local_count(
        observation.get("daemon_root_match_count_after"), maximum=100
    )
    worker_match_count = _retain_daemon_local_count(
        observation.get("worker_match_count"), maximum=100
    )
    worker_match_count_after = _retain_daemon_local_count(
        observation.get("worker_match_count_after"), maximum=100
    )
    daemon_worker_pair_count = _retain_daemon_local_count(
        observation.get("daemon_worker_pair_count"), maximum=100
    )
    daemon_worker_pair_count_after = _retain_daemon_local_count(
        observation.get("daemon_worker_pair_count_after"), maximum=100
    )
    daemon_count_pair_valid = bool(
        daemon_match_count is not None
        and daemon_root_match_count is not None
        and worker_match_count is not None
        and daemon_worker_pair_count is not None
        and daemon_root_match_count <= daemon_match_count
        and daemon_worker_pair_count <= worker_match_count
    )
    daemon_count_pair_after_valid = bool(
        daemon_match_count_after is not None
        and daemon_root_match_count_after is not None
        and worker_match_count_after is not None
        and daemon_worker_pair_count_after is not None
        and daemon_root_match_count_after <= daemon_match_count_after
        and daemon_worker_pair_count_after <= worker_match_count_after
    )
    if not daemon_count_pair_valid:
        daemon_match_count = daemon_root_match_count = None
        worker_match_count = daemon_worker_pair_count = None
    if not daemon_count_pair_after_valid:
        daemon_match_count_after = daemon_root_match_count_after = None
        worker_match_count_after = daemon_worker_pair_count_after = None
    daemon_same = bool(
        daemon_before
        and daemon_after
        and daemon_before["pid"] == daemon_after["pid"]
        and daemon_before["starttime_ticks"] == daemon_after["starttime_ticks"]
    )
    worker_same = bool(
        worker_before
        and worker_after
        and worker_before["pid"] == worker_after["pid"]
        and worker_before["starttime_ticks"] == worker_after["starttime_ticks"]
    )
    cleanup_succeeded = cleanup.get("succeeded") is True and cleanup.get(
        "remaining_sandboxes"
    ) == 0
    observed_backend = _safe_diagnostic_label(observation.get("observed_backend"))
    contract_matches = bool(
        observation.get("requested_source") == "x11-shm"
        and observed_backend == "x11-shm"
        and observation.get("warmups_requested") == X11_SCHEDULING_DIAGNOSTIC_WARMUPS
        and warmups_completed == X11_SCHEDULING_DIAGNOSTIC_WARMUPS
        and observation.get("captures_requested") == X11_SCHEDULING_DIAGNOSTIC_CAPTURES
        and count_prefix_valid
        and captures_completed == X11_SCHEDULING_DIAGNOSTIC_CAPTURES
        and full_captures == X11_SCHEDULING_DIAGNOSTIC_CAPTURES // 2
        and region_captures == X11_SCHEDULING_DIAGNOSTIC_CAPTURES // 2
        and daemon_same
        and daemon_count_pair_valid
        and daemon_match_count is not None
        and daemon_match_count >= 1
        and daemon_root_match_count is not None
        and worker_match_count is not None
        and worker_match_count >= 1
        and daemon_worker_pair_count == 1
        and daemon_count_pair_after_valid
        and daemon_match_count_after is not None
        and daemon_match_count_after >= 1
        and daemon_root_match_count_after is not None
        and worker_match_count_after is not None
        and worker_match_count_after >= 1
        and daemon_worker_pair_count_after == 1
        and worker_same
        and summaries is not None
        and correlations is not None
        and cpu_max is not None
        and cgroup_delta is not None
        and retained_provenance is not None
    )
    if observation.get("passed") is True and not contract_matches:
        validation_failed = True
    passed = bool(
        observation.get("passed") is True
        and not validation_failed
        and contract_matches
        and cleanup_succeeded
    )
    failure_type = (
        "EvidenceValidationError"
        if validation_failed
        else "CleanupError"
        if observation.get("passed") is True and not cleanup_succeeded
        else _safe_diagnostic_label(observation.get("failure_type"))
    )
    failure_phase = (
        "artifact_validation"
        if validation_failed
        else "terminal_cleanup"
        if observation.get("passed") is True and not cleanup_succeeded
        else _safe_diagnostic_label(observation.get("failure_phase"))
    )
    retained_cleanup = {
        "succeeded": cleanup.get("succeeded") is True,
        "remaining_sandboxes": _retain_daemon_local_count(
            cleanup.get("remaining_sandboxes"), maximum=100
        ),
        "survivors_before_sweep": _retain_daemon_local_count(
            cleanup.get("survivors_before_sweep"), maximum=100
        ),
    }
    return {
        "schema_version": "x11-shm-scheduling-diagnostic.v1",
        "benchmark": "x11-shm-scheduling-diagnostic",
        "status": "complete",
        "passed": passed,
        "scope": "daemon-local-scheduling-mechanism-only",
        "non_gating": True,
        "promotion_proxy": False,
        "endpoint_order_confounded": True,
        "instrumentation_intrusive": True,
        "requested_source": "x11-shm",
        "observed_backend": observed_backend,
        "warmups_requested": X11_SCHEDULING_DIAGNOSTIC_WARMUPS,
        "warmups_completed": warmups_completed,
        "expected_sample_count": X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
        "sample_count": captures_completed,
        "captures_requested": X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
        "captures_completed": captures_completed,
        "full_captures": full_captures,
        "region_captures": region_captures,
        "daemon_identity_before": daemon_before,
        "daemon_identity_after": daemon_after,
        "daemon_identity_same": daemon_same,
        "daemon_match_count": daemon_match_count,
        "daemon_root_match_count": daemon_root_match_count,
        "daemon_match_count_after": daemon_match_count_after,
        "daemon_root_match_count_after": daemon_root_match_count_after,
        "worker_match_count": worker_match_count,
        "worker_match_count_after": worker_match_count_after,
        "daemon_worker_pair_count": daemon_worker_pair_count,
        "daemon_worker_pair_count_after": daemon_worker_pair_count_after,
        "worker_identity_before": worker_before,
        "worker_identity_after": worker_after,
        "worker_identity_same": worker_same,
        "daemon_schedstat_available": daemon_schedstat_delta is not None,
        "worker_schedstat_available": worker_schedstat_delta is not None,
        "client_schedstat_available": client_schedstat_delta is not None,
        "cpu_max": cpu_max,
        "cgroup_cpu_stat_before": cgroup_before,
        "cgroup_cpu_stat_after": cgroup_after,
        "cgroup_cpu_stat_deltas": cgroup_delta,
        "per_request_cgroup_delta_sums": per_request_sums,
        "daemon_schedstat_delta": daemon_schedstat_delta,
        "worker_schedstat_delta": worker_schedstat_delta,
        "client_schedstat_delta": client_schedstat_delta,
        "summaries": summaries,
        "tail_schedule": tail_schedule,
        "correlations": correlations,
        "failure_type": failure_type,
        "failure_phase": failure_phase,
        "retries": 0,
        "replacement_samples": 0,
        "provenance": retained_provenance,
        "terminal_cleanup": retained_cleanup,
    }


def _build_x11_shm_soak_diagnostic_script(
    captures: int,
    *,
    snapshot_only: bool = False,
) -> str:
    return dedent(
        f"""
        import http.client
        import json
        import os
        from pathlib import Path

{indent(_DAEMON_ARGV_MATCHER_SOURCE, "        ")}

        def daemon_identity():
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    command = (entry / "cmdline").read_bytes()
                    stat_text = (entry / "stat").read_text(encoding="utf-8")
                except OSError:
                    continue
                if not _is_modal_daemon_cmdline(command):
                    continue
                try:
                    pid = int(entry.name)
                    starttime_ticks = int(stat_text.rsplit(") ", 1)[1].split()[19])
                except (IndexError, ValueError):
                    continue
                return {{
                    "pid": pid,
                    "starttime_ticks": starttime_ticks,
                    "argv_match": True,
                    "argv_module": "modal_computer_use.daemon",
                }}
            return None

        RESOURCE_METRICS = ("maps", "fd", "VmRSS", "sampled_vm_rss")

        class ResourceMetricUnavailable(RuntimeError):
            def __init__(self, metric):
                super().__init__("resource metric unavailable")
                self.metric = metric

        resource_metrics_available = {{metric: False for metric in RESOURCE_METRICS}}

        def status_bytes(pid, key):
            try:
                with open(f"/proc/{{pid}}/status", encoding="utf-8") as status_file:
                    for line in status_file:
                        if line.startswith(key + ":"):
                            parts = line.split()
                            if len(parts) >= 2:
                                return int(parts[1]) * 1024
            except (OSError, ValueError, IndexError):
                return None
            return None

        def counts(pid):
            global resource_metrics_available
            available = {{metric: False for metric in RESOURCE_METRICS}}
            try:
                with open(f"/proc/{{pid}}/maps", encoding="utf-8") as maps_file:
                    mappings = sum(1 for _ in maps_file)
            except (OSError, ValueError):
                resource_metrics_available = available
                raise ResourceMetricUnavailable("maps")
            available["maps"] = True
            try:
                fd = len(os.listdir(f"/proc/{{pid}}/fd"))
            except (OSError, ValueError):
                resource_metrics_available = available
                raise ResourceMetricUnavailable("fd")
            available["fd"] = True
            rss = status_bytes(pid, "VmRSS")
            if rss is None:
                resource_metrics_available = available
                raise ResourceMetricUnavailable("VmRSS")
            available["VmRSS"] = True
            resource_metrics_available = available
            return {{
                "fd": fd,
                "mappings": mappings,
                "rss": rss,
            }}

        rss_samples = []

        def sample_rss(pid):
            global resource_metrics_available
            value = status_bytes(pid, "VmRSS")
            if value is None:
                resource_metrics_available = dict(resource_metrics_available)
                resource_metrics_available["sampled_vm_rss"] = False
                raise ResourceMetricUnavailable("sampled_vm_rss")
            resource_metrics_available = dict(resource_metrics_available)
            resource_metrics_available["sampled_vm_rss"] = True
            rss_samples.append(value)
            return value

        token = os.environ["COMPUTER_USE_TUNNEL_TOKEN"]
        port = int(os.environ.get("COMPUTER_USE_DAEMON_PORT", "8080"))
        headers = {{
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }}
        full = json.dumps({{
            "format": "png", "quality": 90, "scale": 1.0,
            "show_cursor": False, "processing": "auto", "storage": "inline",
        }})
        region = json.dumps({{
            "format": "png", "quality": 90, "scale": 1.0,
            "show_cursor": False, "processing": "auto", "storage": "inline",
            "region": {{"x": 7, "y": 9, "width": 511, "height": 383}},
        }})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        observed_backend = None
        failure_type = None
        failure_phase = None
        identity_before = None
        identity_after = None
        counts_before = None
        counts_after = None
        resource_metrics_before = None
        resource_metrics_after = None
        failure_resource_metric = None
        full_captures = 0
        region_captures = 0
        prime_captures = 0
        rss_before_bytes = None
        rss_current_bytes = None
        rss_final_bytes = None
        rss_observed_peak_bytes = None
        rss_peak_growth_bytes = None

        def capture(path, body, expected_width, expected_height):
            global observed_backend
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            if response.status != 200 or not data.startswith(b"\\x89PNG"):
                raise RuntimeError("daemon-local screenshot failed")
            backend = response.getheader("x-computer-use-capture-backend")
            if backend != "x11-shm":
                raise RuntimeError("daemon-local screenshot used an unexpected source")
            if int(response.getheader("x-computer-use-width", "-1")) != expected_width:
                raise RuntimeError("daemon-local screenshot width changed")
            if int(response.getheader("x-computer-use-height", "-1")) != expected_height:
                raise RuntimeError("daemon-local screenshot height changed")
            observed_backend = backend

        try:
            failure_phase = "prime_capture"
            capture("/v1/screenshots/full/raw", full, 1024, 768)
            prime_captures += 1
            capture("/v1/screenshots/region/raw", region, 511, 383)
            prime_captures += 1
            failure_phase = "identity_before"
            identity_before = daemon_identity()
            if identity_before is None:
                raise RuntimeError("daemon process was not found")
            failure_phase = "counts_before"
            counts_before = counts(identity_before["pid"])
            failure_phase = "rss_sample_before"
            rss_before_bytes = sample_rss(identity_before["pid"])
            rss_observed_peak_bytes = rss_before_bytes
            rss_peak_growth_bytes = 0
            resource_metrics_before = dict(resource_metrics_available)
            if {not snapshot_only}:
                failure_phase = "soak_capture"
                for index in range({captures}):
                    if index % 2:
                        capture("/v1/screenshots/region/raw", region, 511, 383)
                        region_captures += 1
                    else:
                        capture("/v1/screenshots/full/raw", full, 1024, 768)
                        full_captures += 1
                    if (index + 1) % {SOAK_RSS_SAMPLE_INTERVAL} == 0:
                        failure_phase = "rss_sample_cadence"
                        sample_rss(identity_before["pid"])
                        failure_phase = "soak_capture"
                failure_phase = "identity_after"
                identity_after = daemon_identity()
                if identity_after is None:
                    raise RuntimeError("daemon process was not found after soak")
                failure_phase = "counts_after"
                counts_after = counts(identity_after["pid"])
                rss_current_bytes = counts_after["rss"]
                failure_phase = "rss_sample_final"
                rss_final_bytes = sample_rss(identity_after["pid"])
                rss_observed_peak_bytes = max(rss_samples)
                rss_peak_growth_bytes = max(
                    0, rss_observed_peak_bytes - rss_before_bytes
                )
                resource_metrics_after = dict(resource_metrics_available)
        except ResourceMetricUnavailable as exc:
            failure_type = type(exc).__name__
            failure_resource_metric = exc.metric
            if failure_phase in {"counts_before", "rss_sample_before"}:
                resource_metrics_before = dict(resource_metrics_available)
            elif failure_phase in {
                "counts_after",
                "rss_sample_cadence",
                "rss_sample_final",
            }:
                resource_metrics_after = dict(resource_metrics_available)
        except Exception as exc:
            failure_type = type(exc).__name__
        finally:
            connection.close()

        signed_deltas = None
        if counts_before is not None and counts_after is not None:
            signed_deltas = {{
                key: counts_after[key] - counts_before[key]
                for key in ("fd", "mappings", "rss")
            }}
        print(json.dumps({{
            "passed": failure_type is None,
            "requested_source": "auto",
            "observed_backend": observed_backend,
            "prime_captures": prime_captures,
            "captures_requested": {0 if snapshot_only else captures},
            "captures_completed": full_captures + region_captures,
            "full_captures": full_captures,
            "region_captures": region_captures,
            "daemon_identity_before": identity_before,
            "daemon_identity_after": identity_after,
            "counts_before": counts_before,
            "counts_after": counts_after,
            "resource_metrics_before": resource_metrics_before,
            "resource_metrics_after": resource_metrics_after,
            "failure_resource_metric": failure_resource_metric,
            "rss_metric_source": "sampled_vm_rss",
            "rss_sample_count": len(rss_samples),
            "final_included": {not snapshot_only},
            "rss_before_bytes": rss_before_bytes,
            "rss_current_bytes": rss_current_bytes,
            "rss_final_bytes": rss_final_bytes,
            "rss_observed_peak_bytes": rss_observed_peak_bytes,
            "rss_peak_growth_bytes": rss_peak_growth_bytes,
            "signed_deltas": signed_deltas,
            "failure_type": failure_type,
            "failure_phase": failure_phase,
        }}))
        """
    )


def _build_x11_shm_daemon_local_tail_script(
    *,
    captures: int = DAEMON_LOCAL_TAIL_CAPTURES,
    warmups: int = DAEMON_LOCAL_TAIL_WARMUPS,
) -> str:
    if captures != DAEMON_LOCAL_TAIL_CAPTURES:
        raise ValueError("daemon-local tail diagnostic requires exactly 1000 captures")
    if warmups != DAEMON_LOCAL_TAIL_WARMUPS:
        raise ValueError("daemon-local tail diagnostic requires exactly 2 warmups")
    return dedent(
        f"""
        import http.client
        import json
        import math
        import os
        import time
        from pathlib import Path

{indent(_DAEMON_ARGV_MATCHER_SOURCE, "        ")}

{indent(_DAEMON_UNATTRIBUTED_SOURCE, "        ")}

        CAPTURES = {captures}
        WARMUPS = {warmups}
        METRICS = {DAEMON_LOCAL_TAIL_METRICS!r}
        TAIL_THRESHOLDS_MS = {DAEMON_LOCAL_TAIL_THRESHOLDS_MS!r}

        def daemon_identity():
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    command = (entry / "cmdline").read_bytes()
                    stat_text = (entry / "stat").read_text(encoding="utf-8")
                except OSError:
                    continue
                if not _is_modal_daemon_cmdline(command):
                    continue
                try:
                    pid = int(entry.name)
                    starttime_ticks = int(stat_text.rsplit(") ", 1)[1].split()[19])
                except (IndexError, ValueError):
                    continue
                return {{
                    "pid": pid,
                    "starttime_ticks": starttime_ticks,
                    "argv_match": True,
                    "argv_module": "modal_computer_use.daemon",
                }}
            return None

        def finite_timing(value):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("invalid timing header")
            return float(value)

        def percentile(values, percentile_value):
            ordered = sorted(values)
            if not ordered:
                raise ValueError("empty timing sample")
            rank = (len(ordered) - 1) * percentile_value
            lower = int(rank)
            upper = min(lower + 1, len(ordered) - 1)
            fraction = rank - lower
            return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

        def metric_summary(values):
            return {{
                "p50_ms": percentile(values, 0.50),
                "p95_ms": percentile(values, 0.95),
                "p99_ms": percentile(values, 0.99),
                "max_ms": max(values),
                **{{
                    f"over_{{threshold}}_count": sum(
                        value > threshold for value in values
                    )
                    for threshold in TAIL_THRESHOLDS_MS
                }},
            }}

        def summarize(samples):
            return {{
                "sample_count": len(samples),
                "metrics": {{
                    metric: metric_summary([sample[metric] for sample in samples])
                    for metric in METRICS
                }},
            }}

        token = os.environ["COMPUTER_USE_TUNNEL_TOKEN"]
        port = int(os.environ.get("COMPUTER_USE_DAEMON_PORT", "8080"))
        headers = {{
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }}
        full_request = json.dumps({{
            "format": "png", "quality": 90, "scale": 1.0,
            "show_cursor": False, "processing": "auto", "storage": "inline",
        }})
        region_request = json.dumps({{
            "format": "png", "quality": 90, "scale": 1.0,
            "show_cursor": False, "processing": "auto", "storage": "inline",
            "region": {{"x": 7, "y": 9, "width": 511, "height": 383}},
        }})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        observed_backend = None
        warmups_completed = 0
        captures_completed = 0
        full_captures = 0
        region_captures = 0
        identity_before = None
        identity_after = None
        summaries = None
        tail_schedule = {{}}
        failure_type = None
        failure_phase = "identity_before"
        samples = []

        def capture(path, request_body, expected_width, expected_height):
            nonlocal_observed = None
            started_ns = time.perf_counter_ns()
            connection.request("POST", path, body=request_body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            finished_ns = time.perf_counter_ns()
            payload_size = len(response_body)
            png_valid = response_body.startswith(b"\\x89PNG\\r\\n\\x1a\\n")
            del response_body
            if response.status != 200 or not png_valid:
                raise RuntimeError("daemon-local screenshot failed")
            nonlocal_observed = response.getheader(
                "x-computer-use-capture-backend"
            )
            if nonlocal_observed != "x11-shm":
                raise RuntimeError("daemon-local screenshot used an unexpected source")
            if int(response.getheader("x-computer-use-width", "-1")) != expected_width:
                raise RuntimeError("daemon-local screenshot width changed")
            if int(response.getheader("x-computer-use-height", "-1")) != expected_height:
                raise RuntimeError("daemon-local screenshot height changed")
            if int(response.getheader("x-computer-use-size-bytes", "-1")) != payload_size:
                raise RuntimeError("daemon-local screenshot size changed")
            timing_header = response.getheader("x-computer-use-timing-ms")
            try:
                timing = json.loads(timing_header)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid timing header") from exc
            if not isinstance(timing, dict):
                raise ValueError("invalid timing header")
            daemon_total_ms = finite_timing(timing.get("total_ms"))
            hash_ms = finite_timing(timing.get("hash_ms"))
            x11_fused_ms = finite_timing(
                timing.get("x11_shm_capture_encode_ms")
            )
            cursor_value = timing.get("cursor_position_ms")
            if path == "/v1/screenshots/full/raw":
                cursor_position_ms = finite_timing(cursor_value)
            elif cursor_value is None:
                cursor_position_ms = 0.0
            else:
                cursor_position_ms = finite_timing(cursor_value)
                if cursor_position_ms != 0.0:
                    raise ValueError("region timing included cursor positioning")
            local_wall_ms = (finished_ns - started_ns) / 1_000_000.0
            if not math.isfinite(local_wall_ms) or local_wall_ms < 0:
                raise ValueError("invalid local timing")
            return nonlocal_observed, {{
                "local_wall_ms": local_wall_ms,
                "daemon_total_ms": daemon_total_ms,
                "x11_shm_capture_encode_ms": x11_fused_ms,
                "hash_ms": hash_ms,
                "cursor_position_ms": cursor_position_ms,
                "daemon_unattributed_ms": _daemon_unattributed_ms(
                    daemon_total_ms,
                    x11_fused_ms,
                    hash_ms,
                    cursor_position_ms,
                ),
                "local_residual_ms": max(0.0, local_wall_ms - daemon_total_ms),
            }}

        try:
            identity_before = daemon_identity()
            if identity_before is None:
                raise RuntimeError("daemon process was not found")
            failure_phase = "warmup"
            for index in range({warmups}):
                if index % 2:
                    observed_backend, _ = capture(
                        "/v1/screenshots/region/raw", region_request, 511, 383
                    )
                else:
                    observed_backend, _ = capture(
                        "/v1/screenshots/full/raw", full_request, 1024, 768
                    )
                warmups_completed += 1
            failure_phase = "capture"
            for index in range({captures}):
                if index % 2:
                    observed_backend, metrics = capture(
                        "/v1/screenshots/region/raw", region_request, 511, 383
                    )
                    lane = "region"
                    region_captures += 1
                else:
                    observed_backend, metrics = capture(
                        "/v1/screenshots/full/raw", full_request, 1024, 768
                    )
                    lane = "full"
                    full_captures += 1
                samples.append({{
                    "schedule_index": index,
                    "lane": lane,
                    **metrics,
                }})
                captures_completed += 1
            failure_phase = "identity_after"
            identity_after = daemon_identity()
            if identity_after is None:
                raise RuntimeError("daemon process was not found after captures")
            if (
                identity_after["pid"] != identity_before["pid"]
                or identity_after["starttime_ticks"]
                != identity_before["starttime_ticks"]
            ):
                raise RuntimeError("daemon process identity changed")
            failure_phase = "summarize"
            summaries = {{
                "combined": summarize(samples),
                "full": summarize([sample for sample in samples if sample["lane"] == "full"]),
                "region": summarize(
                    [sample for sample in samples if sample["lane"] == "region"]
                ),
            }}
            tail_schedule = {{
                metric: [
                    {{
                        "schedule_index": sample["schedule_index"],
                        "timing_ms": sample[metric],
                    }}
                    for sample in samples
                    if sample[metric] > TAIL_THRESHOLDS_MS[0]
                ]
                for metric in METRICS
            }}
        except Exception as exc:
            failure_type = type(exc).__name__
        finally:
            connection.close()

        print(json.dumps({{
            "passed": failure_type is None,
            "requested_source": "x11-shm",
            "observed_backend": observed_backend,
            "warmups_requested": WARMUPS,
            "warmups_completed": warmups_completed,
            "captures_requested": CAPTURES,
            "captures_completed": captures_completed,
            "full_captures": full_captures,
            "region_captures": region_captures,
            "daemon_identity_before": identity_before,
            "daemon_identity_after": identity_after,
            "summaries": summaries,
            "tail_schedule": tail_schedule,
            "failure_type": failure_type,
            "failure_phase": failure_phase if failure_type is not None else None,
        }}))
        """
    )


def _build_x11_shm_scheduling_diagnostic_script(
    *,
    captures: int = X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
    warmups: int = X11_SCHEDULING_DIAGNOSTIC_WARMUPS,
) -> str:
    if captures != X11_SCHEDULING_DIAGNOSTIC_CAPTURES:
        raise ValueError("scheduling diagnostic requires exactly 1000 captures")
    if warmups != X11_SCHEDULING_DIAGNOSTIC_WARMUPS:
        raise ValueError("scheduling diagnostic requires exactly 2 warmups")
    template = dedent(
        """
        import http.client
        import json
        import math
        import os
        import time
        from pathlib import Path

        __DAEMON_MATCHER__

        __DAEMON_WORKER_SELECTOR__

        __WORKER_MATCHER__

        CAPTURES = __CAPTURES__
        WARMUPS = __WARMUPS__
        TAIL_THRESHOLDS_MS = (50, 100, 500)
        TIMING_METRICS = (
            "local_wall_ms",
            "request_write_ms",
            "response_headers_ms",
            "body_read_ms",
            "controller_total_ms",
            "x11_shm_capture_encode_ms",
            "cursor_position_ms",
            "hash_ms",
            "controller_unattributed_ms",
            "route_ready_ms",
            "route_lock_wait_ms",
            "route_operation_ms",
            "route_total_ms",
            "route_outside_controller_residual_ms",
            "local_outside_route_residual_ms",
        )
        CGROUP_FIELDS = (
            "usage_usec", "nr_periods", "nr_throttled", "throttled_usec",
        )
        CPU_METRICS = tuple("cgroup_" + field + "_delta" for field in CGROUP_FIELDS)
        SCHEDSTAT_FIELDS = ("cpu_runtime_ns", "runqueue_wait_ns", "timeslices")

        def process_stat(pid):
            try:
                stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                fields = stat_text.rsplit(") ", 1)[1].split()
                return {"ppid": int(fields[1]), "starttime_ticks": int(fields[19])}
            except (OSError, IndexError, ValueError):
                return None

        def daemon_candidates():
            matches = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    command = (entry / "cmdline").read_bytes()
                except OSError:
                    continue
                if not _is_modal_daemon_cmdline(command):
                    continue
                stat = process_stat(int(entry.name))
                if stat is not None:
                    matches.append({
                        "pid": int(entry.name),
                        "starttime_ticks": stat["starttime_ticks"],
                        "parent_pid": stat["ppid"],
                        "argv_match": True,
                        "argv_module": "modal_computer_use.daemon",
                    })
            return matches

        def worker_candidates():
            matches = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    command = (entry / "cmdline").read_bytes()
                except OSError:
                    continue
                if not _is_x11_shm_worker_cmdline(command):
                    continue
                stat = process_stat(int(entry.name))
                if stat is not None:
                    matches.append({
                        "pid": int(entry.name),
                        "starttime_ticks": stat["starttime_ticks"],
                        "parent_pid": stat["ppid"],
                        "argv_match": True,
                        "argv_module": "_x11_shm_worker.py",
                    })
            return matches

        def process_identity_pair():
            return _select_daemon_worker_pair(
                daemon_candidates(), worker_candidates()
            )

        def schedstat(pid):
            try:
                fields = Path(f"/proc/{pid}/schedstat").read_text(
                    encoding="utf-8"
                ).split()
                values = [int(value) for value in fields[:3]]
            except (OSError, ValueError):
                return None
            if len(values) != 3 or any(value < 0 for value in values):
                return None
            return dict(zip(SCHEDSTAT_FIELDS, values))

        def cgroup_directory():
            try:
                for line in Path("/proc/self/cgroup").read_text(
                    encoding="utf-8"
                ).splitlines():
                    hierarchy, controllers, relative = line.split(":", 2)
                    if hierarchy == "0" and controllers == "":
                        candidate = Path("/sys/fs/cgroup") / relative.lstrip("/")
                        if (candidate / "cpu.stat").is_file():
                            return candidate
            except (OSError, ValueError):
                pass
            fallback = Path("/sys/fs/cgroup")
            return fallback if (fallback / "cpu.stat").is_file() else None

        def cpu_stat(directory):
            if directory is None:
                return None
            try:
                parsed = {
                    parts[0]: int(parts[1])
                    for line in (directory / "cpu.stat").read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if len(parts := line.split()) == 2
                }
            except (OSError, ValueError):
                return None
            if any(parsed.get(field, -1) < 0 for field in CGROUP_FIELDS):
                return None
            return {field: parsed[field] for field in CGROUP_FIELDS}

        def cpu_max(directory):
            if directory is None:
                return None
            try:
                quota_text, period_text = (directory / "cpu.max").read_text(
                    encoding="utf-8"
                ).split()
                period = int(period_text)
                quota = None if quota_text == "max" else int(quota_text)
            except (OSError, ValueError):
                return None
            if period < 1 or (quota is not None and quota < 1):
                return None
            return {"quota_usec": quota, "period_usec": period}

        def timing_header(response):
            raw = response.getheader("x-computer-use-timing-ms")
            if raw is None:
                raise RuntimeError("timing header unavailable")
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                raise RuntimeError("timing header invalid") from None
            if not isinstance(parsed, dict):
                raise RuntimeError("timing header invalid")
            retained = {}
            for key in (
                "total_ms", "x11_shm_capture_encode_ms", "cursor_position_ms",
                "hash_ms", "route_ready_ms", "route_lock_wait_ms",
                "route_operation_ms", "route_total_ms",
            ):
                value = parsed.get(key)
                if value is None and key == "cursor_position_ms":
                    value = 0.0
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise RuntimeError("timing header stage invalid")
                retained[key] = float(value)
            if retained["route_total_ms"] < sum(
                retained[key]
                for key in (
                    "route_ready_ms", "route_lock_wait_ms", "route_operation_ms"
                )
            ):
                raise RuntimeError("route timing algebra invalid")
            if retained["route_operation_ms"] < retained["total_ms"]:
                raise RuntimeError("controller timing exceeds route operation")
            if retained["total_ms"] + 1e-6 < sum(
                retained[key]
                for key in (
                    "x11_shm_capture_encode_ms", "cursor_position_ms", "hash_ms"
                )
            ):
                raise RuntimeError("controller timing algebra invalid")
            return retained

        def percentile(values, percent):
            ordered = sorted(values)
            index = max(0, math.ceil(len(ordered) * percent / 100) - 1)
            return float(ordered[index])

        def bounded_residual(outer, inner):
            residual = outer - inner
            if residual < -1e-6:
                raise RuntimeError("timing residual algebra invalid")
            return 0.0 if residual < 0 else residual

        def summarize(rows):
            metrics = {}
            for metric in TIMING_METRICS:
                values = [row[metric] for row in rows]
                metrics[metric] = {
                    "p50_ms": percentile(values, 50),
                    "p95_ms": percentile(values, 95),
                    "p99_ms": percentile(values, 99),
                    "max_ms": max(values),
                    **{
                        f"over_{threshold}_count": sum(
                            value > threshold for value in values
                        )
                        for threshold in TAIL_THRESHOLDS_MS
                    },
                }
            for metric in CPU_METRICS:
                values = [row[metric] for row in rows]
                metrics[metric] = {
                    "p50": percentile(values, 50),
                    "p95": percentile(values, 95),
                    "p99": percentile(values, 99),
                    "max": max(values),
                }
            return {"sample_count": len(rows), "metrics": metrics}

        def correlation(rows, first, second):
            first_values = [row[first] for row in rows]
            second_values = [row[second] for row in rows]
            first_mean = sum(first_values) / len(first_values)
            second_mean = sum(second_values) / len(second_values)
            first_variance = sum((value - first_mean) ** 2 for value in first_values)
            second_variance = sum((value - second_mean) ** 2 for value in second_values)
            if first_variance == 0 or second_variance == 0:
                coefficient = None
            else:
                covariance = sum(
                    (first_value - first_mean) * (second_value - second_mean)
                    for first_value, second_value in zip(
                        first_values, second_values
                    )
                )
                coefficient = max(
                    -1.0,
                    min(
                        1.0,
                        covariance / math.sqrt(first_variance * second_variance),
                    ),
                )
            return {"coefficient": coefficient, "sample_count": len(rows)}

        token = os.environ["COMPUTER_USE_TUNNEL_TOKEN"]
        port = int(os.environ.get("COMPUTER_USE_DAEMON_PORT", "8080"))
        request_headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        full_payload = json.dumps({
            "format": "png", "quality": 90, "scale": 1.0,
            "show_cursor": False, "processing": "auto", "storage": "inline",
        })
        region_payload = json.dumps({
            "format": "png", "quality": 90, "scale": 1.0,
            "show_cursor": False, "processing": "auto", "storage": "inline",
            "region": {"x": 7, "y": 9, "width": 511, "height": 383},
        })
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        directory = cgroup_directory()
        cpu_limit = cpu_max(directory)
        samples = []
        observed_backend = None
        warmups_completed = 0
        captures_completed = 0
        full_captures = 0
        region_captures = 0
        daemon_before = None
        daemon_after = None
        daemon_match_count = None
        daemon_root_match_count = None
        daemon_match_count_after = None
        daemon_root_match_count_after = None
        worker_match_count = None
        worker_match_count_after = None
        daemon_worker_pair_count = None
        daemon_worker_pair_count_after = None
        worker_before = None
        worker_after = None
        daemon_sched_before = None
        daemon_sched_after = None
        worker_sched_before = None
        worker_sched_after = None
        client_sched_before = None
        client_sched_after = None
        cgroup_before = None
        cgroup_after = None
        failure_type = None
        failure_phase = "warmup"

        def request(index, *, measured):
            nonlocal_observed = None
            lane = "full" if index % 2 == 0 else "region"
            path = (
                "/v1/screenshots/full/raw"
                if lane == "full"
                else "/v1/screenshots/region/raw"
            )
            payload = full_payload if lane == "full" else region_payload
            expected_width, expected_height = (1024, 768) if lane == "full" else (511, 383)
            cpu_before_request = cpu_stat(directory) if measured else None
            started_ns = time.perf_counter_ns()
            connection.request("POST", path, body=payload, headers=request_headers)
            wrote_ns = time.perf_counter_ns()
            response = connection.getresponse()
            headers_ns = time.perf_counter_ns()
            payload_content = response.read()
            finished_ns = time.perf_counter_ns()
            cpu_after_request = cpu_stat(directory) if measured else None
            if response.status != 200 or not payload_content.startswith(b"\\x89PNG"):
                raise RuntimeError("daemon-local screenshot failed")
            backend = response.getheader("x-computer-use-capture-backend")
            if backend != "x11-shm":
                raise RuntimeError("daemon-local screenshot used an unexpected source")
            if int(response.getheader("x-computer-use-width", "-1")) != expected_width:
                raise RuntimeError("daemon-local screenshot width changed")
            if int(response.getheader("x-computer-use-height", "-1")) != expected_height:
                raise RuntimeError("daemon-local screenshot height changed")
            if int(response.getheader("x-computer-use-size-bytes", "-1")) != len(payload_content):
                raise RuntimeError("daemon-local screenshot size changed")
            stages = timing_header(response)
            if lane == "region" and stages["cursor_position_ms"] != 0:
                raise RuntimeError("region timing unexpectedly contains cursor work")
            del payload_content
            nonlocal_observed = backend
            if not measured:
                return None, nonlocal_observed
            if cpu_before_request is None or cpu_after_request is None:
                raise RuntimeError("cgroup cpu.stat unavailable")
            cpu_delta = {
                field: cpu_after_request[field] - cpu_before_request[field]
                for field in CGROUP_FIELDS
            }
            if any(value < 0 for value in cpu_delta.values()):
                raise RuntimeError("cgroup cpu.stat counters regressed")
            wall_ms = (finished_ns - started_ns) / 1_000_000
            write_ms = (wrote_ns - started_ns) / 1_000_000
            response_headers_ms = (headers_ns - wrote_ns) / 1_000_000
            body_read_ms = (finished_ns - headers_ns) / 1_000_000
            if wall_ms + 1e-6 < write_ms + response_headers_ms + body_read_ms:
                raise RuntimeError("client timing algebra invalid")
            controller_components = (
                stages["x11_shm_capture_encode_ms"]
                + stages["cursor_position_ms"]
                + stages["hash_ms"]
            )
            controller_unattributed = bounded_residual(
                stages["total_ms"], controller_components
            )
            row = {
                "schedule_index": index,
                "lane": lane,
                "local_wall_ms": wall_ms,
                "request_write_ms": write_ms,
                "response_headers_ms": response_headers_ms,
                "body_read_ms": body_read_ms,
                "controller_total_ms": stages["total_ms"],
                "x11_shm_capture_encode_ms": stages["x11_shm_capture_encode_ms"],
                "cursor_position_ms": stages["cursor_position_ms"],
                "hash_ms": stages["hash_ms"],
                "controller_unattributed_ms": controller_unattributed,
                "route_ready_ms": stages["route_ready_ms"],
                "route_lock_wait_ms": stages["route_lock_wait_ms"],
                "route_operation_ms": stages["route_operation_ms"],
                "route_total_ms": stages["route_total_ms"],
                "route_outside_controller_residual_ms": bounded_residual(
                    stages["route_total_ms"], stages["total_ms"]
                ),
                "local_outside_route_residual_ms": bounded_residual(
                    wall_ms, stages["route_total_ms"]
                ),
                **{
                    "cgroup_" + field + "_delta": value
                    for field, value in cpu_delta.items()
                },
            }
            return row, nonlocal_observed

        try:
            for warmup_index in range(WARMUPS):
                _, observed_backend = request(warmup_index, measured=False)
                warmups_completed += 1
            failure_phase = "identity_before"
            (
                daemon_before,
                worker_before,
                daemon_match_count,
                worker_match_count,
                daemon_worker_pair_count,
                daemon_root_match_count,
            ) = process_identity_pair()
            if daemon_before is None or worker_before is None:
                raise RuntimeError("daemon/worker process identity pair unavailable")
            daemon_sched_before = schedstat(daemon_before["pid"])
            client_sched_before = schedstat(os.getpid())
            worker_sched_before = schedstat(worker_before["pid"])
            failure_phase = "cgroup_before"
            cgroup_before = cpu_stat(directory)
            if cgroup_before is None or cpu_limit is None:
                raise RuntimeError("cgroup v2 cpu metrics unavailable")
            if (
                not isinstance(cpu_limit["quota_usec"], int)
                or cpu_limit["quota_usec"] < 1
                or cpu_limit["quota_usec"] != cpu_limit["period_usec"]
            ):
                raise RuntimeError("scheduling diagnostic requires fixed one CPU")
            failure_phase = "captures"
            for index in range(CAPTURES):
                row, observed_backend = request(index, measured=True)
                samples.append(row)
                captures_completed += 1
                if index % 2 == 0:
                    full_captures += 1
                else:
                    region_captures += 1
            failure_phase = "cgroup_after"
            cgroup_after = cpu_stat(directory)
            if cgroup_after is None:
                raise RuntimeError("terminal cgroup cpu.stat unavailable")
            failure_phase = "identity_after"
            (
                daemon_after,
                worker_after,
                daemon_match_count_after,
                worker_match_count_after,
                daemon_worker_pair_count_after,
                daemon_root_match_count_after,
            ) = process_identity_pair()
            if daemon_after is None or worker_after is None:
                raise RuntimeError("terminal daemon/worker identity pair unavailable")
            daemon_sched_after = (
                schedstat(daemon_after["pid"])
                if daemon_sched_before is not None
                else None
            )
            client_sched_after = (
                schedstat(os.getpid()) if client_sched_before is not None else None
            )
            worker_sched_after = (
                schedstat(worker_after["pid"])
                if worker_sched_before is not None
                else None
            )
            if (
                daemon_after["pid"] != daemon_before["pid"]
                or daemon_after["starttime_ticks"] != daemon_before["starttime_ticks"]
                or (worker_before is None) != (worker_after is None)
                or (
                    worker_before is not None
                    and (
                        worker_after["pid"] != worker_before["pid"]
                        or worker_after["starttime_ticks"]
                        != worker_before["starttime_ticks"]
                    )
                )
            ):
                raise RuntimeError("diagnostic process identity changed")
            failure_phase = "summarize"
            summaries = {
                "combined": summarize(samples),
                "full": summarize([row for row in samples if row["lane"] == "full"]),
                "region": summarize([row for row in samples if row["lane"] == "region"]),
            }
            tail_schedule = {
                metric: [
                    {
                        "schedule_index": row["schedule_index"],
                        "timing_ms": row[metric],
                    }
                    for row in samples
                    if row[metric] > TAIL_THRESHOLDS_MS[0]
                ]
                for metric in TIMING_METRICS
            }
            correlations = {
                timing_metric: {
                    cpu_metric: correlation(samples, timing_metric, cpu_metric)
                    for cpu_metric in CPU_METRICS
                }
                for timing_metric in TIMING_METRICS
            }
            per_request_sums = {
                field: sum(row["cgroup_" + field + "_delta"] for row in samples)
                for field in CGROUP_FIELDS
            }
            cgroup_delta = {
                field: cgroup_after[field] - cgroup_before[field]
                for field in CGROUP_FIELDS
            }
            if (
                any(value < 0 for value in cgroup_delta.values())
                or any(per_request_sums[field] > cgroup_delta[field] for field in CGROUP_FIELDS)
            ):
                raise RuntimeError("cgroup cpu.stat aggregate is inconsistent")
        except Exception as exc:
            failure_type = type(exc).__name__
            summaries = None
            tail_schedule = {}
            correlations = None
            per_request_sums = None
            cgroup_delta = None
        finally:
            connection.close()

        print(json.dumps({
            "passed": failure_type is None,
            "requested_source": "x11-shm",
            "observed_backend": observed_backend,
            "warmups_requested": WARMUPS,
            "warmups_completed": warmups_completed,
            "captures_requested": CAPTURES,
            "captures_completed": captures_completed,
            "full_captures": full_captures,
            "region_captures": region_captures,
            "daemon_identity_before": daemon_before,
            "daemon_identity_after": daemon_after,
            "daemon_match_count": daemon_match_count,
            "daemon_root_match_count": daemon_root_match_count,
            "daemon_match_count_after": daemon_match_count_after,
            "daemon_root_match_count_after": daemon_root_match_count_after,
            "worker_match_count": worker_match_count,
            "worker_match_count_after": worker_match_count_after,
            "daemon_worker_pair_count": daemon_worker_pair_count,
            "daemon_worker_pair_count_after": daemon_worker_pair_count_after,
            "worker_identity_before": worker_before,
            "worker_identity_after": worker_after,
            "daemon_schedstat_before": daemon_sched_before,
            "daemon_schedstat_after": daemon_sched_after,
            "worker_schedstat_before": worker_sched_before,
            "worker_schedstat_after": worker_sched_after,
            "client_schedstat_before": client_sched_before,
            "client_schedstat_after": client_sched_after,
            "cpu_max": cpu_limit,
            "cgroup_cpu_stat_before": cgroup_before,
            "cgroup_cpu_stat_after": cgroup_after,
            "cgroup_cpu_stat_deltas": cgroup_delta,
            "per_request_cgroup_delta_sums": per_request_sums,
            "summaries": summaries,
            "tail_schedule": tail_schedule,
            "correlations": correlations,
            "failure_type": failure_type,
            "failure_phase": failure_phase if failure_type is not None else None,
        }))
        """
    )
    return (
        template.replace("__DAEMON_MATCHER__", _DAEMON_ARGV_MATCHER_SOURCE)
        .replace("__DAEMON_WORKER_SELECTOR__", _DAEMON_WORKER_SELECTOR_SOURCE)
        .replace("__WORKER_MATCHER__", _X11_WORKER_ARGV_MATCHER_SOURCE)
        .replace("__CAPTURES__", str(captures))
        .replace("__WARMUPS__", str(warmups))
    )


async def _run_x11_shm_scheduling_diagnostic(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    captures: int = X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
    warmups: int = X11_SCHEDULING_DIAGNOSTIC_WARMUPS,
) -> dict[str, Any]:
    if captures != X11_SCHEDULING_DIAGNOSTIC_CAPTURES:
        raise ValueError("scheduling diagnostic requires exactly 1000 captures")
    if warmups != X11_SCHEDULING_DIAGNOSTIC_WARMUPS:
        raise ValueError("scheduling diagnostic requires exactly 2 warmups")
    context = factory()
    computer: Any | None = None
    phase = "context_enter"
    observation: dict[str, Any] | None = None
    try:
        computer = await context.__aenter__()
        phase = "sandbox_handle"
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for scheduling diagnostic")
        script = _build_x11_shm_scheduling_diagnostic_script(
            captures=captures,
            warmups=warmups,
        )
        phase = "daemon_local_child"
        process = await sandbox.exec.aio("python", "-c", script, timeout=600)
        exit_code = await process.wait.aio()
        raw = await _process_stdout_text(process)
        if exit_code != 0:
            raise RuntimeError("scheduling diagnostic child exited")
        if not raw:
            raise RuntimeError("scheduling diagnostic child returned empty output")
        phase = "parse_child_output"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("scheduling diagnostic returned invalid output")
        observation = payload
    except Exception as exc:
        failure_phase = phase
        if phase == "context_enter":
            enter_phase = getattr(context, "enter_phase", None)
            if isinstance(enter_phase, str) and enter_phase:
                failure_phase = f"context_enter.{enter_phase}"
        observation = {
            "passed": False,
            "requested_source": "x11-shm",
            "observed_backend": None,
            "warmups_requested": warmups,
            "warmups_completed": 0,
            "captures_requested": captures,
            "captures_completed": 0,
            "full_captures": 0,
            "region_captures": 0,
            "failure_type": type(exc).__name__,
            "failure_phase": failure_phase,
        }
    finally:
        if computer is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                if observation is None:
                    observation = {
                        "passed": False,
                        "requested_source": "x11-shm",
                        "observed_backend": None,
                        "warmups_requested": warmups,
                        "warmups_completed": 0,
                        "captures_requested": captures,
                        "captures_completed": 0,
                        "full_captures": 0,
                        "region_captures": 0,
                    }
                observation["passed"] = False
                observation["failure_type"] = type(exc).__name__
                observation["failure_phase"] = "context_cleanup"
    return observation or {
        "passed": False,
        "requested_source": "x11-shm",
        "warmups_requested": warmups,
        "warmups_completed": 0,
        "captures_requested": captures,
        "captures_completed": 0,
        "full_captures": 0,
        "region_captures": 0,
        "failure_type": "NoResult",
        "failure_phase": phase,
    }


async def _run_x11_shm_stage_attribution_diagnostic(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    captures: int = STAGE_ATTRIBUTION_CAPTURES,
    warmups: int = STAGE_ATTRIBUTION_WARMUPS,
) -> dict[str, Any]:
    if captures != STAGE_ATTRIBUTION_CAPTURES or warmups != STAGE_ATTRIBUTION_WARMUPS:
        raise ValueError("stage attribution diagnostic workload is fixed")
    context = factory()
    computer: Any | None = None
    phase = "context_enter"
    observation: dict[str, Any] | None = None
    try:
        computer = await context.__aenter__()
        phase = "sandbox_handle"
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for stage attribution")
        phase = "private_stage_child"
        process = await sandbox.exec.aio(
            "python",
            "-m",
            "modal_computer_use.benchmarks.x11_shm_stage_attribution",
            "--captures",
            str(captures),
            "--warmups",
            str(warmups),
            timeout=900,
        )
        exit_code = await process.wait.aio()
        raw = await _process_stdout_text(process)
        if exit_code != 0 or not raw:
            raise RuntimeError("stage attribution child failed")
        phase = "parse_child_output"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("stage attribution child returned invalid output")
        payload["target_identity"] = getattr(context, "target_identity", None)
        observation = payload
    except Exception as exc:
        failure_phase = phase
        if phase == "context_enter":
            enter_phase = getattr(context, "enter_phase", None)
            if isinstance(enter_phase, str) and enter_phase:
                failure_phase = f"context_enter.{enter_phase}"
            if isinstance(exc, _TargetRuntimeIdentityError):
                failure_phase = f"{failure_phase}.{exc.safe_phase}"
        observation = {
            "passed": False,
            "warmups_completed": 0,
            "captures_completed": 0,
            "full_captures": 0,
            "region_captures": 0,
            "failure_type": (
                "RuntimeError"
                if isinstance(exc, _TargetRuntimeIdentityError)
                else type(exc).__name__
            ),
            "failure_phase": failure_phase,
        }
    finally:
        if computer is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                if observation is None:
                    observation = {}
                observation.update(
                    {
                        "passed": False,
                        "failure_type": type(exc).__name__,
                        "failure_phase": "context_cleanup",
                    }
                )
    return observation or {
        "passed": False,
        "warmups_completed": 0,
        "captures_completed": 0,
        "full_captures": 0,
        "region_captures": 0,
        "failure_type": "NoResult",
        "failure_phase": phase,
    }


async def _run_x11_shm_daemon_local_tail_diagnostic(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    captures: int = DAEMON_LOCAL_TAIL_CAPTURES,
    warmups: int = DAEMON_LOCAL_TAIL_WARMUPS,
) -> dict[str, Any]:
    if captures != DAEMON_LOCAL_TAIL_CAPTURES:
        raise ValueError("daemon-local tail diagnostic requires exactly 1000 captures")
    if warmups != DAEMON_LOCAL_TAIL_WARMUPS:
        raise ValueError("daemon-local tail diagnostic requires exactly 2 warmups")
    context = factory()
    computer: Any | None = None
    phase = "context_enter"
    observation: dict[str, Any] | None = None
    try:
        computer = await context.__aenter__()
        phase = "sandbox_handle"
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for daemon-local diagnostic")
        script = _build_x11_shm_daemon_local_tail_script(
            captures=captures,
            warmups=warmups,
        )
        phase = "daemon_local_child"
        process = await sandbox.exec.aio("python", "-c", script, timeout=300)
        exit_code = await process.wait.aio()
        raw = await _process_stdout_text(process)
        if exit_code != 0:
            raise RuntimeError("daemon-local diagnostic child exited")
        if not raw:
            raise RuntimeError("daemon-local diagnostic child returned empty output")
        phase = "parse_child_output"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("daemon-local diagnostic returned invalid output")
        observation = payload
    except Exception as exc:
        observation = {
            "passed": False,
            "requested_source": "x11-shm",
            "observed_backend": None,
            "warmups_requested": warmups,
            "warmups_completed": 0,
            "captures_requested": captures,
            "captures_completed": 0,
            "full_captures": 0,
            "region_captures": 0,
            "daemon_identity_before": None,
            "daemon_identity_after": None,
            "summaries": None,
            "tail_schedule": {},
            "failure_type": type(exc).__name__,
            "failure_phase": phase,
        }
    finally:
        if computer is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                if observation is None:
                    observation = {
                        "passed": False,
                        "requested_source": "x11-shm",
                        "observed_backend": None,
                        "warmups_requested": warmups,
                        "warmups_completed": 0,
                        "captures_requested": captures,
                        "captures_completed": 0,
                        "full_captures": 0,
                        "region_captures": 0,
                        "daemon_identity_before": None,
                        "daemon_identity_after": None,
                        "summaries": None,
                        "tail_schedule": {},
                    }
                observation["passed"] = False
                observation["failure_type"] = type(exc).__name__
                observation["failure_phase"] = "context_cleanup"
    return observation or {
        "passed": False,
        "requested_source": "x11-shm",
        "warmups_requested": warmups,
        "warmups_completed": 0,
        "captures_requested": captures,
        "captures_completed": 0,
        "full_captures": 0,
        "region_captures": 0,
        "failure_type": "NoResult",
        "failure_phase": phase,
    }


async def _run_x11_shm_soak_diagnostic(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    captures: int = SOAK_DIAGNOSTIC_CAPTURES,
) -> dict[str, Any]:
    if captures != SOAK_DIAGNOSTIC_CAPTURES:
        raise ValueError("the soak diagnostic is fixed at 10000 captures")
    context = factory()
    computer: Any | None = None
    phase = "context_enter"
    observation: dict[str, Any] | None = None
    try:
        computer = await context.__aenter__()
        phase = "sandbox_handle"
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for X11 soak diagnostic")
        script = _build_x11_shm_soak_diagnostic_script(captures)
        phase = "daemon_local_soak"
        process = await sandbox.exec.aio("python", "-c", script, timeout=900)
        exit_code = await process.wait.aio()
        raw = await _process_stdout_text(process)
        if exit_code != 0:
            raise RuntimeError("X11 soak diagnostic child exited")
        if not raw:
            raise RuntimeError("X11 soak diagnostic child returned empty output")
        phase = "parse_soak_output"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("X11 soak diagnostic returned invalid output")
        observation = payload
    except Exception as exc:
        observation = {
            "passed": False,
            "requested_source": "auto",
            "observed_backend": None,
            "captures_requested": captures,
            "captures_completed": 0,
            "full_captures": 0,
            "region_captures": 0,
            "daemon_identity_before": None,
            "daemon_identity_after": None,
            "counts_before": None,
            "counts_after": None,
            "resource_metrics_before": None,
            "resource_metrics_after": None,
            "failure_resource_metric": None,
            "signed_deltas": None,
            "failure_type": type(exc).__name__,
            "failure_phase": phase,
        }
    finally:
        if computer is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                if observation is None:
                    observation = {
                        "passed": False,
                        "requested_source": "auto",
                        "observed_backend": None,
                        "captures_requested": captures,
                        "captures_completed": 0,
                        "full_captures": 0,
                        "region_captures": 0,
                        "daemon_identity_before": None,
                        "daemon_identity_after": None,
                        "counts_before": None,
                        "counts_after": None,
                        "resource_metrics_before": None,
                        "resource_metrics_after": None,
                        "failure_resource_metric": None,
                        "signed_deltas": None,
                        "failure_type": type(exc).__name__,
                        "failure_phase": "cleanup",
                    }
                else:
                    observation["passed"] = False
                    observation["failure_type"] = type(exc).__name__
                    observation["failure_phase"] = "cleanup"
    return observation or {
        "passed": False,
        "failure_type": "NoResult",
        "failure_phase": phase,
    }


async def _run_x11_shm_resource_snapshot_diagnostic(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
) -> dict[str, Any]:
    context = factory()
    computer: Any | None = None
    phase = "context_enter"
    observation: dict[str, Any] | None = None
    try:
        computer = await context.__aenter__()
        phase = "sandbox_handle"
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for X11 resource snapshot")
        script = _build_x11_shm_soak_diagnostic_script(0, snapshot_only=True)
        phase = "daemon_local_snapshot"
        process = await sandbox.exec.aio("python", "-c", script, timeout=120)
        exit_code = await process.wait.aio()
        raw = await _process_stdout_text(process)
        if exit_code != 0:
            raise RuntimeError("X11 resource snapshot child exited")
        if not raw:
            raise RuntimeError("X11 resource snapshot child returned empty output")
        phase = "parse_resource_snapshot_output"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("X11 resource snapshot returned invalid output")
        observation = payload
    except Exception as exc:
        observation = {
            "passed": False,
            "requested_source": "auto",
            "observed_backend": None,
            "prime_captures": 0,
            "daemon_identity_before": None,
            "counts_before": None,
            "resource_metrics_before": None,
            "failure_resource_metric": None,
            "failure_type": type(exc).__name__,
            "failure_phase": phase,
        }
    finally:
        if computer is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                if observation is None:
                    observation = {
                        "passed": False,
                        "requested_source": "auto",
                        "observed_backend": None,
                        "prime_captures": 0,
                        "daemon_identity_before": None,
                        "counts_before": None,
                        "resource_metrics_before": None,
                        "failure_resource_metric": None,
                        "failure_type": type(exc).__name__,
                        "failure_phase": "cleanup",
                    }
                else:
                    observation["passed"] = False
                    observation["failure_type"] = type(exc).__name__
                    observation["failure_phase"] = "cleanup"
    return observation or {
        "passed": False,
        "prime_captures": 0,
        "failure_type": "NoResult",
        "failure_phase": phase,
    }


def _threshold_timing_metrics(headers: Mapping[str, Any]) -> dict[str, float | None]:
    raw = _threshold_header(headers, "x-computer-use-timing-ms")
    try:
        timings = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("threshold timing header is invalid") from exc
    if not isinstance(timings, Mapping):
        raise ValueError("threshold timing header is invalid")
    parsed: dict[str, float | None] = {}
    for key in ("capture_ms", "encode_ms", "x11_shm_capture_encode_ms"):
        value = timings.get(key)
        if value is None:
            parsed[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("threshold timing header is invalid")
        parsed[key] = float(value)
    for key in ("hash_ms", "total_ms"):
        value = timings.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("threshold timing header is invalid")
        parsed[key] = float(value)
    fused = parsed["x11_shm_capture_encode_ms"]
    if fused is None:
        if parsed["capture_ms"] is None or parsed["encode_ms"] is None:
            raise ValueError("threshold timing header is missing capture stages")
    elif parsed["capture_ms"] is not None or parsed["encode_ms"] is not None:
        raise ValueError("threshold timing header mixed capture stages")
    return parsed


def _threshold_request(spec: Mapping[str, int | float | str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": "png",
        "quality": 90,
        "scale": float(spec["scale"]),
        "show_cursor": False,
        "processing": "daemon",
        "storage": "inline",
    }
    if spec["route"] == "/v1/screenshots/region/raw":
        payload["region"] = {
            "x": int(spec["x"]),
            "y": int(spec["y"]),
            "width": int(spec["width"]),
            "height": int(spec["height"]),
        }
    return payload


def _threshold_header(headers: Mapping[str, Any], name: str) -> Any:
    lowered = name.lower()
    return next(
        (value for key, value in headers.items() if str(key).lower() == lowered),
        None,
    )


def _threshold_int_header(headers: Mapping[str, Any], name: str) -> int | None:
    value = _threshold_header(headers, name)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _threshold_wire_metadata(headers: Mapping[str, Any]) -> dict[str, int | str | None]:
    transfer_encoding = _safe_diagnostic_label(
        _threshold_header(headers, "transfer-encoding")
    )
    return {
        "content_length": _threshold_int_header(headers, "content-length"),
        "transfer_encoding": transfer_encoding,
    }


def _threshold_payload_relation(payload_bytes: int) -> str:
    if payload_bytes < TRANSPORT_THRESHOLD_BYTES:
        return "below"
    if payload_bytes == TRANSPORT_THRESHOLD_BYTES:
        return "at"
    return "above"


def _sanitize_x11_shm_transport_observation(
    observation: Mapping[str, Any],
    *,
    schedule_index: int,
) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise ValueError("threshold observation must be an object")
    if observation.get("case") == "context":
        if observation.get("status") != "failed":
            raise ValueError("threshold context observation status is invalid")
        failure_type = _safe_diagnostic_label(observation.get("failure_type"))
        failure_phase = _safe_diagnostic_label(observation.get("failure_phase"))
        if failure_type is None or failure_phase is None:
            raise ValueError("threshold context failure attribution is invalid")
        return {
            "schedule_index": schedule_index,
            "trial_index": None,
            "case": "context",
            "requested_source": "x11-shm",
            "status": "failed",
            "failure_type": failure_type,
            "failure_phase": failure_phase,
        }
    case_index = schedule_index % len(TRANSPORT_THRESHOLD_SWEEP_SPECS)
    spec = TRANSPORT_THRESHOLD_SWEEP_SPECS[case_index]
    if observation.get("case") != spec["case"]:
        raise ValueError("threshold observations are out of fixed case order")
    if observation.get("trial_index") != schedule_index // len(
        TRANSPORT_THRESHOLD_SWEEP_SPECS
    ):
        raise ValueError("threshold observations have an invalid trial index")
    if observation.get("requested_source") != "x11-shm":
        raise ValueError("threshold observation requested source is invalid")
    if observation.get("public_route") != spec["route"]:
        raise ValueError("threshold observation route does not match its case")
    status = observation.get("status")
    if status not in {"ok", "failed"}:
        raise ValueError("threshold observation status is invalid")
    retained: dict[str, Any] = {
        "schedule_index": schedule_index,
        "trial_index": int(observation["trial_index"]),
        "case": str(spec["case"]),
        "requested_source": "x11-shm",
        "public_route": str(spec["route"]),
        "expected_payload_relation": str(spec["expected_payload_relation"]),
        "status": status,
    }
    if status == "failed":
        failure_type = _safe_diagnostic_label(observation.get("failure_type"))
        failure_phase = _safe_diagnostic_label(observation.get("failure_phase"))
        if failure_type is None or failure_phase is None:
            raise ValueError("threshold failure attribution is invalid")
        retained.update({"failure_type": failure_type, "failure_phase": failure_phase})
        return retained

    backend = _safe_diagnostic_label(observation.get("observed_backend"))
    allowed_backends = {"x11-shm", "mss", "mss-fallback", "scrot", "maim"}
    if backend not in allowed_backends:
        raise ValueError("threshold observation backend is invalid")
    if spec["expected_backend"] == "x11-shm" and backend != "x11-shm":
        raise ValueError("scale-one threshold observation did not use x11-shm")
    if spec["expected_backend"] == "mss" and backend != "mss":
        raise ValueError("scaled threshold observation did not use mss")
    try:
        width = int(observation["width"])
        height = int(observation["height"])
        requested_width = int(observation["requested_width"])
        requested_height = int(observation["requested_height"])
        payload_bytes = int(observation["payload_bytes"])
        trial_scale = float(observation["scale"])
        complete_sdk_ms = float(observation["complete_sdk_ms"])
        residual_ms = float(observation["residual_sdk_minus_daemon_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("threshold observation numeric fields are invalid") from exc
    if (
        width < 1
        or height < 1
        or requested_width != int(spec["width"])
        or requested_height != int(spec["height"])
        or trial_scale != float(spec["scale"])
        or payload_bytes < 1
        or complete_sdk_ms < 0
        or residual_ms < 0
        or not all(math.isfinite(value) for value in (trial_scale, complete_sdk_ms, residual_ms))
        or width != round(requested_width * trial_scale)
        or height != round(requested_height * trial_scale)
    ):
        raise ValueError("threshold observation dimensions or timing are invalid")
    if (
        observation.get("png_signature_validated") is not True
        or observation.get("size_header_validated") is not True
    ):
        raise ValueError("threshold observation PNG signature was not validated")
    relation = observation.get("payload_relation")
    if relation != _threshold_payload_relation(payload_bytes):
        raise ValueError("threshold observation payload relation is invalid")
    if not isinstance(observation.get("daemon_timing_ms"), Mapping):
        raise ValueError("threshold daemon timing is invalid")
    timing: dict[str, float | None] = {}
    for key in ("capture_ms", "encode_ms", "x11_shm_capture_encode_ms", "hash_ms", "total_ms"):
        value = observation["daemon_timing_ms"].get(key)
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("threshold daemon timing is invalid") from exc
            if value < 0 or not math.isfinite(value):
                raise ValueError("threshold daemon timing is invalid")
        timing[key] = value
    if timing["total_ms"] is None:
        raise ValueError("threshold daemon total timing is unavailable")
    metadata = observation.get("response_metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("threshold response metadata is invalid")
    content_length = metadata.get("content_length")
    if content_length is not None:
        try:
            content_length = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("threshold content length is invalid") from exc
        if content_length != payload_bytes:
            raise ValueError("threshold content length does not match payload")
    transfer_encoding = metadata.get("transfer_encoding")
    if transfer_encoding is not None:
        transfer_encoding = _safe_diagnostic_label(transfer_encoding)
        if transfer_encoding is None:
            raise ValueError("threshold transfer encoding is invalid")
    retained.update(
        {
            "observed_backend": backend,
            "width": width,
            "height": height,
            "requested_width": requested_width,
            "requested_height": requested_height,
            "requested_scale": trial_scale,
            "scale": trial_scale,
            "payload_bytes": payload_bytes,
            "payload_relation": relation,
            "png_signature_validated": True,
            "size_header_validated": True,
            "complete_sdk_ms": complete_sdk_ms,
            "daemon_timing_ms": timing,
            "residual_sdk_minus_daemon_ms": residual_ms,
            "response_metadata": {
                "content_length": content_length,
                "transfer_encoding": transfer_encoding,
            },
        }
    )
    return retained


def _build_x11_shm_transport_threshold_diagnostic(
    observations: list[dict[str, Any]],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, str | bool],
    *,
    trials: int = TRANSPORT_THRESHOLD_SWEEP_TRIALS,
) -> dict[str, Any]:
    expected_count = len(TRANSPORT_THRESHOLD_SWEEP_SPECS) * trials
    if len(observations) > expected_count:
        raise ValueError("threshold observations exceed the fixed schedule")
    retained_observations: list[dict[str, Any]] = []
    for schedule_index, observation in enumerate(observations):
        if isinstance(observation, Mapping) and observation.get("case") == "context":
            retained_observations.append(
                _sanitize_x11_shm_transport_observation(
                    observation, schedule_index=schedule_index
                )
            )
            continue
        if schedule_index >= expected_count:
            raise ValueError("threshold observations exceed the fixed schedule")
        expected_spec = TRANSPORT_THRESHOLD_SWEEP_SPECS[
            schedule_index % len(TRANSPORT_THRESHOLD_SWEEP_SPECS)
        ]
        expected_trial = schedule_index // len(TRANSPORT_THRESHOLD_SWEEP_SPECS)
        if (
            not isinstance(observation, Mapping)
            or observation.get("case") != expected_spec["case"]
            or observation.get("trial_index") != expected_trial
            or observation.get("requested_source") != "x11-shm"
            or observation.get("public_route") != expected_spec["route"]
        ):
            raise ValueError("threshold observations are out of fixed case order")
        try:
            retained_observations.append(
                _sanitize_x11_shm_transport_observation(
                    observation, schedule_index=schedule_index
                )
            )
        except (ValueError, TypeError, OverflowError):
            retained_observations.append(
                {
                    "schedule_index": schedule_index,
                    "trial_index": expected_trial,
                    "case": str(expected_spec["case"]),
                    "requested_source": "x11-shm",
                    "public_route": str(expected_spec["route"]),
                    "status": "failed",
                    "failure_type": "EvidenceValidationError",
                    "failure_phase": "artifact_validation",
                }
            )
    failure_count = sum(
        observation.get("status") != "ok" for observation in retained_observations
    )
    retained_cleanup = dict(cleanup)
    return {
        "schema_version": "x11-shm-transport-threshold.v1",
        "benchmark": "x11-shm-transport-threshold",
        "status": "complete",
        "scope": "transport-threshold-mechanism-only",
        "non_gating": True,
        "promotion_proxy": False,
        "requested_source": "x11-shm",
        "public_routes": sorted(
            {str(spec["route"]) for spec in TRANSPORT_THRESHOLD_SWEEP_SPECS}
        ),
        "cases": [
            {
                "case": str(spec["case"]),
                "route": str(spec["route"]),
                "x": int(spec["x"]),
                "y": int(spec["y"]),
                "width": int(spec["width"]),
                "height": int(spec["height"]),
                "scale": float(spec["scale"]),
                "expected_payload_relation": str(
                    spec["expected_payload_relation"]
                ),
                "expected_backend": str(spec["expected_backend"]),
            }
            for spec in TRANSPORT_THRESHOLD_SWEEP_SPECS
        ],
        "threshold_bytes": TRANSPORT_THRESHOLD_BYTES,
        "case_order": [
            str(spec["case"]) for spec in TRANSPORT_THRESHOLD_SWEEP_SPECS
        ],
        "trials_per_case": trials,
        "sample_count": len(observations),
        "expected_sample_count": expected_count,
        "failure_count": failure_count,
        "passed": (
            len(observations) == expected_count
            and failure_count == 0
            and retained_cleanup.get("succeeded") is True
            and retained_cleanup.get("remaining_sandboxes") == 0
        ),
        "retries": 0,
        "replacement_samples": 0,
        "provenance": dict(provenance),
        "observations": retained_observations,
        "terminal_cleanup": retained_cleanup,
    }


async def _run_x11_shm_transport_threshold_diagnostic(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    trials: int = TRANSPORT_THRESHOLD_SWEEP_TRIALS,
    provenance: Mapping[str, str | bool] | None = None,
) -> dict[str, Any]:
    if trials != TRANSPORT_THRESHOLD_SWEEP_TRIALS:
        raise ValueError("transport threshold diagnostic requires exactly 30 trials")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    observations: list[dict[str, Any]] = []
    context = factory()
    computer: Any | None = None
    context_entered = False
    try:
        computer = await context.__aenter__()
        context_entered = True
        client = getattr(computer, "client", None)
        if client is None or not hasattr(client, "post_bytes_with_headers"):
            raise RuntimeError("threshold diagnostic client is unavailable")
        for trial_index in range(trials):
            for schedule_index, spec in enumerate(TRANSPORT_THRESHOLD_SWEEP_SPECS):
                route = str(spec["route"])
                row: dict[str, Any] = {
                    "schedule_index": trial_index * len(TRANSPORT_THRESHOLD_SWEEP_SPECS)
                    + schedule_index,
                    "trial_index": trial_index,
                    "case": str(spec["case"]),
                    "requested_source": "x11-shm",
                    "public_route": route,
                    "expected_payload_relation": str(
                        spec["expected_payload_relation"]
                    ),
                }
                started = time.perf_counter()
                try:
                    data, headers = await client.post_bytes_with_headers(
                        route,
                        json=_threshold_request(spec),
                    )
                    complete_sdk_ms = max(
                        0.0, (time.perf_counter() - started) * 1000.0
                    )
                    if not isinstance(data, bytes) or not data:
                        raise TypeError("threshold response body is invalid")
                    if not isinstance(headers, Mapping):
                        raise TypeError("threshold response headers are invalid")
                    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("threshold response is not PNG")
                    timings = _threshold_timing_metrics(headers)
                    payload_bytes = len(data)
                    declared_size = _threshold_int_header(
                        headers, "x-computer-use-size-bytes"
                    )
                    if declared_size is None or declared_size != payload_bytes:
                        raise ValueError("threshold response size metadata mismatched")
                    dimensions = {
                        "width": _threshold_int_header(
                            headers, "x-computer-use-width"
                        ),
                        "height": _threshold_int_header(
                            headers, "x-computer-use-height"
                        ),
                    }
                    if dimensions["width"] is None or dimensions["height"] is None:
                        raise ValueError("threshold response dimensions are unavailable")
                    daemon_total_ms = timings["total_ms"]
                    if daemon_total_ms is None:
                        raise ValueError("threshold daemon total timing is unavailable")
                    row.update(
                        {
                            "status": "ok",
                            "observed_backend": _safe_diagnostic_label(
                                _threshold_header(
                                    headers, "x-computer-use-capture-backend"
                                )
                            ),
                            "width": dimensions["width"],
                            "height": dimensions["height"],
                            "requested_width": int(spec["width"]),
                            "requested_height": int(spec["height"]),
                            "requested_scale": float(spec["scale"]),
                            "scale": float(spec["scale"]),
                            "payload_bytes": payload_bytes,
                            "payload_relation": _threshold_payload_relation(
                                payload_bytes
                            ),
                            "png_signature_validated": True,
                            "size_header_validated": True,
                            "complete_sdk_ms": round(complete_sdk_ms, 4),
                            "daemon_timing_ms": timings,
                            "residual_sdk_minus_daemon_ms": round(
                                complete_sdk_ms - daemon_total_ms, 4
                            ),
                            "response_metadata": _threshold_wire_metadata(headers),
                        }
                    )
                except Exception as exc:
                    row.update(
                        {
                            "status": "failed",
                            "failure_type": type(exc).__name__,
                            "failure_phase": "capture_or_response_validation",
                        }
                    )
                observations.append(row)
    except Exception as exc:
        observations.append(
            {
                "schedule_index": len(observations),
                "trial_index": None,
                "case": "context",
                "requested_source": "x11-shm",
                "status": "failed",
                "failure_type": type(exc).__name__,
                "failure_phase": "context_enter",
            }
        )
    finally:
        if context_entered:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                observations.append(
                    {
                        "schedule_index": len(observations),
                        "trial_index": None,
                        "case": "context",
                        "requested_source": "x11-shm",
                        "status": "failed",
                        "failure_type": type(exc).__name__,
                        "failure_phase": "context_exit",
                    }
                )
        cleanup = await _final_sandbox_cleanup()
    return _build_x11_shm_transport_threshold_diagnostic(
        observations, cleanup, provenance, trials=trials
    )


def _normalize_placement(placement: Mapping[str, Any]) -> dict[str, str | None]:
    cloud = placement.get("cloud")
    cloud_names = {
        "CLOUD_PROVIDER_AWS": "aws",
        "CLOUD_PROVIDER_GCP": "gcp",
        "CLOUD_PROVIDER_OCI": "oci",
    }
    normalized_cloud = cloud_names.get(cloud, cloud) if isinstance(cloud, str) else None
    region = placement.get("region")
    return {
        "cloud": normalized_cloud,
        "region": region if isinstance(region, str) else None,
    }


def _observed_runner_placement() -> dict[str, str | None]:
    return _normalize_placement(
        {
            "cloud": os.environ.get("MODAL_CLOUD_PROVIDER") or CLOUD,
            "region": os.environ.get("MODAL_REGION") or REGION,
        }
    )


def _observed_rust_target(
    target_identities: Mapping[str, Mapping[str, Any]],
) -> str:
    machines = {str(target_identities[arm].get("machine")) for arm in ("mss", "x11-shm")}
    if len(machines) != 1:
        raise RuntimeError("benchmark arms ran on different CPU architectures")
    machine = machines.pop()
    targets = {
        "x86_64": "x86_64-unknown-linux-gnu",
        "aarch64": "aarch64-unknown-linux-gnu",
    }
    try:
        return targets[machine]
    except KeyError as exc:
        raise RuntimeError("benchmark target architecture is unsupported") from exc


def _promotion_artifact(
    measurement: Mapping[str, Any],
    *,
    samples: int,
    warmups: int,
    target_placements: Mapping[str, Mapping[str, str | None]],
    target_identities: Mapping[str, Mapping[str, Any]],
    concurrency: Mapping[str, Any],
    readiness: Mapping[str, Any],
    failure_matrix: Mapping[str, Any],
    soak: Mapping[str, Any],
    restart: Mapping[str, Any],
    x_server_timeout: Mapping[str, Any],
    region_parity: Mapping[str, Any],
    terminal_cleanup: Mapping[str, Any],
    chromium_fixture_verified: bool,
    provenance: Mapping[str, str | bool],
) -> dict[str, Any]:
    operational = {
        "chromium_fixture": chromium_fixture_verified,
        "failure_matrix": failure_matrix.get("passed") is True,
        "concurrency_matrix": concurrency.get("passed") is True,
        "readiness_parity": readiness.get("passed") is True,
        "x_server_restart": restart.get("passed") is True,
        "bounded_x_server_failure": x_server_timeout.get("passed") is True,
        "region_parity": region_parity.get("passed") is True,
        "captures": soak.get("captures", 0),
        "full_captures": soak.get("full_captures", 0),
        "region_captures": soak.get("region_captures", 0),
        # A failed soak may not have taken either resource snapshot. Keep that
        # state explicit instead of manufacturing a signed count.
        "fd_delta": soak.get("fd_delta"),
        "mapping_delta": soak.get("mapping_delta"),
        "rss_growth_bytes": soak.get("rss_growth_bytes", 0),
        "peak_rss_growth_bytes": soak.get("peak_rss_growth_bytes", 0),
        "cleanup_succeeded": (
            measurement.get("cleanup", {}).get("succeeded") is True
            and terminal_cleanup.get("succeeded") is True
        ),
    }
    artifact = {
        "schema_version": 1,
        "benchmark": "x11-shm-screenshot-promotion",
        "status": "complete",
        "public_call": "await computer.screenshots.full()",
        "preregistration": {
            "samples_per_arm": samples,
            "warmup_iterations": warmups,
            "schedule_seed": SCHEDULE_SEED,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "gates": dict(FIXED_GATES),
            "operational_ceilings": {
                "maximum_readiness_p95_regression_percent": MAX_OPERATIONAL_REGRESSION_PERCENT,
                "maximum_concurrency_p95_regression_percent": MAX_OPERATIONAL_REGRESSION_PERCENT,
                "maximum_rss_growth_bytes": MAX_RSS_GROWTH_BYTES,
                "maximum_fd_delta": 0,
                "maximum_mapping_delta": 0,
            },
        },
        "configuration": {
            "source_revision": provenance["source_revision"],
            "worktree_clean": provenance["worktree_clean"],
            "x11_shm_source_sha256": provenance["x11_shm_source_sha256"],
            "cargo_lock_sha256": provenance["cargo_lock_sha256"],
            "rust_toolchain": RUST_TOOLCHAIN,
            "python_version": platform.python_version(),
            "target": _observed_rust_target(target_identities),
            "image_identity": provenance["image_identity"],
            "image_object_id": target_identities["x11-shm"]["image_object_id"],
            "native_builds": {
                arm: {
                    key: target_identities[arm][key]
                    for key in (
                        "backend",
                        "codec",
                        "module_sha256",
                        "image_object_id",
                        "machine",
                    )
                }
                for arm in ("mss", "x11-shm")
            },
            "requested_placement": {"cloud": CLOUD, "region": REGION},
            "observed_placement": {
                "runner": _observed_runner_placement(),
                "targets": {
                    arm: dict(target_placements.get(arm, {}))
                    for arm in ("mss", "x11-shm")
                },
            },
            "resources": {"cpu": CPU, "memory_mib": MEMORY_MIB},
            "observed_resources": {
                arm: {
                    "cpu": target_identities[arm]["cpu"],
                    "memory_bytes": target_identities[arm]["memory_bytes"],
                }
                for arm in ("mss", "x11-shm")
            },
            "browser": "chromium",
            "browser_launch_args": list(BROWSER_LAUNCH_ARGS),
            "browser_gpu_mode": "off",
            "display": {"width": WIDTH, "height": HEIGHT, "depth": DEPTH},
            "screenshot": {
                "format": "png",
                "lossless": True,
                "show_cursor": False,
                "scale": 1.0,
                "storage": "inline",
            },
            "ingress": "attested-tunnel",
            "http_version": "1.1",
            "connection_reuse": "one-pooled-async-client-per-arm",
        },
        "schedule": measurement["schedule"],
        "arms": {
            arm: {
                "requested_source": "auto" if arm == "x11-shm" else arm,
                "expected_backend": measurement["arms"][arm]["expected_backend"],
                "observations": measurement["arms"][arm]["observations"],
            }
            for arm in ("mss", "x11-shm")
        },
        "fallback_counts": measurement["fallback_counts"],
        "replacement_samples": 0,
        "retries": 0,
        "failures": [],
        "cleanup": {
            "succeeded": (
                measurement.get("cleanup", {}).get("succeeded") is True
                and terminal_cleanup.get("succeeded") is True
            ),
            "remaining_sandboxes": terminal_cleanup.get("remaining_sandboxes"),
            "survivors_before_sweep": terminal_cleanup.get("survivors_before_sweep"),
        },
        "operational_gates": operational,
        "operational_details": {
            "concurrency": dict(concurrency),
            "readiness": dict(readiness),
            "failure_matrix": dict(failure_matrix),
            "soak": dict(soak),
            "x_server_restart": dict(restart),
            "x_server_timeout": dict(x_server_timeout),
            "region_parity": dict(region_parity),
            "terminal_cleanup": dict(terminal_cleanup),
        },
    }
    operational_gates = artifact["operational_gates"]
    failed_operational_gate = any(
        operational_gates.get(gate) is not True
        for gate in (
            "chromium_fixture",
            "failure_matrix",
            "x_server_restart",
            "bounded_x_server_failure",
            "region_parity",
            "cleanup_succeeded",
        )
    ) or any(
        operational_gates.get(key) != expected
        for key, expected in (
            ("captures", 10_000),
            ("full_captures", 5_000),
            ("region_captures", 5_000),
            ("fd_delta", 0),
            ("mapping_delta", 0),
        )
    )
    if failed_operational_gate:
        artifact["status"] = "rejected"
    artifact["promotion"] = evaluate_x11_shm_screenshot_promotion(
        artifact, require_publishable=not failed_operational_gate
    )
    validate_x11_shm_screenshot_artifact(
        artifact, require_publishable=not failed_operational_gate
    )
    return artifact


async def _measure(
    samples: int,
    warmups: int,
    soak_captures: int,
    provenance: Mapping[str, str | bool],
) -> dict[str, Any]:
    from modal_computer_use.benchmarks.full_screenshot_sdk_harness import (
        measure_full_screenshot_arms,
    )

    baseline_pixels: bytes | None = None

    def parity(data: bytes) -> bool:
        nonlocal baseline_pixels
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            image.load()
            pixels = image.convert("RGB").tobytes()
        if baseline_pixels is None:
            baseline_pixels = pixels
            return True
        return pixels == baseline_pixels

    contexts: dict[str, _ArmContext] = {}

    def borrow(arm: str, source: str) -> _ArmContext:
        context = _ArmContext(source)
        contexts[arm] = context
        return context

    measurement = await measure_full_screenshot_arms(
        {
            "mss": lambda: borrow("mss", "mss"),
            "x11-shm": lambda: borrow("x11-shm", "auto"),
        },
        sample_count=samples,
        warmup_iterations=warmups,
        schedule_seed=SCHEDULE_SEED,
        decode_parity=parity,
        expected_capture_backends={"mss": "mss", "x11-shm": "x11-shm"},
    )
    # Operational checks are intentionally outside the timed paired sample.
    concurrency_arms = {
        arm: await _run_concurrency_probe(
            lambda arm=arm: _ArmContext("auto" if arm == "x11-shm" else arm),
            expected_backend=arm,
        )
        for arm in ("mss", "x11-shm")
    }
    concurrency_passed = all(result.get("passed") is True for result in concurrency_arms.values())
    if concurrency_passed:
        baseline_rows = concurrency_arms["mss"]["levels"]
        candidate_rows = concurrency_arms["x11-shm"]["levels"]
        concurrency_passed = all(
            float(candidate["elapsed_p95_ms"])
            <= float(baseline["elapsed_p95_ms"])
            * (1.0 + MAX_OPERATIONAL_REGRESSION_PERCENT / 100.0)
            for baseline, candidate in zip(baseline_rows, candidate_rows, strict=True)
        )
    concurrency = {
        "passed": concurrency_passed,
        "maximum_p95_regression_percent": MAX_OPERATIONAL_REGRESSION_PERCENT,
        "arms": concurrency_arms,
    }
    readiness = await _run_readiness_probe(
        {
            arm: (
                lambda arm=arm: _ArmContext("auto" if arm == "x11-shm" else arm)
            )
            for arm in ("mss", "x11-shm")
        }
    )
    failure_matrix = await _run_x11_shm_failure_matrix(
        lambda: _ArmContext("x11-shm")
    )
    restart = await _run_x_server_restart_probe(lambda: _ArmContext("auto"))
    x_server_timeout = await _run_x_server_timeout_probe(lambda: _ArmContext("auto"))
    region_parity = await _run_region_parity_probe(
        {
            arm: (
                lambda arm=arm: _ArmContext("auto" if arm == "x11-shm" else arm)
            )
            for arm in ("mss", "x11-shm")
        }
    )
    soak = await _run_x11_shm_soak(
        lambda: _ArmContext("auto"), captures=soak_captures
    )
    terminal_cleanup = await _final_sandbox_cleanup()
    target_placements = {
        arm: context.target_placement or {} for arm, context in contexts.items()
    }
    target_identities = {
        arm: context.target_identity or {} for arm, context in contexts.items()
    }
    return _promotion_artifact(
        measurement,
        samples=samples,
        warmups=warmups,
        target_placements=target_placements,
        target_identities=target_identities,
        concurrency=concurrency,
        readiness=readiness,
        failure_matrix=failure_matrix,
        soak=soak,
        restart=restart,
        x_server_timeout=x_server_timeout,
        region_parity=region_parity,
        terminal_cleanup=terminal_cleanup,
        chromium_fixture_verified=all(
            context.fixture_verified for context in contexts.values()
        ),
        provenance=provenance,
    )


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=PROMOTION_RUN_TIMEOUT_SECONDS,
    region=REGION,
    retries=0,
)
def run(
    samples: int = 100,
    warmups: int = 10,
    soak_captures: int = 10_000,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    if samples < 100:
        raise ValueError("samples must be at least 100 per arm")
    if warmups < 10:
        raise ValueError("warmups must be at least 10")
    if soak_captures != 10_000:
        raise ValueError("soak_captures is fixed at 10000")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")
    async def execute() -> dict[str, Any]:
        try:
            return await _measure(samples, warmups, soak_captures, provenance)
        except BaseException as primary:
            try:
                cleanup = await _final_sandbox_cleanup()
            except BaseException as cleanup_error:
                primary.add_note(
                    "terminal benchmark cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
            else:
                if cleanup.get("succeeded") is not True:
                    primary.add_note("terminal benchmark cleanup found live Sandboxes")
            raise

    return asyncio.run(execute())


def _build_repeated_bounded_x_server_diagnostic(
    observations: list[dict[str, Any]],
    cleanup: Mapping[str, Any],
    provenance: Mapping[str, str | bool],
) -> dict[str, Any]:
    retained_observations = [dict(observation) for observation in observations]
    failure_count = sum(
        observation.get("passed") is not True
        for observation in retained_observations
    )
    retained_cleanup = dict(cleanup)
    return {
        "schema_version": "x11-shm-bounded-x-diagnostic.v1",
        "benchmark": "x11-shm-bounded-x-diagnostic",
        "status": "complete",
        "sample_count": len(retained_observations),
        "failure_count": failure_count,
        "passed": (
            failure_count == 0
            and retained_cleanup.get("succeeded") is True
            and retained_cleanup.get("remaining_sandboxes") == 0
        ),
        "retries": 0,
        "replacement_samples": 0,
        "provenance": dict(provenance),
        "observations": retained_observations,
        "terminal_cleanup": retained_cleanup,
    }


async def _run_repeated_bounded_x_server_diagnostic(
    *,
    sample_count: int = BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLES,
    provenance: Mapping[str, str | bool] | None = None,
) -> dict[str, Any]:
    _validate_bounded_x_server_sample_count(sample_count)
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")
    observations: list[dict[str, Any]] = []
    for sample_index in range(sample_count):
        result = await _run_x_server_timeout_probe(lambda: _ArmContext("auto"))
        observations.append({"sample_index": sample_index, **result})
    cleanup = await _final_sandbox_cleanup()
    return _build_repeated_bounded_x_server_diagnostic(
        observations, cleanup, provenance
    )


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=4_200,
    region=REGION,
    retries=0,
)
def run_repeated_bounded_x_server_probe(
    sample_count: int = BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLES,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Run an exact-count bounded-X diagnostic and retain safe observations."""

    _validate_bounded_x_server_sample_count(sample_count)
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")
    return asyncio.run(
        _run_repeated_bounded_x_server_diagnostic(
            sample_count=sample_count, provenance=provenance
        )
    )


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=600,
    region=REGION,
    retries=0,
)
def run_bounded_x_server_probe() -> dict[str, Any]:
    """Run only the preregistered stalled-X failure probe for diagnosis."""

    async def execute() -> dict[str, Any]:
        try:
            return await _run_x_server_timeout_probe(lambda: _ArmContext("auto"))
        finally:
            cleanup = await _final_sandbox_cleanup()
            if cleanup.get("succeeded") is not True:
                raise RuntimeError("bounded X server probe cleanup found live Sandboxes")

    result = asyncio.run(execute())
    print(json.dumps(result, sort_keys=True))
    return result


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=600,
    region=REGION,
    retries=0,
)
def run_x_server_restart_probe() -> dict[str, Any]:
    """Run only display-generation restart recovery for diagnosis."""

    async def execute() -> dict[str, Any]:
        try:
            return await _run_x_server_restart_probe(lambda: _ArmContext("auto"))
        finally:
            cleanup = await _final_sandbox_cleanup()
            if cleanup.get("succeeded") is not True:
                raise RuntimeError("X server restart probe cleanup found live Sandboxes")

    result = asyncio.run(execute())
    print(json.dumps(result, sort_keys=True))
    return result


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=7_200,
    region=REGION,
    retries=0,
)
def run_readiness_replication(
    samples: int = 100,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Run one retained, balanced readiness replication without changing promotion gates."""

    if samples != 100:
        raise ValueError("readiness replication requires exactly 100 samples per arm")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    async def execute() -> dict[str, Any]:
        try:
            readiness = await _run_readiness_probe(
                {
                    arm: (
                        lambda arm=arm: _ArmContext(
                            "auto" if arm == "x11-shm" else arm
                        )
                    )
                    for arm in ("mss", "x11-shm")
                },
                sample_count=samples,
                continue_on_failure=True,
            )
        except BaseException as primary:
            try:
                cleanup = await _final_sandbox_cleanup()
            except BaseException as cleanup_error:
                primary.add_note(
                    "readiness replication cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
            raise

        cleanup = await _final_sandbox_cleanup()
        if cleanup.get("succeeded") is not True:
            raise RuntimeError("readiness replication cleanup found live Sandboxes")
        return {
            "schema_version": "x11-shm-readiness-replication.v1",
            "benchmark": "x11-shm-readiness-replication",
            "sample_count_per_arm": samples,
            "schedule_seed": SCHEDULE_SEED,
            "provenance": provenance,
            "readiness": readiness,
            "terminal_cleanup": cleanup,
        }

    return asyncio.run(execute())


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=4_200,
    region=REGION,
    retries=0,
)
def run_x11_shm_timeout_origin_probe(
    samples: int = 100,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Run 100 fresh candidate captures to classify timeout ownership."""

    if samples != 100:
        raise ValueError("timeout-origin probe requires exactly 100 samples")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    async def execute() -> dict[str, Any]:
        try:
            result = await _run_x11_shm_timeout_origin_probe(sample_count=samples)
        except BaseException as primary:
            try:
                await _final_sandbox_cleanup()
            except BaseException as cleanup_error:
                primary.add_note(
                    "timeout-origin probe cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
            raise

        cleanup = await _final_sandbox_cleanup()
        if cleanup.get("succeeded") is not True:
            raise RuntimeError("timeout-origin probe cleanup found live Sandboxes")
        return {
            "schema_version": "x11-shm-timeout-origin.v1",
            "benchmark": "x11-shm-timeout-origin",
            "retries": 0,
            "provenance": provenance,
            **result,
            "terminal_cleanup": cleanup,
        }

    return asyncio.run(execute())


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=1_200,
    region=REGION,
    retries=0,
)
def run_x11_shm_soak_diagnostic(
    captures: int = SOAK_DIAGNOSTIC_CAPTURES,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Retain one exact X11 soak's signed resource and process identity evidence."""

    if captures != SOAK_DIAGNOSTIC_CAPTURES:
        raise ValueError("the soak diagnostic is fixed at 10000 captures")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    async def execute() -> dict[str, Any]:
        try:
            observation = await _run_x11_shm_soak_diagnostic(
                lambda: _ArmContext("auto"), captures=captures
            )
        except BaseException as exc:
            observation = {
                "passed": False,
                "requested_source": "auto",
                "observed_backend": None,
                "captures_completed": 0,
                "full_captures": 0,
                "region_captures": 0,
                "failure_type": type(exc).__name__,
                "failure_phase": "diagnostic",
            }
        cleanup = await _final_sandbox_cleanup()
        return _build_x11_shm_soak_diagnostic(observation, cleanup, provenance)

    return asyncio.run(execute())


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=300,
    region=REGION,
    retries=0,
)
def run_x11_shm_resource_snapshot_diagnostic(
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Retain one prime-capture daemon resource snapshot with safe attribution."""

    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    async def execute() -> dict[str, Any]:
        try:
            observation = await _run_x11_shm_resource_snapshot_diagnostic(
                lambda: _ArmContext("auto")
            )
        except BaseException as exc:
            observation = {
                "passed": False,
                "requested_source": "auto",
                "observed_backend": None,
                "prime_captures": 0,
                "daemon_identity_before": None,
                "counts_before": None,
                "resource_metrics_before": None,
                "failure_resource_metric": None,
                "failure_type": type(exc).__name__,
                "failure_phase": "diagnostic",
            }
        cleanup = await _final_sandbox_cleanup()
        return _build_x11_shm_resource_snapshot_diagnostic(
            observation, cleanup, provenance
        )

    return asyncio.run(execute())


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=1_200,
    region=REGION,
    retries=0,
)
def run_x11_shm_transport_threshold_diagnostic(
    trials: int = TRANSPORT_THRESHOLD_SWEEP_TRIALS,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Run one non-gating x11-shm wire-threshold mechanism diagnostic."""

    if trials != TRANSPORT_THRESHOLD_SWEEP_TRIALS:
        raise ValueError("transport threshold diagnostic requires exactly 30 trials")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    async def execute() -> dict[str, Any]:
        try:
            return await _run_x11_shm_transport_threshold_diagnostic(
                lambda: _ArmContext("x11-shm"),
                trials=trials,
                provenance=provenance,
            )
        except BaseException as primary:
            try:
                cleanup = await _final_sandbox_cleanup()
            except BaseException as cleanup_error:
                primary.add_note(
                    "transport threshold diagnostic cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
            else:
                if cleanup.get("succeeded") is not True:
                    primary.add_note("transport threshold cleanup found live Sandboxes")
            raise

    return asyncio.run(execute())


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=900,
    region=REGION,
    retries=0,
)
def run_x11_shm_daemon_local_tail_diagnostic(
    captures: int = DAEMON_LOCAL_TAIL_CAPTURES,
    warmups: int = DAEMON_LOCAL_TAIL_WARMUPS,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Run one diagnostic-only daemon-local X11 capture tail campaign."""

    if captures != DAEMON_LOCAL_TAIL_CAPTURES:
        raise ValueError("daemon-local tail diagnostic requires exactly 1000 captures")
    if warmups != DAEMON_LOCAL_TAIL_WARMUPS:
        raise ValueError("daemon-local tail diagnostic requires exactly 2 warmups")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    async def execute() -> dict[str, Any]:
        try:
            observation = await _run_x11_shm_daemon_local_tail_diagnostic(
                lambda: _ArmContext("x11-shm"),
                captures=captures,
                warmups=warmups,
            )
        except BaseException as exc:
            observation = {
                "passed": False,
                "requested_source": "x11-shm",
                "observed_backend": None,
                "warmups_requested": warmups,
                "warmups_completed": 0,
                "captures_requested": captures,
                "captures_completed": 0,
                "full_captures": 0,
                "region_captures": 0,
                "daemon_identity_before": None,
                "daemon_identity_after": None,
                "summaries": None,
                "tail_schedule": {},
                "failure_type": type(exc).__name__,
                "failure_phase": "diagnostic",
            }
        cleanup = await _final_sandbox_cleanup()
        return _build_x11_shm_daemon_local_tail_diagnostic(
            observation,
            cleanup,
            provenance,
        )

    return asyncio.run(execute())


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=1_200,
    region=REGION,
    retries=0,
)
def run_x11_shm_scheduling_diagnostic(
    captures: int = X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
    warmups: int = X11_SCHEDULING_DIAGNOSTIC_WARMUPS,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Run one instrumentation-intrusive, non-gating scheduling diagnostic."""

    if captures != X11_SCHEDULING_DIAGNOSTIC_CAPTURES:
        raise ValueError("scheduling diagnostic requires exactly 1000 captures")
    if warmups != X11_SCHEDULING_DIAGNOSTIC_WARMUPS:
        raise ValueError("scheduling diagnostic requires exactly 2 warmups")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    async def execute() -> dict[str, Any]:
        try:
            observation = await _run_x11_shm_scheduling_diagnostic(
                lambda: _ArmContext("x11-shm"),
                captures=captures,
                warmups=warmups,
            )
        except BaseException as exc:
            observation = {
                "passed": False,
                "requested_source": "x11-shm",
                "observed_backend": None,
                "warmups_requested": warmups,
                "warmups_completed": 0,
                "captures_requested": captures,
                "captures_completed": 0,
                "full_captures": 0,
                "region_captures": 0,
                "failure_type": type(exc).__name__,
                "failure_phase": "diagnostic",
            }
        cleanup = await _final_sandbox_cleanup()
        return _build_x11_shm_scheduling_diagnostic(
            observation,
            cleanup,
            provenance,
        )

    return asyncio.run(execute())


@app.function(
    image=image,
    cpu=1,
    memory=MEMORY_MIB,
    timeout=1_200,
    region=REGION,
    retries=0,
)
def run_x11_shm_stage_attribution_diagnostic(
    captures: int = STAGE_ATTRIBUTION_CAPTURES,
    warmups: int = STAGE_ATTRIBUTION_WARMUPS,
    provenance: dict[str, str | bool] | None = None,
) -> dict[str, Any]:
    """Run one same-Sandbox private source-stage diagnostic."""

    if captures != STAGE_ATTRIBUTION_CAPTURES or warmups != STAGE_ATTRIBUTION_WARMUPS:
        raise ValueError("stage attribution diagnostic workload is fixed")
    if provenance is None:
        raise ValueError("clean local benchmark provenance is required")

    async def execute() -> dict[str, Any]:
        try:
            observation = await _run_x11_shm_stage_attribution_diagnostic(
                lambda: _ArmContext("mss"),
                captures=captures,
                warmups=warmups,
            )
        except BaseException as exc:
            observation = {
                "passed": False,
                "warmups_completed": 0,
                "captures_completed": 0,
                "full_captures": 0,
                "region_captures": 0,
                "failure_type": type(exc).__name__,
                "failure_phase": "diagnostic",
            }
        cleanup = await _final_sandbox_cleanup()
        return build_stage_attribution_artifact(observation, cleanup, provenance)

    return asyncio.run(execute())


@app.local_entrypoint()
def main(
    samples: int = 100,
    warmups: int = 10,
    soak_captures: int = 10_000,
    output: str = "",
) -> None:
    result = run.remote(
        samples=samples,
        warmups=warmups,
        soak_captures=soak_captures,
        provenance=_local_provenance(),
    )
    path = Path(output) if output else Path("benchmark-data/x11-shm-screenshot-promotion.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def readiness_main(
    samples: int = 100,
    output: str = "",
) -> None:
    result = run_readiness_replication.remote(
        samples=samples,
        provenance=_local_provenance(),
    )
    path = (
        Path(output)
        if output
        else Path("benchmark-data/x11-shm-readiness-replication-100.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def timeout_origin_main(
    samples: int = 100,
    output: str = "",
) -> None:
    result = run_x11_shm_timeout_origin_probe.remote(
        samples=samples,
        provenance=_local_provenance(),
    )
    path = (
        Path(output)
        if output
        else Path("benchmark-data/x11-shm-timeout-origin-100.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def x11_shm_soak_diagnostic_main(
    captures: int = SOAK_DIAGNOSTIC_CAPTURES,
    output: str = "",
) -> None:
    result = run_x11_shm_soak_diagnostic.remote(
        captures=captures,
        provenance=_local_provenance(),
    )
    path = (
        Path(output)
        if output
        else Path(f"benchmark-data/x11-shm-soak-diagnostic-{captures}.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def x11_shm_resource_snapshot_main(output: str = "") -> None:
    result = run_x11_shm_resource_snapshot_diagnostic.remote(
        provenance=_local_provenance(),
    )
    path = (
        Path(output)
        if output
        else Path("benchmark-data/x11-shm-resource-snapshot.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def x11_shm_transport_threshold_main(
    trials: int = TRANSPORT_THRESHOLD_SWEEP_TRIALS,
    output: str = "",
) -> None:
    result = run_x11_shm_transport_threshold_diagnostic.remote(
        trials=trials,
        provenance=_local_provenance(),
    )
    path = (
        Path(output)
        if output
        else Path(f"benchmark-data/x11-shm-transport-threshold-{trials}.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def x11_shm_daemon_local_tail_main(
    captures: int = DAEMON_LOCAL_TAIL_CAPTURES,
    warmups: int = DAEMON_LOCAL_TAIL_WARMUPS,
    output: str = "",
) -> None:
    result = run_x11_shm_daemon_local_tail_diagnostic.remote(
        captures=captures,
        warmups=warmups,
        provenance=_local_provenance(),
    )
    path = (
        Path(output)
        if output
        else Path("benchmark-data/x11-shm-daemon-local-tail-1000.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def x11_shm_scheduling_diagnostic_main(
    captures: int = X11_SCHEDULING_DIAGNOSTIC_CAPTURES,
    warmups: int = X11_SCHEDULING_DIAGNOSTIC_WARMUPS,
    output: str = "",
) -> None:
    result = run_x11_shm_scheduling_diagnostic.remote(
        captures=captures,
        warmups=warmups,
        provenance=_local_provenance(),
    )
    path = (
        Path(output)
        if output
        else Path("benchmark-data/x11-shm-scheduling-diagnostic-1000.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def x11_shm_stage_attribution_main(
    captures: int = STAGE_ATTRIBUTION_CAPTURES,
    warmups: int = STAGE_ATTRIBUTION_WARMUPS,
    output: str = "",
) -> None:
    path = (
        Path(output)
        if output
        else Path("benchmark-data/x11-shm-stage-attribution-1000.json")
    )
    if path.exists():
        raise FileExistsError("stage attribution output already exists")
    result = run_x11_shm_stage_attribution_diagnostic.remote(
        captures=captures,
        warmups=warmups,
        provenance=_local_provenance(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


@app.local_entrypoint()
def repeated_bounded_x_server_main(
    sample_count: int = BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLES,
    output: str = "",
) -> None:
    result = run_repeated_bounded_x_server_probe.remote(
        sample_count=sample_count,
        provenance=_local_provenance(),
    )
    path = (
        Path(output)
        if output
        else Path(f"benchmark-data/x11-shm-bounded-x-diagnostic-{sample_count}.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
