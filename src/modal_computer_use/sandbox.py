from __future__ import annotations

import json
import secrets as _secrets
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit, urlunsplit

from ._version import __version__
from .client import DaemonClient
from .config import ComputerConfig, ModalIngress, normalize_vnc_mode
from .errors import (
    ConfigConflictError,
    ModalNotInstalledError,
    SandboxAmbiguousError,
    SandboxUnavailableError,
)
from .hot_session import HotSessionClient
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
from .observations import ObservationClient
from .state import compute_config_hash, default_tags, new_run_id
from .transports import HotSessionTransport, ObservationStreamTransport

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


def modal_workspace_billing_report(
    *,
    start: datetime,
    end: datetime | None,
    resolution: str,
    tag_names: list[str] | None,
) -> list[object]:
    try:
        import modal.billing
    except ImportError as exc:
        raise ModalNotInstalledError(
            "Modal billing reconciliation requires the modal extra, for example "
            "`uv sync --extra modal` in this repository or "
            "`uv add 'modal-computer-use[modal]'` downstream"
        ) from exc

    return list(
        modal.billing.workspace_billing_report(
            start=start,
            end=end,
            resolution=resolution,
            tag_names=tag_names,
        )
    )


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
        app_tags: dict[str, str] | None = None,
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
        volumes = volumes or {}
        artifact_volume_mounted = _has_artifact_volume_mount(
            volumes, config.storage.artifacts_dir
        )
        if config.storage.persist_artifacts and not artifact_volume_mounted:
            raise ConfigConflictError(
                "persist_artifacts=True requires a Volume mounted at storage.artifacts_dir "
                "or one of its parent directories"
            )
        vnc_mode = normalize_vnc_mode(expose_vnc if expose_vnc is not None else config.expose_vnc)
        image = image or default_image(
            profile=config.resources.profile,
            browser=config.browser.kind if config.browser else None,
            window_manager=config.desktop.window_manager,
            browser_prewarm=config.browser.prewarm if config.browser else False,
        )
        app = modal.App.lookup(app_name, create_if_missing=True)
        if app_tags:
            _set_modal_object_tags(app, app_tags)
        env = _daemon_environment(
            config,
            vnc_mode=vnc_mode,
            artifact_volume_mounted=artifact_volume_mounted,
        )
        sandbox_tags = {**(tags or {}), **default_tags(config, owner=owner)}
        http2 = config.network.daemon_http_version == "2"
        ports = _encrypted_ports_for_ingress(config.ingress, vnc_mode=vnc_mode, http2=http2)
        h2_ports = _h2_ports_for_ingress(config.ingress, http2=http2)
        tunnel_token = _secrets.token_urlsafe(32) if config.ingress == "tunnel" else None
        if tunnel_token:
            env["COMPUTER_USE_TUNNEL_TOKEN"] = tunnel_token
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
            "cidr_allowlist": config.network.cidr_allowlist,
            "name": name,
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
        if hasattr(sandbox, "set_tags"):
            _set_modal_object_tags(sandbox, sandbox_tags)
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
            config_hash=compute_config_hash(config),
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
            computer.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
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
                computer.wait_until_ready(timeout=readiness_timeout)
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
                computer.wait_until_ready(timeout=readiness_timeout)
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
                    wait=wait,
                    readiness_timeout=readiness_timeout or config.runtime.readiness_timeout_seconds,
                )
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
                computer = cls.attach(
                    name=name,
                    app_name=app_name,
                    wait=wait,
                    readiness_timeout=readiness_timeout or config.runtime.readiness_timeout_seconds,
                )
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

        return cls.create(config=config, app_name=app_name, name=name, wait=wait, **kwargs)

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
        last_payload: object | None = None
        last_error: Exception | None = None
        while True:
            try:
                payload = self.client.get_json("/readyz")
                last_payload = payload
                last_error = None
                if payload.get("ready") is True:
                    return
            except Exception as exc:
                last_error = exc
            if time.monotonic() >= deadline:
                detail = _readiness_timeout_detail(last_payload, last_error)
                raise TimeoutError(
                    f"daemon did not become ready before timeout ({timeout:g}s){detail}"
                )
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
        frame_encoding: Literal["json-binary", "binary-envelope"] | None = None,
        timeout: float = 30.0,
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
        )

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

    def snapshot_directory(self, path: str = "/home/desktop/artifacts") -> object:
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
        return self._sandbox.snapshot_directory(path)

    def mount_image(self, path: str, image: object) -> None:
        """Mount a Modal Image into a running Modal-backed sandbox."""
        if self._sandbox is None or not hasattr(self._sandbox, "mount_image"):
            raise SandboxUnavailableError(
                "mount_image requires a Modal-backed sandbox with Sandbox.mount_image support"
            )
        self._sandbox.mount_image(path, image)


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
            return f"; last /readyz errors: {', '.join(str(item) for item in errors)}"
        if payload:
            return f"; last /readyz response: {payload}"
    if error is not None:
        return f"; last error: {type(error).__name__}: {error}"
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
    existing_tags: dict[str, str] = {}
    get_tags = getattr(target, "get_tags", None)
    if callable(get_tags):
        raw_tags = get_tags()
        if isinstance(raw_tags, dict):
            existing_tags = {str(key): str(value) for key, value in raw_tags.items()}
    set_tags({**existing_tags, **tags})
