from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets as _secrets
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit, urlunsplit

from ._version import __version__
from .borrowed import AsyncBorrowedComputer, BorrowedComputer
from .client import AsyncDaemonClient, DaemonClient
from .config import ComputerConfig, ModalIngress, normalize_vnc_mode
from .errors import (
    BrowserReadinessError,
    ConfigConflictError,
    ModalNotInstalledError,
    SandboxAmbiguousError,
    SandboxUnavailableError,
    SessionCompatibilityError,
    SessionEnvironmentMismatchError,
    SessionLeaseLostError,
    SessionPlacementMismatchError,
    SessionTargetMismatchError,
)
from .hot_session import HotSessionClient
from .image import default_image, named_image, selected_image_identity
from .latency import SessionStartupTiming, validate_first_frame
from .models import (
    ComputerSessionHandle,
    ComputerStatus,
    DebugUrls,
    SandboxRef,
    SessionRecoveryAcknowledgement,
    SessionRecoveryStatus,
)
from .namespaces import (
    ActionsNamespace,
    AppsNamespace,
    ArtifactsNamespace,
    BrowserNamespace,
    ClipboardNamespace,
    CommandsNamespace,
    DebugNamespace,
    DisplayNamespace,
    InputNamespace,
    KeyboardNamespace,
    LifecycleNamespace,
    MouseNamespace,
    ProcessesNamespace,
    RecordingsNamespace,
    ScreenshotsNamespace,
    SessionNamespace,
    WindowsNamespace,
)
from .observations import ObservationClient
from .state import compute_config_hash, default_tags, new_run_id, warm_pool_tags
from .transports import (
    AsyncHTTPTransport,
    HotSessionTransport,
    HTTPTransport,
    ObservationStreamTransport,
)

ReusePolicy = Literal["by_run_id", "by_name", "never"]
ConfigMismatchPolicy = Literal["raise", "reuse"]
ModalDaemonEndpointPath = Literal["inherited", "connect", "target-loopback"]
ModalBenchmarkBackend = Literal["v1", "v2"]
ModalBenchmarkTransport = Literal[
    "connect-endpoint",
    "encrypted-tunnel",
    "workspace-private-i6pn",
]
MODAL_OPERATION_TIMEOUT_SECONDS = 55
MODAL_SNAPSHOT_RETENTION_SECONDS = 30 * 24 * 3600
_ASYNC_BORROW_REQUEST_TIMEOUT_SECONDS = 30.0
_ASYNC_HEARTBEAT_JOIN_TIMEOUT_SECONDS = 31.0


@dataclass(frozen=True)
class ModalDaemonEndpoint:
    path: ModalDaemonEndpointPath
    base_url: str
    token: str | None
    target_sandbox_id: str | None
    execute_in_target: bool = False


@dataclass(frozen=True)
class ModalSandboxExecResult:
    sandbox_id: str
    returncode: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ModalDaemonCommandResult:
    result: ModalSandboxExecResult
    selected_path: Literal["same-region-connect", "external"]
    requested_region: str
    fallback_used: bool
    fallback_reason: str | None = None
    fallback_error_type: str | None = None


@dataclass(frozen=True)
class _SessionHandoffPolicy:
    session_id: str | None
    app_name: str
    modal_environment: str | None
    modal_region: str | None
    ingress: ModalIngress
    daemon_http_version: Literal["1.1", "2"]
    vnc_mode: Literal["off", "view_only", "control"]
    config_hash: str


@dataclass
class ModalBenchmarkRunner:
    _sandbox: object
    placement: dict[str, str | None]

    def execute(
        self,
        computer: ComputerSandbox,
        command: Sequence[str],
        *,
        transport: ModalBenchmarkTransport,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 240,
    ) -> ModalSandboxExecResult:
        command_tuple, _, runner_env = _prepare_modal_daemon_command(
            computer,
            command,
            path="inherited",
            env=env,
        )
        runner_env["COMPUTER_USE_DAEMON_RUNNER_PATH"] = transport
        return modal_sandbox_exec_in_place(
            self._sandbox,
            command_tuple,
            env=runner_env,
            exec_timeout_seconds=timeout_seconds,
        )

    def terminate(self) -> bool:
        try:
            self._sandbox.terminate(wait=True)
        except Exception:
            return False
        return True


@dataclass(frozen=True)
class ModalCandidatePlacementProbe:
    run_id: str
    backend: ModalBenchmarkBackend
    requested_cloud: str | None
    requested_region: str
    actual_cloud: str | None
    actual_region: str | None
    i6pn_enabled: bool
    i6pn_verified: bool
    sandbox_created: bool
    cleanup_succeeded: bool
    status: Literal["valid", "failed"]
    error_type: str | None = None


@dataclass
class ModalBenchmarkAllocationContext:
    app: object
    image: object
    run_id: str
    cloud: str | None
    region: str
    cpu: float
    memory_mib: int
    benchmark_tag: str

    def __post_init__(self) -> None:
        if self.cloud is not None and not self.cloud.strip():
            raise ValueError("benchmark throughput cloud must be non-empty when provided")
        if not self.benchmark_tag.strip():
            raise ValueError("benchmark throughput tag must be non-empty")

    async def run_batch(
        self,
        *,
        backend: ModalBenchmarkBackend,
        concurrency: int,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        if backend not in {"v1", "v2"}:
            raise ValueError("backend must be v1 or v2")
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        import modal

        creator = modal.Sandbox.create if backend == "v1" else modal.Sandbox._experimental_create
        batch_started = time.perf_counter()

        async def create_one(index: int) -> dict[str, Any]:
            started = time.perf_counter()
            kwargs: dict[str, Any] = {
                "app": self.app,
                "image": self.image,
                "cpu": self.cpu,
                "memory": self.memory_mib,
                "region": self.region,
                "timeout": timeout_seconds,
                "tags": {
                    "computer-use.benchmark": self.benchmark_tag,
                    "computer-use.backend": backend,
                    "computer-use.run_id": f"{self.run_id}-{backend}-{concurrency}-{index}",
                },
            }
            if self.cloud is not None:
                kwargs["cloud"] = self.cloud
            if backend == "v2":
                kwargs["i6pn"] = False
            sandbox = await creator.aio("sleep", "infinity", **kwargs)
            return {
                "index": index,
                "sandbox": sandbox,
                "allocation_ms": (time.perf_counter() - started) * 1000.0,
            }

        outcomes = await asyncio.gather(
            *(create_one(index) for index in range(concurrency)),
            return_exceptions=True,
        )
        batch_elapsed = time.perf_counter() - batch_started
        attempts: list[dict[str, Any]] = []
        cleanup_succeeded = True
        for index, outcome in enumerate(outcomes):
            if isinstance(outcome, BaseException):
                attempts.append(
                    {
                        "index": index,
                        "status": "failed",
                        "allocation_ms": None,
                        "actual_cloud": None,
                        "actual_region": None,
                        "error_type": type(outcome).__name__,
                        "cleanup_succeeded": False,
                    }
                )
                cleanup_succeeded = False
                continue
            sandbox = outcome["sandbox"]
            placement = {"cloud": None, "region": None}
            ready = False
            cleanup = False
            error_type: str | None = None
            try:
                placement = await _sandbox_runtime_placement_async(sandbox)
                ready = True
            except Exception as exc:
                error_type = type(exc).__name__
            finally:
                try:
                    await sandbox.terminate.aio(wait=True)
                except Exception:
                    cleanup = False
                else:
                    cleanup = True
            cleanup_succeeded = cleanup_succeeded and cleanup
            attempts.append(
                {
                    "index": index,
                    "status": "valid" if ready and error_type is None else "failed",
                    "allocation_ms": outcome["allocation_ms"],
                    "actual_cloud": placement["cloud"],
                    "actual_region": placement["region"],
                    "error_type": error_type,
                    "cleanup_succeeded": cleanup,
                }
            )
        valid_count = sum(attempt["status"] == "valid" for attempt in attempts)
        return {
            "backend": backend,
            "concurrency": concurrency,
            "status": "valid" if valid_count == concurrency and cleanup_succeeded else "failed",
            "attempts": attempts,
            "batch_elapsed_seconds": batch_elapsed,
            "throughput_allocations_per_second": concurrency / batch_elapsed,
            "cleanup_succeeded": cleanup_succeeded,
        }


def cleanup_modal_benchmark_run(
    *,
    app_name: str,
    run_id: str,
    modal_runtime: object | None = None,
    include_inventory: bool = False,
) -> dict[str, Any]:
    """Terminate only benchmark Sandboxes carrying the exact run ID or its children."""
    if not run_id:
        raise ValueError("benchmark cleanup requires an exact run ID")
    if modal_runtime is None:
        try:
            import modal as imported_modal_runtime
        except ImportError as exc:
            raise ModalNotInstalledError(
                "Modal benchmark cleanup requires the modal extra"
            ) from exc
        modal_runtime = imported_modal_runtime
    runtime: Any = modal_runtime
    app = runtime.App.lookup(app_name, create_if_missing=False)
    before, before_inventory = _list_modal_benchmark_sandboxes_with_inventory(
        runtime, app_id=app.app_id, run_id=run_id
    )
    matched = before
    terminated = 0
    failed = 0
    for sandbox in matched:
        try:
            sandbox.terminate(wait=True)
        except Exception:
            failed += 1
        else:
            terminated += 1
    after, after_inventory = _list_modal_benchmark_sandboxes_with_inventory(
        runtime, app_id=app.app_id, run_id=run_id
    )
    remaining = len(after)
    result: dict[str, Any] = {
        "matched_sandboxes": len(matched),
        "terminated_sandboxes": terminated,
        "termination_failures": failed,
        "remaining_sandboxes": remaining,
        "cleanup_succeeded": failed == 0 and remaining == 0,
    }
    if include_inventory:
        inventory_complete = all(
            not isinstance(value, bool) and isinstance(value, int)
            for inventory in (before_inventory, after_inventory)
            for value in inventory.values()
        )
        result["cleanup_succeeded"] = result["cleanup_succeeded"] and inventory_complete
        result["enumeration"] = {
            "before": before_inventory,
            "after": after_inventory,
            "apis": ["Sandbox.list", "Sandbox._experimental_list"],
        }
    return result


def _list_modal_benchmark_sandboxes(runtime: Any, *, app_id: str) -> list[Any]:
    sandboxes, _ = _list_modal_benchmark_sandboxes_with_inventory(runtime, app_id=app_id)
    return sandboxes


def _list_modal_benchmark_sandboxes_with_inventory(
    runtime: Any, *, app_id: str, run_id: str | None = None
) -> tuple[list[Any], dict[str, int | bool]]:
    sandboxes: list[Any] = []
    seen: set[str] = set()
    inventory: dict[str, int | bool] = {}
    for method_name in ("list", "_experimental_list"):
        method = getattr(runtime.Sandbox, method_name, None)
        if not callable(method):
            inventory[method_name] = False
            continue
        listed = list(method(app_id=app_id))
        if run_id is not None:
            listed = [
                sandbox for sandbox in listed if _modal_benchmark_run_matches(sandbox, run_id)
            ]
        inventory[method_name] = len(listed)
        for sandbox in listed:
            identity = str(getattr(sandbox, "object_id", id(sandbox)))
            if identity in seen:
                continue
            seen.add(identity)
            sandboxes.append(sandbox)
    return sandboxes, inventory


def _modal_benchmark_run_matches(sandbox: Any, run_id: str) -> bool:
    try:
        tags = sandbox.get_tags()
    except Exception as exc:
        if _is_modal_not_found_error(exc):
            return False
        raise
    target_run_id = tags.get("computer-use.run_id") if isinstance(tags, dict) else None
    runner_run_id = tags.get("benchmark_run") if isinstance(tags, dict) else None
    return (
        isinstance(runner_run_id, str)
        and (runner_run_id == run_id or runner_run_id.startswith(f"{run_id}-"))
    ) or (
        isinstance(target_run_id, str)
        and (target_run_id == run_id or target_run_id.startswith(f"{run_id}-"))
    )


def _is_modal_not_found_error(exc: Exception) -> bool:
    try:
        from modal.exception import NotFoundError
    except ImportError:
        return False
    return isinstance(exc, NotFoundError)


class _TimedModalRuntime:
    def __init__(self, runtime: object, timing: SessionStartupTiming) -> None:
        self._runtime = runtime
        self._timing = timing

    def __getattr__(self, name: str) -> object:
        value = getattr(self._runtime, name)
        if name == "Sandbox":
            return _TimedSandboxType(value, self._timing)
        return value


class _TimedSandboxType:
    def __init__(self, sandbox_type: object, timing: SessionStartupTiming) -> None:
        self._sandbox_type = sandbox_type
        self._timing = timing

    def __getattr__(self, name: str) -> object:
        return getattr(self._sandbox_type, name)

    def create(self, *args: object, **kwargs: object) -> object:
        create = getattr(self._sandbox_type, "create")  # noqa: B009 - dynamic Modal SDK type
        self._timing.mark("sandbox_create_started")
        sandbox = create(*args, **kwargs)
        self._timing.mark("sandbox_registered")
        return _TimedSandboxInstance(sandbox, self._timing)


class _TimedSandboxInstance:
    def __init__(self, sandbox: object, timing: SessionStartupTiming) -> None:
        self._sandbox = sandbox
        self._timing = timing

    def __getattr__(self, name: str) -> object:
        value = getattr(self._sandbox, name)
        if name == "wait_until_ready":
            return self._wait_until_ready
        if name == "create_connect_token":
            return self._create_connect_access
        return value

    def _wait_until_ready(self, *args: object, **kwargs: object) -> object:
        wait_until_ready = getattr(  # noqa: B009 - dynamic Modal SDK type
            self._sandbox, "wait_until_ready"
        )
        try:
            result = wait_until_ready(*args, **kwargs)
        except Exception:
            _terminate_failed_sandbox(self._sandbox)
            raise
        self._timing.mark("tcp_ready")
        return result

    def _create_connect_access(self, *args: object, **kwargs: object) -> object:
        create_access = getattr(  # noqa: B009 - dynamic Modal SDK type
            self._sandbox, "create_connect_token"
        )
        try:
            result = create_access(*args, **kwargs)
        except Exception:
            _terminate_failed_sandbox(self._sandbox)
            raise
        self._timing.mark("connect_token_ready")
        return result


@dataclass(frozen=True)
class ModalVolumeMount:
    """Opt-in Modal Volume mount options while preserving raw Volume support."""

    volume: object
    read_only: bool = False
    sub_path: str | None = None

    def __post_init__(self) -> None:
        if self.sub_path is None:
            return
        if not self.sub_path.strip() or "\x00" in self.sub_path:
            raise ValueError("Volume sub_path must be a non-empty POSIX path")
        if ".." in PurePosixPath(self.sub_path).parts:
            raise ValueError("Volume sub_path must not contain parent traversal")


def modal_sandbox_exec_runner_from_id(sandbox_id: str):
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "Sandbox.exec benchmark requires the modal extra, for example "
            "`uv sync --extra modal` in this repository or "
            "`uv add 'modal-computer-use[modal]'` downstream"
        ) from exc

    sandbox = modal.Sandbox.from_id(sandbox_id)

    def run(command: tuple[str, ...], timeout: int) -> object:
        return sandbox.exec(*command, timeout=timeout)

    return run


