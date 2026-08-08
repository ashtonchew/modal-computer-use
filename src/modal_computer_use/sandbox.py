from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets as _secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast
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
    SessionDaemonProtocolError,
    SessionEnvironmentMismatchError,
    SessionLeaseLostError,
    SessionPlacementMalformedError,
    SessionPlacementMismatchError,
    SessionPlacementMissingError,
    SessionPlacementUnverifiableError,
    SessionTargetMismatchError,
)
from .hot_session import AsyncHotSessionClient, HotSessionClient
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
from .observations import AsyncObservationClient, ObservationClient
from .protocol_compatibility import validate_default_trajectory_protocol
from .state import APP_ID_TAG, compute_config_hash, default_tags, new_run_id, warm_pool_tags
from .transports import (
    AsyncHTTPTransport,
    HotSessionTransport,
    HTTPTransport,
    ObservationStreamTransport,
)

_SandboxLifecycleMode = Literal["local", "owned", "attached", "detached"]
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
_NAMED_ACQUISITION_ATTEMPTS = 3
_NAMED_ACQUISITION_BACKOFF_SECONDS = 0.05
_NAMED_ACQUISITION_REMOVED_KWARGS = frozenset(
    {
        "allow_legacy_unscoped",
        "on_config_mismatch",
        "readiness_timeout",
        "reuse",
        "tag_profile",
        "wait",
    }
)
_SECURITY_OWNED_SANDBOX_KWARGS = frozenset(
    {
        "app",
        "block_network",
        "encrypted_ports",
        "env",
        "h2_ports",
        "inbound_cidr_allowlist",
        "outbound_cidr_allowlist",
        "outbound_domain_allowlist",
        "readiness_probe",
        "tags",
    }
)


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


@dataclass(frozen=True, repr=False)
class _SandboxCreateInputs:
    config: ComputerConfig
    app_name: str
    name: str | None
    image: object | None
    custom_image_supplied: bool
    artifact_volume_mounted: bool
    vnc_mode: Literal["off", "view_only", "control"]
    caller_tags: Mapping[str, str]
    app_tags: Mapping[str, str]
    secrets: tuple[object, ...]
    volumes: Mapping[str, object]
    owner: str | None
    tag_profile: Literal["default", "warm_pool"]
    sandbox_kwargs: Mapping[str, Any]
    app_lookup_kwargs: Mapping[str, object]


@dataclass(frozen=True, repr=False)
class _SandboxCreatePlan:
    inputs: _SandboxCreateInputs
    sandbox_tags: Mapping[str, str]
    config_hash: str
    session_id: str | None
    daemon_bearer: str
    http2: bool
    create_kwargs: Mapping[str, Any]


@dataclass(frozen=True)
class _AttachSelector:
    kind: Literal["sandbox_id", "name", "run_id", "base_url"]
    value: str


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
                    APP_ID_TAG: _modal_app_id(self.app),
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


def _is_modal_already_exists_error(exc: Exception) -> bool:
    try:
        from modal.exception import AlreadyExistsError
    except ImportError:
        return False
    return isinstance(exc, AlreadyExistsError)


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
        result = wait_until_ready(*args, **kwargs)
        self._timing.mark("tcp_ready")
        return result

    def _create_connect_access(self, *args: object, **kwargs: object) -> object:
        create_access = getattr(  # noqa: B009 - dynamic Modal SDK type
            self._sandbox, "create_connect_token"
        )
        result = create_access(*args, **kwargs)
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
        "tags": {**(tags or {}), APP_ID_TAG: _modal_app_id(app)},
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
                    f"runner cleanup also failed: terminate ({type(cleanup_exc).__name__})"
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
    image = named_image(revision=image_revision, profile="browser", browser="chromium")
    return run_modal_benchmark_function_with_image_once(
        entrypoint,
        config=config,
        run_tag=run_tag,
        app_name=f"{app_name}-optimized-provider-runner",
        region=region,
        environment_name=None,
        image=image,
        cpu=cpu,
        memory_mib=memory_mib,
        timeout_seconds=timeout_seconds,
        retries=retries,
    )


def run_modal_benchmark_function_with_image_once(
    entrypoint: Callable[..., dict[str, Any]],
    *,
    config: object,
    run_tag: str,
    app_name: str,
    region: str,
    environment_name: str | None,
    image: object,
    cpu: float,
    memory_mib: int,
    timeout_seconds: int,
    cpu_limit: float | None = None,
    memory_limit_mib: int | None = None,
    retries: int = 0,
) -> dict[str, Any]:
    """Invoke one regional benchmark Function from an exact caller Image."""
    if retries != 0:
        raise ValueError("benchmark Function retries must be disabled")
    if not region.strip():
        raise ValueError("benchmark Function region must be explicit")
    if environment_name is not None and not environment_name.strip():
        raise ValueError("benchmark Function environment must be non-empty")
    if (cpu_limit is None) != (memory_limit_mib is None):
        raise ValueError("benchmark Function resource limits must be provided together")
    if cpu_limit is not None and cpu_limit < cpu:
        raise ValueError("benchmark Function CPU limit must not be below its request")
    if memory_limit_mib is not None and memory_limit_mib < memory_mib:
        raise ValueError("benchmark Function memory limit must not be below its request")
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError("Modal benchmark Function requires the modal extra") from exc

    app = modal.App(app_name)
    remote = app.function(
        image=image,
        region=region,
        cpu=(cpu, cpu_limit) if cpu_limit is not None else cpu,
        memory=(memory_mib, memory_limit_mib)
        if memory_limit_mib is not None
        else memory_mib,
        timeout=timeout_seconds,
        retries=0,
        min_containers=0,
        max_containers=1,
        single_use_containers=True,
        # The exact Image owns this importable module. Avoid cloudpickling the
        # Function or mounting caller source, so its release identity stays exact.
        serialized=False,
        include_source=False,
    )(entrypoint)
    run_options = (
        {"environment_name": environment_name}
        if environment_name is not None
        else {}
    )
    with app.run(**run_options):
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
            APP_ID_TAG: _modal_app_id(app),
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
        APP_ID_TAG: _modal_app_id(app),
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
        cleanup_errors: list[tuple[str, BaseException]] = []
        for operation_name, cleanup in (
            ("lease_coordinator.close", coordinator.close),
            ("client.close", client.close),
            ("sandbox.detach", sandbox.detach),
        ):
            try:
                cleanup()
            except BaseException as cleanup_exc:
                cleanup_errors.append((operation_name, cleanup_exc))
        if cleanup_errors:
            if isinstance(exc, BaseException):
                for operation_name, cleanup_error in cleanup_errors:
                    exc.add_note(
                        "borrowed session cleanup also failed: "
                        f"{operation_name} ({type(cleanup_error).__name__})"
                    )
                return
            cleanup_failure = SessionLeaseLostError()
            for operation_name, cleanup_error in cleanup_errors:
                cleanup_failure.add_note(
                    "borrowed session cleanup failed: "
                    f"{operation_name} ({type(cleanup_error).__name__})"
                )
            raise cleanup_failure from None


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

        async def cleanup() -> list[tuple[str, BaseException]]:
            cleanup_errors: list[tuple[str, BaseException]] = []
            for operation_name, operation in (
                ("lease_coordinator.aclose", coordinator.aclose),
                ("client.aclose", client.aclose),
                ("sandbox.detach.aio", sandbox.detach.aio),
            ):
                try:
                    await operation()
                except BaseException as cleanup_exc:
                    cleanup_errors.append((operation_name, cleanup_exc))
            return cleanup_errors

        task = asyncio.create_task(cleanup())
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                cleanup_errors = await asyncio.shield(task)
            except asyncio.CancelledError as cleanup_cancelled:
                if task.cancelled():
                    raise
                cancellation = cancellation or cleanup_cancelled
                continue
            if isinstance(exc, BaseException):
                for operation_name, cleanup_error in cleanup_errors:
                    exc.add_note(
                        "borrowed session cleanup also failed: "
                        f"{operation_name} ({type(cleanup_error).__name__})"
                    )
                if cancellation is not None:
                    exc.add_note("borrowed session cleanup also observed cancellation")
                return
            if cancellation is not None:
                for operation_name, cleanup_error in cleanup_errors:
                    cancellation.add_note(
                        "borrowed session cleanup also failed: "
                        f"{operation_name} ({type(cleanup_error).__name__})"
                    )
                raise cancellation
            if cleanup_errors:
                cleanup_failure = SessionLeaseLostError()
                for operation_name, cleanup_error in cleanup_errors:
                    cleanup_failure.add_note(
                        "borrowed session cleanup failed: "
                        f"{operation_name} ({type(cleanup_error).__name__})"
                    )
                raise cleanup_failure from None
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
        raise SessionPlacementMissingError
    if not _is_modal_region_selector(function_region):
        raise SessionPlacementMalformedError
    if not _is_exact_modal_region_selector(function_region):
        raise SessionPlacementUnverifiableError
    requested_region = handle.requested_modal_region
    if not _is_modal_region_selector(requested_region):
        raise SessionPlacementMalformedError
    if not _is_exact_modal_region_selector(requested_region):
        raise SessionPlacementUnverifiableError
    if function_region != handle.requested_modal_region:
        raise SessionPlacementMismatchError
    observed_function_region = os.environ.get("MODAL_REGION")
    if observed_function_region is None or not observed_function_region.strip():
        raise SessionPlacementMissingError
    if not _is_modal_region_selector(observed_function_region):
        raise SessionPlacementMalformedError
    if not _is_exact_modal_region_selector(observed_function_region):
        raise SessionPlacementUnverifiableError
    if observed_function_region != requested_region:
        raise SessionPlacementMismatchError
    if handle.vnc_mode not in {"off", "view_only"}:
        raise SessionCompatibilityError


