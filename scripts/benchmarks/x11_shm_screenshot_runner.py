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
from modal_computer_use.config import (
    ActionConfig,
    BrowserConfig,
    DesktopConfig,
    ResourceConfig,
    RuntimeConfig,
)
from modal_computer_use.errors import DaemonHTTPError
from modal_computer_use.image import default_image
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
    default_image(
        profile="browser",
        browser="chromium",
        window_manager="xfce",
        browser_prewarm=True,
    )
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
        raise RuntimeError("sandbox handle unavailable for target identity")
    script = dedent(
        """
        import hashlib
        import json
        import os
        import platform
        from pathlib import Path

        import _modal_computer_use_x11_shm as native

        def read_text(path):
            return Path(path).read_text(encoding="utf-8").strip()

        def first_text(paths):
            for path in paths:
                if Path(path).is_file():
                    return read_text(path)
            raise RuntimeError(f"target cgroup limit is unavailable: {paths}")

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
        memory_limit = first_text((
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ))
        if memory_limit in {"max", "-1"}:
            raise RuntimeError("target memory limit is unbounded")
        module_bytes = Path(native.__file__).read_bytes()
        print(json.dumps({
            "backend": native.backend,
            "codec": native.codec,
            "module_sha256": hashlib.sha256(module_bytes).hexdigest(),
            "image_object_id": os.environ.get("MODAL_IMAGE_ID"),
            "cpu": int(cpu_quota) / int(cpu_period),
            "memory_bytes": int(memory_limit),
            "machine": platform.machine(),
        }, sort_keys=True))
        """
    )
    process = await sandbox.exec.aio("python", "-c", script, timeout=30)
    raw = await _completed_process_stdout_text(process)
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or payload.get("backend") != "x11-shm"
        or payload.get("codec") != "png-deflate-level1-fixed-up"
        or not isinstance(payload.get("module_sha256"), str)
        or not str(payload.get("image_object_id", "")).startswith("im-")
    ):
        raise RuntimeError("target native build identity is invalid")
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


_DAEMON_ARGV_MATCHER_SOURCE = dedent(
    inspect.getsource(_is_modal_daemon_cmdline)
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
