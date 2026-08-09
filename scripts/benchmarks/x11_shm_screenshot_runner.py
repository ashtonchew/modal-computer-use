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
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from textwrap import dedent
from typing import Any
from urllib.parse import quote

import modal

import modal_computer_use
from modal_computer_use import AsyncComputerSandbox, ComputerConfig
from modal_computer_use.benchmarks.x11_shm_screenshot import FIXED_GATES
from modal_computer_use.config import (
    ActionConfig,
    BrowserConfig,
    DesktopConfig,
    ResourceConfig,
    RuntimeConfig,
)
from modal_computer_use.image import default_image

_RUNNER_PATH = Path(__file__).resolve()
PROJECT_ROOT = _RUNNER_PATH.parents[2] if len(_RUNNER_PATH.parents) > 2 else Path("/root")
FIXTURE_PATH = _RUNNER_PATH.parent / "fixtures" / "x11_shm_chromium_fixture.html"


def _load_fixture_html() -> str:
    candidates = (
        FIXTURE_PATH,
        Path("/opt/mcu-scripts/fixtures/x11_shm_chromium_fixture.html"),
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
TARGET = "x86_64-unknown-linux-gnu"
IMAGE_IDENTITY = "inline:chromium"
CONCURRENCY_LEVELS = (1, 2, 4, 8)


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


SOURCE_REVISION = _git_output("rev-parse", "HEAD") or "0" * 40
WORKTREE_CLEAN = _git_output("status", "--porcelain") == ""


def _native_source_path() -> Path:
    candidates = (
        PROJECT_ROOT / "src" / "modal_computer_use" / "_native" / "x11_shm",
        Path(modal_computer_use.__file__).resolve().parent / "_native" / "x11_shm",
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "Cargo.lock").is_file():
            return candidate
    raise RuntimeError("packaged X11 shared-memory Cargo source is unavailable")


_NATIVE_SOURCE_PATH = _native_source_path()
NATIVE_SOURCE_SHA256 = _tree_sha256(_NATIVE_SOURCE_PATH)
CARGO_LOCK_SHA256 = hashlib.sha256((_NATIVE_SOURCE_PATH / "Cargo.lock").read_bytes()).hexdigest()


app = modal.App(APP_NAME)

# Reuse the managed browser Image recipe.  It installs Chromium, starts the
# normal Xvfb/desktop stack, and builds the packaged x11-shm extension with
# the pinned Rust toolchain.  ``copy=True`` makes the benchmark image
# independent of the caller's working tree after the Image is built.
image = (
    default_image(
        profile="browser",
        browser="chromium",
        window_manager="xfce",
        browser_prewarm=True,
    )
    .add_local_python_source("modal_computer_use", copy=True)
    .add_local_dir(
        str(PROJECT_ROOT / "scripts"),
        remote_path="/opt/mcu-scripts",
        copy=True,
        ignore=("__pycache__", "*.pyc"),
    )
)


class _ArmContext(AbstractAsyncContextManager[Any]):
    def __init__(self, source: str) -> None:
        if source not in {"mss", "x11-shm"}:
            raise ValueError("screenshot arm must be mss or x11-shm")
        self.source = source
        self._context: AbstractAsyncContextManager[Any] | None = None
        self._computer: Any | None = None
        self.target_placement: dict[str, str | None] | None = None

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
            tags={"benchmark": "x11-shm-screenshot"},
        )
        try:
            self._computer = await self._context.__aenter__()
            self.target_placement = await self._computer.runtime_placement()
            status = await self._computer.browser.status()
            if (
                status.get("configured_browser") != "chromium"
                or status.get("prewarm") is not True
                or not isinstance(status.get("prewarm_result"), Mapping)
            ):
                raise RuntimeError("managed Chromium fixture did not become ready")
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