def _is_modal_region_selector(value: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", value) is not None


def _is_exact_modal_region_selector(value: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9]*-[a-z][a-z0-9]*-[0-9][a-z0-9]*", value) is not None


def _validate_placed_owner_configuration(inputs: _SandboxCreateInputs) -> None:
    """Reject an owner that cannot produce a verifiably placed handoff."""
    config = inputs.config
    environment = config.runtime.modal_environment
    if not isinstance(environment, str) or not environment.strip():
        raise SessionEnvironmentMismatchError

    region = config.runtime.modal_region
    if not isinstance(region, str) or not region.strip():
        raise SessionPlacementMissingError
    if not _is_modal_region_selector(region):
        raise SessionPlacementMalformedError
    if not _is_exact_modal_region_selector(region):
        raise SessionPlacementUnverifiableError
    if config.ingress not in {"attested-tunnel", "connect"}:
        raise SessionCompatibilityError
    if inputs.vnc_mode == "control":
        raise SessionCompatibilityError
    if inputs.tag_profile != "default":
        raise SessionCompatibilityError


def _prepare_sandbox_create_inputs(
    *,
    config: ComputerConfig | None,
    app_name: str,
    name: str | None,
    image: object | None,
    expose_vnc: bool | str | None,
    tags: Mapping[str, str] | None,
    app_tags: Mapping[str, str] | None,
    secrets: Sequence[object] | None,
    volumes: Mapping[str, object] | None,
    owner: str | None,
    tag_profile: Literal["default", "warm_pool"],
    sandbox_kwargs: Mapping[str, Any],
    generate_run_id: bool = True,
) -> _SandboxCreateInputs:
    _reject_security_owned_sandbox_kwargs(sandbox_kwargs)
    resolved_config = ComputerConfig() if config is None else config.model_copy(deep=True)
    if generate_run_id and not resolved_config.run_id:
        resolved_config.run_id = new_run_id()
    prepared_volumes = _prepare_volume_mounts(dict(volumes or {}))
    artifact_volume_mounted = _has_artifact_volume_mount(
        prepared_volumes,
        resolved_config.storage.artifacts_dir,
    )
    if resolved_config.storage.persist_artifacts and not artifact_volume_mounted:
        raise ConfigConflictError(
            "persist_artifacts=True requires a Volume mounted at storage.artifacts_dir "
            "or one of its parent directories"
        )
    vnc_mode = normalize_vnc_mode(
        expose_vnc if expose_vnc is not None else resolved_config.expose_vnc
    )
    custom_image_supplied = image is not None
    app_lookup_kwargs: dict[str, object] = {"create_if_missing": True}
    if resolved_config.runtime.modal_environment is not None:
        app_lookup_kwargs["environment_name"] = resolved_config.runtime.modal_environment
    return _SandboxCreateInputs(
        config=resolved_config,
        app_name=app_name,
        name=name,
        image=image,
        custom_image_supplied=custom_image_supplied,
        artifact_volume_mounted=artifact_volume_mounted,
        vnc_mode=vnc_mode,
        caller_tags=MappingProxyType(dict(tags or {})),
        app_tags=MappingProxyType(dict(app_tags or {})),
        secrets=tuple(secrets or ()),
        volumes=MappingProxyType(prepared_volumes),
        owner=owner,
        tag_profile=tag_profile,
        sandbox_kwargs=MappingProxyType(dict(sandbox_kwargs)),
        app_lookup_kwargs=MappingProxyType(app_lookup_kwargs),
    )


def _prepare_named_attach_or_create_inputs(
    *,
    name: str,
    config: ComputerConfig | None,
    app_name: str,
    run_id: str | None,
    image: object | None,
    expose_vnc: bool | str | None,
    tags: Mapping[str, str] | None,
    app_tags: Mapping[str, str] | None,
    secrets: Sequence[object] | None,
    volumes: Mapping[str, object] | None,
    owner: str | None,
    readiness_timeout: float | None,
    tag_profile: Literal["default", "warm_pool"],
    sandbox_kwargs: Mapping[str, Any],
) -> tuple[_SandboxCreateInputs, float]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    unsupported_kwargs = _NAMED_ACQUISITION_REMOVED_KWARGS.intersection(sandbox_kwargs)
    if unsupported_kwargs:
        joined = ", ".join(sorted(unsupported_kwargs))
        raise ValueError(f"unsupported attach_or_create keyword(s): {joined}")
    resolved_config = ComputerConfig() if config is None else config.model_copy(deep=True)
    configured_run_id = resolved_config.run_id
    for value in (configured_run_id, run_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("run_id must be a non-empty string when provided")
    if (
        configured_run_id is not None
        and run_id is not None
        and configured_run_id != run_id
    ):
        raise ValueError("config.run_id and run_id must match when both are provided")
    if run_id is not None:
        resolved_config.run_id = run_id
    timeout = (
        float(resolved_config.runtime.readiness_timeout_seconds)
        if readiness_timeout is None
        else readiness_timeout
    )
    _validate_async_readiness_timeout(timeout)
    return (
        _prepare_sandbox_create_inputs(
            config=resolved_config,
            app_name=app_name,
            name=name,
            image=image,
            expose_vnc=expose_vnc,
            tags=tags,
            app_tags=app_tags,
            secrets=secrets,
            volumes=volumes,
            owner=owner,
            tag_profile=tag_profile,
            sandbox_kwargs=sandbox_kwargs,
            generate_run_id=False,
        ),
        timeout,
    )


def _validate_named_existing_sandbox(
    modal: object,
    *,
    sandbox: object,
    inputs: _SandboxCreateInputs,
) -> tuple[SandboxRef, ComputerConfig]:
    try:
        lookup_kwargs: dict[str, object] = {"create_if_missing": False}
        modal_environment = inputs.config.runtime.modal_environment
        if modal_environment is not None:
            lookup_kwargs["environment_name"] = modal_environment
        app = modal.App.lookup(inputs.app_name, **lookup_kwargs)
        tags = _read_modal_object_tags(sandbox) or {}
        _require_async_app_tag(
            tags,
            app_id=_modal_app_id(app),
            allow_legacy_unscoped=False,
            description=f"sandbox name={inputs.name}",
        )
        metadata = _metadata_from_sandbox_tags(
            sandbox,
            app_name=inputs.app_name,
            tags=tags,
        )
        existing_hash = metadata.config_hash
        if not existing_hash:
            raise ConfigConflictError(
                "existing named Sandbox is missing computer-use.config_hash",
                sandbox_id=metadata.sandbox_id,
            )
        resolved_config = inputs.config.model_copy(deep=True)
        tagged_run_id = metadata.run_id
        if not tagged_run_id:
            raise ConfigConflictError(
                "existing named Sandbox is missing computer-use.run_id",
                sandbox_id=metadata.sandbox_id,
            )
        if (
            resolved_config.run_id is not None
            and resolved_config.run_id != tagged_run_id
        ):
            raise ConfigConflictError(
                "existing named Sandbox run_id does not match the requested run_id",
                sandbox_id=metadata.sandbox_id,
            )
        if resolved_config.run_id is None:
            resolved_config.run_id = tagged_run_id
        requested_hash = compute_config_hash(resolved_config)
        if existing_hash != requested_hash:
            raise ConfigConflictError(
                "existing named Sandbox config_hash does not match the requested config",
                requested_hash=requested_hash,
                existing_hash=existing_hash,
                sandbox_id=metadata.sandbox_id,
            )
        return metadata, resolved_config
    except BaseException as exc:
        _cleanup_failed_attached_sandbox(sandbox, client=None, primary=exc)
        raise


def _resolve_sandbox_create_image(inputs: _SandboxCreateInputs) -> object:
    if inputs.image is not None:
        return inputs.image
    config = inputs.config
    browser_kind = config.browser.kind if config.browser else None
    if config.image.source == "named":
        return named_image(
            revision=config.image.revision or "",
            profile=config.resources.profile,
            browser=browser_kind,
            environment_name=config.image.environment_name,
        )
    return default_image(
        profile=config.resources.profile,
        browser=browser_kind,
        window_manager=config.desktop.window_manager,
        browser_prewarm=config.browser.prewarm if config.browser else False,
    )


def _build_sandbox_create_plan(
    inputs: _SandboxCreateInputs,
    *,
    app: object,
    modal: object,
    image: object,
) -> _SandboxCreatePlan:
    config = inputs.config
    env = _daemon_environment(
        config,
        vnc_mode=inputs.vnc_mode,
        artifact_volume_mounted=inputs.artifact_volume_mounted,
    )
    app_id = _modal_app_id(app)
    base_tags = (
        warm_pool_tags(app_id=app_id)
        if inputs.tag_profile == "warm_pool"
        else default_tags(config, app_id=app_id, owner=inputs.owner)
    )
    sandbox_tags = {**inputs.caller_tags, **base_tags}
    config_hash = compute_config_hash(config)
    session_id: str | None = None
    if config.runtime.modal_environment is not None and config.runtime.modal_region is not None:
        session_id = _session_policy_id_prefix(
            app_name=inputs.app_name,
            modal_environment=config.runtime.modal_environment,
            requested_modal_region=config.runtime.modal_region,
            ingress=config.ingress,
            daemon_http_version=config.network.daemon_http_version,
            vnc_mode=inputs.vnc_mode,
            config_hash=config_hash,
        ) + _secrets.token_hex(8)
        sandbox_tags["computer-use.session_id"] = session_id
    browser_kind = config.browser.kind if config.browser else None
    if inputs.tag_profile != "warm_pool":
        sandbox_tags["computer-use.image_identity"] = (
            "custom"
            if inputs.custom_image_supplied
            else selected_image_identity(
                source=config.image.source,
                revision=config.image.revision,
                profile=config.resources.profile,
                browser=browser_kind,
            )
        )
    _validate_sandbox_tags(sandbox_tags)
    http2 = config.network.daemon_http_version == "2"
    ports = _encrypted_ports_for_ingress(
        config.ingress,
        vnc_mode=inputs.vnc_mode,
        http2=http2,
    )
    h2_ports = _h2_ports_for_ingress(config.ingress, http2=http2)
    daemon_bearer = _secrets.token_urlsafe(32)
    env["COMPUTER_USE_TUNNEL_TOKEN"] = daemon_bearer
    create_kwargs: dict[str, Any] = {
        "app": app,
        "image": image,
        "cpu": config.resources.cpu,
        "memory": config.resources.memory_mib,
        "gpu": config.resources.gpu,
        "encrypted_ports": ports,
        "timeout": config.runtime.timeout_seconds,
        "idle_timeout": config.runtime.idle_timeout_seconds,
        "secrets": list(inputs.secrets),
        "volumes": dict(inputs.volumes),
        "env": env,
        "block_network": config.network.block_all,
        "outbound_cidr_allowlist": config.network.outbound_cidr_allowlist,
        "outbound_domain_allowlist": config.network.outbound_domain_allowlist,
        "inbound_cidr_allowlist": config.network.inbound_cidr_allowlist,
        "name": inputs.name,
        "tags": sandbox_tags,
        **inputs.sandbox_kwargs,
    }
    if h2_ports:
        create_kwargs["h2_ports"] = h2_ports
    if config.runtime.modal_region:
        create_kwargs["region"] = config.runtime.modal_region
    readiness_probe = _readiness_probe(modal)
    if readiness_probe is not None:
        create_kwargs["readiness_probe"] = readiness_probe
    return _SandboxCreatePlan(
        inputs=inputs,
        sandbox_tags=MappingProxyType(sandbox_tags),
        config_hash=config_hash,
        session_id=session_id,
        daemon_bearer=daemon_bearer,
        http2=http2,
        create_kwargs=MappingProxyType(create_kwargs),
    )


def _materialize_sandbox_create_kwargs(plan: _SandboxCreatePlan) -> dict[str, Any]:
    kwargs = dict(plan.create_kwargs)
    kwargs["secrets"] = list(plan.inputs.secrets)
    kwargs["volumes"] = dict(plan.inputs.volumes)
    kwargs["env"] = dict(plan.create_kwargs["env"])
    kwargs["tags"] = dict(plan.sandbox_tags)
    return kwargs


def _metadata_from_create_plan(plan: _SandboxCreatePlan, sandbox: object) -> SandboxRef:
    inputs = plan.inputs
    config = inputs.config
    tags = dict(plan.sandbox_tags)
    return SandboxRef(
        sandbox_id=getattr(sandbox, "object_id", "unknown"),
        app_name=inputs.app_name,
        name=inputs.name,
        run_id=config.run_id,
        owner=tags.get("computer-use.owner"),
        created_at=_created_at_from_tags(tags),
        config_hash=plan.config_hash,
        status="started",
        tags=tags,
        vnc_url=None,
        artifacts_dir=config.storage.artifacts_dir,
    )


def _validate_attach_selector(
    *,
    sandbox_id: str | None,
    name: str | None,
    run_id: str | None,
    base_url: str | None,
    token: str | None,
) -> _AttachSelector:
    selectors = {
        "sandbox_id": sandbox_id,
        "name": name,
        "run_id": run_id,
        "base_url": base_url,
    }
    selected = [(kind, value) for kind, value in selectors.items() if value is not None]
    if len(selected) != 1:
        raise ValueError("attach requires exactly one of sandbox_id, name, run_id, or base_url")
    kind, raw_value = selected[0]
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{kind} must be a non-empty string")
    if token is not None and kind != "base_url":
        raise ValueError("token is valid only when attaching by base_url")
    return _AttachSelector(
        kind=cast(Literal["sandbox_id", "name", "run_id", "base_url"], kind),
        value=value,
    )


class ComputerSandbox:
    def __init__(
        self,
        client: DaemonClient,
        *,
        sandbox: object | None = None,
        metadata: SandboxRef | None = None,
        startup_timing: SessionStartupTiming | None = None,
        _lifecycle_mode: _SandboxLifecycleMode | None = None,
    ) -> None:
        self.client = client
        self._sandbox = sandbox
        self._metadata = metadata
        self._requested_modal_region: str | None = None
        self._session_handoff_policy: _SessionHandoffPolicy | None = None
        self._daemon_bearer: str | None = None
        self.startup_timing = startup_timing
        self._lifecycle_mode: _SandboxLifecycleMode = (
            _lifecycle_mode
            if _lifecycle_mode is not None
            else ("owned" if sandbox is not None else "local")
        )
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
        return cls(
            DaemonClient(base_url=base_url, token=token, timeout=timeout),
            _lifecycle_mode="local",
        )

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
        return cls._create_with_readiness_timeout(
            config=config,
            app_name=app_name,
            name=name,
            image=image,
            expose_vnc=expose_vnc,
            tags=tags,
            app_tags=app_tags,
            secrets=secrets,
            volumes=volumes,
            owner=owner,
            wait=wait,
            readiness_timeout=None,
            timing=timing,
            tag_profile=tag_profile,
            sandbox_kwargs=sandbox_kwargs,
        )

    @classmethod
    def _create_with_readiness_timeout(
        cls,
        *,
        config: ComputerConfig | None,
        app_name: str,
        name: str | None,
        image: object | None,
        expose_vnc: bool | str | None,
        tags: dict[str, str] | None,
        app_tags: dict[str, str] | None,
        secrets: list[object] | None,
        volumes: dict[str, object] | None,
        owner: str | None,
        wait: bool,
        readiness_timeout: float | None,
        timing: SessionStartupTiming | None,
        tag_profile: Literal["default", "warm_pool"],
        sandbox_kwargs: Mapping[str, Any],
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
        inputs = _prepare_sandbox_create_inputs(
            config=config,
            app_name=app_name,
            name=name,
            image=image,
            expose_vnc=expose_vnc,
            tags=tags,
            app_tags=app_tags,
            secrets=secrets,
            volumes=volumes,
            owner=owner,
            tag_profile=tag_profile,
            sandbox_kwargs=sandbox_kwargs,
        )
        timeout = (
            float(inputs.config.runtime.readiness_timeout_seconds)
            if readiness_timeout is None
            else readiness_timeout
        )
        _validate_async_readiness_timeout(timeout)
        try:
            import modal
        except ImportError as exc:
            raise ModalNotInstalledError(
                "ComputerSandbox.create requires the modal extra, for example "
                "`uv sync --extra modal` in this repository or "
                "`uv add 'modal-computer-use[modal]'` downstream"
            ) from exc
        modal = _TimedModalRuntime(modal, timing)
        resolved_image = _resolve_sandbox_create_image(inputs)
        app = modal.App.lookup(inputs.app_name, **dict(inputs.app_lookup_kwargs))
        if inputs.app_tags:
            _set_modal_object_tags(app, dict(inputs.app_tags))
        plan = _build_sandbox_create_plan(
            inputs,
            app=app,
            modal=modal,
            image=resolved_image,
        )
        sandbox: object | None = None
        computer: ComputerSandbox | None = None
        try:
            sandbox = modal.Sandbox.create(
                "python",
                "-m",
                "modal_computer_use.daemon",
                **_materialize_sandbox_create_kwargs(plan),
            )
            config = plan.inputs.config
            if wait and hasattr(sandbox, "wait_until_ready"):
                sandbox.wait_until_ready(timeout=timeout)
            if config.ingress == "connect":
                token_info = sandbox.create_connect_token(
                    user_metadata={"sdk": "modal-computer-use", "version": __version__},
                    port=8080,
                )
                connect_base_url, connect_token = _connect_token_parts(token_info)
            else:
                connect_base_url = _tunnel_url(sandbox, 8080)
                connect_token = plan.daemon_bearer
                timing.mark("connect_token_ready")
            base_url, token = _client_ingress_parts(
                sandbox,
                ingress=config.ingress,
                connect_base_url=connect_base_url,
                connect_token=connect_token,
                tunnel_token=plan.daemon_bearer,
            )
            metadata = _metadata_from_create_plan(plan, sandbox)
            token_resolver = None
            if config.ingress == "attested-tunnel" and not wait:
                token = None

                def resolve_attested_token() -> str:
                    return _attested_tunnel_parts(
                        sandbox,
                        connect_base_url=connect_base_url,
                        connect_token=connect_token,
                    )[1]

                token_resolver = resolve_attested_token
            computer = cls(
                DaemonClient(
                    base_url=base_url,
                    token=token,
                    http2=plan.http2,
                    _token_resolver=token_resolver,
                ),
                sandbox=sandbox,
                metadata=metadata,
                _lifecycle_mode="owned",
            )
            computer.startup_timing = timing
            computer._daemon_bearer = plan.daemon_bearer
            if wait:
                computer.wait_until_ready(timeout=timeout)
            if wait and config.ingress == "attested-tunnel":
                computer.client.close()
                base_url, token = _attested_tunnel_parts(
                    sandbox,
                    connect_base_url=connect_base_url,
                    connect_token=connect_token,
                )
                computer = cls(
                    DaemonClient(base_url=base_url, token=token, http2=plan.http2),
                    sandbox=sandbox,
                    metadata=metadata,
                    _lifecycle_mode="owned",
                )
                computer.startup_timing = timing
                computer._daemon_bearer = plan.daemon_bearer
                computer._readiness_stage_count = 1
                timing.mark("attestation_ready")
                computer.wait_until_ready(timeout=timeout)
            computer._requested_modal_region = config.runtime.modal_region
            computer._session_handoff_policy = _session_handoff_policy(
                config,
                session_id=plan.session_id,
                app_name=inputs.app_name,
                vnc_mode=inputs.vnc_mode,
                config_hash=metadata.config_hash or plan.config_hash,
            )
            return computer
        except BaseException as exc:
            if sandbox is not None:
                _cleanup_failed_created_sandbox(
                    sandbox,
                    client=None if computer is None else computer.client,
                    primary=exc,
                )
            raise

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
        modal_environment: str | None = None,
        allow_legacy_unscoped: bool = False,
    ) -> ComputerSandbox:
        selector = _validate_attach_selector(
            sandbox_id=sandbox_id,
            name=name,
            run_id=run_id,
            base_url=base_url,
            token=token,
        )
        if selector.kind == "base_url":
            computer = cls(
                DaemonClient(base_url=selector.value, token=token, http2=http2),
                _lifecycle_mode="attached",
            )
            if wait:
                try:
                    computer.wait_until_ready(timeout=readiness_timeout)
                except BaseException as exc:
                    try:
                        computer.client.close()
                    except BaseException as cleanup_exc:
                        _note_cleanup_failure(exc, "client.close", cleanup_exc)
                    raise
            return computer
        try:
            import modal
        except ImportError as exc:
            raise ModalNotInstalledError(
                "ComputerSandbox.attach requires the modal extra, for example "
                "`uv sync --extra modal` in this repository or "
                "`uv add 'modal-computer-use[modal]'` downstream"
            ) from exc
        from .registry import SandboxRegistry

        registry = SandboxRegistry(
            app_name=app_name,
            environment_name=modal_environment,
            allow_legacy_unscoped=allow_legacy_unscoped,
        )
        if selector.kind == "sandbox_id":
            sandbox = registry.require_sandbox_by_id(selector.value)
        elif selector.kind == "name":
            sandbox = _sandbox_from_name(
                modal,
                app_name=app_name,
                name=selector.value,
                environment_name=modal_environment,
            )
            try:
                registry.require_app_tag(sandbox, description="sandbox name")
            except BaseException as exc:
                _cleanup_failed_attached_sandbox(sandbox, client=None, primary=exc)
                raise
        else:
            sandbox = registry.require_sandbox_by_run_id(selector.value)
        return cls._attach_resolved_sandbox(
            sandbox,
            app_name=app_name,
            ingress=ingress,
            http2=http2,
            wait=wait,
            readiness_timeout=readiness_timeout,
        )

    @classmethod
    def _attach_resolved_sandbox(
        cls,
        sandbox: object,
        *,
        app_name: str,
        ingress: ModalIngress,
        http2: bool,
        wait: bool,
        readiness_timeout: float,
        metadata: SandboxRef | None = None,
    ) -> ComputerSandbox:
        client: DaemonClient | None = None
        try:
            resolved_metadata = (
                _metadata_from_sandbox(sandbox, app_name=app_name)
                if metadata is None
                else metadata
            )
            if ingress == "connect":
                token_info = sandbox.create_connect_token(
                    user_metadata={"sdk": "modal-computer-use", "version": __version__},
                    port=8080,
                )
                connect_base_url, connect_token = _connect_token_parts(token_info)
            else:
                connect_base_url = _tunnel_url(sandbox, 8080)
                connect_token = _sandbox_daemon_bearer(sandbox)
            token_resolver = None
            if ingress == "attested-tunnel" and not wait:
                bootstrap_base_url = connect_base_url
                bootstrap_token = connect_token
                connect_token = None

                def resolve_attested_token() -> str:
                    return _attested_tunnel_parts(
                        sandbox,
                        connect_base_url=bootstrap_base_url,
                        connect_token=bootstrap_token,
                    )[1]

                token_resolver = resolve_attested_token
            client = DaemonClient(
                base_url=connect_base_url,
                token=connect_token,
                http2=http2,
                _token_resolver=token_resolver,
            )
            computer = cls(
                client,
                sandbox=sandbox,
                metadata=resolved_metadata,
                _lifecycle_mode="attached",
            )
            if wait:
                computer.wait_until_ready(timeout=readiness_timeout)
                if ingress == "attested-tunnel":
                    client.close()
                    connect_base_url, connect_token = _attested_tunnel_parts(
                        sandbox,
                        connect_base_url=connect_base_url,
                        connect_token=connect_token,
                    )
                    client = DaemonClient(
                        base_url=connect_base_url,
                        token=connect_token,
                        http2=http2,
                    )
                    computer = cls(
                        client,
                        sandbox=sandbox,
                        metadata=resolved_metadata,
                        _lifecycle_mode="attached",
                    )
                    computer._readiness_stage_count = 1
                    computer.wait_until_ready(timeout=readiness_timeout)
            return computer
        except BaseException as exc:
            _cleanup_failed_attached_sandbox(sandbox, client=client, primary=exc)
            raise

    @classmethod
    def attach_or_create(
        cls,
        *,
        name: str,
        config: ComputerConfig | None = None,
        app_name: str = "modal-computer-use",
        run_id: str | None = None,
        image: object | None = None,
        expose_vnc: bool | str | None = None,
        tags: dict[str, str] | None = None,
        app_tags: dict[str, str] | None = None,
        secrets: list[object] | None = None,
        volumes: dict[str, object] | None = None,
        owner: str | None = None,
        wait: bool = True,
        readiness_timeout: float | None = None,
        timing: SessionStartupTiming | None = None,
        **sandbox_kwargs: Any,
    ) -> ComputerSandbox:
        inputs, timeout = _prepare_named_attach_or_create_inputs(
            name=name,
            config=config,
            app_name=app_name,
            run_id=run_id,
            image=image,
            expose_vnc=expose_vnc,
            tags=tags,
            app_tags=app_tags,
            secrets=secrets,
            volumes=volumes,
            owner=owner,
            readiness_timeout=readiness_timeout,
            tag_profile="default",
            sandbox_kwargs=sandbox_kwargs,
        )
        try:
            import modal
        except ImportError as exc:
            raise ModalNotInstalledError(
                "ComputerSandbox.attach_or_create requires the modal extra, for example "
                "`uv sync --extra modal` in this repository or "
                "`uv add 'modal-computer-use[modal]'` downstream"
            ) from exc

        environment_name = inputs.config.runtime.modal_environment
        try:
            sandbox = _sandbox_from_name(
                modal,
                app_name=inputs.app_name,
                name=name,
                environment_name=environment_name,
            )
        except Exception as exc:
            if not _is_modal_not_found_error(exc):
                raise
            sandbox = None

        if sandbox is None:
            last_error: Exception | None = None
            for attempt in range(_NAMED_ACQUISITION_ATTEMPTS):
                try:
                    return cls._create_with_readiness_timeout(
                        config=inputs.config,
                        app_name=inputs.app_name,
                        name=name,
                        image=inputs.image,
                        expose_vnc=inputs.vnc_mode,
                        tags=dict(inputs.caller_tags),
                        app_tags=dict(inputs.app_tags),
                        secrets=list(inputs.secrets),
                        volumes=dict(inputs.volumes),
                        owner=inputs.owner,
                        wait=wait,
                        readiness_timeout=timeout,
                        timing=timing,
                        tag_profile=inputs.tag_profile,
                        sandbox_kwargs=inputs.sandbox_kwargs,
                    )
                except Exception as exc:
                    if not _is_modal_already_exists_error(exc):
                        raise
                    last_error = exc
                try:
                    sandbox = _sandbox_from_name(
                        modal,
                        app_name=inputs.app_name,
                        name=name,
                        environment_name=environment_name,
                    )
                except Exception as exc:
                    if not _is_modal_not_found_error(exc):
                        raise
                    last_error = exc
                    if attempt + 1 < _NAMED_ACQUISITION_ATTEMPTS:
                        time.sleep(_NAMED_ACQUISITION_BACKOFF_SECONDS)
                    continue
                break
            else:
                raise SandboxUnavailableError(
                    f"named Sandbox {name!r} disappeared during acquisition"
                ) from last_error

        metadata, resolved_config = _validate_named_existing_sandbox(
            modal,
            sandbox=sandbox,
            inputs=inputs,
        )
        computer = cls._attach_resolved_sandbox(
            sandbox,
            app_name=inputs.app_name,
            ingress=resolved_config.ingress,
            http2=resolved_config.network.daemon_http_version == "2",
            wait=wait,
            readiness_timeout=timeout,
            metadata=metadata,
        )
        computer.startup_timing = timing
        computer._requested_modal_region = resolved_config.runtime.modal_region
        computer._session_handoff_policy = _session_handoff_policy(
            resolved_config,
            session_id=metadata.tags.get("computer-use.session_id"),
            app_name=inputs.app_name,
            vnc_mode=inputs.vnc_mode,
            config_hash=metadata.config_hash or compute_config_hash(resolved_config),
        )
        return computer

    def session_handle(self) -> ComputerSessionHandle:
        """Return the safe reconnect policy for an SDK-owned Modal desktop."""
        return _session_handle_from_state(
            sandbox=self._sandbox,
            metadata=self._metadata,
            policy=self._session_handoff_policy,
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
                raise TimeoutError(
                    f"daemon did not become ready before timeout ({timeout:g}s){detail}"
                )
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
        sandbox = self._sandbox
        if sandbox is not None and hasattr(sandbox, "detach"):
            try:
                sandbox.detach()
            except BaseException as exc:
                try:
                    self.client.close()
                except BaseException as cleanup_exc:
                    _note_cleanup_failure(exc, "client.close", cleanup_exc)
                raise
        self._sandbox = None
        self._lifecycle_mode = "detached"
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
            raise BrowserReadinessError("configured browser does not match the requested browser")
        if not config.browser.prewarm:
            return
        prewarm_result = status.get("prewarm_result")
        if not isinstance(prewarm_result, dict) or prewarm_result.get("ok") is not True:
            raise BrowserReadinessError("browser prewarm did not succeed")
        if not isinstance(status.get("windows"), int) or status["windows"] < 1:
            raise BrowserReadinessError("browser prewarm did not create a browser window")
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

    def modal_image_object_id(self) -> str:
        """Return the exact Modal Image object ID used by this Sandbox."""
        sandbox = _require_modal_backing(self, path="modal_image_object_id")
        process = sandbox.exec(
            "python",
            "-c",
            "import os; print(os.environ.get('MODAL_IMAGE_ID', ''))",
            timeout=10,
        )
        object_id = _read_modal_process_stream(getattr(process, "stdout", "")).strip()
        if not object_id.startswith("im-"):
            raise SandboxUnavailableError(
                "Modal runtime did not return a valid Image object ID"
            )
        return object_id

    def __enter__(self) -> ComputerSandbox:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        cleanup_errors: list[tuple[str, BaseException]] = []
        operations: list[tuple[str, Callable[[], object]]]
        if self._lifecycle_mode == "owned":
            operations = [("client.close", self.client.close)]
            operations.append(("sandbox.terminate", lambda: self.terminate(wait=True)))
            if self._sandbox is not None and hasattr(self._sandbox, "detach"):
                operations.append(("sandbox.detach", self._sandbox.detach))
        elif self._lifecycle_mode == "attached":
            operations = [("client.close", self.client.close)]
            if self._sandbox is not None and hasattr(self._sandbox, "detach"):
                operations.append(("sandbox.detach", self._sandbox.detach))
        elif self._lifecycle_mode == "local":
            operations = [("client.close", self.client.close)]
        else:
            operations = []
        for operation_name, operation in operations:
            try:
                operation()
            except BaseException as caught_cleanup_error:
                cleanup_errors.append((operation_name, caught_cleanup_error))
        if not cleanup_errors:
            return
        if isinstance(exc, BaseException):
            for operation_name, recorded_error in cleanup_errors:
                _note_cleanup_failure(exc, operation_name, recorded_error)
            return
        primary_name, primary_error = cleanup_errors[0]
        for operation_name, recorded_error in cleanup_errors[1:]:
            primary_error.add_note(
                f"additional resource cleanup failed: {operation_name} "
                f"({type(recorded_error).__name__})"
            )
        primary_error.add_note(f"resource cleanup operation: {primary_name}")
        raise primary_error

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


class AsyncComputerSandbox:
    """Native-async owner or attachment for one Modal computer Sandbox.

    The primary :meth:`create` path requires an explicit Modal environment and
    exact region so the owner can produce a placed session handoff. Use
    :meth:`create_unplaced` only for intentional low-level compatibility work.
    Each constructor returns a lazy async context manager and performs no Modal
    work until entry.
    """

    def __init__(
        self,
        client: AsyncDaemonClient,
        *,
        sandbox: object,
        metadata: SandboxRef,
        lifecycle_mode: Literal["owned", "attached"],
        startup_timing: SessionStartupTiming | None = None,
        session_handoff_policy: _SessionHandoffPolicy | None = None,
    ) -> None:
        self.client = client
        self._sandbox: object | None = sandbox
        self._metadata = metadata
        self._lifecycle_mode: _SandboxLifecycleMode = lifecycle_mode
        self._lifecycle_lock = asyncio.Lock()
        self.startup_timing = startup_timing
        self._session_handoff_policy = session_handoff_policy

        self.lifecycle = client.lifecycle
        self.mouse = client.mouse
        self.keyboard = client.keyboard
        self.clipboard = client.clipboard
        self.screenshots = client.screenshots
        self.recordings = client.recordings
        self.display = client.display
        self.windows = client.windows
        self.processes = client.processes
        self.actions = client.actions
        self.input = client.input
        self.artifacts = client.artifacts
        self.browser = client.browser
        self.apps = client.apps
        self.commands = client.commands
        self.debug = client.debug
        self.session = client.session

    @classmethod
    def create(
        cls,
        *,
        config: ComputerConfig | None = None,
        app_name: str = "modal-computer-use",
        name: str | None = None,
        image: object | None = None,
        expose_vnc: bool | str | None = None,
        tags: Mapping[str, str] | None = None,
        app_tags: Mapping[str, str] | None = None,
        secrets: Sequence[object] | None = None,
        volumes: Mapping[str, object] | None = None,
        owner: str | None = None,
        timing: SessionStartupTiming | None = None,
        tag_profile: Literal["default", "warm_pool"] = "default",
        **sandbox_kwargs: Any,
    ) -> AbstractAsyncContextManager[AsyncComputerSandbox]:
        """Own a placed Sandbox that can be handed to a nearby Modal Function."""
        return _AsyncComputerSandboxContext(
            lambda: _create_async_computer_sandbox(
                config=config,
                app_name=app_name,
                name=name,
                image=image,
                expose_vnc=expose_vnc,
                tags=tags,
                app_tags=app_tags,
                secrets=secrets,
                volumes=volumes,
                owner=owner,
                readiness_timeout=None,
                timing=timing,
                tag_profile=tag_profile,
                require_placed_handoff=True,
                sandbox_kwargs=sandbox_kwargs,
            )
        )

    @classmethod
    def create_unplaced(
        cls,
        *,
        config: ComputerConfig | None = None,
        app_name: str = "modal-computer-use",
        name: str | None = None,
        image: object | None = None,
        expose_vnc: bool | str | None = None,
        tags: Mapping[str, str] | None = None,
        app_tags: Mapping[str, str] | None = None,
        secrets: Sequence[object] | None = None,
        volumes: Mapping[str, object] | None = None,
        owner: str | None = None,
        timing: SessionStartupTiming | None = None,
        tag_profile: Literal["default", "warm_pool"] = "default",
        **sandbox_kwargs: Any,
    ) -> AbstractAsyncContextManager[AsyncComputerSandbox]:
        """Own a low-level Sandbox without requiring a handoff placement."""
        return _AsyncComputerSandboxContext(
            lambda: _create_async_computer_sandbox(
                config=config,
                app_name=app_name,
                name=name,
                image=image,
                expose_vnc=expose_vnc,
                tags=tags,
                app_tags=app_tags,
                secrets=secrets,
                volumes=volumes,
                owner=owner,
                readiness_timeout=None,
                timing=timing,
                tag_profile=tag_profile,
                require_placed_handoff=False,
                sandbox_kwargs=sandbox_kwargs,
            )
        )

    @classmethod
    def attach(
        cls,
        *,
        sandbox_id: str | None = None,
        name: str | None = None,
        run_id: str | None = None,
        app_name: str = "modal-computer-use",
        ingress: ModalIngress = "attested-tunnel",
        http2: bool = False,
        readiness_timeout: float = 120.0,
        modal_environment: str | None = None,
        allow_legacy_unscoped: bool = False,
        timing: SessionStartupTiming | None = None,
    ) -> AbstractAsyncContextManager[AsyncComputerSandbox]:
        """Build a lazy context that attaches to an existing Modal Sandbox."""
        return _AsyncComputerSandboxContext(
            lambda: _attach_async_computer_sandbox(
                sandbox_id=sandbox_id,
                name=name,
                run_id=run_id,
                app_name=app_name,
                ingress=ingress,
                http2=http2,
                readiness_timeout=readiness_timeout,
                modal_environment=modal_environment,
                allow_legacy_unscoped=allow_legacy_unscoped,
                timing=timing,
            )
        )

    @classmethod
    def attach_or_create(
        cls,
        *,
        name: str,
        config: ComputerConfig | None = None,
        app_name: str = "modal-computer-use",
        run_id: str | None = None,
        image: object | None = None,
        expose_vnc: bool | str | None = None,
        tags: Mapping[str, str] | None = None,
        app_tags: Mapping[str, str] | None = None,
        secrets: Sequence[object] | None = None,
        volumes: Mapping[str, object] | None = None,
        owner: str | None = None,
        timing: SessionStartupTiming | None = None,
        **sandbox_kwargs: Any,
    ) -> AbstractAsyncContextManager[AsyncComputerSandbox]:
        """Build a lazy context that attaches by name or creates that exact name."""
        return _AsyncComputerSandboxContext(
            lambda: _attach_or_create_async_computer_sandbox(
                name=name,
                config=config,
                app_name=app_name,
                run_id=run_id,
                image=image,
                expose_vnc=expose_vnc,
                tags=tags,
                app_tags=app_tags,
                secrets=secrets,
                volumes=volumes,
                owner=owner,
                readiness_timeout=None,
                timing=timing,
                tag_profile="default",
                sandbox_kwargs=sandbox_kwargs,
            )
        )

    def metadata(self) -> SandboxRef:
        return self._metadata

    def session_handle(self) -> ComputerSessionHandle:
        """Return the safe reconnect policy for an asynchronously created desktop."""
        return _session_handle_from_state(
            sandbox=self._sandbox,
            metadata=self._metadata,
            policy=self._session_handoff_policy,
        )

    async def runtime_placement(self) -> dict[str, str | None]:
        """Return the cloud and region observed inside the Modal Sandbox."""
        sandbox = self._sandbox
        if sandbox is None:
            raise SandboxUnavailableError("the Modal Sandbox handle has been detached")
        return await _sandbox_runtime_placement_async(sandbox)

    async def terminate(self, *, wait: bool = False) -> None:
        async with self._lifecycle_lock:
            sandbox = self._sandbox
            if sandbox is None:
                raise SandboxUnavailableError("the Modal Sandbox handle has been detached")
            terminate = getattr(sandbox, "terminate", None)
            terminate_aio = getattr(terminate, "aio", None)
            if not callable(terminate_aio):
                raise SandboxUnavailableError("Modal Sandbox.terminate.aio is unavailable")
            await terminate_aio(wait=wait)

    async def detach(self) -> None:
        """Transfer lifecycle ownership and close this client's connections."""
        await _detach_async_computer(self)

    def hot_session(self, *, timeout: float = 30.0) -> AsyncHotSessionClient:
        return self.client.hot_session(timeout=timeout)

    def observation_stream(self, **kwargs: Any) -> AsyncObservationClient:
        return self.client.observation_stream(**kwargs)


class _AsyncComputerSandboxContext:
    def __init__(
        self,
        factory: Callable[[], Awaitable[AsyncComputerSandbox]],
    ) -> None:
        self._factory = factory
        self._computer: AsyncComputerSandbox | None = None
        self._entered = False

    async def __aenter__(self) -> AsyncComputerSandbox:
        if self._entered:
            raise RuntimeError("an async computer context can only be entered once")
        self._entered = True
        computer = await self._factory()
        self._computer = computer
        return computer

    async def __aexit__(self, _exc_type: object, exc: object, _traceback: object) -> None:
        computer, self._computer = self._computer, None
        if computer is None:
            return
        await _close_async_computer_context(
            computer,
            primary=exc if isinstance(exc, BaseException) else None,
        )


async def _create_async_computer_sandbox(
    *,
    config: ComputerConfig | None,
    app_name: str,
    name: str | None,
    image: object | None,
    expose_vnc: bool | str | None,
    tags: Mapping[str, str] | None,
    app_tags: Mapping[str, str] | None,
    secrets: Sequence[object] | None,
    volumes: Mapping[str, object] | None,
    owner: str | None,
    readiness_timeout: float | None,
    timing: SessionStartupTiming | None,
    tag_profile: Literal["default", "warm_pool"],
    require_placed_handoff: bool,
    sandbox_kwargs: Mapping[str, Any],
) -> AsyncComputerSandbox:
    startup_timing = timing or SessionStartupTiming()
    startup_timing.mark("request_received")
    startup_timing.unsupported(
        "scheduled",
        "Modal V1 does not expose a supported scheduling timestamp",
    )
    startup_timing.unsupported(
        "daemon_started",
        "the daemon process does not yet emit an attested startup timestamp",
    )
    inputs = _prepare_sandbox_create_inputs(
        config=config,
        app_name=app_name,
        name=name,
        image=image,
        expose_vnc=expose_vnc,
        tags=tags,
        app_tags=app_tags,
        secrets=secrets,
        volumes=volumes,
        owner=owner,
        tag_profile=tag_profile,
        sandbox_kwargs=sandbox_kwargs,
    )
    if require_placed_handoff:
        _validate_placed_owner_configuration(inputs)
    timeout = (
        float(inputs.config.runtime.readiness_timeout_seconds)
        if readiness_timeout is None
        else readiness_timeout
    )
    _validate_async_readiness_timeout(timeout)
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "AsyncComputerSandbox.create requires the modal extra, for example "
            "`uv sync --extra modal` in this repository or "
            "`uv add 'modal-computer-use[modal]'` downstream"
        ) from exc

    resolved_image = _resolve_sandbox_create_image(inputs)
    app = await modal.App.lookup.aio(
        inputs.app_name,
        **dict(inputs.app_lookup_kwargs),
    )
    if inputs.app_tags:
        await _set_modal_object_tags_async(app, dict(inputs.app_tags))
    plan = _build_sandbox_create_plan(
        inputs,
        app=app,
        modal=modal,
        image=resolved_image,
    )
    startup_timing.mark("sandbox_create_started")
    sandbox = await _allocate_modal_sandbox_async(
        modal.Sandbox.create.aio(
            "python",
            "-m",
            "modal_computer_use.daemon",
            **_materialize_sandbox_create_kwargs(plan),
        )
    )
    client: AsyncDaemonClient | None = None
    try:
        startup_timing.mark("sandbox_registered")
        await sandbox.wait_until_ready.aio(timeout=timeout)
        startup_timing.mark("tcp_ready")
        client = await _async_daemon_client_for_sandbox(
            sandbox,
            ingress=inputs.config.ingress,
            http2=plan.http2,
            readiness_timeout=timeout,
            daemon_bearer=plan.daemon_bearer,
            timing=startup_timing,
        )
        metadata = _metadata_from_create_plan(plan, sandbox)
        session_policy = _session_handoff_policy(
            inputs.config,
            session_id=plan.session_id,
            app_name=inputs.app_name,
            vnc_mode=inputs.vnc_mode,
            config_hash=metadata.config_hash or plan.config_hash,
        )
        return AsyncComputerSandbox(
            client,
            sandbox=sandbox,
            metadata=metadata,
            lifecycle_mode="owned",
            startup_timing=startup_timing,
            session_handoff_policy=session_policy,
        )
    except BaseException as exc:
        await _cleanup_failed_created_sandbox_async(
            sandbox,
            client=client,
            primary=exc,
        )
        raise


async def _attach_or_create_async_computer_sandbox(
    *,
    name: str,
    config: ComputerConfig | None,
    app_name: str,
    run_id: str | None,
    image: object | None,
    expose_vnc: bool | str | None,
    tags: Mapping[str, str] | None,
    app_tags: Mapping[str, str] | None,
    secrets: Sequence[object] | None,
    volumes: Mapping[str, object] | None,
    owner: str | None,
    readiness_timeout: float | None,
    timing: SessionStartupTiming | None,
    tag_profile: Literal["default", "warm_pool"],
    sandbox_kwargs: Mapping[str, Any],
) -> AsyncComputerSandbox:
    inputs, timeout = _prepare_named_attach_or_create_inputs(
        name=name,
        config=config,
        app_name=app_name,
        run_id=run_id,
        image=image,
        expose_vnc=expose_vnc,
        tags=tags,
        app_tags=app_tags,
        secrets=secrets,
        volumes=volumes,
        owner=owner,
        readiness_timeout=readiness_timeout,
        tag_profile=tag_profile,
        sandbox_kwargs=sandbox_kwargs,
    )
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "AsyncComputerSandbox.attach_or_create requires the modal extra, for example "
            "`uv sync --extra modal` in this repository or "
            "`uv add 'modal-computer-use[modal]'` downstream"
        ) from exc

    environment_name = inputs.config.runtime.modal_environment
    try:
        sandbox = await modal.Sandbox.from_name.aio(
            inputs.app_name,
            name,
            environment_name=environment_name,
        )
    except Exception as exc:
        if not _is_modal_not_found_error(exc):
            raise
        sandbox = None

    if sandbox is None:
        last_error: Exception | None = None
        for attempt in range(_NAMED_ACQUISITION_ATTEMPTS):
            try:
                return await _create_async_computer_sandbox(
                    config=inputs.config,
                    app_name=inputs.app_name,
                    name=name,
                    image=inputs.image,
                    expose_vnc=inputs.vnc_mode,
                    tags=inputs.caller_tags,
                    app_tags=inputs.app_tags,
                    secrets=inputs.secrets,
                    volumes=inputs.volumes,
                    owner=inputs.owner,
                    readiness_timeout=timeout,
                    timing=timing,
                    tag_profile=inputs.tag_profile,
                    require_placed_handoff=False,
                    sandbox_kwargs=inputs.sandbox_kwargs,
                )
            except Exception as exc:
                if not _is_modal_already_exists_error(exc):
                    raise
                last_error = exc
            try:
                sandbox = await modal.Sandbox.from_name.aio(
                    inputs.app_name,
                    name,
                    environment_name=environment_name,
                )
            except Exception as exc:
                if not _is_modal_not_found_error(exc):
                    raise
                last_error = exc
                if attempt + 1 < _NAMED_ACQUISITION_ATTEMPTS:
                    await asyncio.sleep(_NAMED_ACQUISITION_BACKOFF_SECONDS)
                continue
            break
        else:
            raise SandboxUnavailableError(
                f"named Sandbox {name!r} disappeared during acquisition"
            ) from last_error

    metadata, resolved_config = await _validate_named_existing_sandbox_async(
        modal,
        sandbox=sandbox,
        inputs=inputs,
    )
    return await _attach_resolved_async_computer_sandbox(
        sandbox,
        metadata=metadata,
        ingress=resolved_config.ingress,
        http2=resolved_config.network.daemon_http_version == "2",
        readiness_timeout=timeout,
        timing=timing,
        session_handoff_policy=_session_handoff_policy(
            resolved_config,
            session_id=metadata.tags.get("computer-use.session_id"),
            app_name=inputs.app_name,
            vnc_mode=inputs.vnc_mode,
            config_hash=metadata.config_hash or compute_config_hash(resolved_config),
        ),
    )


async def _validate_named_existing_sandbox_async(
    modal: object,
    *,
    sandbox: object,
    inputs: _SandboxCreateInputs,
) -> tuple[SandboxRef, ComputerConfig]:
    try:
        lookup_kwargs: dict[str, object] = {"create_if_missing": False}
        modal_environment = inputs.config.runtime.modal_environment
        if modal_environment is not None:
            lookup_kwargs["environment_name"] = modal_environment
        app = await modal.App.lookup.aio(inputs.app_name, **lookup_kwargs)
        tags = await _read_modal_object_tags_async(sandbox) or {}
        _require_async_app_tag(
            tags,
            app_id=_modal_app_id(app),
            allow_legacy_unscoped=False,
            description=f"sandbox name={inputs.name}",
        )
        metadata = _metadata_from_sandbox_tags(
            sandbox,
            app_name=inputs.app_name,
            tags=tags,
        )
        existing_hash = metadata.config_hash
        if not existing_hash:
            raise ConfigConflictError(
                "existing named Sandbox is missing computer-use.config_hash",
                sandbox_id=metadata.sandbox_id,
            )
        resolved_config = inputs.config.model_copy(deep=True)
        tagged_run_id = metadata.run_id
        if not tagged_run_id:
            raise ConfigConflictError(
                "existing named Sandbox is missing computer-use.run_id",
                sandbox_id=metadata.sandbox_id,
            )
        if (
            resolved_config.run_id is not None
            and resolved_config.run_id != tagged_run_id
        ):
            raise ConfigConflictError(
                "existing named Sandbox run_id does not match the requested run_id",
                sandbox_id=metadata.sandbox_id,
            )
        if resolved_config.run_id is None:
            resolved_config.run_id = tagged_run_id
        requested_hash = compute_config_hash(resolved_config)
        if existing_hash != requested_hash:
            raise ConfigConflictError(
                "existing named Sandbox config_hash does not match the requested config",
                requested_hash=requested_hash,
                existing_hash=existing_hash,
                sandbox_id=metadata.sandbox_id,
            )
        return metadata, resolved_config
    except BaseException as exc:
        await _cleanup_failed_attached_sandbox_async(
            sandbox,
            client=None,
            primary=exc,
        )
        raise


async def _attach_async_computer_sandbox(
    *,
    sandbox_id: str | None,
    name: str | None,
    run_id: str | None,
    app_name: str,
    ingress: ModalIngress,
    http2: bool,
    readiness_timeout: float,
    modal_environment: str | None,
    allow_legacy_unscoped: bool,
    timing: SessionStartupTiming | None,
) -> AsyncComputerSandbox:
    selector = _validate_async_modal_attach_selector(
        sandbox_id=sandbox_id,
        name=name,
        run_id=run_id,
    )
    _validate_async_readiness_timeout(readiness_timeout)
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "AsyncComputerSandbox.attach requires the modal extra, for example "
            "`uv sync --extra modal` in this repository or "
            "`uv add 'modal-computer-use[modal]'` downstream"
        ) from exc

    sandbox, tags = await _resolve_async_attach_target(
        modal,
        selector=selector,
        app_name=app_name,
        modal_environment=modal_environment,
        allow_legacy_unscoped=allow_legacy_unscoped,
    )
    metadata = _metadata_from_sandbox_tags(
        sandbox,
        app_name=app_name,
        tags=tags,
    )
    return await _attach_resolved_async_computer_sandbox(
        sandbox,
        metadata=metadata,
        ingress=ingress,
        http2=http2,
        readiness_timeout=readiness_timeout,
        timing=timing,
    )