def modal_sandbox_exec_once(
    command: tuple[str, ...],
    *,
    app_name: str,
    name: str | None = None,
    image: object | None = None,
    region: str | None = None,
    env: dict[str, str] | None = None,
    app_tags: dict[str, str] | None = None,
    tags: dict[str, str] | None = None,
    cpu: float | None = None,
    memory_mib: int | None = None,
    timeout_seconds: int = 300,
    idle_timeout_seconds: int = 60,
    exec_timeout_seconds: int = 240,
    backend: ModalBenchmarkBackend = "v1",
    cloud: str | None = None,
    i6pn: bool = False,
    image_revision: str | None = None,
    image_profile: Literal["standard", "browser", "browser-gpu", "custom"] = "standard",
    browser: Literal["firefox", "chromium"] | None = None,
) -> ModalSandboxExecResult:
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "Modal sandbox exec requires the modal extra, for example "
            "`uv sync --extra modal` in this repository or "
            "`uv add 'modal-computer-use[modal]'` downstream"
        ) from exc

    app = modal.App.lookup(app_name, create_if_missing=True)
    if app_tags:
        _set_modal_object_tags(app, app_tags)
    if image is None and image_revision is not None:
        image = named_image(
            revision=image_revision,
            profile=image_profile,
            browser=browser,
        )
    runner_timeout_seconds = max(timeout_seconds, exec_timeout_seconds + 60)
    create_kwargs: dict[str, Any] = {
        "app": app,
        "image": image or default_image(profile="standard"),
        "cpu": cpu,
        "memory": memory_mib,
        "encrypted_ports": [],
        "timeout": runner_timeout_seconds,
        "idle_timeout": idle_timeout_seconds,
        "name": name,
        "tags": tags,
    }
    if backend not in {"v1", "v2"}:
        raise ValueError("backend must be v1 or v2")
    if i6pn and backend != "v2":
        raise ValueError("i6pn runner networking requires the V2 backend")
    if cloud:
        create_kwargs["cloud"] = cloud
    if region:
        create_kwargs["region"] = region
    if backend == "v2":
        create_kwargs["i6pn"] = i6pn
        runner = modal.Sandbox._experimental_create("sleep", "infinity", **create_kwargs)
    else:
        runner = modal.Sandbox.create("sleep", "infinity", **create_kwargs)
    try:
        process = runner.exec(*command, timeout=exec_timeout_seconds, env=env or {})
        stdout = _read_modal_process_stream(getattr(process, "stdout", ""))
        stderr = _read_modal_process_stream(getattr(process, "stderr", ""))
        returncode = _modal_process_returncode(process)
        result = ModalSandboxExecResult(
            sandbox_id=getattr(runner, "object_id", "unknown"),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except BaseException as exc:
        if hasattr(runner, "terminate"):
            try:
                runner.terminate(wait=True)
            except Exception as cleanup_exc:
                exc.add_note(
                    "runner cleanup also failed: "
                    f"terminate ({type(cleanup_exc).__name__})"
                )
        raise
    if hasattr(runner, "terminate"):
        runner.terminate(wait=True)
    return result


def run_modal_benchmark_function_once(
    entrypoint: Callable[..., dict[str, Any]],
    *,
    config: object,
    run_tag: str,
    app_name: str,
    region: str,
    image_revision: str,
    cpu: float,
    memory_mib: int,
    timeout_seconds: int,
    retries: int = 0,
) -> dict[str, Any]:
    """Invoke one regional benchmark Function without including dispatch in its measurements."""
    if retries != 0:
        raise ValueError("benchmark Function retries must be disabled")
    if not region.strip():
        raise ValueError("benchmark Function region must be explicit")
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError("Modal benchmark Function requires the modal extra") from exc

    app = modal.App(f"{app_name}-optimized-provider-runner")
    image = named_image(revision=image_revision, profile="browser", browser="chromium")
    remote = app.function(
        image=image,
        region=region,
        cpu=cpu,
        memory=memory_mib,
        timeout=timeout_seconds,
        retries=0,
        min_containers=0,
        max_containers=1,
        single_use_containers=True,
        # The named image owns this importable module. Avoid cloudpickling the
        # function or mounting local source so the image revision is authoritative
        # and the local caller and the Python 3.12 image may differ.
        serialized=False,
        include_source=False,
    )(entrypoint)
    with app.run():
        result = remote.remote(config, run_tag=run_tag)
    if not isinstance(result, dict):
        raise SandboxUnavailableError("benchmark Function returned a non-object result")
    return result


def modal_sandbox_exec_in_place(
    sandbox: object,
    command: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    exec_timeout_seconds: int = 240,
) -> ModalSandboxExecResult:
    process = sandbox.exec(*command, timeout=exec_timeout_seconds, env=env or {})
    stdout = _read_modal_process_stream(getattr(process, "stdout", ""))
    stderr = _read_modal_process_stream(getattr(process, "stderr", ""))
    return ModalSandboxExecResult(
        sandbox_id=getattr(sandbox, "object_id", "unknown"),
        returncode=_modal_process_returncode(process),
        stdout=stdout,
        stderr=stderr,
    )


def create_modal_benchmark_runner(
    *,
    app_name: str,
    cloud: str | None,
    region: str,
    image_revision: str,
    cpu: float = 1.0,
    memory_mib: int = 1024,
    tags: dict[str, str] | None = None,
    app_tags: dict[str, str] | None = None,
    backend: ModalBenchmarkBackend = "v2",
    i6pn: bool = True,
    runner_label: str,
) -> ModalBenchmarkRunner:
    if cloud is not None and not cloud.strip():
        raise ValueError("benchmark runner cloud must be non-empty when provided")
    if backend not in {"v1", "v2"}:
        raise ValueError("benchmark runner backend must be v1 or v2")
    if backend == "v1" and i6pn:
        raise ConfigConflictError("V1 optimized-frontier runners cannot enable i6pn")
    if not runner_label.strip():
        raise ValueError("benchmark runner label must be non-empty")
    if cpu <= 0 or memory_mib <= 0:
        raise ValueError("benchmark runner resources must be positive")
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError("Modal benchmark runner requires the modal extra") from exc
    app = modal.App.lookup(app_name, create_if_missing=True)
    if app_tags:
        _set_modal_object_tags(app, app_tags)
    image = named_image(
        revision=image_revision,
        profile="browser",
        browser="chromium",
    )
    create_kwargs: dict[str, Any] = {
        "app": app,
        "image": image,
        "cpu": cpu,
        "memory": memory_mib,
        "region": region,
        "timeout": 3600,
        "idle_timeout": 600,
        "tags": {
            "computer-use.runner": runner_label,
            **(tags or {}),
        },
    }
    if cloud is not None:
        create_kwargs["cloud"] = cloud
    creator = modal.Sandbox.create if backend == "v1" else modal.Sandbox._experimental_create
    if backend == "v2":
        create_kwargs["i6pn"] = i6pn
    sandbox = creator("sleep", "infinity", **create_kwargs)
    try:
        placement = _sandbox_runtime_placement(sandbox)
    except Exception:
        _terminate_failed_sandbox(sandbox)
        raise
    return ModalBenchmarkRunner(_sandbox=sandbox, placement=placement)


def create_modal_benchmark_allocation_context(
    *,
    app_name: str,
    image_revision: str,
    run_id: str,
    cloud: str | None,
    region: str,
    cpu: float,
    memory_mib: int,
    benchmark_tag: str,
) -> ModalBenchmarkAllocationContext:
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError("Modal benchmark throughput requires the modal extra") from exc
    app = modal.App.lookup(app_name, create_if_missing=True)
    image = named_image(
        revision=image_revision,
        profile="browser",
        browser="chromium",
    )
    return ModalBenchmarkAllocationContext(
        app=app,
        image=image,
        run_id=run_id,
        cloud=cloud,
        region=region,
        cpu=cpu,
        memory_mib=memory_mib,
        benchmark_tag=benchmark_tag,
    )


def probe_modal_candidate_placement(
    *,
    app_name: str,
    image_revision: str,
    run_id: str,
    backend: ModalBenchmarkBackend,
    cloud: str | None,
    region: str,
    cpu: float,
    memory_mib: int,
    i6pn: bool,
    tags: dict[str, str] | None = None,
) -> ModalCandidatePlacementProbe:
    """Observe one unmeasured candidate placement and always terminate it."""
    if not run_id:
        raise ValueError("candidate placement probes require an exact run ID")
    if backend not in {"v1", "v2"}:
        raise ValueError("backend must be v1 or v2")
    if i6pn and backend != "v2":
        raise ValueError("i6pn placement probes require the V2 backend")
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError("Modal placement probes require the modal extra") from exc

    app = modal.App.lookup(app_name, create_if_missing=True)
    image = named_image(
        revision=image_revision,
        profile="browser",
        browser="chromium",
    )
    sandbox_tags = {
        "computer-use.benchmark": "modal-v2-placement-probe",
        **(tags or {}),
        "computer-use.run_id": f"{run_id}-placement-probe-{backend}",
    }
    _validate_sandbox_tags(sandbox_tags)
    create_kwargs: dict[str, Any] = {
        "app": app,
        "image": image,
        "cpu": cpu,
        "memory": memory_mib,
        "region": region,
        "timeout": 300,
        "idle_timeout": 60,
        "tags": sandbox_tags,
    }
    if cloud is not None:
        create_kwargs["cloud"] = cloud
    if backend == "v2":
        create_kwargs["i6pn"] = i6pn
        create = modal.Sandbox._experimental_create
    else:
        create = modal.Sandbox.create

    sandbox: object | None = None
    actual = {"cloud": None, "region": None}
    i6pn_verified = False
    status: Literal["valid", "failed"] = "failed"
    error_type: str | None = None
    cleanup_succeeded = False
    try:
        sandbox = create("sleep", "infinity", **create_kwargs)
        actual = _sandbox_runtime_placement(sandbox)
        if i6pn:
            _sandbox_i6pn_address(sandbox)
            i6pn_verified = True
        status = "valid"
    except Exception as exc:
        error_type = type(exc).__name__
    finally:
        if sandbox is not None:
            with suppress(Exception):
                sandbox.terminate(wait=True)
        try:
            cleanup = cleanup_modal_benchmark_run(
                app_name=app_name,
                run_id=run_id,
                modal_runtime=modal,
            )
        except Exception:
            cleanup_succeeded = False
        else:
            cleanup_succeeded = cleanup["cleanup_succeeded"] is True
    return ModalCandidatePlacementProbe(
        run_id=run_id,
        backend=backend,
        requested_cloud=cloud,
        requested_region=region,
        actual_cloud=actual["cloud"],
        actual_region=actual["region"],
        i6pn_enabled=i6pn,
        i6pn_verified=i6pn_verified,
        sandbox_created=sandbox is not None,
        cleanup_succeeded=cleanup_succeeded,
        status=status,
        error_type=error_type,
    )


def _read_modal_process_stream(stream: object) -> str:
    read = getattr(stream, "read", None)
    value = read() if callable(read) else stream
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _modal_process_returncode(process: object) -> int | None:
    wait = getattr(process, "wait", None)
    if callable(wait):
        value = wait()
        if isinstance(value, int):
            return value
    value = getattr(process, "returncode", None)
    return value if isinstance(value, int) else None


def modal_billing_report(
    *,
    start: datetime,
    end: datetime | None,
    resolution: str,
    tag_names: list[str] | None,
    environment_name: str | None = None,
) -> list[object]:
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "Modal billing reconciliation requires the modal extra, for example "
            "`uv sync --extra modal` in this repository or "
            "`uv add 'modal-computer-use[modal]'` downstream"
        ) from exc

    scope = (
        modal.Environment.from_name(environment_name)
        if environment_name is not None
        else modal.Workspace.from_context()
    )
    return list(
        scope.billing.report(start=start, end=end, resolution=resolution, tag_names=tag_names)
    )