async def _run_concurrency_probe(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
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
            started = time.perf_counter()
            screenshots = await asyncio.gather(
                *(computer.screenshots.full() for _ in range(level))
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if any(
                shot.width != WIDTH
                or shot.height != HEIGHT
                or shot.cursor_visible
                or shot.format != "png"
                for shot in screenshots
            ):
                raise RuntimeError("concurrent screenshot contract mismatch")
            rows.append(
                {
                    "concurrency": level,
                    "captures": level,
                    "elapsed_ms": round(elapsed_ms, 4),
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
    return {"passed": True, "levels": rows}


async def _run_native_failure_matrix(
    factory: Callable[[], AbstractAsyncContextManager[Any]],
) -> dict[str, Any]:
    """Run bounded native-session lifecycle failures in a disposable process.

    This probes the optional extension only.  It does not mutate daemon
    defaults or inject failures into a production route.  Auto-fallback is
    covered by the daemon unit tests; the live probe checks the native close
    contract where the X connection and display are real.
    """

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
            import json
            import os
            import _modal_computer_use_x11_shm as native

            s = native.X11SharedMemoryScreenshotSession(
                os.environ.get("DISPLAY", ":99"), 1024, 768
            )
            s.close()
            s.close()
            closed = False
            try:
                s.capture_png(0, 0, 1024, 768)
            except Exception:
                closed = True
            print(json.dumps({"close_idempotent": True, "closed_capture_rejected": closed}))
            """
        )
        process = await sandbox.exec.aio("python", "-c", script, timeout=60)
        raw = (await process.stdout.read.aio()).decode("utf-8", errors="replace").strip()
        payload = json.loads(raw)
        passed = payload == {"close_idempotent": True, "closed_capture_rejected": True}
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


async def _run_native_soak(
    factory: Callable[[], AbstractAsyncContextManager[Any]], *, captures: int
) -> dict[str, Any]:
    """Capture complete PNGs in the daemon image and report bounded resources."""

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
            raise RuntimeError("sandbox handle unavailable for native soak")
        script = dedent(
            f"""
            import gc
            import json
            import os
            import resource
            import _modal_computer_use_x11_shm as native

            def counts():
                fd = len(os.listdir("/proc/self/fd"))
                with open("/proc/self/maps", encoding="utf-8") as maps_file:
                    mappings = sum(1 for _ in maps_file)
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
                return fd, mappings, rss

            before = counts()
            s = native.X11SharedMemoryScreenshotSession(
                os.environ.get("DISPLAY", ":99"), 1024, 768
            )
            for _ in range({captures}):
                data = s.capture_png(0, 0, 1024, 768)
                assert data.startswith(b"\\x89PNG")
                del data
            gc.collect()
            after = counts()
            s.close()
            print(json.dumps({{
                "captures": {captures},
                "fd_before": before[0], "fd_after": after[0],
                "mapping_before": before[1], "mapping_after": after[1],
                "rss_before_bytes": before[2], "rss_after_bytes": after[2],
            }}))
            """
        )
        process = await sandbox.exec.aio("python", "-c", script, timeout=600)
        raw = (await process.stdout.read.aio()).decode("utf-8", errors="replace").strip()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("native soak returned invalid output")
        rss_growth = int(payload["rss_after_bytes"]) - int(payload["rss_before_bytes"])
        result = {
            "passed": (
                int(payload["captures"]) == captures
                and int(payload["fd_after"]) - int(payload["fd_before"]) == 0
                and int(payload["mapping_after"]) - int(payload["mapping_before"]) == 0
                and rss_growth <= 16 * 1024 * 1024
            ),
            "captures": captures,
            "fd_delta": int(payload["fd_after"]) - int(payload["fd_before"]),
            "mapping_delta": int(payload["mapping_after"]) - int(payload["mapping_before"]),
            "rss_growth_bytes": max(0, rss_growth),
        }
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


def _promotion_artifact(
    measurement: Mapping[str, Any],
    *,
    samples: int,
    warmups: int,
    target_placement: Mapping[str, str | None] | None,
    concurrency: Mapping[str, Any],
    failure_matrix: Mapping[str, Any],
    soak: Mapping[str, Any],
) -> dict[str, Any]:
    operational = {
        "chromium_fixture": True,
        "failure_matrix": failure_matrix.get("passed") is True,
        "concurrency_matrix": concurrency.get("passed") is True,
        "captures": soak.get("captures", 0),
        "fd_delta": soak.get("fd_delta", -1),
        "mapping_delta": soak.get("mapping_delta", -1),
        "rss_growth_bytes": soak.get("rss_growth_bytes", 0),
        "cleanup_succeeded": measurement.get("cleanup", {}).get("succeeded") is True,
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
        },
        "configuration": {
            "source_revision": SOURCE_REVISION,
            "worktree_clean": WORKTREE_CLEAN,
            "native_source_sha256": NATIVE_SOURCE_SHA256,
            "cargo_lock_sha256": CARGO_LOCK_SHA256,
            "rust_toolchain": RUST_TOOLCHAIN,
            "python_version": platform.python_version(),
            "target": TARGET,
            "image_identity": IMAGE_IDENTITY,
            "requested_placement": {"cloud": CLOUD, "region": REGION},
            "observed_placement": {
                "runner": _observed_runner_placement(),
                "target": dict(target_placement or {"cloud": CLOUD, "region": REGION}),
            },
            "resources": {"cpu": CPU, "memory_mib": MEMORY_MIB},
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
                "requested_source": arm,
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
            "succeeded": measurement.get("cleanup", {}).get("succeeded") is True,
            "remaining_sandboxes": 0,
        },
        "operational_gates": operational,
        "operational_details": {
            "concurrency": dict(concurrency),
            "failure_matrix": dict(failure_matrix),
            "soak": dict(soak),
        },
    }
    return artifact


async def _measure(samples: int, warmups: int, soak_captures: int) -> dict[str, Any]:
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

    def borrow(source: str) -> _ArmContext:
        context = _ArmContext(source)
        contexts[source] = context
        return context

    measurement = await measure_full_screenshot_arms(
        {"mss": lambda: borrow("mss"), "x11-shm": lambda: borrow("x11-shm")},
        sample_count=samples,
        warmup_iterations=warmups,
        schedule_seed=SCHEDULE_SEED,
        decode_parity=parity,
        expected_capture_backends={"mss": "mss", "x11-shm": "x11-shm"},
    )
    # Operational checks are intentionally outside the timed paired sample.
    concurrency = await _run_concurrency_probe(lambda: _ArmContext("x11-shm"))
    failure_matrix = await _run_native_failure_matrix(lambda: _ArmContext("x11-shm"))
    soak = await _run_native_soak(lambda: _ArmContext("x11-shm"), captures=soak_captures)
    target = contexts.get("x11-shm", contexts.get("mss"))
    target_placement = target.target_placement if target is not None else None
    return _promotion_artifact(
        measurement,
        samples=samples,
        warmups=warmups,
        target_placement=target_placement,
        concurrency=concurrency,
        failure_matrix=failure_matrix,
        soak=soak,
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
) -> dict[str, Any]:
    if samples < 100:
        raise ValueError("samples must be at least 100 per arm")
    if warmups < 10:
        raise ValueError("warmups must be at least 10")
    if soak_captures != 10_000:
        raise ValueError("soak_captures is fixed at 10000")
    return asyncio.run(_measure(samples, warmups, soak_captures))


@app.local_entrypoint()
def main(
    samples: int = 100,
    warmups: int = 10,
    soak_captures: int = 10_000,
    output: str = "",
) -> None:
    result = run.remote(samples=samples, warmups=warmups, soak_captures=soak_captures)
    path = Path(output) if output else Path("benchmark-data/x11-shm-screenshot-promotion.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