async def _attach_resolved_async_computer_sandbox(
    sandbox: object,
    *,
    metadata: SandboxRef,
    ingress: ModalIngress,
    http2: bool,
    readiness_timeout: float,
    timing: SessionStartupTiming | None,
    session_handoff_policy: _SessionHandoffPolicy | None = None,
) -> AsyncComputerSandbox:
    client: AsyncDaemonClient | None = None
    try:
        await sandbox.wait_until_ready.aio(timeout=readiness_timeout)
        if timing is not None:
            timing.mark("tcp_ready")
        client = await _async_daemon_client_for_sandbox(
            sandbox,
            ingress=ingress,
            http2=http2,
            readiness_timeout=readiness_timeout,
            daemon_bearer=None,
            timing=timing,
        )
        return AsyncComputerSandbox(
            client,
            sandbox=sandbox,
            metadata=metadata,
            lifecycle_mode="attached",
            startup_timing=timing,
            session_handoff_policy=session_handoff_policy,
        )
    except BaseException as exc:
        await _cleanup_failed_attached_sandbox_async(
            sandbox,
            client=client,
            primary=exc,
        )
        raise


def _validate_async_modal_attach_selector(
    *,
    sandbox_id: str | None,
    name: str | None,
    run_id: str | None,
) -> _AttachSelector:
    selectors = {
        "sandbox_id": sandbox_id,
        "name": name,
        "run_id": run_id,
    }
    selected = [(kind, value) for kind, value in selectors.items() if value is not None]
    if len(selected) != 1:
        raise ValueError("attach requires exactly one of sandbox_id, name, or run_id")
    kind, raw_value = selected[0]
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{kind} must be a non-empty string")
    return _AttachSelector(
        kind=cast(Literal["sandbox_id", "name", "run_id"], kind),
        value=value,
    )