def modal_workspace_billing_report(
    *,
    start: datetime,
    end: datetime | None,
    resolution: str,
    tag_names: list[str] | None,
) -> list[object]:
    """Compatibility wrapper for callers of the previous workspace-only adapter."""
    return modal_billing_report(
        start=start,
        end=end,
        resolution=resolution,
        tag_names=tag_names,
    )


def _daemon_bearer_from_auth(auth: dict[str, str]) -> str:
    return auth["COMPUTER_USE_TUNNEL_TOKEN"]


def _modal_function_environment(environ: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    return source.get("MODAL_ENVIRONMENT")


class _ModalFunctionSessionBorrow:
    """Lazy owner of one borrowed daemon connection for a Function invocation."""

    def __init__(
        self,
        handle: ComputerSessionHandle,
        *,
        run_id: str,
        function_region: str,
        readiness_timeout: float,
    ) -> None:
        self._handle = handle
        self._run_id = run_id
        self._function_region = function_region
        self._readiness_timeout = readiness_timeout
        self._sandbox: object | None = None
        self._client: DaemonClient | None = None
        self._coordinator: object | None = None
        self._borrowed: BorrowedComputer | None = None
        self._entered = False

    def __enter__(self) -> BorrowedComputer:
        if self._entered:
            raise RuntimeError("a session borrow context can only be entered once")
        self._entered = True
        _validate_borrow_request(
            self._handle,
            run_id=self._run_id,
            function_region=self._function_region,
            readiness_timeout=self._readiness_timeout,
        )
        borrowed, sandbox, client, coordinator = _borrow_modal_function_session(
            self._handle,
            run_id=self._run_id,
            readiness_timeout=float(self._readiness_timeout),
        )
        self._sandbox = sandbox
        self._client = client
        self._coordinator = coordinator
        self._borrowed = borrowed
        return borrowed

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        sandbox, self._sandbox = self._sandbox, None
        client, self._client = self._client, None
        coordinator, self._coordinator = self._coordinator, None
        borrowed, self._borrowed = self._borrowed, None
        if borrowed is not None:
            borrowed._invalidate()
        if sandbox is None or client is None or coordinator is None:
            return
        cleanup_errors: list[BaseException] = []
        for cleanup in (coordinator.close, client.close, sandbox.detach):
            try:
                cleanup()
            except BaseException as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
        if cleanup_errors:
            first_cleanup_error = cleanup_errors[0]
            if isinstance(exc, BaseException):
                exc.add_note(
                    "borrowed session cleanup also failed: "
                    f"cleanup ({type(first_cleanup_error).__name__})"
                )
                return
            raise SessionLeaseLostError() from first_cleanup_error


class _AsyncModalFunctionSessionBorrow:
    """Native-async owner of one borrowed daemon connection."""

    def __init__(
        self,
        handle: ComputerSessionHandle,
        *,
        run_id: str,
        function_region: str,
        readiness_timeout: float,
    ) -> None:
        self._handle = handle
        self._run_id = run_id
        self._function_region = function_region
        self._readiness_timeout = readiness_timeout
        self._sandbox: object | None = None
        self._client: AsyncDaemonClient | None = None
        self._coordinator: object | None = None
        self._borrowed: AsyncBorrowedComputer | None = None
        self._entered = False

    async def __aenter__(self) -> AsyncBorrowedComputer:
        if self._entered:
            raise RuntimeError("a session borrow context can only be entered once")
        self._entered = True
        _validate_borrow_request(
            self._handle,
            run_id=self._run_id,
            function_region=self._function_region,
            readiness_timeout=self._readiness_timeout,
        )
        borrowed, sandbox, client, coordinator = await _borrow_modal_function_session_async(
            self._handle,
            run_id=self._run_id,
            readiness_timeout=float(self._readiness_timeout),
        )
        self._sandbox = sandbox
        self._client = client
        self._coordinator = coordinator
        self._borrowed = borrowed
        return borrowed

    async def __aexit__(self, _exc_type: object, exc: object, _traceback: object) -> None:
        sandbox, self._sandbox = self._sandbox, None
        client, self._client = self._client, None
        coordinator, self._coordinator = self._coordinator, None
        borrowed, self._borrowed = self._borrowed, None
        if borrowed is not None:
            borrowed._invalidate()
        if sandbox is None or client is None or coordinator is None:
            return

        async def cleanup() -> None:
            cleanup_errors: list[BaseException] = []
            for operation in (
                coordinator.aclose,
                client.aclose,
                sandbox.detach.aio,
            ):
                try:
                    await operation()
                except BaseException as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)
            if cleanup_errors:
                raise SessionLeaseLostError() from cleanup_errors[0]

        task = asyncio.create_task(cleanup())
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as cleanup_cancelled:
                if task.cancelled():
                    raise
                cancellation = cancellation or cleanup_cancelled
                continue
            except Exception as cleanup_exc:
                if isinstance(exc, BaseException):
                    exc.add_note(
                        "borrowed session cleanup also failed: "
                        f"cleanup ({type(cleanup_exc).__name__})"
                    )
                    return
                raise
            if cancellation is not None:
                raise cancellation
            return


def _validate_borrow_request(
    handle: ComputerSessionHandle,
    *,
    run_id: str,
    function_region: str,
    readiness_timeout: float,
) -> None:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if (
        isinstance(readiness_timeout, bool)
        or not isinstance(readiness_timeout, (int, float))
        or not math.isfinite(readiness_timeout)
        or readiness_timeout <= 0
    ):
        raise ValueError("readiness_timeout must be a positive finite number")
    if os.environ.get("MODAL_IS_REMOTE") != "1":
        raise SessionEnvironmentMismatchError
    function_environment = _modal_function_environment()
    if (
        function_environment is None
        or not function_environment.strip()
        or function_environment != handle.modal_environment
    ):
        raise SessionEnvironmentMismatchError
    if not isinstance(function_region, str) or not function_region.strip():
        raise ValueError("function_region must be a non-empty string")
    if function_region != handle.requested_modal_region:
        raise SessionPlacementMismatchError
    if handle.vnc_mode not in {"off", "view_only"}:
        raise SessionCompatibilityError


