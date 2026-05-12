from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Literal

from ._version import __version__
from .client import DaemonClient
from .config import ComputerConfig, normalize_vnc_mode
from .errors import (
    ConfigConflictError,
    ModalNotInstalledError,
    SandboxAmbiguousError,
    SandboxUnavailableError,
)
from .image import default_image
from .models import ComputerStatus, DebugUrls, SandboxRef
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
from .state import compute_config_hash, default_tags, new_run_id

ReusePolicy = Literal["by_run_id", "by_name", "never"]
ConfigMismatchPolicy = Literal["raise", "reuse"]


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


class ComputerSandbox:
    def __init__(
        self,
        client: DaemonClient,
        *,
        sandbox: object | None = None,
        metadata: SandboxRef | None = None,
    ) -> None:
        self.client = client
        self._sandbox = sandbox
        self._metadata = metadata
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
        secrets: list[object] | None = None,
        volumes: dict[str, object] | None = None,
        owner: str | None = None,
        wait: bool = True,
        **sandbox_kwargs: Any,
    ) -> ComputerSandbox:
        try:
            import modal
        except ImportError as exc:
            raise ModalNotInstalledError(
                "ComputerSandbox.create requires the modal extra, for example "
                "`uv sync --extra modal` in this repository or "
                "`uv add 'modal-computer-use[modal]'` downstream"
            ) from exc

        config = config or ComputerConfig()
        if not config.run_id:
            config.run_id = new_run_id()
        vnc_mode = normalize_vnc_mode(expose_vnc if expose_vnc is not None else config.expose_vnc)
        image = image or default_image(
            profile=config.resources.profile,
            browser=config.browser.kind if config.browser else None,
            window_manager=config.desktop.window_manager,
            browser_prewarm=config.browser.prewarm if config.browser else False,
        )
        app = modal.App.lookup(app_name, create_if_missing=True)
        env = _daemon_environment(config, vnc_mode=vnc_mode)
        sandbox_tags = {**default_tags(config, owner=owner), **(tags or {})}
        ports = [6080] if vnc_mode != "off" else []
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
            "volumes": volumes or {},
            "env": env,
            "block_network": config.network.block_all,
            "cidr_allowlist": config.network.cidr_allowlist,
            "name": name,
            **sandbox_kwargs,
        }
        if config.runtime.modal_region:
            create_kwargs["region"] = config.runtime.modal_region
        readiness_probe = _readiness_probe(modal)
        if readiness_probe is not None:
            create_kwargs["readiness_probe"] = readiness_probe
        sandbox = modal.Sandbox.create("python", "-m", "modal_computer_use.daemon", **create_kwargs)
        if hasattr(sandbox, "set_tags"):
            sandbox.set_tags(sandbox_tags)
        if wait and hasattr(sandbox, "wait_until_ready"):
            sandbox.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
        token_info = sandbox.create_connect_token(
            user_metadata={"sdk": "modal-computer-use", "version": __version__}
        )
        base_url, token = _connect_token_parts(token_info)
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
            vnc_url=_vnc_url(sandbox) if vnc_mode != "off" else None,
            artifacts_dir=config.storage.artifacts_dir,
        )
        return cls(DaemonClient(base_url=base_url, token=token), sandbox=sandbox, metadata=metadata)

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
    ) -> ComputerSandbox:
        if base_url:
            return cls(DaemonClient(base_url=base_url, token=token))
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
        return cls(
            DaemonClient(base_url=connect_base_url, token=connect_token),
            sandbox=sandbox,
            metadata=metadata,
        )

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
                computer = cls.attach(run_id=config.run_id, app_name=app_name)
                _check_config_hash(
                    computer.metadata(),
                    requested_hash=requested_hash,
                    on_config_mismatch=on_config_mismatch,
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
                computer = cls.attach(name=name, app_name=app_name)
                _check_config_hash(
                    computer.metadata(),
                    requested_hash=requested_hash,
                    on_config_mismatch=on_config_mismatch,
                )
                return computer
            except SandboxAmbiguousError:
                raise
            except SandboxUnavailableError:
                pass
        elif reuse_policy != "never":
            raise ValueError("reuse must be 'by_run_id', 'by_name', 'never', True, or False")

        return cls.create(config=config, app_name=app_name, name=name, **kwargs)

    def start(self) -> object:
        return self.lifecycle.start()

    def stop(self) -> object:
        return self.lifecycle.stop()

    def restart(self) -> object:
        return self.lifecycle.restart()

    def status(self) -> ComputerStatus:
        return self.lifecycle.status()

    def wait_until_ready(self, timeout: float = 120.0, interval: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                payload = self.client.get_json("/readyz")
                if payload.get("ready") is True:
                    return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
            if time.monotonic() >= deadline:
                raise TimeoutError("daemon did not become ready before timeout")
            time.sleep(interval)

    def terminate(self) -> None:
        if self._sandbox is not None and hasattr(self._sandbox, "terminate"):
            self._sandbox.terminate()
        else:
            self.stop()

    def detach(self) -> None:
        if self._sandbox is not None and hasattr(self._sandbox, "detach"):
            self._sandbox.detach()
        self.client.close()

    def metadata(self) -> SandboxRef | None:
        return self._metadata

    def debug_urls(self) -> DebugUrls:
        return self.debug.urls()

    def snapshot_filesystem(self) -> object:
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
        return self._sandbox.snapshot_filesystem()


def _daemon_environment(config: ComputerConfig, *, vnc_mode: str) -> dict[str, str]:
    env = {
        "COMPUTER_USE_RUN_ID": config.run_id or "",
        "COMPUTER_USE_DESKTOP_WIDTH": str(config.desktop.resolution[0]),
        "COMPUTER_USE_DESKTOP_HEIGHT": str(config.desktop.resolution[1]),
        "COMPUTER_USE_DESKTOP_DPI": str(config.desktop.dpi),
        "COMPUTER_USE_DISPLAY_DEPTH": str(config.desktop.display_depth),
        "COMPUTER_USE_RECORDINGS_DIR": config.storage.recordings_dir,
        "COMPUTER_USE_ARTIFACTS_DIR": config.storage.artifacts_dir,
        "COMPUTER_USE_TRACE_DIR": config.storage.trace_dir,
        "COMPUTER_USE_WINDOW_MANAGER": config.desktop.window_manager,
        "COMPUTER_USE_IMAGE_PROFILE": config.resources.profile,
        "COMPUTER_USE_BROWSER": config.browser.kind
        if config.browser and config.browser.kind
        else "",
        "COMPUTER_USE_BROWSER_PREWARM": str(
            config.browser.prewarm if config.browser else False
        ).lower(),
        "COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION": (
            config.actions.screenshot_processing_location
        ),
        "COMPUTER_USE_POST_ACTION_DELAY_MS": str(config.actions.post_action_delay_ms),
        "COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS": str(config.actions.default_action_timeout_ms),
        "COMPUTER_USE_MAX_ACTION_TIMEOUT_MS": str(config.actions.max_action_timeout_ms),
        "COMPUTER_USE_TRACE_ACTIONS": str(config.actions.trace_actions).lower(),
        "COMPUTER_USE_VNC_MODE": vnc_mode,
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
    }
    return env


def _connect_token_parts(token_info: object) -> tuple[str, str | None]:
    if isinstance(token_info, str):
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
    return str(base_url).rstrip("/"), str(token) if token else None


def _normalize_reuse_policy(reuse: bool | ReusePolicy) -> ReusePolicy:
    if reuse is True:
        return "by_run_id"
    if reuse is False:
        return "never"
    if reuse in ("by_run_id", "by_name", "never"):
        return reuse
    raise ValueError("reuse must be 'by_run_id', 'by_name', 'never', True, or False")


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