def _validate_async_readiness_timeout(value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("readiness_timeout must be a positive finite number")


async def _resolve_async_attach_target(
    modal: object,
    *,
    selector: _AttachSelector,
    app_name: str,
    modal_environment: str | None,
    allow_legacy_unscoped: bool,
) -> tuple[object, dict[str, str]]:
    lookup_kwargs: dict[str, object] = {"create_if_missing": False}
    if modal_environment is not None:
        lookup_kwargs["environment_name"] = modal_environment
    app = await modal.App.lookup.aio(app_name, **lookup_kwargs)
    app_id = _modal_app_id(app)

    if selector.kind == "run_id":
        scoped_tags = {"computer-use.run_id": selector.value}
        if not allow_legacy_unscoped:
            scoped_tags[APP_ID_TAG] = app_id
        matches = await _list_modal_sandboxes_async(
            modal,
            app_id=app_id,
            tags=scoped_tags,
        )
        if not matches:
            raise SandboxUnavailableError(f"no matching run_id={selector.value} found")
        if len(matches) > 1:
            error = SandboxAmbiguousError(
                f"multiple matching run_id={selector.value}s found; "
                "attach by sandbox_id or name"
            )
            await _cleanup_rejected_async_sandboxes(matches, primary=error)
            raise error
        sandbox = matches[0]
        try:
            tags = await _read_modal_object_tags_async(sandbox) or {}
            return sandbox, tags
        except BaseException as exc:
            await _cleanup_rejected_async_sandboxes([sandbox], primary=exc)
            raise

    if selector.kind == "sandbox_id":
        try:
            sandbox = await modal.Sandbox.from_id.aio(selector.value)
        except Exception as exc:
            raise SandboxUnavailableError(
                f"no matching sandbox_id={selector.value} found"
            ) from exc
    else:
        sandbox = await modal.Sandbox.from_name.aio(
            app_name,
            selector.value,
            environment_name=modal_environment,
        )

    try:
        tags = await _read_modal_object_tags_async(sandbox) or {}
        _require_async_app_tag(
            tags,
            app_id=app_id,
            allow_legacy_unscoped=allow_legacy_unscoped,
            description=f"{selector.kind}={selector.value}",
        )
        if selector.kind == "sandbox_id":
            membership_tags = (
                None
                if allow_legacy_unscoped and APP_ID_TAG not in tags
                else {APP_ID_TAG: app_id}
            )
            candidates = await _list_modal_sandboxes_async(
                modal,
                app_id=app_id,
                tags=membership_tags,
            )
            if not any(
                str(getattr(candidate, "object_id", "")) == selector.value
                for candidate in candidates
            ):
                raise SandboxUnavailableError(
                    f"no app-owned sandbox_id={selector.value} found"
                )
        return sandbox, tags
    except BaseException as exc:
        await _cleanup_rejected_async_sandboxes([sandbox], primary=exc)
        raise


def _require_async_app_tag(
    tags: Mapping[str, str],
    *,
    app_id: str,
    allow_legacy_unscoped: bool,
    description: str,
) -> None:
    if tags.get(APP_ID_TAG) == app_id:
        return
    if allow_legacy_unscoped and APP_ID_TAG not in tags:
        return
    raise SandboxUnavailableError(f"no app-owned {description} found")


async def _list_modal_sandboxes_async(
    modal: object,
    *,
    app_id: str,
    tags: Mapping[str, str] | None,
) -> list[object]:
    kwargs: dict[str, object] = {"app_id": app_id}
    if tags is not None:
        kwargs["tags"] = dict(tags)
    return [sandbox async for sandbox in modal.Sandbox.list.aio(**kwargs)]


async def _async_daemon_client_for_sandbox(
    sandbox: object,
    *,
    ingress: ModalIngress,
    http2: bool,
    readiness_timeout: float,
    daemon_bearer: str | None,
    timing: SessionStartupTiming | None,
) -> AsyncDaemonClient:
    if ingress == "connect":
        token_info = await sandbox.create_connect_token.aio(
            user_metadata={"sdk": "modal-computer-use", "version": __version__},
            port=8080,
        )
        base_url, token = _connect_token_parts(token_info)
        if timing is not None:
            timing.mark("connect_token_ready")
    else:
        base_url = await _tunnel_url_async(sandbox, 8080)
        token = daemon_bearer or await _sandbox_daemon_bearer_async(sandbox)
        if timing is not None:
            timing.mark("connect_token_ready")

    client = AsyncDaemonClient(base_url, token=token, http2=http2)
    try:
        await client.wait_until_ready(timeout=readiness_timeout)
        if timing is not None:
            timing.mark("connect_ready")
        if ingress != "attested-tunnel":
            return client

        payload = await client.post_json("/v1/session/tunnel-authorize")
        attested_token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(attested_token, str) or not attested_token:
            raise SandboxUnavailableError("daemon did not return an attested tunnel token")
        await _finish_async_cleanup(
            [("client.aclose", client.aclose)],
            primary=None,
        )
        if timing is not None:
            timing.mark("attestation_ready")
        final_client = AsyncDaemonClient(
            await _tunnel_url_async(sandbox, 8080),
            token=attested_token,
            http2=http2,
        )
        try:
            await final_client.wait_until_ready(timeout=readiness_timeout)
        except BaseException as exc:
            await _finish_async_cleanup(
                [("client.aclose", final_client.aclose)],
                primary=exc,
            )
            raise
        if timing is not None:
            timing.mark("tunnel_ready")
        return final_client
    except BaseException as exc:
        await _finish_async_cleanup(
            [("client.aclose", client.aclose)],
            primary=exc,
        )
        raise


async def _tunnel_url_async(sandbox: object, port: int) -> str:
    try:
        tunnels = await sandbox.tunnels.aio()
    except Exception as exc:
        raise SandboxUnavailableError(
            f"could not retrieve Modal tunnel for port {port}"
        ) from exc
    tunnel = tunnels.get(port) if isinstance(tunnels, dict) else None
    value = None if tunnel is None else getattr(tunnel, "url", None)
    if not value:
        raise SandboxUnavailableError(f"Modal tunnel for port {port} is not available")
    return str(value).rstrip("/")


def _metadata_from_sandbox_tags(
    sandbox: object,
    *,
    app_name: str,
    tags: Mapping[str, str],
) -> SandboxRef:
    safe_tags = {str(key): str(value) for key, value in tags.items()}
    return SandboxRef(
        sandbox_id=str(getattr(sandbox, "object_id", "unknown")),
        app_name=app_name,
        name=getattr(sandbox, "name", None),
        run_id=safe_tags.get("computer-use.run_id"),
        owner=safe_tags.get("computer-use.owner"),
        created_at=_created_at_from_tags(safe_tags),
        config_hash=safe_tags.get("computer-use.config_hash"),
        status="started",
        tags=safe_tags,
        artifacts_dir=safe_tags.get(
            "computer-use.artifacts_dir",
            "/home/desktop/artifacts",
        ),
    )


async def _set_modal_object_tags_async(target: object, tags: dict[str, str]) -> None:
    existing = await _read_modal_object_tags_async(target) or {}
    set_tags = getattr(target, "set_tags", None)
    set_tags_aio = getattr(set_tags, "aio", None)
    if callable(set_tags_aio):
        await set_tags_aio({**existing, **tags})


async def _allocate_modal_sandbox_async(awaitable: Awaitable[object]) -> object:
    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            sandbox = await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            if task.cancelled():
                raise
            cancellation = cancellation or cancelled
            continue
        except BaseException as allocation_exc:
            if cancellation is not None:
                cancellation.add_note(
                    "Modal allocation also failed after cancellation "
                    f"({type(allocation_exc).__name__})"
                )
                raise cancellation from allocation_exc
            raise
        if cancellation is None:
            return sandbox
        await _cleanup_failed_created_sandbox_async(
            sandbox,
            client=None,
            primary=cancellation,
        )
        raise cancellation


async def _close_async_computer_context(
    computer: AsyncComputerSandbox,
    *,
    primary: BaseException | None,
) -> None:
    async with computer._lifecycle_lock:
        sandbox = computer._sandbox
        if sandbox is None or computer._lifecycle_mode == "detached":
            return
        operations: list[tuple[str, Callable[[], Awaitable[object]]]] = [
            ("client.aclose", computer.client.aclose)
        ]
        if computer._lifecycle_mode == "owned":
            operations.append(
                (
                    "sandbox.terminate.aio",
                    _modal_aio_operation(sandbox, "terminate", wait=True),
                )
            )
        operations.append(
            ("sandbox.detach.aio", _modal_aio_operation(sandbox, "detach"))
        )
        errors, cancellation = await _collect_async_cleanup(operations)
        if not any(name == "sandbox.detach.aio" for name, _error in errors):
            computer._sandbox = None
            computer._lifecycle_mode = "detached"
        if primary is not None:
            for operation_name, cleanup_exc in errors:
                _note_cleanup_failure(primary, operation_name, cleanup_exc)
            if cancellation is not None:
                primary.add_note("resource cleanup also observed cancellation")
            return
        if errors:
            operation_name, cleanup_error = errors[0]
            for additional_name, additional_error in errors[1:]:
                _note_cleanup_failure(cleanup_error, additional_name, additional_error)
            cleanup_error.add_note(f"resource cleanup operation: {operation_name}")
            if cancellation is not None:
                cleanup_error.add_note("resource cleanup also observed cancellation")
            raise cleanup_error
        if cancellation is not None:
            raise cancellation


async def _detach_async_computer(computer: AsyncComputerSandbox) -> None:
    async with computer._lifecycle_lock:
        sandbox = computer._sandbox
        if sandbox is None or computer._lifecycle_mode == "detached":
            await computer.client.aclose()
            return
        errors, cancellation = await _collect_async_cleanup(
            [("sandbox.detach.aio", _modal_aio_operation(sandbox, "detach"))]
        )
        if errors:
            primary = errors[0][1]
            await _finish_async_cleanup(
                [("client.aclose", computer.client.aclose)],
                primary=primary,
            )
            raise primary

        computer._sandbox = None
        computer._lifecycle_mode = "detached"
        close_errors, close_cancellation = await _collect_async_cleanup(
            [("client.aclose", computer.client.aclose)]
        )
        if close_errors:
            primary = close_errors[0][1]
            for operation_name, cleanup_exc in close_errors[1:]:
                _note_cleanup_failure(primary, operation_name, cleanup_exc)
            if cancellation is not None or close_cancellation is not None:
                primary.add_note("resource cleanup also observed cancellation")
            raise primary
        if cancellation is not None:
            raise cancellation
        if close_cancellation is not None:
            raise close_cancellation


async def _cleanup_failed_created_sandbox_async(
    sandbox: object,
    *,
    client: AsyncDaemonClient | None,
    primary: BaseException,
) -> None:
    operations: list[tuple[str, Callable[[], Awaitable[object]]]] = []
    if client is not None:
        operations.append(("client.aclose", client.aclose))
    operations.extend(
        [
            (
                "sandbox.terminate.aio",
                _modal_aio_operation(sandbox, "terminate", wait=True),
            ),
            ("sandbox.detach.aio", _modal_aio_operation(sandbox, "detach")),
        ]
    )
    await _finish_async_cleanup(operations, primary=primary)


async def _cleanup_failed_attached_sandbox_async(
    sandbox: object,
    *,
    client: AsyncDaemonClient | None,
    primary: BaseException,
) -> None:
    operations: list[tuple[str, Callable[[], Awaitable[object]]]] = []
    if client is not None:
        operations.append(("client.aclose", client.aclose))
    operations.append(
        ("sandbox.detach.aio", _modal_aio_operation(sandbox, "detach"))
    )
    await _finish_async_cleanup(operations, primary=primary)


async def _cleanup_rejected_async_sandboxes(
    sandboxes: Sequence[object],
    *,
    primary: BaseException,
) -> None:
    await _finish_async_cleanup(
        [
            (
                "sandbox.detach.aio",
                _modal_aio_operation(sandbox, "detach"),
            )
            for sandbox in sandboxes
        ],
        primary=primary,
    )


async def _call_modal_aio(
    target: object,
    operation_name: str,
    **kwargs: object,
) -> object:
    operation = getattr(target, operation_name, None)
    operation_aio = getattr(operation, "aio", None)
    if not callable(operation_aio):
        raise SandboxUnavailableError(
            f"Modal Sandbox.{operation_name}.aio is unavailable"
        )
    return await operation_aio(**kwargs)


def _modal_aio_operation(
    target: object,
    operation_name: str,
    **kwargs: object,
) -> Callable[[], Awaitable[object]]:
    async def operation() -> object:
        return await _call_modal_aio(target, operation_name, **kwargs)

    return operation


async def _collect_async_cleanup(
    operations: Sequence[tuple[str, Callable[[], Awaitable[object]]]],
) -> tuple[list[tuple[str, BaseException]], asyncio.CancelledError | None]:
    async def cleanup() -> list[tuple[str, BaseException]]:
        errors: list[tuple[str, BaseException]] = []
        for operation_name, operation in operations:
            try:
                await operation()
            except BaseException as cleanup_exc:
                errors.append((operation_name, cleanup_exc))
        return errors

    task = asyncio.create_task(cleanup())
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as cancelled:
            if task.cancelled():
                raise
            cancellation = cancellation or cancelled


async def _finish_async_cleanup(
    operations: Sequence[tuple[str, Callable[[], Awaitable[object]]]],
    *,
    primary: BaseException | None,
) -> None:
    errors, cancellation = await _collect_async_cleanup(operations)
    if primary is not None:
        for operation_name, cleanup_exc in errors:
            _note_cleanup_failure(primary, operation_name, cleanup_exc)
        if cancellation is not None:
            primary.add_note("resource cleanup also observed cancellation")
        return
    if errors:
        operation_name, cleanup_error = errors[0]
        for additional_name, additional_error in errors[1:]:
            _note_cleanup_failure(cleanup_error, additional_name, additional_error)
        cleanup_error.add_note(f"resource cleanup operation: {operation_name}")
        if cancellation is not None:
            cleanup_error.add_note("resource cleanup also observed cancellation")
        raise cleanup_error
    if cancellation is not None:
        raise cancellation


def _required_loopback_bearer(computer: ComputerSandbox) -> str:
    bearer = getattr(computer, "_daemon_bearer", None)
    if not bearer:
        raise SandboxUnavailableError("target-loopback requires an SDK-owned daemon bearer")
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
            user_metadata={"sdk": "modal-computer-use", "runner_path": path},
            port=8080,
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
        "::" if transport == "workspace-private-i6pn" else "0.0.0.0"  # noqa: S104 - Modal tunnel/connect must accept external ingress.
    )
    sandbox_tags = {
        **(tags or {}),
        **default_tags(config, app_id=_modal_app_id(app)),
    }
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
    readiness_probe = None if transport == "workspace-private-i6pn" else _readiness_probe(runtime)
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
                user_metadata={"sdk": "modal-computer-use", "benchmark": "modal-direct-path"},
                port=8080,
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
        None if path == "target-loopback" else _resolve_modal_runner_region(computer, modal_region)
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


