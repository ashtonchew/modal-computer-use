from __future__ import annotations

import time
from typing import Any

from ._version import __version__
from .client import DaemonClient
from .config import ComputerConfig, normalize_vnc_mode
from .errors import ModalNotInstalledError, SandboxUnavailableError
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
            "environment_variables": env,
            "block_network": config.network.block_all,
            "cidr_allowlist": config.network.cidr_allowlist,
            "name": name,
            "tags": sandbox_tags,
            **sandbox_kwargs,
        }
        if hasattr(modal, "web_server"):
            create_kwargs["readiness_probe"] = modal.web_server(
                port=8080,
                startup_timeout=config.runtime.readiness_timeout_seconds,
            )
        sandbox = modal.Sandbox.create("python", "-m", "modal_computer_use.daemon", **create_kwargs)
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
            app = modal.App.lookup(app_name, create_if_missing=False)
            sandbox = modal.Sandbox.from_name(name, app=app)
        elif run_id:
            matches = list(modal.Sandbox.list(tags={"computer-use.run_id": run_id}))
            if not matches:
                raise SandboxUnavailableError(f"no running sandbox for run_id={run_id}")
            sandbox = matches[0]
        else:
            raise ValueError("attach requires sandbox_id, name, run_id, or base_url")
        token_info = sandbox.create_connect_token(
            user_metadata={"sdk": "modal-computer-use", "version": __version__}
        )
        connect_base_url, connect_token = _connect_token_parts(token_info)
        return cls(DaemonClient(base_url=connect_base_url, token=connect_token), sandbox=sandbox)

    @classmethod
    def attach_or_create(
        cls,
        *,
        config: ComputerConfig | None = None,
        app_name: str = "modal-computer-use",
        reuse: bool = True,
        **kwargs: Any,
    ) -> ComputerSandbox:
        config = config or ComputerConfig()
        if reuse and config.run_id:
            try:
                return cls.attach(run_id=config.run_id, app_name=app_name)
            except SandboxUnavailableError:
                pass
        return cls.create(config=config, app_name=app_name, **kwargs)

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
        "COMPUTER_USE_TRACE_ACTIONS": str(config.actions.trace_actions).lower(),
        "COMPUTER_USE_VNC_MODE": vnc_mode,
        "COMPUTER_USE_MAX_BATCH_ACTIONS": str(config.actions.max_batch_actions),
        "COMPUTER_USE_MAX_ACTIONS": ""
        if config.budgets.max_actions is None
        else str(config.budgets.max_actions),
        "COMPUTER_USE_MAX_SCREENSHOTS": ""
        if config.budgets.max_screenshots is None
        else str(config.budgets.max_screenshots),
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


def _vnc_url(sandbox: object) -> str | None:
    try:
        tunnels = sandbox.tunnels()
    except Exception:
        return None
    tunnel = tunnels.get(6080) if isinstance(tunnels, dict) else None
    if tunnel is None:
        return None
    return str(getattr(tunnel, "url", None) or getattr(tunnel, "tcp_socket", None) or "")
