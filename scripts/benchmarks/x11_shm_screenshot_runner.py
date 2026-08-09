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
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from textwrap import dedent
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
SCHEDULE_SEED = 20260808
BOOTSTRAP_SEED = 20260808
BOOTSTRAP_RESAMPLES = 1_000
RUST_TOOLCHAIN = "rustc 1.91.0"
CONCURRENCY_LEVELS = (1, 2, 4, 8)
CONCURRENCY_TRIALS = 5
READINESS_SAMPLES = 20
MAX_OPERATIONAL_REGRESSION_PERCENT = 5.0
MAX_RSS_GROWTH_BYTES = 16 * 1024 * 1024
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
                open_url_on_start=FIXTURE_DATA_URL,
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
        )
        try:
            self._computer = await self._context.__aenter__()
            self.target_placement = await self._computer.runtime_placement()
            self.target_identity = await _target_runtime_identity(self._computer)
            status = await self._computer.browser.status()
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

        cpu_quota, cpu_period = read_text("/sys/fs/cgroup/cpu.max").split()
        if cpu_quota == "max":
            raise RuntimeError("target CPU quota is unbounded")
        memory_limit = read_text("/sys/fs/cgroup/memory.max")
        if memory_limit == "max":
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


async def _completed_process_stdout_text(process: Any) -> str:
    exit_code = await process.wait.aio()
    raw = await _process_stdout_text(process)
    if exit_code != 0:
        raise RuntimeError(f"benchmark subprocess exited with status {exit_code}")
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
) -> dict[str, Any]:
    """Measure paired fresh create-to-ready samples for both capture sources."""

    samples: dict[str, list[float]] = {"mss": [], "x11-shm": []}
    rng = random.Random(SCHEDULE_SEED)  # noqa: S311 - reproducible benchmark order.
    failure_type: str | None = None
    cleanup_failure_type: str | None = None
    try:
        for _ in range(READINESS_SAMPLES):
            pair = ["mss", "x11-shm"]
            rng.shuffle(pair)
            for arm in pair:
                context = factories[arm]()
                computer: Any | None = None
                started = time.perf_counter()
                try:
                    computer = await context.__aenter__()
                    samples[arm].append((time.perf_counter() - started) * 1000.0)
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
                    try:
                        await computer.screenshots.full()
                    finally:
                        computer.client.post_bytes_with_headers = original
                    if backends != [arm]:
                        raise RuntimeError("fresh readiness used an unexpected source")
                finally:
                    if computer is not None:
                        try:
                            await context.__aexit__(None, None, None)
                        except Exception as exc:
                            cleanup_failure_type = type(exc).__name__
                            raise
    except Exception as exc:
        failure_type = type(exc).__name__

    arms = {
        arm: {
            "passed": len(values) == READINESS_SAMPLES,
            "source": arm,
            "samples": len(values),
            "startup_p50_ms": round(statistics.median(values), 4) if values else 0.0,
            "startup_p95_ms": round(_percentile(values, 0.95), 4) if values else 0.0,
            "capture_backend": arm,
        }
        for arm, values in samples.items()
    }
    passed = failure_type is None and cleanup_failure_type is None
    if passed:
        passed = float(arms["x11-shm"]["startup_p95_ms"]) <= float(
            arms["mss"]["startup_p95_ms"]
        ) * (1.0 + MAX_OPERATIONAL_REGRESSION_PERCENT / 100.0)
    return {
        "passed": passed,
        "maximum_p95_regression_percent": MAX_OPERATIONAL_REGRESSION_PERCENT,
        "arms": arms,
        **({"failure_type": failure_type} if failure_type else {}),
        **(
            {"cleanup_failure_type": cleanup_failure_type}
            if cleanup_failure_type
            else {}
        ),
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

        before, backend_before = await capture_with_backend()
        restarted = await computer.lifecycle.restart()
        ready = False
        for _ in range(100):
            status = await computer.lifecycle.status()
            if status.ready:
                ready = True
                break
            await asyncio.sleep(0.1)
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
    result: dict[str, Any] | None = None
    resumed = False
    try:
        computer = await context.__aenter__()
        sandbox = getattr(computer, "_sandbox", None)
        if sandbox is None or not hasattr(sandbox, "exec"):
            raise RuntimeError("sandbox handle unavailable for X server timeout probe")
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
            "python", "-c", constructor_probe, timeout=3
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
        started = time.perf_counter()
        failed_bounded = False
        public_error_type: str | None = None
        public_error_code: str | None = None
        public_error_detail_type: str | None = None
        try:
            await asyncio.wait_for(computer.screenshots.full(), timeout=3.0)
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
        resume = await sandbox.exec.aio(
            "sh", "-c", 'kill -CONT "$1"', "sh", xvfb_pid, timeout=10
        )
        resume_exit = await resume.wait.aio()
        if resume_exit != 0:
            raise RuntimeError("Xvfb resume command failed")
        resumed = True
        restarted = await computer.lifecycle.restart()
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
    if failure_type is not None or cleanup_type is not None:
        return {
            "passed": False,
            **({"failure_type": failure_type} if failure_type else {}),
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

            class ConstructorFailure:
                calls = 0
                def __init__(self, *_args):
                    ConstructorFailure.calls += 1
                    raise RuntimeError("attach failed")

            class CaptureFailure:
                calls = 0
                def __init__(self, *_args):
                    CaptureFailure.calls += 1
                def capture_png(self, *_args):
                    raise RuntimeError("GetImage failed")
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

            checks["constructor_failure_falls_back_once"] = asyncio.run(
                fallback_check(ConstructorFailure)
            )
            checks["capture_failure_falls_back_once"] = asyncio.run(
                fallback_check(CaptureFailure)
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
            "constructor_failure_falls_back_once",
            "capture_failure_falls_back_once",
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
        script = dedent(
            f"""
            import http.client
            import json
            import os
            from pathlib import Path

            def daemon_pid():
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        command = (entry / "cmdline").read_bytes()
                    except OSError:
                        continue
                    if b"modal_computer_use.daemon" in command:
                        return int(entry.name)
                raise RuntimeError("daemon process was not found")

            def status_bytes(pid, key):
                with open(f"/proc/{{pid}}/status", encoding="utf-8") as status_file:
                    for line in status_file:
                        if line.startswith(key + ":"):
                            return int(line.split()[1]) * 1024
                raise RuntimeError(f"{{key}} missing for daemon process")

            def counts(pid):
                with open(f"/proc/{{pid}}/maps", encoding="utf-8") as maps_file:
                    mappings = sum(1 for _ in maps_file)
                return {{
                    "fd": len(os.listdir(f"/proc/{{pid}}/fd")),
                    "mappings": mappings,
                    "rss": status_bytes(pid, "VmRSS"),
                    "peak_rss": status_bytes(pid, "VmHWM"),
                }}

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

            def capture(path, body, expected_width, expected_height):
                connection.request("POST", path, body=body, headers=headers)
                response = connection.getresponse()
                data = response.read()
                if response.status != 200 or not data.startswith(b"\\x89PNG"):
                    raise RuntimeError("daemon-local screenshot failed")
                if response.getheader("x-computer-use-capture-backend") != "x11-shm":
                    raise RuntimeError("daemon-local screenshot used an unexpected source")
                if int(response.getheader("x-computer-use-width", "-1")) != expected_width:
                    raise RuntimeError("daemon-local screenshot width changed")
                if int(response.getheader("x-computer-use-height", "-1")) != expected_height:
                    raise RuntimeError("daemon-local screenshot height changed")

            capture("/v1/screenshots/full/raw", full, 1024, 768)
            capture("/v1/screenshots/region/raw", region, 511, 383)
            pid = daemon_pid()
            before = counts(pid)
            full_captures = 0
            region_captures = 0
            try:
                for index in range({captures}):
                    if index % 2:
                        capture("/v1/screenshots/region/raw", region, 511, 383)
                        region_captures += 1
                    else:
                        capture("/v1/screenshots/full/raw", full, 1024, 768)
                        full_captures += 1
                after = counts(pid)
            finally:
                connection.close()
            print(json.dumps({{
                "captures": {captures},
                "full_captures": full_captures,
                "region_captures": region_captures,
                "fd_before": before["fd"], "fd_after": after["fd"],
                "mapping_before": before["mappings"],
                "mapping_after": after["mappings"],
                "rss_before_bytes": before["rss"],
                "rss_after_bytes": after["rss"],
                "peak_rss_before_bytes": before["peak_rss"],
                "peak_rss_after_bytes": after["peak_rss"],
            }}))
            """
        )
        process = await sandbox.exec.aio("python", "-c", script, timeout=900)
        raw = await _completed_process_stdout_text(process)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("X11 shared-memory soak returned invalid output")
        rss_growth = int(payload["rss_after_bytes"]) - int(payload["rss_before_bytes"])
        peak_growth = int(payload["peak_rss_after_bytes"]) - int(
            payload["peak_rss_before_bytes"]
        )
        result = {
            "passed": (
                int(payload["captures"]) == captures
                and int(payload["full_captures"]) == captures // 2
                and int(payload["region_captures"]) == captures // 2
                and int(payload["fd_after"]) - int(payload["fd_before"]) == 0
                and int(payload["mapping_after"]) - int(payload["mapping_before"]) == 0
                and rss_growth <= 16 * 1024 * 1024
                and peak_growth <= 16 * 1024 * 1024
            ),
            "captures": captures,
            "full_captures": int(payload["full_captures"]),
            "region_captures": int(payload["region_captures"]),
            "fd_delta": int(payload["fd_after"]) - int(payload["fd_before"]),
            "mapping_delta": int(payload["mapping_after"]) - int(payload["mapping_before"]),
            "rss_growth_bytes": max(0, rss_growth),
            "peak_rss_growth_bytes": max(0, peak_growth),
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


def _observed_runner_placement() -> dict[str, str | None]:
    return {
        "cloud": os.environ.get("MODAL_CLOUD_PROVIDER") or CLOUD,
        "region": os.environ.get("MODAL_REGION") or REGION,
    }


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
        "fd_delta": soak.get("fd_delta", -1),
        "mapping_delta": soak.get("mapping_delta", -1),
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
                arm: dict(target_identities[arm]) for arm in ("mss", "x11-shm")
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
    artifact["promotion"] = evaluate_x11_shm_screenshot_promotion(artifact)
    validate_x11_shm_screenshot_artifact(artifact)
    return artifact


async def _measure(
    samples: int,
    warmups: int,
    soak_captures: int,
    provenance: Mapping[str, str | bool],
) -> dict[str, Any]:
    sys.path.insert(0, "/opt/mcu-scripts")
    from benchmarks.full_screenshot_sdk_harness import measure_full_screenshot_arms

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
    timeout=1_800,
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