def _note_cleanup_failure(primary: BaseException, operation: str, exc: BaseException) -> None:
    primary.add_note(f"resource cleanup also failed: {operation} ({type(exc).__name__})")


def _cleanup_failed_created_sandbox(
    sandbox: object,
    *,
    client: DaemonClient | None,
    primary: BaseException,
) -> None:
    operations: list[tuple[str, Callable[[], object]]] = []
    if client is not None:
        operations.append(("client.close", client.close))
    terminate = getattr(sandbox, "terminate", None)
    if callable(terminate):
        operations.append(("sandbox.terminate", lambda: terminate(wait=True)))
    detach = getattr(sandbox, "detach", None)
    if callable(detach):
        operations.append(("sandbox.detach", detach))
    for operation_name, operation in operations:
        try:
            operation()
        except BaseException as cleanup_exc:
            _note_cleanup_failure(primary, operation_name, cleanup_exc)


def _cleanup_failed_attached_sandbox(
    sandbox: object,
    *,
    client: DaemonClient | None,
    primary: BaseException,
) -> None:
    if client is not None:
        try:
            client.close()
        except BaseException as cleanup_exc:
            _note_cleanup_failure(primary, "client.close", cleanup_exc)
    detach = getattr(sandbox, "detach", None)
    if callable(detach):
        try:
            detach()
        except BaseException as cleanup_exc:
            _note_cleanup_failure(primary, "sandbox.detach", cleanup_exc)


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
        "COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY": str(config.ingress == "connect").lower(),
        "COMPUTER_USE_REQUIRE_CONNECT_USER": str(config.ingress == "connect").lower(),
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


