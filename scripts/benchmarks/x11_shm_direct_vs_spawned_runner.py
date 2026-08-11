#!/usr/bin/env python3
"""Run the private same-Sandbox direct/native-vs-spawned-worker probe.

The target Sandbox is created once, so both sessions share one Xvfb and one
browser.  The child owns the persistent native sessions and emits only the
sanitized observation contract from
``modal_computer_use.benchmarks.x11_shm_direct_vs_spawned``.  This runner is
benchmark-only and never changes the public screenshot source or its defaults.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

import modal

import modal_computer_use
from modal_computer_use import AsyncComputerSandbox, ComputerConfig
from modal_computer_use.benchmarks.x11_shm_direct_vs_spawned import (
    PAIRS,
    WARMUPS,
    build_artifact,
)
from modal_computer_use.config import (
    ActionConfig,
    BrowserConfig,
    DesktopConfig,
    ResourceConfig,
    RuntimeConfig,
)
from modal_computer_use.image import _named_image_recipe

APP_NAME = "mcu-x11-shm-direct-vs-spawned"
REGION = "us-west-2"
ENVIRONMENT = "main"
CPU = 1.0
MEMORY_MIB = 2048
WIDTH = 1024
HEIGHT = 768
DEPTH = 24
RUN_TAG = f"x11-shm-direct-vs-spawned-{uuid.uuid4().hex}"

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
    # Keep a self-contained fallback for a shallow relocated script.  The
    # mounted fixture is preferred so the local/remote page remains identical.
    return (
        "<html><body style='margin:0;background:#fff'>"
        "<div style='width:511px;height:383px;background:#fff'></div>"
        "</body></html>"
    )


_FIXTURE_DATA_URL = "data:text/html;charset=utf-8," + quote(_load_fixture_html(), safe="")


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


NATIVE_SOURCE = _native_source_path()

app = modal.App(APP_NAME)
image = _named_image_recipe(variant="chromium", window_manager="xfce").add_local_dir(
    str(PROJECT_ROOT / "scripts"),
    remote_path="/opt/mcu-scripts",
    copy=False,
    ignore=("__pycache__", "*.pyc"),
)


def _git_output(*args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed local Git metadata command.
            ("git", *args),  # noqa: S607 - fixed executable name for local metadata.
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


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


def local_provenance() -> dict[str, str | bool]:
    revision = _git_output("rev-parse", "HEAD")
    clean = _git_output("status", "--porcelain") == ""
    if revision is None or len(revision) != 40 or not clean:
        raise RuntimeError("direct/spawned diagnostic requires a clean source revision")
    return {
        "source_revision": revision,
        "worktree_clean": True,
        "x11_shm_source_sha256": _tree_sha256(NATIVE_SOURCE),
        "cargo_lock_sha256": hashlib.sha256(
            (NATIVE_SOURCE / "Cargo.lock").read_bytes()
        ).hexdigest(),
        "image_identity": "inline:browser-chromium-x11-shm",
    }


class _TargetContext(AbstractAsyncContextManager[Any]):
    """One managed Chromium/Xvfb Sandbox shared by both diagnostic arms."""

    def __init__(self) -> None:
        self._context: AbstractAsyncContextManager[Any] | None = None
        self.computer: Any | None = None

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
                launch_args=[
                    "--kiosk",
                    "--window-position=0,0",
                    "--window-size=1024,768",
                    "--force-device-scale-factor=1",
                    "--no-first-run",
                    "--disable-session-crashed-bubble",
                    "--disable-infobars",
                ],
                open_url_on_start=_FIXTURE_DATA_URL,
                gpu_mode="off",
            ),
            runtime=RuntimeConfig(
                modal_environment=ENVIRONMENT,
                modal_region=REGION,
                timeout_seconds=3_600,
                readiness_timeout_seconds=180,
            ),
            resources=ResourceConfig(
                profile="browser",
                cpu=CPU,
                memory_mib=MEMORY_MIB,
            ),
            actions=ActionConfig(
                input_backend="xtest",
                # The discriminator owns exactly one direct native session
                # and one normal spawned worker.  Keep the daemon on MSS
                # so it does not create a third idle X11-SHM worker.
                screenshot_capture_source="mss",
            ),
            ingress="attested-tunnel",
            expose_vnc="off",
        )
        self._context = AsyncComputerSandbox.create(
            config=config,
            app_name=APP_NAME,
            image=image,
            owner=APP_NAME,
            tags={"benchmark_run": RUN_TAG},
            cpu=(CPU, CPU),
            memory=(MEMORY_MIB, MEMORY_MIB),
        )
        try:
            self.computer = await self._context.__aenter__()
            status = await self.computer.browser.status()
            prewarm_result = status.get("prewarm_result") if isinstance(status, Mapping) else None
            if (
                not isinstance(status, Mapping)
                or status.get("configured_browser") != "chromium"
                or status.get("prewarm") is not True
                or not isinstance(prewarm_result, Mapping)
                or prewarm_result.get("ok") is not True
                or status.get("open_url_on_start") != _FIXTURE_DATA_URL
                or not isinstance(status.get("windows"), int)
                or status["windows"] < 1
            ):
                raise RuntimeError("managed Chromium fixture did not become ready")
            return self.computer
        except BaseException as exc:
            context, self._context = self._context, None
            if context is not None:
                with suppress(Exception):
                    await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._context is not None:
            await self._context.__aexit__(exc_type, exc, traceback)
        self._context = None
        self.computer = None


async def _read_stdout(process: Any) -> str:
    raw = await process.stdout.read.aio()
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    if isinstance(raw, str):
        return raw.strip()
    raise RuntimeError("diagnostic child returned an invalid stdout type")


async def _run_child(computer: Any) -> dict[str, Any]:
    sandbox = getattr(computer, "_sandbox", None)
    if sandbox is None or not hasattr(sandbox, "exec"):
        return {
            "passed": False,
            "failure_type": "RuntimeError",
            "failure_phase": "sandbox_handle",
        }
    try:
        process = await sandbox.exec.aio(
            "python",
            "-m",
            "modal_computer_use.benchmarks.x11_shm_direct_vs_spawned",
            "--child",
            "--pairs",
            str(PAIRS),
            "--warmups",
            str(WARMUPS),
            timeout=3_600,
        )
        exit_code = await process.wait.aio()
        output = await _read_stdout(process)
        value = json.loads(output) if output else {}
        if not isinstance(value, dict):
            raise ValueError("diagnostic child returned a non-object")
        if exit_code != 0:
            value["passed"] = False
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "passed": False,
            "failure_type": "RuntimeError",
            "failure_phase": "child_result",
        }


async def _terminal_cleanup() -> dict[str, Any]:
    """Terminate tagged survivors and retain only bounded cleanup evidence."""

    errors: list[str] = []
    survivors_before = 0
    remaining = 0
    try:
        async for sandbox in modal.Sandbox.list.aio(
            app_id=app.app_id,
            tags={"benchmark_run": RUN_TAG},
        ):
            if await sandbox.poll.aio() is None:
                survivors_before += 1
                try:
                    await sandbox.terminate.aio(wait=True)
                except Exception as exc:
                    errors.append(type(exc).__name__)
        async for sandbox in modal.Sandbox.list.aio(
            app_id=app.app_id,
            tags={"benchmark_run": RUN_TAG},
        ):
            if await sandbox.poll.aio() is None:
                remaining += 1
    except Exception as exc:
        errors.append(type(exc).__name__)
    return {
        "succeeded": not errors and remaining == 0,
        "remaining_sandboxes": remaining,
        "survivors_before_sweep": survivors_before,
        "cleanup_error_types": sorted(set(errors)),
    }


@app.function(
    image=image,
    cpu=CPU,
    memory=MEMORY_MIB,
    timeout=3_900,
    region=REGION,
    retries=0,
)
def run() -> dict[str, Any]:
    async def measure() -> dict[str, Any]:
        context = _TargetContext()
        observation: dict[str, Any]
        context_cleanup_failed = False
        try:
            computer = await context.__aenter__()
            observation = await _run_child(computer)
        except BaseException:
            observation = {
                "passed": False,
                "failure_type": "RuntimeError",
                "failure_phase": "session_start",
            }
        finally:
            try:
                await context.__aexit__(None, None, None)
            except BaseException:
                # Do not expose exception text from teardown; the terminal
                # sweep below carries only a safe error type/count contract.
                context_cleanup_failed = True
        terminal_cleanup = await _terminal_cleanup()
        if context_cleanup_failed:
            error_types = set(terminal_cleanup.get("cleanup_error_types", []))
            error_types.add("CleanupError")
            terminal_cleanup["cleanup_error_types"] = sorted(error_types)
            terminal_cleanup["succeeded"] = False
        observation["terminal_cleanup"] = terminal_cleanup
        return observation

    return asyncio.run(measure())


@app.local_entrypoint()
def main(output: str | None = None) -> None:
    if not output:
        raise SystemExit("an explicit --output artifact path is required")
    path = Path(output).expanduser()
    if not path.is_absolute():
        raise SystemExit("--output must be an absolute path")
    if path.exists() or path.is_symlink():
        raise SystemExit("--output already exists; refusing to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    provenance = local_provenance()
    observation = run.remote()
    terminal_cleanup = observation.pop("terminal_cleanup", {})
    artifact = build_artifact(observation, terminal_cleanup, provenance)
    rendered = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    except (FileExistsError, OSError):
        raise SystemExit("--output appeared during the run; refusing to overwrite it") from None
    print(json.dumps(artifact, indent=2, sort_keys=True))
    if artifact["status"] != "complete":
        raise SystemExit(1)