class ComputerSandbox:
    def __init__(
        self,
        client: DaemonClient,
        *,
        sandbox: object | None = None,
        metadata: SandboxRef | None = None,
        startup_timing: SessionStartupTiming | None = None,
    ) -> None:
        self.client = client
        self._sandbox = sandbox
        self._metadata = metadata
        self._requested_modal_region: str | None = None
        self._session_handoff_policy: _SessionHandoffPolicy | None = None
        self._daemon_bearer: str | None = None
        self.startup_timing = startup_timing
        self._readiness_failure_cleanup: Literal["none", "client", "sandbox"] = "none"
        self._readiness_stage_count = 0
        self.lifecycle = LifecycleNamespace(client)
        self.mouse = MouseNamespace(client)
        self.keyboard = KeyboardNamespace(client)
        self.clipboard = ClipboardNamespace(client)
        self.screenshots = ScreenshotsNamespace(client)
        self.recordings = RecordingsNamespace(client)
        self.display = DisplayNamespace(client)
        self.windows = WindowsNamespace(client)
        self.processes = ProcessesNamespace(client)
        self.actions = ActionsNamespace(client)
        self.input = InputNamespace(client)
        self.artifacts = ArtifactsNamespace(client)
        self.browser = BrowserNamespace(client)
        self.apps = AppsNamespace(client)
        self.commands = CommandsNamespace(client)
        self.debug = DebugNamespace(client)
        self.session = SessionNamespace(client)

    @classmethod
    def local(
        cls,
        *,
        base_url: str = "http://127.0.0.1:8080",
        token: str | None = None,
        timeout: float = 30.0,
    ) -> ComputerSandbox:
        return cls(DaemonClient(base_url=base_url, token=token, timeout=timeout))

    @classmethod
    def create(
        cls,
        *,
        config: ComputerConfig | None = None,
        app_name: str = "modal-computer-use",
        name: str | None = None,
        image: object | None = None,
        expose_vnc: bool | str | None = None,
        tags: dict[str, str] | None = None,
        app_tags: dict[str, str] | None = None,
        secrets: list[object] | None = None,
        volumes: dict[str, object] | None = None,
        owner: str | None = None,
        wait: bool = True,
        timing: SessionStartupTiming | None = None,
        tag_profile: Literal["default", "warm_pool"] = "default",
        **sandbox_kwargs: Any,
    ) -> ComputerSandbox:
        timing = timing or SessionStartupTiming()
        timing.mark("request_received")
        timing.unsupported(
            "scheduled",
            "Modal V1 does not expose a supported scheduling timestamp",
        )
        timing.unsupported(
            "daemon_started",
            "the daemon process does not yet emit an attested startup timestamp",
        )
        try:
            import modal
        except ImportError as exc:
            raise ModalNotInstalledError(
                "ComputerSandbox.create requires the modal extra, for example "
                "`uv sync --extra modal` in this repository or "
                "`uv add 'modal-computer-use[modal]'` downstream"
            ) from exc
        modal = _TimedModalRuntime(modal, timing)

        config = config or ComputerConfig()
        if not config.run_id:
            config.run_id = new_run_id()
        volumes = _prepare_volume_mounts(volumes or {})
        artifact_volume_mounted = _has_artifact_volume_mount(volumes, config.storage.artifacts_dir)
        if config.storage.persist_artifacts and not artifact_volume_mounted:
            raise ConfigConflictError(
                "persist_artifacts=True requires a Volume mounted at storage.artifacts_dir "
                "or one of its parent directories"
            )
        vnc_mode = normalize_vnc_mode(expose_vnc if expose_vnc is not None else config.expose_vnc)
        custom_image_supplied = image is not None
        browser_kind = config.browser.kind if config.browser else None
        if image is None and config.image.source == "named":
            image = named_image(
                revision=config.image.revision or "",
                profile=config.resources.profile,
                browser=browser_kind,
                environment_name=config.image.environment_name,
            )
        elif image is None:
            image = default_image(
                profile=config.resources.profile,
                browser=browser_kind,
                window_manager=config.desktop.window_manager,
                browser_prewarm=config.browser.prewarm if config.browser else False,
            )
        app_lookup_kwargs: dict[str, object] = {"create_if_missing": True}
        if config.runtime.modal_environment is not None:
            app_lookup_kwargs["environment_name"] = config.runtime.modal_environment
        app = modal.App.lookup(app_name, **app_lookup_kwargs)
        if app_tags:
            _set_modal_object_tags(app, app_tags)
        env = _daemon_environment(
            config,
            vnc_mode=vnc_mode,
            artifact_volume_mounted=artifact_volume_mounted,
        )
        base_tags = (
            warm_pool_tags() if tag_profile == "warm_pool" else default_tags(config, owner=owner)
        )
        sandbox_tags = {**(tags or {}), **base_tags}
        config_hash = compute_config_hash(config)
        session_id: str | None = None
        if (
            config.runtime.modal_environment is not None
            and config.runtime.modal_region is not None
        ):
            session_id = _session_policy_id_prefix(
                app_name=app_name,
                modal_environment=config.runtime.modal_environment,
                requested_modal_region=config.runtime.modal_region,
                ingress=config.ingress,
                daemon_http_version=config.network.daemon_http_version,
                vnc_mode=vnc_mode,
                config_hash=config_hash,
            ) + _secrets.token_hex(8)
            sandbox_tags["computer-use.session_id"] = session_id
        sandbox_tags["computer-use.image_identity"] = (
            "custom"
            if custom_image_supplied
            else selected_image_identity(
                source=config.image.source,
                revision=config.image.revision,
                profile=config.resources.profile,
                browser=browser_kind,
            )
        )
        _validate_sandbox_tags(sandbox_tags)
        http2 = config.network.daemon_http_version == "2"
        ports = _encrypted_ports_for_ingress(config.ingress, vnc_mode=vnc_mode, http2=http2)
        h2_ports = _h2_ports_for_ingress(config.ingress, http2=http2)
        daemon_auth = {"COMPUTER_USE_TUNNEL_TOKEN": _secrets.token_urlsafe(32)}
        env.update(daemon_auth)
        tunnel_token = _daemon_bearer_from_auth(daemon_auth)
        create_kwargs: dict[str, Any] = {
            "app": app,
            "image": image,
            "cpu": config.resources.cpu,
            "memory": config.resources.memory_mib,
            "gpu": config.resources.gpu,
            "encrypted_ports": ports,
            "timeout": config.runtime.timeout_seconds,
            "idle_timeout": config.runtime.idle_timeout_seconds,
            "secrets": secrets or [],
            "volumes": volumes,
            "env": env,
            "block_network": config.network.block_all,
            "outbound_cidr_allowlist": config.network.outbound_cidr_allowlist,
            "outbound_domain_allowlist": config.network.outbound_domain_allowlist,
            "inbound_cidr_allowlist": config.network.inbound_cidr_allowlist,
            "name": name,
            "tags": sandbox_tags,
            **sandbox_kwargs,
        }
        if h2_ports:
            create_kwargs["h2_ports"] = h2_ports
        if config.runtime.modal_region:
            create_kwargs["region"] = config.runtime.modal_region
        readiness_probe = _readiness_probe(modal)
        if readiness_probe is not None:
            create_kwargs["readiness_probe"] = readiness_probe
        sandbox = modal.Sandbox.create("python", "-m", "modal_computer_use.daemon", **create_kwargs)
        if wait and hasattr(sandbox, "wait_until_ready"):
            sandbox.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
        token_info = sandbox.create_connect_token(
            user_metadata={"sdk": "modal-computer-use", "version": __version__}
        )
        connect_base_url, connect_token = _connect_token_parts(token_info)
        base_url, token = _client_ingress_parts(
            sandbox,
            ingress=config.ingress,
            connect_base_url=connect_base_url,
            connect_token=connect_token,
            tunnel_token=tunnel_token,
        )
        metadata = SandboxRef(
            sandbox_id=getattr(sandbox, "object_id", "unknown"),
            app_name=app_name,
            name=name,
            run_id=config.run_id,
            owner=sandbox_tags.get("computer-use.owner"),
            created_at=_created_at_from_tags(sandbox_tags),
            config_hash=config_hash,
            status="started",
            tags=sandbox_tags,
            vnc_url=None,
            artifacts_dir=config.storage.artifacts_dir,
        )
        computer = cls(
            DaemonClient(base_url=base_url, token=token, http2=http2),
            sandbox=sandbox,
            metadata=metadata,
        )
        computer.startup_timing = timing
        computer._daemon_bearer = _daemon_bearer_from_auth(daemon_auth)
        computer._readiness_failure_cleanup = "sandbox"
        if wait:
            computer.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
        if wait and config.ingress == "attested-tunnel":
            computer.client.close()
            base_url, token = _attested_tunnel_parts(
                sandbox,
                connect_base_url=connect_base_url,
                connect_token=connect_token,
            )
            computer = cls(
                DaemonClient(base_url=base_url, token=token, http2=http2),
                sandbox=sandbox,
                metadata=metadata,
            )
            computer.startup_timing = timing
            computer._daemon_bearer = _daemon_bearer_from_auth(daemon_auth)
            computer._readiness_failure_cleanup = "sandbox"
            computer._readiness_stage_count = 1
            timing.mark("attestation_ready")
            computer.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
        computer._requested_modal_region = config.runtime.modal_region
        computer._session_handoff_policy = _session_handoff_policy(
            config,
            session_id=session_id,
            app_name=app_name,
            vnc_mode=vnc_mode,
            config_hash=metadata.config_hash or compute_config_hash(config),
        )
        computer._readiness_failure_cleanup = "none"
        return computer

    @classmethod
    def attach(
        cls,
        *,
        sandbox_id: str | None = None,
        name: str | None = None,
        run_id: str | None = None,
        app_name: str = "modal-computer-use",
        base_url: str | None = None,
        token: str | None = None,
        ingress: ModalIngress = "attested-tunnel",
        http2: bool = False,
        wait: bool = False,
        readiness_timeout: float = 120.0,
    ) -> ComputerSandbox:
        if base_url:
            computer = cls(DaemonClient(base_url=base_url, token=token, http2=http2))
            if wait:
                computer._readiness_failure_cleanup = "client"
                computer.wait_until_ready(timeout=readiness_timeout)
                computer._readiness_failure_cleanup = "none"
            return computer
        try:
            import modal
        except ImportError as exc:
            raise ModalNotInstalledError(
                "ComputerSandbox.attach requires the modal extra, for example "
                "`uv sync --extra modal` in this repository or "
                "`uv add 'modal-computer-use[modal]'` downstream"
            ) from exc
        if sandbox_id:
            sandbox = modal.Sandbox.from_id(sandbox_id)
        elif name:
            sandbox = _sandbox_from_name(modal, app_name=app_name, name=name)
        elif run_id:
            from .registry import SandboxRegistry

            sandbox = SandboxRegistry(app_name=app_name).require_sandbox_by_run_id(run_id)
        else:
            raise ValueError("attach requires sandbox_id, name, run_id, or base_url")
        metadata = _metadata_from_sandbox(sandbox, app_name=app_name)
        token_info = sandbox.create_connect_token(
            user_metadata={"sdk": "modal-computer-use", "version": __version__}
        )
        connect_base_url, connect_token = _connect_token_parts(token_info)
        computer = cls(
            DaemonClient(base_url=connect_base_url, token=connect_token, http2=http2),
            sandbox=sandbox,
            metadata=metadata,
        )
        if wait:
            computer._readiness_failure_cleanup = "client"
            computer.wait_until_ready(timeout=readiness_timeout)
            if ingress == "attested-tunnel":
                computer.client.close()
                connect_base_url, connect_token = _attested_tunnel_parts(
                    sandbox,
                    connect_base_url=connect_base_url,
                    connect_token=connect_token,
                )
                computer = cls(
                    DaemonClient(base_url=connect_base_url, token=connect_token, http2=http2),
                    sandbox=sandbox,
                    metadata=metadata,
                )
                computer._readiness_failure_cleanup = "client"
                computer.wait_until_ready(timeout=readiness_timeout)
            computer._readiness_failure_cleanup = "none"
        return computer

    @classmethod
    def attach_or_create(
        cls,
        *,
        config: ComputerConfig | None = None,
        app_name: str = "modal-computer-use",
        run_id: str | None = None,
        name: str | None = None,
        reuse: bool | ReusePolicy = "by_run_id",
        on_config_mismatch: ConfigMismatchPolicy = "raise",
        wait: bool = True,
        readiness_timeout: float | None = None,
        **kwargs: Any,
    ) -> ComputerSandbox:
        config = config or ComputerConfig()
        if run_id is not None:
            config.run_id = run_id
        if on_config_mismatch not in ("raise", "reuse"):
            raise ValueError("on_config_mismatch must be 'raise' or 'reuse'")
        reuse_policy = _normalize_reuse_policy(reuse)
        requested_hash = compute_config_hash(config)

        if reuse_policy == "by_run_id" and config.run_id:
            try:
                computer = cls.attach(
                    run_id=config.run_id,
                    app_name=app_name,
                    ingress=config.ingress,
                    http2=config.network.daemon_http_version == "2",
                    wait=wait,
                    readiness_timeout=readiness_timeout or config.runtime.readiness_timeout_seconds,
                )
                _check_config_hash(
                    computer.metadata(),
                    requested_hash=requested_hash,
                    on_config_mismatch=on_config_mismatch,
                )
                if computer.metadata() is not None and (
                    computer.metadata().config_hash == requested_hash
                ):
                    computer._requested_modal_region = config.runtime.modal_region
                    computer._session_handoff_policy = _session_handoff_policy(
                        config,
                        session_id=computer.metadata().tags.get("computer-use.session_id"),
                        app_name=app_name,
                        config_hash=requested_hash,
                    )
                return computer
            except SandboxAmbiguousError:
                raise
            except SandboxUnavailableError:
                pass
        elif reuse_policy == "by_name":
            if not name:
                raise ValueError("reuse='by_name' requires name")
            try:
                computer = cls.attach(
                    name=name,
                    app_name=app_name,
                    ingress=config.ingress,
                    http2=config.network.daemon_http_version == "2",
                    wait=wait,
                    readiness_timeout=readiness_timeout or config.runtime.readiness_timeout_seconds,
                )
                _check_config_hash(
                    computer.metadata(),
                    requested_hash=requested_hash,
                    on_config_mismatch=on_config_mismatch,
                )
                if computer.metadata() is not None and (
                    computer.metadata().config_hash == requested_hash
                ):
                    computer._requested_modal_region = config.runtime.modal_region
                    computer._session_handoff_policy = _session_handoff_policy(
                        config,
                        session_id=computer.metadata().tags.get("computer-use.session_id"),
                        app_name=app_name,
                        config_hash=requested_hash,
                    )
                return computer
            except SandboxAmbiguousError:
                raise
            except SandboxUnavailableError:
                pass
        elif reuse_policy != "never":
            raise ValueError("reuse must be 'by_run_id', 'by_name', 'never', True, or False")

        return cls.create(config=config, app_name=app_name, name=name, wait=wait, **kwargs)

    def session_handle(self) -> ComputerSessionHandle:
        """Return the safe reconnect policy for an SDK-owned Modal desktop."""
        policy = self._session_handoff_policy
        metadata = self._metadata
        if self._sandbox is None or metadata is None or policy is None:
            raise SandboxUnavailableError(
                "session handles require an SDK-owned Modal desktop created by create() "
                "or compatibly reused by attach_or_create()"
            )
        if metadata.sandbox_id == "unknown" or not metadata.sandbox_id.strip():
            raise SandboxUnavailableError("session handle sandbox identity is unavailable")
        if not policy.app_name.strip():
            raise SandboxUnavailableError("session handle app identity is unavailable")
        if metadata.app_name != policy.app_name or metadata.config_hash != policy.config_hash:
            raise SessionTargetMismatchError
        if metadata.tags.get("computer-use") != "true":
            raise SandboxUnavailableError("session handle target is not identified as SDK-owned")
        if metadata.tags.get("computer-use.config_hash") != policy.config_hash:
            raise SessionTargetMismatchError
        if policy.modal_environment is None or not policy.modal_environment.strip():
            raise SessionCompatibilityError
        if policy.modal_region is None or not policy.modal_region.strip():
            raise SessionCompatibilityError
        if (
            policy.session_id is None
            or metadata.tags.get("computer-use.session_id") != policy.session_id
        ):
            raise SessionTargetMismatchError
        if policy.ingress not in {"attested-tunnel", "connect"}:
            raise SessionCompatibilityError
        if policy.vnc_mode == "control":
            raise SessionCompatibilityError
        return ComputerSessionHandle(
            sandbox_id=metadata.sandbox_id,
            session_id=policy.session_id,
            app_name=policy.app_name,
            modal_environment=policy.modal_environment,
            requested_modal_region=policy.modal_region,
            ingress=policy.ingress,
            daemon_http_version=policy.daemon_http_version,
            vnc_mode=policy.vnc_mode,
            config_hash=policy.config_hash,
        )

    def start(self) -> object:
        return self.lifecycle.start()

    def stop(self) -> object:
        return self.lifecycle.stop()

    def restart(self) -> object:
        return self.lifecycle.restart()

    def status(self) -> ComputerStatus:
        return self.lifecycle.status()

    def recovery_status(self) -> SessionRecoveryStatus:
        """Return durable target recovery state using owner-only authorization."""
        bearer = self._daemon_bearer
        if not bearer:
            raise SandboxUnavailableError(
                "owner recovery requires the original SDK-owned daemon authorization"
            )
        payload = self.client.transport.request(
            "GET",
            "/v1/recovery/status",
            headers={"x-computer-use-owner-proof": bearer},
        ).json()
        return SessionRecoveryStatus.model_validate(payload)

    def acknowledge_recovery(
        self,
        *,
        incident_id: str,
    ) -> SessionRecoveryAcknowledgement:
        """Acknowledge one exact recovery incident as the original owner."""
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise ValueError("incident_id must be a non-empty string")
        bearer = self._daemon_bearer
        if not bearer:
            raise SandboxUnavailableError(
                "owner recovery requires the original SDK-owned daemon authorization"
            )
        payload = self.client.transport.request(
            "POST",
            "/v1/recovery/acknowledge",
            json={"incident_id": incident_id},
            headers={"x-computer-use-owner-proof": bearer},
        ).json()
        return SessionRecoveryAcknowledgement.model_validate(payload)

    def wait_until_ready(self, timeout: float = 120.0, interval: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        last_payload: object | None = None
        last_error: Exception | None = None
        while True:
            try:
                payload = self.client.get_json("/readyz")
                last_payload = payload
                last_error = None
                if payload.get("ready") is True:
                    if self.startup_timing is not None:
                        stage = (
                            "connect_ready" if self._readiness_stage_count == 0 else "tunnel_ready"
                        )
                        self.startup_timing.mark(stage)
                    self._readiness_stage_count += 1
                    return
            except Exception as exc:
                last_error = exc
            if time.monotonic() >= deadline:
                detail = _readiness_timeout_detail(last_payload, last_error)
                timeout_error = TimeoutError(
                    f"daemon did not become ready before timeout ({timeout:g}s){detail}"
                )
                if self._readiness_failure_cleanup != "none":
                    try:
                        self.client.close()
                    except Exception as exc:
                        timeout_error.add_note(
                            "readiness cleanup also failed: "
                            f"client.close ({type(exc).__name__})"
                        )
                if (
                    self._readiness_failure_cleanup == "sandbox"
                    and self._sandbox is not None
                ):
                    _terminate_failed_sandbox(self._sandbox)
                raise timeout_error
            time.sleep(interval)

    def terminate(self, *, wait: bool = False) -> None:
        if self._sandbox is not None and hasattr(self._sandbox, "terminate"):
            if wait:
                self._sandbox.terminate(wait=True)
            else:
                self._sandbox.terminate()
        else:
            self.stop()

    def detach(self) -> None:
        try:
            if self._sandbox is not None and hasattr(self._sandbox, "detach"):
                self._sandbox.detach()
        finally:
            self.client.close()

    def metadata(self) -> SandboxRef | None:
        return self._metadata

    def poll(self) -> int | None:
        sandbox = _require_modal_backing(self, path="poll")
        poll = getattr(sandbox, "poll", None)
        if not callable(poll):
            raise SandboxUnavailableError("Modal Sandbox.poll is unavailable")
        value = poll()
        return value if isinstance(value, int) else None

    def runtime_region(self) -> str | None:
        return self.runtime_placement()["region"]

    def runtime_placement(self) -> dict[str, str | None]:
        sandbox = _require_modal_backing(self, path="runtime placement")
        return _sandbox_runtime_placement(sandbox)

    def runtime_i6pn_address(self) -> str:
        sandbox = _require_modal_backing(self, path="runtime i6pn address")
        return _sandbox_i6pn_address(sandbox)

    def tags(self) -> dict[str, str]:
        sandbox = _require_modal_backing(self, path="tags")
        return _get_modal_object_tags(sandbox)

    def set_tags(
        self,
        tags: dict[str, str],
        *,
        remove: set[str] | None = None,
    ) -> None:
        sandbox = _require_modal_backing(self, path="set_tags")
        remote_tags = _read_modal_object_tags(sandbox)
        metadata_tags = self._metadata.tags if self._metadata is not None else {}
        complete_tags = {**(metadata_tags if remote_tags is None else remote_tags), **tags}
        for key in remove or ():
            complete_tags.pop(key, None)
        _validate_sandbox_tags(complete_tags)
        _replace_modal_object_tags(sandbox, complete_tags)
        if self._metadata is not None:
            self._metadata = self._metadata.model_copy(update={"tags": complete_tags})

    def ensure_browser_ready(
        self,
        config: ComputerConfig,
        *,
        timing: SessionStartupTiming | None = None,
    ) -> None:
        if config.browser is None or config.browser.kind is None:
            return
        status = self.browser.status()
        if status.get("configured_browser") != config.browser.kind:
            raise BrowserReadinessError(
                "configured browser does not match the requested browser"
            )
        if not config.browser.prewarm:
            return
        prewarm_result = status.get("prewarm_result")
        if not isinstance(prewarm_result, dict) or prewarm_result.get("ok") is not True:
            raise BrowserReadinessError("browser prewarm did not succeed")
        if not isinstance(status.get("windows"), int) or status["windows"] < 1:
            raise BrowserReadinessError(
                "browser prewarm did not create a browser window"
            )
        if timing is not None:
            timing.mark("browser_ready")

    def first_valid_frame(
        self,
        config: ComputerConfig,
        *,
        timing: SessionStartupTiming | None = None,
    ) -> bytes:
        screenshot = self.screenshots.full(format="png", processing="daemon")
        payload = validate_first_frame(
            screenshot.as_bytes(),
            expected_width=config.desktop.resolution[0],
            expected_height=config.desktop.resolution[1],
            image_format="png",
        )
        if timing is not None:
            timing.mark("first_valid_frame")
        return payload

    def __enter__(self) -> ComputerSandbox:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.terminate()
        finally:
            self.client.close()

    def debug_urls(self) -> DebugUrls:
        if self._sandbox is not None:
            return DebugUrls(vnc=_vnc_url(self._sandbox), daemon=None, recording_dashboard=None)
        return self.debug.urls()

    def hot_session(self, *, timeout: float = 30.0) -> HotSessionClient:
        return HotSessionClient(
            HotSessionTransport(
                self.client.base_url,
                token=self.client.transport.token,
                timeout=timeout,
            )
        )

    def observation_stream(
        self,
        *,
        options: dict[str, Any] | None = None,
        fps: float = 5.0,
        max_frames: int | None = None,
        idle_timeout_ms: int | None = None,
        send_unchanged: bool = False,
        delivery: Literal["latest", "reliable"] | None = None,
        delta_mode: Literal["auto", "off"] | None = None,
        delta_max_ratio: float | None = None,
        keyframe_interval: int | None = None,
        tile_size: int | None = None,
        max_patch_rects: int | None = None,
        multi_rect_min_savings: float | None = None,
        frame_encoding: Literal["json-binary", "binary-envelope"] | None = "binary-envelope",
        timeout: float = 30.0,
        timing: SessionStartupTiming | None = None,
    ) -> ObservationClient:
        return ObservationClient(
            ObservationStreamTransport(
                self.client.base_url,
                token=self.client.transport.token,
                timeout=timeout,
            ),
            options=options,
            fps=fps,
            max_frames=max_frames,
            idle_timeout_ms=idle_timeout_ms,
            send_unchanged=send_unchanged,
            delivery=delivery,
            delta_mode=delta_mode,
            delta_max_ratio=delta_max_ratio,
            keyframe_interval=keyframe_interval,
            tile_size=tile_size,
            max_patch_rects=max_patch_rects,
            multi_rect_min_savings=multi_rect_min_savings,
            frame_encoding=frame_encoding,
            startup_timing=timing,
        )

    def snapshot_filesystem(
        self,
        timeout: int = MODAL_OPERATION_TIMEOUT_SECONDS,
        *,
        ttl: int | None = MODAL_SNAPSHOT_RETENTION_SECONDS,
    ) -> object:
        """Create a Modal filesystem snapshot image for this sandbox.

        This is an orchestration helper over Modal's Sandbox API. It snapshots
        filesystem state, not a provider loop, trace replay, or guaranteed GUI
        memory state.
        """
        if self._sandbox is None or not hasattr(self._sandbox, "snapshot_filesystem"):
            raise SandboxUnavailableError(
                "filesystem snapshots require a Modal-backed sandbox with "
                "Sandbox.snapshot_filesystem support"
            )
        _validate_modal_operation_policy(timeout=timeout, ttl=ttl)
        return self._sandbox.snapshot_filesystem(timeout, ttl=ttl)

    def snapshot_directory(
        self,
        path: str = "/home/desktop/artifacts",
        *,
        timeout: int = MODAL_OPERATION_TIMEOUT_SECONDS,
        ttl: int | None = MODAL_SNAPSHOT_RETENTION_SECONDS,
    ) -> object:
        """Snapshot a directory from a Modal-backed sandbox as a Modal Image.

        Modal's current documented restore path for Sandbox directory snapshots is
        `snapshot_directory(path)` followed by `mount_image(path, snapshot)` on a
        fresh sandbox. Prefer this helper for artifacts, project files, and other
        directory-scoped state.
        """
        if self._sandbox is None or not hasattr(self._sandbox, "snapshot_directory"):
            raise SandboxUnavailableError(
                "directory snapshots require a Modal-backed sandbox with "
                "Sandbox.snapshot_directory support"
            )
        _validate_modal_operation_policy(timeout=timeout, ttl=ttl)
        return self._sandbox.snapshot_directory(path, timeout=timeout, ttl=ttl)

    def reload_volumes(self, *, timeout: int = MODAL_OPERATION_TIMEOUT_SECONDS) -> None:
        """Block until every mounted Modal Volume has reloaded or the timeout expires."""
        if self._sandbox is None or not hasattr(self._sandbox, "reload_volumes"):
            raise SandboxUnavailableError(
                "Volume reload requires a Modal-backed sandbox with Sandbox.reload_volumes support"
            )
        _validate_modal_operation_policy(timeout=timeout, ttl=None)
        self._sandbox.reload_volumes(timeout=timeout)

    def mount_image(self, path: str, image: object) -> None:
        """Mount a Modal Image into a running Modal-backed sandbox."""
        if self._sandbox is None or not hasattr(self._sandbox, "mount_image"):
            raise SandboxUnavailableError(
                "mount_image requires a Modal-backed sandbox with Sandbox.mount_image support"
            )
        self._sandbox.mount_image(path, image)


def _required_loopback_bearer(computer: ComputerSandbox) -> str:
    bearer = getattr(computer, "_daemon_bearer", None)
    if not bearer:
        raise SandboxUnavailableError(
            "target-loopback requires an SDK-owned daemon bearer"
        )
    return bearer


def modal_daemon_endpoint(
    computer: ComputerSandbox,
    path: ModalDaemonEndpointPath = "inherited",
) -> ModalDaemonEndpoint:
    metadata = computer.metadata()
    target_sandbox_id = None if metadata is None else metadata.sandbox_id
    if path == "inherited":
        return ModalDaemonEndpoint(
            path=path,
            base_url=computer.client.base_url,
            token=computer.client.transport.token,
            target_sandbox_id=target_sandbox_id,
        )
    sandbox = _require_modal_backing(computer, path=path)
    if path == "connect":
        token_info = sandbox.create_connect_token(
            user_metadata={"sdk": "modal-computer-use", "runner_path": path}
        )
        base_url, token = _connect_token_parts(token_info)
        return ModalDaemonEndpoint(
            path=path,
            base_url=base_url,
            token=token,
            target_sandbox_id=target_sandbox_id,
        )
    if path == "target-loopback":
        return ModalDaemonEndpoint(
            path=path,
            base_url="http://127.0.0.1:8080",
            token=_required_loopback_bearer(computer),
            target_sandbox_id=target_sandbox_id,
            execute_in_target=True,
        )
    raise ValueError("path must be inherited, connect, or target-loopback")


def create_modal_benchmark_computer(
    *,
    config: ComputerConfig,
    backend: ModalBenchmarkBackend,
    transport: ModalBenchmarkTransport,
    cloud: str | None,
    app_name: str = "modal-computer-use",
    image: object | None = None,
    tags: dict[str, str] | None = None,
    app_tags: dict[str, str] | None = None,
    wait: bool = True,
    timing: SessionStartupTiming | None = None,
    modal_runtime: object | None = None,
    client_factory: Callable[..., DaemonClient] = DaemonClient,
) -> ComputerSandbox:
    """Create one provenance-bound V1/V2 benchmark target.

    This constructor intentionally supports only the direct-path benchmark's
    matched feature subset. It keeps the daemon image, resources, application
    bearer, IPv6 bind, readiness probe, and placement arguments identical while
    varying the backend and declared transport arm.
    """
    if backend not in {"v1", "v2"}:
        raise ValueError("backend must be v1 or v2")
    if transport == "connect-endpoint" and backend != "v1":
        raise ConfigConflictError("Modal V2 Connect Tokens are unsupported")
    if transport == "workspace-private-i6pn" and backend != "v2":
        raise ConfigConflictError("workspace-private i6pn requires Modal V2")
    if transport not in {
        "connect-endpoint",
        "encrypted-tunnel",
        "workspace-private-i6pn",
    }:
        raise ValueError("benchmark transport is unsupported")
    if cloud is not None and not cloud.strip():
        raise ValueError("benchmark target cloud must be non-empty when provided")
    if config.image.source != "named":
        raise ConfigConflictError("benchmark targets require an exact named image")
    if config.storage.persist_artifacts:
        raise ConfigConflictError("benchmark targets do not mount artifact storage")
    if config.resources.gpu is not None:
        raise ConfigConflictError("benchmark targets do not support GPUs")

    timing = timing or SessionStartupTiming()
    timing.mark("request_received")
    timing.unsupported(
        "scheduled",
        f"Modal {backend.upper()} does not expose a common supported scheduling timestamp",
    )
    timing.unsupported(
        "daemon_started",
        "the daemon process does not emit an attested startup timestamp",
    )
    if modal_runtime is None:
        try:
            import modal as imported_modal_runtime
        except ImportError as exc:
            raise ModalNotInstalledError(
                "Modal benchmark execution requires the modal extra"
            ) from exc
        modal_runtime = imported_modal_runtime
    runtime: Any = modal_runtime
    if not config.run_id:
        config.run_id = new_run_id()
    browser_kind = config.browser.kind if config.browser else None
    if image is None:
        image = named_image(
            revision=config.image.revision or "",
            profile=config.resources.profile,
            browser=browser_kind,
            environment_name=config.image.environment_name,
        )
    app_lookup_kwargs: dict[str, object] = {"create_if_missing": True}
    if config.runtime.modal_environment is not None:
        app_lookup_kwargs["environment_name"] = config.runtime.modal_environment
    app = runtime.App.lookup(app_name, **app_lookup_kwargs)
    if app_tags:
        _set_modal_object_tags(app, app_tags)

    daemon_auth = {"COMPUTER_USE_TUNNEL_TOKEN": _secrets.token_urlsafe(32)}
    env = _daemon_environment(config, vnc_mode="off", artifact_volume_mounted=False)
    env.update(daemon_auth)
    env["COMPUTER_USE_DAEMON_HOST"] = (
        "::"
        if transport == "workspace-private-i6pn"
        else "0.0.0.0"  # noqa: S104 - Modal tunnel/connect must accept external ingress.
    )
    sandbox_tags = {**(tags or {}), **default_tags(config)}
    sandbox_tags.update(
        {
            "computer-use.image_identity": selected_image_identity(
                source=config.image.source,
                revision=config.image.revision,
                profile=config.resources.profile,
                browser=browser_kind,
            ),
            "computer-use.modal_backend": backend,
            "computer-use.benchmark_transport": transport,
        }
    )
    _validate_sandbox_tags(sandbox_tags)
    encrypted_ports = [8080] if transport == "encrypted-tunnel" else []
    create_kwargs: dict[str, Any] = {
        "app": app,
        "image": image,
        "cpu": config.resources.cpu,
        "memory": config.resources.memory_mib,
        "region": config.runtime.modal_region,
        "encrypted_ports": encrypted_ports,
        "timeout": config.runtime.timeout_seconds,
        "idle_timeout": config.runtime.idle_timeout_seconds,
        "env": env,
        "block_network": config.network.block_all,
        "outbound_cidr_allowlist": config.network.outbound_cidr_allowlist,
        "outbound_domain_allowlist": config.network.outbound_domain_allowlist,
        "inbound_cidr_allowlist": config.network.inbound_cidr_allowlist,
        "tags": sandbox_tags,
    }
    if cloud is not None:
        create_kwargs["cloud"] = cloud
    readiness_probe = (
        None if transport == "workspace-private-i6pn" else _readiness_probe(runtime)
    )
    if readiness_probe is not None:
        create_kwargs["readiness_probe"] = readiness_probe
    if backend == "v2":
        create_kwargs["i6pn"] = transport == "workspace-private-i6pn"
        create = runtime.Sandbox._experimental_create
    else:
        create = runtime.Sandbox.create

    timing.mark("sandbox_create_started")
    sandbox = create("python", "-m", "modal_computer_use.daemon", **create_kwargs)
    timing.mark("sandbox_registered")
    client: DaemonClient | None = None
    try:
        if wait and readiness_probe is not None and hasattr(sandbox, "wait_until_ready"):
            sandbox.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
            timing.mark("container_ready")
        if transport == "connect-endpoint":
            token_info = sandbox.create_connect_token(
                user_metadata={"sdk": "modal-computer-use", "benchmark": "modal-direct-path"}
            )
            base_url, token = _connect_token_parts(token_info)
            timing.mark("connect_endpoint_ready")
        elif transport == "encrypted-tunnel":
            base_url = _tunnel_url(sandbox, 8080)
            token = _daemon_bearer_from_auth(daemon_auth)
            timing.mark("encrypted_tunnel_ready")
        else:
            address = _sandbox_i6pn_address(sandbox)
            base_url = f"http://[{address}]:8080"
            token = _daemon_bearer_from_auth(daemon_auth)
            timing.mark("workspace_private_endpoint_ready")
        metadata = SandboxRef(
            sandbox_id=getattr(sandbox, "object_id", "unknown"),
            app_name=app_name,
            run_id=config.run_id,
            created_at=_created_at_from_tags(sandbox_tags),
            config_hash=compute_config_hash(config),
            status="started",
            tags=sandbox_tags,
            vnc_url=None,
            artifacts_dir=config.storage.artifacts_dir,
        )
        client = client_factory(base_url=base_url, token=token, http2=False)
        computer = ComputerSandbox(
            client,
            sandbox=sandbox,
            metadata=metadata,
            startup_timing=timing,
        )
        computer._daemon_bearer = _daemon_bearer_from_auth(daemon_auth)
        computer._requested_modal_region = config.runtime.modal_region
        if wait and transport != "workspace-private-i6pn":
            computer.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
            timing.mark("authenticated_daemon_ready")
        return computer
    except BaseException:
        if client is not None:
            client.close()
        _terminate_failed_sandbox(sandbox)
        raise


def create_modal_v2_tunnel_computer(
    *,
    config: ComputerConfig,
    app_name: str = "modal-computer-use",
    name: str | None = None,
    image: object | None = None,
    tags: dict[str, str] | None = None,
    app_tags: dict[str, str] | None = None,
    wait: bool = True,
    timing: SessionStartupTiming | None = None,
    modal_runtime: object | None = None,
    client_factory: Callable[..., DaemonClient] = DaemonClient,
) -> ComputerSandbox:
    """Create the benchmark-only V2 encrypted-tunnel target.

    V2 does not support Connect Tokens in Modal 1.5.2. This path exposes only the
    encrypted daemon port and authenticates every daemon request with an
    application bearer token. It is intentionally separate from the default
    ComputerSandbox.create lifecycle.
    """
    timing = timing or SessionStartupTiming()
    timing.mark("request_received")
    timing.unsupported("scheduled", "Modal V2 does not expose a supported scheduling timestamp")
    timing.unsupported(
        "daemon_started",
        "the daemon process does not yet emit an attested startup timestamp",
    )
    timing.unsupported(
        "connect_token_ready",
        "Modal V2 does not support Sandbox Connect Tokens in Modal 1.5.2",
    )
    if config.ingress != "tunnel":
        raise ConfigConflictError("the V2 benchmark path requires encrypted tunnel ingress")
    if config.image.source != "named":
        raise ConfigConflictError("the V2 benchmark path requires an exact named image")
    if config.storage.persist_artifacts:
        raise ConfigConflictError("the V2 benchmark path does not mount artifact storage")
    if modal_runtime is None:
        try:
            import modal as imported_modal_runtime
        except ImportError as exc:
            raise ModalNotInstalledError(
                "Modal V2 benchmark execution requires the modal extra"
            ) from exc
        modal_runtime = imported_modal_runtime
    runtime: Any = modal_runtime
    if not config.run_id:
        config.run_id = new_run_id()
    browser_kind = config.browser.kind if config.browser else None
    if image is None:
        image = named_image(
            revision=config.image.revision or "",
            profile=config.resources.profile,
            browser=browser_kind,
            environment_name=config.image.environment_name,
        )
    app_lookup_kwargs: dict[str, object] = {"create_if_missing": True}
    if config.runtime.modal_environment is not None:
        app_lookup_kwargs["environment_name"] = config.runtime.modal_environment
    app = runtime.App.lookup(app_name, **app_lookup_kwargs)
    if app_tags:
        _set_modal_object_tags(app, app_tags)
    tunnel_auth = {"COMPUTER_USE_TUNNEL_TOKEN": _secrets.token_urlsafe(32)}
    env = _daemon_environment(config, vnc_mode="off", artifact_volume_mounted=False)
    env.update(tunnel_auth)
    sandbox_tags = {**(tags or {}), **default_tags(config)}
    sandbox_tags["computer-use.image_identity"] = selected_image_identity(
        source=config.image.source,
        revision=config.image.revision,
        profile=config.resources.profile,
        browser=browser_kind,
    )
    sandbox_tags["computer-use.modal_backend"] = "v2"
    _validate_sandbox_tags(sandbox_tags)
    create_kwargs: dict[str, Any] = {
        "app": app,
        "image": image,
        "cpu": config.resources.cpu,
        "memory": config.resources.memory_mib,
        "encrypted_ports": [8080],
        "timeout": config.runtime.timeout_seconds,
        "idle_timeout": config.runtime.idle_timeout_seconds,
        "env": env,
        "block_network": config.network.block_all,
        "outbound_cidr_allowlist": config.network.outbound_cidr_allowlist,
        "outbound_domain_allowlist": config.network.outbound_domain_allowlist,
        "inbound_cidr_allowlist": config.network.inbound_cidr_allowlist,
        "name": name,
        "tags": sandbox_tags,
    }
    if config.runtime.modal_region:
        create_kwargs["region"] = config.runtime.modal_region
    readiness_probe = _readiness_probe(runtime)
    if readiness_probe is not None:
        create_kwargs["readiness_probe"] = readiness_probe
    create = runtime.Sandbox._experimental_create
    timing.mark("sandbox_create_started")
    sandbox = create("python", "-m", "modal_computer_use.daemon", **create_kwargs)
    timing.mark("sandbox_registered")
    client: DaemonClient | None = None
    try:
        if wait and hasattr(sandbox, "wait_until_ready"):
            sandbox.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
            timing.mark("tcp_ready")
        base_url = _tunnel_url(sandbox, 8080)
        timing.mark("encrypted_tunnel_ready")
        client = client_factory(
            base_url=base_url,
            token=_daemon_bearer_from_auth(tunnel_auth),
            http2=False,
        )
        metadata = SandboxRef(
            sandbox_id=getattr(sandbox, "object_id", "unknown"),
            app_name=app_name,
            name=name,
            run_id=config.run_id,
            owner=sandbox_tags.get("computer-use.owner"),
            created_at=_created_at_from_tags(sandbox_tags),
            config_hash=compute_config_hash(config),
            status="started",
            tags=sandbox_tags,
            vnc_url=None,
            artifacts_dir=config.storage.artifacts_dir,
        )
        computer = ComputerSandbox(client, sandbox=sandbox, metadata=metadata)
        computer._daemon_bearer = _daemon_bearer_from_auth(tunnel_auth)
        computer._requested_modal_region = config.runtime.modal_region
        if wait:
            computer.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
            timing.mark("authenticated_tunnel_ready")
        return computer
    except Exception:
        if client is not None:
            client.close()
        _terminate_failed_sandbox(sandbox)
        raise


def modal_daemon_env(
    endpoint: ModalDaemonEndpoint,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    reserved = {
        "COMPUTER_USE_DAEMON_BASE_URL": endpoint.base_url,
        "COMPUTER_USE_DAEMON_RUNNER_PATH": endpoint.path,
    }
    if endpoint.token:
        reserved["COMPUTER_USE_DAEMON_TOKEN"] = endpoint.token
    if endpoint.target_sandbox_id:
        reserved["COMPUTER_USE_TARGET_SANDBOX_ID"] = endpoint.target_sandbox_id
    conflicts = sorted(set(reserved) & set(env or {}))
    if conflicts:
        raise ValueError("runner env cannot override reserved daemon keys: " + ", ".join(conflicts))
    return {**reserved, **(env or {})}


def run_modal_daemon_command(
    computer: ComputerSandbox,
    command: Sequence[str],
    *,
    path: ModalDaemonEndpointPath = "inherited",
    app_name: str = "modal-computer-use",
    modal_region: str | None = None,
    runner_name: str | None = None,
    env: dict[str, str] | None = None,
    runner_cpu: float | None = None,
    runner_memory_mib: int | None = None,
    exec_timeout_seconds: int = 240,
    app_tags: dict[str, str] | None = None,
    tags: dict[str, str] | None = None,
    exec_once: Callable[..., ModalSandboxExecResult] = modal_sandbox_exec_once,
    exec_in_target: Callable[..., ModalSandboxExecResult] = modal_sandbox_exec_in_place,
) -> ModalSandboxExecResult:
    selected_region = (
        None
        if path == "target-loopback"
        else _resolve_modal_runner_region(computer, modal_region)
    )
    command_tuple, endpoint, runner_env = _prepare_modal_daemon_command(
        computer,
        command,
        path=path,
        env=env,
    )
    if endpoint.execute_in_target:
        sandbox = _require_modal_backing(computer, path=path)
        return exec_in_target(
            sandbox,
            command_tuple,
            env=runner_env,
            exec_timeout_seconds=exec_timeout_seconds,
        )
    assert selected_region is not None
    return exec_once(
        command_tuple,
        app_name=app_name,
        name=runner_name,
        region=selected_region,
        env=runner_env,
        app_tags=app_tags,
        tags={
            "computer-use.runner": "colocated",
            "computer-use.runner_path": path,
            **(tags or {}),
        },
        cpu=runner_cpu,
        memory_mib=runner_memory_mib,
        exec_timeout_seconds=exec_timeout_seconds,
    )


def run_modal_daemon_command_with_fallback(
    computer: ComputerSandbox,
    command: Sequence[str],
    *,
    modal_region: str | None = None,
    app_name: str = "modal-computer-use",
    runner_name: str | None = None,
    env: dict[str, str] | None = None,
    runner_cpu: float | None = None,
    runner_memory_mib: int | None = None,
    exec_timeout_seconds: int = 240,
    app_tags: dict[str, str] | None = None,
    tags: dict[str, str] | None = None,
    external_runner: Callable[..., ModalSandboxExecResult] | None = None,
    exec_once: Callable[..., ModalSandboxExecResult] = modal_sandbox_exec_once,
) -> ModalDaemonCommandResult:
    """Run a daemon workload in-region with an explicit pre-dispatch fallback.

    The workload process receives the daemon endpoint directly. Action and frame
    bytes do not pass through an allocation broker. The target-loopback path is
    intentionally unavailable here because it is diagnostic-only. The caller must
    supply ``external_runner`` to opt into execution outside Modal. When the target
    was created by this SDK with an explicit region, the runner inherits it.
    """
    command_tuple = tuple(command)
    if not command_tuple:
        raise ValueError("command must not be empty")
    selected_region = _resolve_modal_runner_region(
        computer,
        modal_region,
        require_target_match=True,
    )
    try:
        endpoint = modal_daemon_endpoint(computer, "connect")
    except Exception as exc:
        if not _is_connect_preparation_failure(exc):
            raise
        if external_runner is None:
            raise
        endpoint = modal_daemon_endpoint(computer, "inherited")
        runner_env = modal_daemon_env(endpoint, env)
        result = external_runner(
            command_tuple,
            env=runner_env,
            timeout=exec_timeout_seconds,
        )
        return ModalDaemonCommandResult(
            result=result,
            selected_path="external",
            requested_region=selected_region,
            fallback_used=True,
            fallback_reason="connect_endpoint_unavailable",
            fallback_error_type=type(exc).__name__,
        )
    runner_env = modal_daemon_env(endpoint, env)
    result = exec_once(
        command_tuple,
        app_name=app_name,
        name=runner_name,
        region=selected_region,
        env=runner_env,
        app_tags=app_tags,
        tags={
            "computer-use.runner": "colocated",
            "computer-use.runner_path": endpoint.path,
            **(tags or {}),
        },
        cpu=runner_cpu,
        memory_mib=runner_memory_mib,
        exec_timeout_seconds=exec_timeout_seconds,
    )
    return ModalDaemonCommandResult(
        result=result,
        selected_path="same-region-connect",
        requested_region=selected_region,
        fallback_used=False,
    )


def _is_connect_preparation_failure(exc: Exception) -> bool:
    if isinstance(exc, SandboxUnavailableError):
        return True
    return _is_modal_availability_error(exc)


def _is_modal_availability_error(exc: Exception) -> bool:
    try:
        from modal.exception import (
            ConnectionError as ModalConnectionError,
        )
        from modal.exception import (
            InternalFailure,
            NotFoundError,
            SandboxTerminatedError,
            ServiceError,
        )
        from modal.exception import (
            TimeoutError as ModalTimeoutError,
        )
    except ImportError:
        return False
    return isinstance(
        exc,
        (
            ModalConnectionError,
            InternalFailure,
            NotFoundError,
            SandboxTerminatedError,
            ServiceError,
            ModalTimeoutError,
        ),
    )


def _resolve_modal_runner_region(
    computer: ComputerSandbox,
    modal_region: str | None,
    *,
    require_target_match: bool = False,
) -> str:
    explicit_region = modal_region.strip() if modal_region is not None else None
    if modal_region is not None and not explicit_region:
        raise ValueError("modal_region must be non-empty when provided")
    requested_region_value = getattr(computer, "_requested_modal_region", None)
    requested_region = (
        requested_region_value.strip()
        if isinstance(requested_region_value, str) and requested_region_value.strip()
        else None
    )
    if explicit_region is not None:
        if (
            require_target_match
            and requested_region is not None
            and explicit_region != requested_region
        ):
            raise ConfigConflictError(
                f"runner modal_region {explicit_region!r} does not match the target sandbox's "
                f"requested region {requested_region!r}"
            )
        return explicit_region
    if requested_region is not None:
        return requested_region
    raise ValueError(
        "modal_region is required when the target sandbox's requested region is unknown"
    )


def _prepare_modal_daemon_command(
    computer: ComputerSandbox,
    command: Sequence[str],
    *,
    path: ModalDaemonEndpointPath,
    env: dict[str, str] | None,
) -> tuple[tuple[str, ...], ModalDaemonEndpoint, dict[str, str]]:
    command_tuple = tuple(command)
    if not command_tuple:
        raise ValueError("command must not be empty")
    endpoint = modal_daemon_endpoint(computer, path)
    return command_tuple, endpoint, modal_daemon_env(endpoint, env)


def _require_modal_backing(computer: ComputerSandbox, *, path: str) -> object:
    if computer._sandbox is None:
        raise SandboxUnavailableError(f"{path} requires a Modal-backed sandbox")
    return computer._sandbox


def _sandbox_i6pn_address(sandbox: object) -> str:
    process = sandbox.exec(
        "python",
        "-c",
        (
            "import socket; print(socket.getaddrinfo("
            "'i6pn.modal.local', None, socket.AF_INET6)[0][4][0])"
        ),
        timeout=10,
    )
    address = _read_modal_process_stream(getattr(process, "stdout", "")).strip()
    if not address or any(character not in "0123456789abcdefABCDEF:" for character in address):
        raise SandboxUnavailableError("Modal runtime did not return a valid i6pn address")
    return address


def _sandbox_runtime_placement(sandbox: object) -> dict[str, str | None]:
    process = sandbox.exec(
        "python",
        "-c",
        (
            "import json, os; print(json.dumps({"
            "'cloud': os.environ.get('MODAL_CLOUD_PROVIDER') or None, "
            "'region': os.environ.get('MODAL_REGION') or None}))"
        ),
        timeout=10,
    )
    raw = _read_modal_process_stream(getattr(process, "stdout", "")).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SandboxUnavailableError("Modal runtime placement returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SandboxUnavailableError("Modal runtime placement returned an invalid payload")
    return {
        "cloud": payload.get("cloud") if isinstance(payload.get("cloud"), str) else None,
        "region": payload.get("region") if isinstance(payload.get("region"), str) else None,
    }


async def _sandbox_runtime_placement_async(sandbox: object) -> dict[str, str | None]:
    process = await sandbox.exec.aio(
        "python",
        "-c",
        (
            "import json, os; print(json.dumps({"
            "'cloud': os.environ.get('MODAL_CLOUD_PROVIDER') or None, "
            "'region': os.environ.get('MODAL_REGION') or None}))"
        ),
        timeout=10,
    )
    raw = (await process.stdout.read.aio()).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SandboxUnavailableError("Modal runtime placement returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SandboxUnavailableError("Modal runtime placement returned an invalid payload")
    return {
        "cloud": payload.get("cloud") if isinstance(payload.get("cloud"), str) else None,
        "region": payload.get("region") if isinstance(payload.get("region"), str) else None,
    }


def _terminate_failed_sandbox(sandbox: object) -> None:
    terminate = getattr(sandbox, "terminate", None)
    if callable(terminate):
        with suppress(Exception):
            terminate(wait=True)


def _validate_sandbox_tags(tags: dict[str, str]) -> None:
    if len(tags) > 10:
        raise ConfigConflictError(
            f"Modal Sandboxes support at most 10 tags; resolved {len(tags)} tags"
        )


def _daemon_environment(
    config: ComputerConfig, *, vnc_mode: str, artifact_volume_mounted: bool = False
) -> dict[str, str]:
    env = {
        "COMPUTER_USE_RUN_ID": config.run_id or "",
        "COMPUTER_USE_DESKTOP_WIDTH": str(config.desktop.resolution[0]),
        "COMPUTER_USE_DESKTOP_HEIGHT": str(config.desktop.resolution[1]),
        "COMPUTER_USE_DESKTOP_DPI": str(config.desktop.dpi),
        "COMPUTER_USE_DISPLAY_DEPTH": str(config.desktop.display_depth),
        "COMPUTER_USE_RECORDINGS_DIR": config.storage.recordings_dir,
        "COMPUTER_USE_ARTIFACTS_DIR": config.storage.artifacts_dir,
        "COMPUTER_USE_ARTIFACTS_PERSISTENT": str(config.storage.persist_artifacts).lower(),
        "COMPUTER_USE_ARTIFACTS_VOLUME_MOUNTED": str(artifact_volume_mounted).lower(),
        "COMPUTER_USE_TRACE_DIR": config.storage.trace_dir,
        "COMPUTER_USE_WINDOW_MANAGER": config.desktop.window_manager,
        "COMPUTER_USE_IMAGE_PROFILE": config.resources.profile,
        "COMPUTER_USE_BROWSER": config.browser.kind
        if config.browser and config.browser.kind
        else "",
        "COMPUTER_USE_BROWSER_PREWARM": str(
            config.browser.prewarm if config.browser else False
        ).lower(),
        "COMPUTER_USE_BROWSER_PROFILE_DIR": config.browser.profile_dir
        if config.browser and config.browser.profile_dir
        else "",
        "COMPUTER_USE_BROWSER_LAUNCH_ARGS": json.dumps(
            config.browser.launch_args if config.browser else []
        ),
        "COMPUTER_USE_BROWSER_OPEN_URL_ON_START": (
            config.browser.open_url_on_start
            if config.browser and config.browser.open_url_on_start
            else ""
        ),
        "COMPUTER_USE_BROWSER_GPU_MODE": _browser_gpu_mode(config),
        "COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION": (
            config.actions.screenshot_processing_location
        ),
        "COMPUTER_USE_POST_ACTION_DELAY_MS": str(config.actions.post_action_delay_ms),
        "COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS": str(config.actions.default_action_timeout_ms),
        "COMPUTER_USE_MAX_ACTION_TIMEOUT_MS": str(config.actions.max_action_timeout_ms),
        "COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC": str(config.actions.input_rate_limit_per_sec),
        "COMPUTER_USE_INPUT_BACKEND": config.actions.input_backend,
        "COMPUTER_USE_SUBPROCESS_BACKEND": config.actions.subprocess_backend,
        "COMPUTER_USE_DAEMON_HTTP_VERSION": config.network.daemon_http_version,
        "COMPUTER_USE_TRACE_ACTIONS": str(config.actions.trace_actions).lower(),
        "COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY": "true",
        "COMPUTER_USE_VNC_MODE": vnc_mode,
        "COMPUTER_USE_VNC_PASSWORD": (
            config.vnc_password or _secrets.token_urlsafe(24) if vnc_mode != "off" else ""
        ),
        "COMPUTER_USE_MAX_BATCH_ACTIONS": str(config.actions.max_batch_actions),
        "COMPUTER_USE_MAX_BATCH_DURATION_MS": str(config.actions.max_batch_duration_ms),
        "COMPUTER_USE_MAX_ACTIONS": ""
        if config.budgets.max_actions is None
        else str(config.budgets.max_actions),
        "COMPUTER_USE_MAX_SCREENSHOTS": ""
        if config.budgets.max_screenshots is None
        else str(config.budgets.max_screenshots),
        "COMPUTER_USE_MAX_ARTIFACT_BYTES": ""
        if config.budgets.max_artifact_bytes is None
        else str(config.budgets.max_artifact_bytes),
        "COMPUTER_USE_MAX_RECORDING_SECONDS": ""
        if config.budgets.max_recording_seconds is None
        else str(config.budgets.max_recording_seconds),
        "COMPUTER_USE_MAX_IDLE_SECONDS": ""
        if config.budgets.max_idle_seconds is None
        else str(config.budgets.max_idle_seconds),
    }
    return env


def _has_artifact_volume_mount(volumes: dict[str, object], artifacts_dir: str) -> bool:
    artifact_path = _normalize_mount_path(artifacts_dir)
    for mount_path in volumes:
        mount = _normalize_mount_path(str(mount_path))
        if artifact_path == mount or artifact_path.startswith(f"{mount}/"):
            return True
    return False


def _prepare_volume_mounts(volumes: dict[str, object]) -> dict[str, object]:
    prepared: dict[str, object] = {}
    for mount_path, value in volumes.items():
        if not isinstance(value, ModalVolumeMount):
            prepared[mount_path] = value
            continue
        if not value.read_only and value.sub_path is None:
            prepared[mount_path] = value.volume
            continue
        with_mount_options = getattr(value.volume, "with_mount_options", None)
        if not callable(with_mount_options):
            raise ConfigConflictError(
                "read-only and subpath Volume controls require Volume.with_mount_options"
            )
        prepared[mount_path] = with_mount_options(
            read_only=True if value.read_only else None,
            sub_path=value.sub_path,
        )
    return prepared


def _validate_modal_operation_policy(*, timeout: int, ttl: int | None) -> None:
    if timeout <= 0:
        raise ValueError("Modal operation timeout must be positive")
    if ttl is not None and ttl <= 0:
        raise ValueError("Modal snapshot ttl must be positive or None")


def _normalize_mount_path(path: str) -> str:
    normalized = "/" + path.strip("/")
    return normalized.rstrip("/") or "/"


def _browser_gpu_mode(config: ComputerConfig) -> str:
    if not config.browser:
        return "auto"
    if config.browser.gpu_mode:
        return config.browser.gpu_mode
    return "auto"


def _connect_token_parts(token_info: object) -> tuple[str, str | None]:
    if isinstance(token_info, str):
        if token_info.startswith(("http://", "https://")):
            return _connect_url_parts(token_info, token=None)
        return "https://connect.modal.run", token_info
    base_url = (
        getattr(token_info, "url", None)
        or getattr(token_info, "base_url", None)
        or getattr(token_info, "web_url", None)
    )
    token = getattr(token_info, "token", None) or getattr(token_info, "secret", None)
    if not base_url:
        raise SandboxUnavailableError(
            "could not infer connect-token base URL from Modal SDK response"
        )
    return _connect_url_parts(str(base_url), token=str(token) if token else None)


def _connect_url_parts(base_url: str, *, token: str | None) -> tuple[str, str | None]:
    parts = urlsplit(base_url)
    query_token = parse_qs(parts.query).get("_modal_connect_token", [None])[0]
    safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return safe_url, token or query_token


def _encrypted_ports_for_ingress(
    ingress: ModalIngress,
    *,
    vnc_mode: str,
    http2: bool = False,
) -> list[int]:
    ports: list[int] = []
    if ingress in {"attested-tunnel", "tunnel"} and not http2:
        ports.append(8080)
    if vnc_mode != "off":
        ports.append(6080)
    return ports


def _h2_ports_for_ingress(ingress: ModalIngress, *, http2: bool = False) -> list[int]:
    if ingress in {"attested-tunnel", "tunnel"} and http2:
        return [8080]
    return []


def _client_ingress_parts(
    sandbox: object,
    *,
    ingress: ModalIngress,
    connect_base_url: str,
    connect_token: str | None,
    tunnel_token: str | None,
) -> tuple[str, str | None]:
    if ingress == "connect" or ingress == "attested-tunnel":
        return connect_base_url, connect_token
    if ingress == "tunnel":
        if not tunnel_token:
            raise SandboxUnavailableError("tunnel ingress requires a daemon bearer token")
        return _tunnel_url(sandbox, 8080), tunnel_token
    raise ValueError("ingress must be attested-tunnel, connect, or tunnel")


def _attested_tunnel_parts(
    sandbox: object,
    *,
    connect_base_url: str,
    connect_token: str | None,
) -> tuple[str, str]:
    connect_client = DaemonClient(base_url=connect_base_url, token=connect_token)
    try:
        payload = connect_client.post_json("/v1/session/tunnel-authorize")
    finally:
        connect_client.close()
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise SandboxUnavailableError("daemon did not return an attested tunnel token")
    return _tunnel_url(sandbox, 8080), token


def _normalize_reuse_policy(reuse: bool | ReusePolicy) -> ReusePolicy:
    if reuse is True:
        return "by_run_id"
    if reuse is False:
        return "never"
    if reuse in ("by_run_id", "by_name", "never"):
        return reuse
    raise ValueError("reuse must be 'by_run_id', 'by_name', 'never', True, or False")


def _session_handoff_policy(
    config: ComputerConfig,
    *,
    session_id: str | None,
    app_name: str,
    vnc_mode: Literal["off", "view_only", "control"] | None = None,
    config_hash: str,
) -> _SessionHandoffPolicy:
    return _SessionHandoffPolicy(
        session_id=session_id,
        app_name=app_name,
        modal_environment=config.runtime.modal_environment,
        modal_region=config.runtime.modal_region,
        ingress=config.ingress,
        daemon_http_version=config.network.daemon_http_version,
        vnc_mode=vnc_mode or normalize_vnc_mode(config.expose_vnc),
        config_hash=config_hash,
    )


def _session_policy_id_prefix(
    *,
    app_name: str,
    modal_environment: str,
    requested_modal_region: str,
    ingress: str,
    daemon_http_version: str,
    vnc_mode: str,
    config_hash: str,
) -> str:
    """Bind handoff policy into the existing session tag without consuming tag budget."""
    policy = "\0".join(
        (
            app_name,
            modal_environment,
            requested_modal_region,
            ingress,
            daemon_http_version,
            vnc_mode,
            config_hash,
        )
    ).encode()
    return hashlib.sha256(policy).hexdigest()[:16]


def _borrow_modal_function_session(
    handle: ComputerSessionHandle,
    *,
    run_id: str,
    readiness_timeout: float,
) -> tuple[BorrowedComputer, object, DaemonClient, object]:
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "session borrowing requires the modal extra"
        ) from exc
    sandbox = modal.Sandbox.from_id(handle.sandbox_id)
    transport: HTTPTransport | None = None
    coordinator: object | None = None
    try:
        tags = _read_modal_object_tags(sandbox)
        if not _live_borrow_target_matches(handle, sandbox=sandbox, tags=tags):
            raise SessionTargetMismatchError()
        token_info = sandbox.create_connect_token(
            user_metadata={"sdk": "modal-computer-use", "version": __version__}
        )
        connect_base_url, connect_token = _connect_token_parts(token_info)
        base_url, token = _borrow_ingress_parts_sync(
            sandbox,
            handle=handle,
            connect_base_url=connect_base_url,
            connect_token=connect_token,
        )
        transport = HTTPTransport(
            base_url,
            token=token,
            http2=handle.daemon_http_version == "2",
        )
        from .session_lease import SessionLeaseCoordinator

        coordinator = SessionLeaseCoordinator(transport, run_id=run_id)
        coordinator.acquire()
        client = DaemonClient(
            base_url,
            transport=transport,
            _mutation_executor=coordinator.execute,
        )
        _wait_borrowed_ready_sync(client, readiness_timeout)
        return (
            BorrowedComputer(
                client,
                coordinator,
                base_url=base_url,
                token=token,
                http2=handle.daemon_http_version == "2",
            ),
            sandbox,
            client,
            coordinator,
        )
    except BaseException:
        if coordinator is not None:
            with suppress(Exception):
                coordinator.close()
        if transport is not None:
            with suppress(Exception):
                transport.close()
        _detach_after_failed_borrow(sandbox)
        raise


async def _borrow_modal_function_session_async(
    handle: ComputerSessionHandle,
    *,
    run_id: str,
    readiness_timeout: float,
) -> tuple[AsyncBorrowedComputer, object, AsyncDaemonClient, object]:
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "session borrowing requires the modal extra"
        ) from exc
    sandbox = await modal.Sandbox.from_id.aio(handle.sandbox_id)
    transport: AsyncHTTPTransport | None = None
    heartbeat_transport: HTTPTransport | None = None
    coordinator: object | None = None
    try:
        tags = await _read_modal_object_tags_async(sandbox)
        if not _live_borrow_target_matches(handle, sandbox=sandbox, tags=tags):
            raise SessionTargetMismatchError()
        token_info = await sandbox.create_connect_token.aio(
            user_metadata={"sdk": "modal-computer-use", "version": __version__}
        )
        connect_base_url, connect_token = _connect_token_parts(token_info)
        base_url, token = await _borrow_ingress_parts_async(
            sandbox,
            handle=handle,
            connect_base_url=connect_base_url,
            connect_token=connect_token,
        )
        transport = AsyncHTTPTransport(
            base_url,
            token=token,
            timeout=_ASYNC_BORROW_REQUEST_TIMEOUT_SECONDS,
            http2=handle.daemon_http_version == "2",
        )
        heartbeat_transport = HTTPTransport(
            base_url,
            token=token,
            timeout=_ASYNC_BORROW_REQUEST_TIMEOUT_SECONDS,
            http2=handle.daemon_http_version == "2",
        )
        from .session_lease import AsyncSessionLeaseCoordinator

        coordinator = AsyncSessionLeaseCoordinator(
            transport,
            run_id=run_id,
            heartbeat_transport=heartbeat_transport,
            heartbeat_join_timeout_seconds=_ASYNC_HEARTBEAT_JOIN_TIMEOUT_SECONDS,
        )
        heartbeat_transport = None
        await coordinator.acquire()
        client = AsyncDaemonClient(
            base_url,
            transport=transport,
            _mutation_executor=coordinator.execute,
        )
        await _wait_borrowed_ready_async(client, readiness_timeout)
        return (
            AsyncBorrowedComputer(
                client,
                coordinator,
                base_url=base_url,
                token=token,
                http2=handle.daemon_http_version == "2",
            ),
            sandbox,
            client,
            coordinator,
        )
    except BaseException:
        await _cleanup_failed_borrow_async(
            sandbox,
            coordinator=coordinator,
            transport=transport,
            heartbeat_transport=heartbeat_transport,
        )
        raise


def _live_borrow_target_matches(
    handle: ComputerSessionHandle,
    *,
    sandbox: object,
    tags: dict[str, str] | None,
) -> bool:
    return bool(
        getattr(sandbox, "object_id", None) == handle.sandbox_id
        and isinstance(tags, dict)
        and tags.get("computer-use") == "true"
        and tags.get("computer-use.config_hash") == handle.config_hash
        and tags.get("computer-use.session_id") == handle.session_id
        and handle.session_id.startswith(
            _session_policy_id_prefix(
                app_name=handle.app_name,
                modal_environment=handle.modal_environment,
                requested_modal_region=handle.requested_modal_region,
                ingress=handle.ingress,
                daemon_http_version=handle.daemon_http_version,
                vnc_mode=handle.vnc_mode,
                config_hash=handle.config_hash,
            )
        )
    )


def _borrow_ingress_parts_sync(
    sandbox: object,
    *,
    handle: ComputerSessionHandle,
    connect_base_url: str,
    connect_token: str | None,
) -> tuple[str, str | None]:
    if handle.ingress == "connect":
        return connect_base_url, connect_token
    return _attested_tunnel_parts(
        sandbox,
        connect_base_url=connect_base_url,
        connect_token=connect_token,
    )


async def _borrow_ingress_parts_async(
    sandbox: object,
    *,
    handle: ComputerSessionHandle,
    connect_base_url: str,
    connect_token: str | None,
) -> tuple[str, str | None]:
    if handle.ingress == "connect":
        return connect_base_url, connect_token
    connect_client = AsyncDaemonClient(connect_base_url, token=connect_token)
    try:
        payload = await connect_client.post_json("/v1/session/tunnel-authorize")
    finally:
        await connect_client.aclose()
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise SandboxUnavailableError("daemon did not authorize the selected ingress")
    tunnels = await sandbox.tunnels.aio()
    tunnel = tunnels.get(8080) if isinstance(tunnels, dict) else None
    value = None if tunnel is None else getattr(tunnel, "url", None)
    if not value:
        raise SandboxUnavailableError("the selected daemon ingress is unavailable")
    return str(value).rstrip("/"), token


def _wait_borrowed_ready_sync(client: DaemonClient, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        with suppress(Exception):
            if client.get_json("/readyz").get("ready") is True:
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("borrowed daemon readiness timed out")
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


async def _wait_borrowed_ready_async(client: AsyncDaemonClient, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        with suppress(Exception):
            if (await client.get_json("/readyz")).get("ready") is True:
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("borrowed daemon readiness timed out")
        await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _detach_after_failed_borrow(sandbox: object) -> None:
    detach = getattr(sandbox, "detach", None)
    if callable(detach):
        with suppress(Exception):
            detach()


async def _detach_after_failed_borrow_async(sandbox: object) -> None:
    detach = getattr(sandbox, "detach", None)
    aio = getattr(detach, "aio", None)
    if callable(aio):
        task = asyncio.create_task(aio())
        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    with suppress(BaseException):
                        await task
                    return
                continue
            except Exception:
                return
            else:
                return


async def _cleanup_failed_borrow_async(
    sandbox: object,
    *,
    coordinator: Any | None,
    transport: AsyncHTTPTransport | None,
    heartbeat_transport: HTTPTransport | None,
) -> None:
    async def cleanup() -> None:
        if coordinator is not None:
            with suppress(BaseException):
                await coordinator.aclose()
        if transport is not None:
            with suppress(BaseException):
                await transport.aclose()
        if heartbeat_transport is not None:
            with suppress(BaseException):
                heartbeat_transport.close()
        await _detach_after_failed_borrow_async(sandbox)

    task = asyncio.create_task(cleanup())
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                with suppress(BaseException):
                    await task
                return
            continue
        except BaseException:
            return
        else:
            return


async def _read_modal_object_tags_async(target: object) -> dict[str, str] | None:
    get_tags = getattr(target, "get_tags", None)
    aio = getattr(get_tags, "aio", None)
    if not callable(aio):
        return None
    raw_tags = await aio()
    if isinstance(raw_tags, dict):
        return {str(key): str(value) for key, value in raw_tags.items()}
    return None


def _metadata_from_sandbox(sandbox: object, *, app_name: str) -> SandboxRef:
    from .registry import SandboxRegistry

    return SandboxRegistry(app_name=app_name).ref_from_sandbox(sandbox)


def _check_config_hash(
    metadata: SandboxRef | None,
    *,
    requested_hash: str,
    on_config_mismatch: ConfigMismatchPolicy,
) -> None:
    if metadata is None or metadata.config_hash is None:
        return
    if metadata.config_hash == requested_hash:
        return
    if on_config_mismatch == "reuse":
        return
    raise ConfigConflictError(
        "existing sandbox config_hash does not match requested config; "
        "terminate it, attach by sandbox_id intentionally, or pass on_config_mismatch='reuse'",
        requested_hash=requested_hash,
        existing_hash=metadata.config_hash,
        sandbox_id=metadata.sandbox_id,
    )


def _readiness_probe(modal: object) -> object | None:
    probe = getattr(modal, "Probe", None)
    with_tcp = getattr(probe, "with_tcp", None)
    if with_tcp is None:
        return None
    return with_tcp(8080)


def _sandbox_from_name(modal: object, *, app_name: str, name: str) -> object:
    try:
        return modal.Sandbox.from_name(app_name, name)
    except TypeError:
        app = modal.App.lookup(app_name, create_if_missing=False)
        return modal.Sandbox.from_name(name, app=app)


def _vnc_url(sandbox: object) -> str | None:
    try:
        tunnels = sandbox.tunnels()
    except Exception:
        return None
    tunnel = tunnels.get(6080) if isinstance(tunnels, dict) else None
    if tunnel is None:
        return None
    return str(getattr(tunnel, "url", None) or getattr(tunnel, "tcp_socket", None) or "")


def _tunnel_url(sandbox: object, port: int) -> str:
    try:
        tunnels = sandbox.tunnels()
    except Exception as exc:
        raise SandboxUnavailableError(f"could not retrieve Modal tunnel for port {port}") from exc
    tunnel = tunnels.get(port) if isinstance(tunnels, dict) else None
    value = None if tunnel is None else getattr(tunnel, "url", None)
    if not value:
        raise SandboxUnavailableError(f"Modal tunnel for port {port} is not available")
    return str(value).rstrip("/")


def _readiness_timeout_detail(payload: object | None, error: Exception | None) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            suffix = "" if len(errors) == 1 else "s"
            return f"; last /readyz reported {len(errors)} error{suffix}"
        if payload:
            return "; last /readyz response was not ready"
    if error is not None:
        return f"; last error type: {type(error).__name__}"
    return ""


def _created_at_from_tags(tags: dict[str, str]) -> datetime | None:
    value = tags.get("computer-use.created_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _set_modal_object_tags(target: object, tags: dict[str, str]) -> None:
    set_tags = getattr(target, "set_tags", None)
    if not callable(set_tags):
        return
    set_tags({**_get_modal_object_tags(target), **tags})


def _replace_modal_object_tags(target: object, tags: dict[str, str]) -> None:
    set_tags = getattr(target, "set_tags", None)
    if callable(set_tags):
        set_tags(tags)


def _get_modal_object_tags(target: object) -> dict[str, str]:
    return _read_modal_object_tags(target) or {}


def _read_modal_object_tags(target: object) -> dict[str, str] | None:
    get_tags = getattr(target, "get_tags", None)
    if callable(get_tags):
        raw_tags = get_tags()
        if isinstance(raw_tags, dict):
            return {str(key): str(value) for key, value in raw_tags.items()}
    return None