def _sandbox_daemon_bearer(sandbox: object) -> str:
    process = sandbox.exec(
        "python",
        "-c",
        ("import os, sys; sys.stdout.write(os.environ.get('COMPUTER_USE_TUNNEL_TOKEN', ''))"),
        timeout=10,
    )
    token = _read_modal_process_stream(getattr(process, "stdout", "")).strip()
    if not token:
        raise SandboxUnavailableError("sandbox daemon bearer is unavailable")
    return token


async def _sandbox_daemon_bearer_async(sandbox: object) -> str:
    process = await sandbox.exec.aio(
        "python",
        "-c",
        ("import os, sys; sys.stdout.write(os.environ.get('COMPUTER_USE_TUNNEL_TOKEN', ''))"),
        timeout=10,
    )
    token = (await process.stdout.read.aio()).strip()
    if not token:
        raise SandboxUnavailableError("sandbox daemon bearer is unavailable")
    return token


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


def _session_handle_from_state(
    *,
    sandbox: object | None,
    metadata: SandboxRef | None,
    policy: _SessionHandoffPolicy | None,
) -> ComputerSessionHandle:
    if sandbox is None or metadata is None or policy is None:
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
    if metadata.tags.get("computer-use") != "true" and APP_ID_TAG not in metadata.tags:
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
        raise ModalNotInstalledError("session borrowing requires the modal extra") from exc
    sandbox = modal.Sandbox.from_id(handle.sandbox_id)
    transport: HTTPTransport | None = None
    coordinator: object | None = None
    try:
        try:
            tags = _read_modal_object_tags(sandbox)
        except Exception:
            raise SessionPlacementUnverifiableError from None
        if tags is None:
            raise SessionPlacementUnverifiableError
        if not _live_borrow_target_matches(handle, sandbox=sandbox, tags=tags):
            raise SessionTargetMismatchError()
        token_info = sandbox.create_connect_token(
            user_metadata={"sdk": "modal-computer-use", "version": __version__},
            port=8080,
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
        client = DaemonClient(base_url, transport=transport)
        _wait_borrowed_ready_sync(client, readiness_timeout)
        _verify_borrowed_daemon_protocol_sync(client)
        from .session_lease import SessionLeaseCoordinator

        coordinator = SessionLeaseCoordinator(transport, run_id=run_id)
        coordinator.acquire()
        client = DaemonClient(
            base_url,
            transport=transport,
            _mutation_executor=coordinator.execute,
        )
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
        raise ModalNotInstalledError("session borrowing requires the modal extra") from exc
    sandbox = await modal.Sandbox.from_id.aio(handle.sandbox_id)
    transport: AsyncHTTPTransport | None = None
    heartbeat_transport: HTTPTransport | None = None
    coordinator: object | None = None
    try:
        try:
            tags = await _read_modal_object_tags_async(sandbox)
        except Exception:
            raise SessionPlacementUnverifiableError from None
        if tags is None:
            raise SessionPlacementUnverifiableError
        if not _live_borrow_target_matches(handle, sandbox=sandbox, tags=tags):
            raise SessionTargetMismatchError()
        token_info = await sandbox.create_connect_token.aio(
            user_metadata={"sdk": "modal-computer-use", "version": __version__},
            port=8080,
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
        client = AsyncDaemonClient(base_url, transport=transport)
        await _wait_borrowed_ready_async(client, readiness_timeout)
        await _verify_borrowed_daemon_protocol_async(client)
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
    tunnel_base_url = _tunnel_url(sandbox, 8080)
    bootstrap_token = _sandbox_daemon_bearer(sandbox)
    return _attested_tunnel_parts(
        sandbox,
        connect_base_url=tunnel_base_url,
        connect_token=bootstrap_token,
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
    tunnels = await sandbox.tunnels.aio()
    tunnel = tunnels.get(8080) if isinstance(tunnels, dict) else None
    tunnel_url = None if tunnel is None else getattr(tunnel, "url", None)
    if not tunnel_url:
        raise SandboxUnavailableError("Modal tunnel for port 8080 is not available")
    bootstrap_token = await _sandbox_daemon_bearer_async(sandbox)
    connect_client = AsyncDaemonClient(str(tunnel_url).rstrip("/"), token=bootstrap_token)
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


def _verify_borrowed_daemon_protocol_sync(client: DaemonClient) -> None:
    try:
        version_payload = client.get_json("/v1/version")
        capabilities_payload = client.get_json("/v1/capabilities")
    except Exception:
        raise SessionDaemonProtocolError from None
    validate_default_trajectory_protocol(
        version_payload=version_payload,
        capabilities_payload=capabilities_payload,
    )


async def _verify_borrowed_daemon_protocol_async(client: AsyncDaemonClient) -> None:
    try:
        version_payload = await client.get_json("/v1/version")
        capabilities_payload = await client.get_json("/v1/capabilities")
    except Exception:
        raise SessionDaemonProtocolError from None
    validate_default_trajectory_protocol(
        version_payload=version_payload,
        capabilities_payload=capabilities_payload,
    )


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


def _readiness_probe(modal: object) -> object | None:
    probe = getattr(modal, "Probe", None)
    with_tcp = getattr(probe, "with_tcp", None)
    if with_tcp is None:
        return None
    return with_tcp(8080)


def _sandbox_from_name(
    modal: object,
    *,
    app_name: str,
    name: str,
    environment_name: str | None = None,
) -> object:
    try:
        return modal.Sandbox.from_name(
            app_name,
            name,
            environment_name=environment_name,
        )
    except TypeError:
        lookup_kwargs = {"create_if_missing": False}
        if environment_name is not None:
            lookup_kwargs["environment_name"] = environment_name
        app = modal.App.lookup(app_name, **lookup_kwargs)
        return modal.Sandbox.from_name(name, app=app)


def _modal_app_id(app: object) -> str:
    app_id = str(getattr(app, "app_id", ""))
    if not app_id:
        raise SandboxUnavailableError("Modal app identity is unavailable")
    return app_id


def _reject_security_owned_sandbox_kwargs(sandbox_kwargs: Mapping[str, object]) -> None:
    conflicts = sorted(_SECURITY_OWNED_SANDBOX_KWARGS.intersection(sandbox_kwargs))
    if conflicts:
        joined = ", ".join(conflicts)
        raise ConfigConflictError(f"sandbox_kwargs cannot override security-owned fields: {joined}")


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
