from __future__ import annotations

import ast
import asyncio
import json
import pickle
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from modal_computer_use import (
    AsyncBorrowedComputer,
    BorrowedComputer,
    ComputerConfig,
    ComputerSandbox,
    ComputerSessionHandle,
    SandboxRef,
)
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.leases import (
    LEASE_EPOCH_HEADER,
    LEASE_FENCE_HEADER,
    LEASE_ID_HEADER,
    LEASE_TOKEN_HEADER,
)
from modal_computer_use.daemon.receipts import OPERATION_SEQUENCE_HEADER
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.errors import (
    SandboxUnavailableError,
    SessionCompatibilityError,
    SessionEnvironmentMismatchError,
    SessionLeaseLostError,
    SessionPlacementMismatchError,
    SessionTargetMismatchError,
)
from modal_computer_use.sandbox import _session_policy_id_prefix
from modal_computer_use.state import APP_ID_TAG, compute_config_hash

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _modal_function_environment(monkeypatch) -> None:
    monkeypatch.setenv("MODAL_ENVIRONMENT", "prod")
    monkeypatch.setenv("MODAL_IS_REMOTE", "1")
    monkeypatch.setenv("MODAL_REGION", "us-west-2")

    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_runtime_placement",
        lambda _sandbox: {"cloud": "aws", "region": "us-west-2"},
    )

    async def runtime_placement_async(_sandbox: object) -> dict[str, str]:
        return {"cloud": "aws", "region": "us-west-2"}

    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_runtime_placement_async",
        runtime_placement_async,
    )


def _session_id(
    *,
    app_name: str = "desktop-app",
    modal_environment: str = "prod",
    requested_modal_region: str = "us-west-2",
    ingress: str = "connect",
    daemon_http_version: str = "1.1",
    vnc_mode: str = "off",
    config_hash: str = "a" * 16,
) -> str:
    return _session_policy_id_prefix(
        app_name=app_name,
        modal_environment=modal_environment,
        requested_modal_region=requested_modal_region,
        ingress=ingress,
        daemon_http_version=daemon_http_version,
        vnc_mode=vnc_mode,
        config_hash=config_hash,
    ) + "b" * 16


class _ConnectToken:
    url = "https://connect.invalid"
    token = "credential-" + "value"


class _OwnedSandbox:
    def __init__(self, *, config_hash: str | None = None) -> None:
        self.object_id = "sb-owned"
        self._tags = {
            "computer-use": "true",
            "computer-use.config_hash": config_hash or "ignored",
            "computer-use.session_id": _session_id(
                config_hash=config_hash or "ignored"
            ),
            "computer-use.app_name": "desktop-app",
            "computer-use.vnc_mode": "off",
        }
        self.detach_calls = 0
        self.terminate_calls = 0
        self.credential_calls = 0

    def create_connect_token(self, **kwargs: object) -> _ConnectToken:
        assert kwargs["port"] == 8080
        self.credential_calls += 1
        return _ConnectToken()

    def get_tags(self) -> dict[str, str]:
        return self._tags

    def detach(self) -> None:
        self.detach_calls += 1

    def terminate(self, **_kwargs: object) -> None:
        self.terminate_calls += 1

    def tunnels(self) -> dict[int, object]:
        return {8080: SimpleNamespace(url="https://daemon.invalid")}


class _ModalSandboxType:
    created: _OwnedSandbox | None = None
    from_id_result: _OwnedSandbox | None = None

    @classmethod
    def create(cls, *_args: str, **kwargs: object) -> _OwnedSandbox:
        tags = kwargs["tags"]
        assert isinstance(tags, dict)
        cls.created = _OwnedSandbox(config_hash=tags.get("computer-use.config_hash"))
        cls.created._tags = tags
        return cls.created

    @classmethod
    def from_id(cls, _sandbox_id: str) -> _OwnedSandbox:
        assert cls.from_id_result is not None
        return cls.from_id_result


class _ModalApp:
    @classmethod
    def lookup(cls, _app_name: str, **_kwargs: object) -> object:
        return SimpleNamespace(app_id=f"ap-{_app_name}")


class _Client:
    def __init__(self) -> None:
        self.close_calls = 0
        self.base_url = "https://private.invalid"
        self.transport = SimpleNamespace(token="credential-value")

    def close(self) -> None:
        self.close_calls += 1


class _Coordinator:
    def __init__(self) -> None:
        self.close_calls = 0
        self.closed = False

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def ensure_open(self) -> None:
        if self.closed:
            raise SessionLeaseLostError()

    def execute(self, request):
        return request({})

    def observe_after_result_loss(self, request):
        return request()

    def metadata_headers(self) -> dict[str, str]:
        return {}

    def track(self, resource):
        return resource


def _handle(**updates: object) -> ComputerSessionHandle:
    values: dict[str, object] = {
        "sandbox_id": "sb-owned",
        "app_name": "desktop-app",
        "modal_environment": "prod",
        "requested_modal_region": "us-west-2",
        "ingress": "connect",
        "daemon_http_version": "1.1",
        "vnc_mode": "off",
        "config_hash": "a" * 16,
    }
    values.update(updates)
    if "session_id" not in updates:
        values["session_id"] = _session_id(
            app_name=str(values["app_name"]),
            modal_environment=str(values["modal_environment"]),
            requested_modal_region=str(values["requested_modal_region"]),
            ingress=str(values["ingress"]),
            daemon_http_version=str(values["daemon_http_version"]),
            vnc_mode=str(values["vnc_mode"]),
            config_hash=str(values["config_hash"]),
        )
    return ComputerSessionHandle(**values)  # type: ignore[arg-type]


def _borrowed_computer(
    *, config_hash: str = "a" * 16
) -> tuple[ComputerSandbox, _OwnedSandbox, _Client]:
    target = _OwnedSandbox(config_hash=config_hash)
    client = _Client()
    metadata = SandboxRef(
        sandbox_id="sb-owned",
        app_name="desktop-app",
        config_hash=config_hash,
        status="ready",
        tags=target.get_tags(),
    )
    return ComputerSandbox(client, sandbox=target, metadata=metadata), target, client


def _borrow_result(computer: ComputerSandbox):
    coordinator = _Coordinator()
    client = computer.client
    target = computer._sandbox
    return (
        BorrowedComputer(
            client,  # type: ignore[arg-type]
            coordinator,  # type: ignore[arg-type]
            base_url=client.base_url,
            token=client.transport.token,
            http2=False,
        ),
        target,
        client,
        coordinator,
    )


def test_create_produces_safe_versioned_session_handle(monkeypatch) -> None:
    runtime = SimpleNamespace(App=_ModalApp, Sandbox=_ModalSandboxType, Probe=None)
    monkeypatch.setitem(sys.modules, "modal", runtime)
    config = ComputerConfig(
        run_id="run-123",
        ingress="connect",
        runtime={"modal_environment": "prod", "modal_region": "us-west-2"},
        network={"daemon_http_version": "2"},
    )

    computer = ComputerSandbox.create(
        config=config,
        image=object(),
        app_name="desktop-app",
        wait=False,
    )
    handle = computer.session_handle()

    assert handle.schema_version == 2
    assert handle.handoff_protocol == "computer-use.session-handoff.v2"
    assert handle.sandbox_id == "sb-owned"
    assert len(handle.session_id) == 32
    assert handle.app_name == "desktop-app"
    assert handle.modal_environment == "prod"
    assert handle.requested_modal_region == "us-west-2"
    assert handle.ingress == "connect"
    assert handle.daemon_http_version == "2"
    assert handle.vnc_mode == "off"
    assert handle.config_hash == computer.metadata().config_hash  # type: ignore[union-attr]
    assert computer.metadata().tags["computer-use.session_id"] == handle.session_id  # type: ignore[union-attr]


def test_session_handle_rejects_local_unsupported_and_insufficient_targets(monkeypatch) -> None:
    with pytest.raises(SandboxUnavailableError, match="SDK-owned Modal desktop"):
        ComputerSandbox.local().session_handle()

    runtime = SimpleNamespace(App=_ModalApp, Sandbox=_ModalSandboxType, Probe=None)
    monkeypatch.setitem(sys.modules, "modal", runtime)
    no_environment = ComputerSandbox.create(
        config=ComputerConfig(
            run_id="run-no-environment",
            ingress="connect",
            runtime={"modal_region": "us-west-2"},
        ),
        image=object(),
        wait=False,
    )
    assert "computer-use.session_id" not in no_environment.metadata().tags  # type: ignore[union-attr]
    with pytest.raises(SessionCompatibilityError):
        no_environment.session_handle()

    no_region = ComputerSandbox.create(
        config=ComputerConfig(
            run_id="run-no-region",
            ingress="connect",
            runtime={"modal_environment": "prod"},
        ),
        image=object(),
        wait=False,
    )
    assert "computer-use.session_id" not in no_region.metadata().tags  # type: ignore[union-attr]
    with pytest.raises(SessionCompatibilityError):
        no_region.session_handle()

    raw_tunnel = ComputerSandbox.create(
        config=ComputerConfig(
            run_id="run-tunnel",
            ingress="tunnel",
            runtime={"modal_environment": "prod", "modal_region": "us-west-2"},
        ),
        image=object(),
        wait=False,
    )
    with pytest.raises(SessionCompatibilityError):
        raw_tunnel.session_handle()

    control_vnc = ComputerSandbox.create(
        config=ComputerConfig(
            run_id="run-control-vnc",
            ingress="connect",
            expose_vnc="control",
            runtime={"modal_environment": "prod", "modal_region": "us-west-2"},
        ),
        image=object(),
        wait=False,
    )
    with pytest.raises(SessionCompatibilityError):
        control_vnc.session_handle()

    unverifiable = ComputerSandbox.create(
        config=ComputerConfig(
            run_id="run-warm",
            ingress="connect",
            runtime={"modal_environment": "prod", "modal_region": "us-west-2"},
        ),
        image=object(),
        tag_profile="warm_pool",
        wait=False,
    )
    with pytest.raises(SessionTargetMismatchError):
        unverifiable.session_handle()

    direct_attach = ComputerSandbox.attach(base_url="https://fixture.invalid")
    with pytest.raises(SandboxUnavailableError, match="SDK-owned Modal desktop"):
        direct_attach.session_handle()


def test_handle_is_strict_frozen_and_json_pickle_serializable() -> None:
    handle = _handle()
    assert set(ComputerSessionHandle.model_fields) == {
        "schema_version",
        "handoff_protocol",
        "sandbox_id",
        "session_id",
        "app_name",
        "modal_environment",
        "requested_modal_region",
        "ingress",
        "daemon_http_version",
        "vnc_mode",
        "config_hash",
    }
    assert ComputerSessionHandle.model_validate_json(handle.model_dump_json()) == handle
    assert pickle.loads(pickle.dumps(handle)) == handle  # noqa: S301 - trusted local object

    with pytest.raises(ValidationError):
        ComputerSessionHandle.model_validate({**handle.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        _handle(schema_version=1)
    with pytest.raises(ValidationError):
        _handle(handoff_protocol="computer-use.session-handoff.v1")
    with pytest.raises(ValidationError):
        _handle(session_id="not-a-session-id")
    with pytest.raises(ValidationError):
        _handle(modal_environment=" ")
    with pytest.raises(ValidationError):
        _handle(requested_modal_region=" ")
    with pytest.raises(ValidationError):
        _handle(ingress="tunnel")
    with pytest.raises(ValidationError):
        _handle(daemon_http_version=2)
    with pytest.raises(ValidationError):
        _handle(vnc_mode="control")
    with pytest.raises(ValidationError):
        _handle(config_hash="not-a-config-hash")
    with pytest.raises(ValidationError):
        handle.app_name = "changed"  # type: ignore[misc]


def test_handle_serialization_and_repr_contain_no_credentials_or_endpoints() -> None:
    handle = _handle()
    serialized = json.dumps(handle.model_dump(mode="json"), sort_keys=True)
    rendered = repr(handle)

    assert "sb-owned" in serialized
    assert "sb-owned" not in rendered
    assert handle.session_id in serialized
    assert handle.session_id not in rendered
    for forbidden in ("credential-value", "connect.invalid", "private.invalid", "novnc"):
        assert forbidden not in serialized
        assert forbidden not in rendered


def test_handle_validation_errors_hide_rejected_secret_bearing_inputs() -> None:
    rejected_endpoint = "https://" + "credential-value.invalid/private"
    with pytest.raises(ValidationError) as error:
        ComputerSessionHandle.model_validate(
            {**_handle().model_dump(), "unexpected_endpoint": rejected_endpoint}
        )

    rendered = f"{error.value!s} {error.value!r}"
    assert rejected_endpoint not in rendered
    assert "credential-value" not in rendered


def test_borrow_is_lazy_and_rejects_region_before_attach(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    raw_computer = _borrowed_computer()[0]

    def attach(_handle: object, **kwargs: object):
        calls.append(kwargs)
        return _borrow_result(raw_computer)

    monkeypatch.setattr("modal_computer_use.sandbox._borrow_modal_function_session", attach)
    context = _handle().borrow(run_id="run-123", function_region="us-west-2")
    assert calls == []

    with context as computer:
        assert isinstance(computer, BorrowedComputer)
        assert type(computer.actions) is type(raw_computer.actions)
        assert len(calls) == 1
        assert calls[0]["run_id"] == "run-123"

    calls.clear()
    with (
        pytest.raises(SessionPlacementMismatchError),
        _handle().borrow(run_id="run-123", function_region="us-east-1"),
    ):
        raise AssertionError("unreachable")
    assert calls == []


def test_borrow_validates_run_id_before_attach(monkeypatch) -> None:
    calls = 0

    def attach(_handle: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        return _borrow_result(_borrowed_computer()[0])

    monkeypatch.setattr("modal_computer_use.sandbox._borrow_modal_function_session", attach)
    with pytest.raises(ValueError, match="run_id"), _handle().borrow(
        run_id=" ", function_region="us-west-2"
    ):
        raise AssertionError("unreachable")
    assert calls == 0


def test_borrow_rejects_non_remote_runtime_before_attach(monkeypatch) -> None:
    calls = 0

    def attach(_handle: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        return _borrow_result(_borrowed_computer()[0])

    monkeypatch.delenv("MODAL_IS_REMOTE")
    monkeypatch.setattr("modal_computer_use.sandbox._borrow_modal_function_session", attach)
    with pytest.raises(SessionEnvironmentMismatchError), _handle().borrow(
        run_id="run-123", function_region="us-west-2"
    ):
        raise AssertionError("unreachable")
    assert calls == 0


@pytest.mark.parametrize("function_environment", [None, "staging"])
def test_borrow_rejects_missing_or_mismatched_modal_environment_before_attach(
    monkeypatch,
    function_environment: str | None,
) -> None:
    calls = 0

    def attach(_handle: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        return _borrow_result(_borrowed_computer()[0])

    monkeypatch.setattr("modal_computer_use.sandbox._borrow_modal_function_session", attach)
    if function_environment is None:
        monkeypatch.delenv("MODAL_ENVIRONMENT")
    else:
        monkeypatch.setenv("MODAL_ENVIRONMENT", function_environment)

    with pytest.raises(SessionEnvironmentMismatchError), _handle().borrow(
        run_id="run-123", function_region="us-west-2"
    ):
        raise AssertionError("unreachable")
    assert calls == 0


def test_borrowed_computer_exposes_only_daemon_capabilities(monkeypatch) -> None:
    computer, _target, _client = _borrowed_computer()
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda _handle, **_kwargs: _borrow_result(computer),
    )

    with _handle().borrow(run_id="run-123", function_region="us-west-2") as borrowed:
        for capability in (
            "actions",
            "apps",
            "artifacts",
            "browser",
            "clipboard",
            "commands",
            "display",
            "input",
            "keyboard",
            "mouse",
            "recordings",
            "screenshots",
            "windows",
            "hot_session",
            "observation_stream",
            "observe_after_result_loss",
        ):
            assert hasattr(borrowed, capability)
        for forbidden in (
            "client",
            "lifecycle",
            "processes",
            "debug",
            "session",
            "start",
            "stop",
            "restart",
            "terminate",
            "detach",
            "poll",
            "runtime_placement",
            "tags",
            "snapshot_filesystem",
            "mount_image",
            "reload_volumes",
            "session_handle",
            "metadata",
        ):
            assert not hasattr(borrowed, forbidden)


def _inline_png_binary_response() -> tuple[bytes, dict[str, str]]:
    return b"\x00", {
        "content-type": "image/png",
        "x-computer-use-width": "1",
        "x-computer-use-height": "1",
        "x-computer-use-size-bytes": "1",
        "x-computer-use-sha256": (
            "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
        ),
        "x-computer-use-captured-at": "2026-08-08T12:30:00+00:00",
        "x-computer-use-coordinate-space": (
            '{"desktop_width":1,"desktop_height":1,"image_width":1,'
            '"image_height":1,"scale_x":1.0,"scale_y":1.0,'
            '"source_region":null}'
        ),
        "x-computer-use-cursor-visible": "false",
        "x-computer-use-cursor-position": '{"x":0,"y":0}',
        "x-computer-use-timing-ms": "{}",
        "x-computer-use-capture-backend": "mss",
    }


def test_sync_lost_result_observation_has_fixed_inline_full_png_semantics() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def post_bytes_with_headers(
            self, path: str, **kwargs: object
        ) -> tuple[bytes, dict[str, str]]:
            self.calls.append((path, kwargs))
            return _inline_png_binary_response()

    client = Client()
    coordinator = _Coordinator()
    borrowed = BorrowedComputer(
        client,  # type: ignore[arg-type]
        coordinator,  # type: ignore[arg-type]
        base_url="https://private.invalid",
        token="credential-value",
        http2=False,
    )

    frame = borrowed.observe_after_result_loss()

    assert frame.format == "png"
    assert frame.artifact_uri is None
    assert client.calls == [
        (
            "/v1/screenshots/full/raw",
            {
                "json": {
                    "format": "png",
                    "quality": 90,
                    "scale": 1.0,
                    "show_cursor": False,
                    "processing": "daemon",
                    "storage": "inline",
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_async_lost_result_observation_has_fixed_inline_full_png_semantics() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def post_bytes_with_headers(
            self, path: str, **kwargs: object
        ) -> tuple[bytes, dict[str, str]]:
            self.calls.append((path, kwargs))
            return _inline_png_binary_response()

    class Coordinator:
        async def observe_after_result_loss(self, request):
            return await request()

    client = Client()
    borrowed = AsyncBorrowedComputer(
        client,  # type: ignore[arg-type]
        Coordinator(),  # type: ignore[arg-type]
        base_url="https://private.invalid",
        token="credential-value",
        http2=False,
    )

    frame = await borrowed.observe_after_result_loss()

    assert frame.format == "png"
    assert frame.artifact_uri is None
    assert client.calls == [
        (
            "/v1/screenshots/full/raw",
            {
                "json": {
                    "format": "png",
                    "quality": 90,
                    "scale": 1.0,
                    "show_cursor": False,
                    "processing": "daemon",
                    "storage": "inline",
                },
            },
        )
    ]


def test_failed_sync_lost_result_observation_still_releases_and_detaches(
    monkeypatch,
) -> None:
    _computer, target, client = _borrowed_computer()

    class Coordinator(_Coordinator):
        def observe_after_result_loss(self, _request):
            raise ConnectionError("private endpoint and token")

    coordinator = Coordinator()
    borrowed = BorrowedComputer(
        client,  # type: ignore[arg-type]
        coordinator,  # type: ignore[arg-type]
        base_url=client.base_url,
        token=client.transport.token,
        http2=False,
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda _handle, **_kwargs: (borrowed, target, client, coordinator),
    )

    with (
        pytest.raises(ConnectionError, match="private endpoint"),
        _handle().borrow(run_id="run-123", function_region="us-west-2") as retained,
    ):
        retained.observe_after_result_loss()

    assert coordinator.close_calls == 1
    assert client.close_calls == 1
    assert target.detach_calls == 1
    assert target.terminate_calls == 0


@pytest.mark.asyncio
async def test_failed_async_lost_result_observation_still_releases_and_detaches(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Client:
        async def aclose(self) -> None:
            calls.append("client.aclose")

    class Coordinator:
        async def observe_after_result_loss(self, _request):
            raise ConnectionError("private endpoint and token")

        async def aclose(self) -> None:
            calls.append("coordinator.aclose")

    class Target:
        def __init__(self) -> None:
            self.detach = _AioCall(self._detach)

        async def _detach(self) -> None:
            calls.append("detach.aio")

    client = Client()
    coordinator = Coordinator()
    target = Target()
    borrowed = AsyncBorrowedComputer(
        client,  # type: ignore[arg-type]
        coordinator,  # type: ignore[arg-type]
        base_url="https://private.invalid",
        token="credential-value",
        http2=False,
    )

    async def attach(*_args: object, **_kwargs: object):
        return borrowed, target, client, coordinator

    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session_async",
        attach,
    )

    with pytest.raises(ConnectionError, match="private endpoint"):
        async with _handle().borrow_async(
            run_id="async-run",
            function_region="us-west-2",
        ) as retained:
            await retained.observe_after_result_loss()

    assert calls == ["coordinator.aclose", "client.aclose", "detach.aio"]


def test_borrow_context_rejects_second_entry_without_replacing_live_client(monkeypatch) -> None:
    computer, target, client = _borrowed_computer()
    calls = 0

    def attach(_handle: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        return _borrow_result(computer)

    monkeypatch.setattr("modal_computer_use.sandbox._borrow_modal_function_session", attach)
    context = _handle().borrow(run_id="run-123", function_region="us-west-2")
    assert isinstance(context.__enter__(), BorrowedComputer)
    with pytest.raises(RuntimeError, match="only be entered once"):
        context.__enter__()
    context.__exit__(None, None, None)

    assert calls == 1
    assert target.detach_calls == 1
    assert client.close_calls == 1
    assert target.terminate_calls == 0


def test_sync_borrowed_facade_cannot_resurrect_connections_after_exit(monkeypatch) -> None:
    computer, _target, _client = _borrowed_computer()
    borrowed, target, client, coordinator = _borrow_result(computer)
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda _handle, **_kwargs: (borrowed, target, client, coordinator),
    )

    with _handle().borrow(run_id="run-123", function_region="us-west-2") as retained:
        pass

    with pytest.raises(SessionLeaseLostError):
        retained.hot_session()
    with pytest.raises(SessionLeaseLostError):
        retained.observation_stream()


def test_attach_or_create_reuse_aligns_ingress_and_http_policy(monkeypatch) -> None:
    config = ComputerConfig(
        run_id="run-reuse",
        ingress="connect",
        runtime={"modal_environment": "prod", "modal_region": "us-west-2"},
        network={"daemon_http_version": "2"},
    )
    config_hash = compute_config_hash(config)
    computer, target, _client = _borrowed_computer(config_hash=config_hash)
    target._tags["computer-use.run_id"] = "run-reuse"
    target._tags[APP_ID_TAG] = "ap-desktop-app"
    calls: list[dict[str, object]] = []

    def attach_resolved(
        cls: type[ComputerSandbox],
        sandbox: object,
        **kwargs: object,
    ) -> ComputerSandbox:
        assert sandbox is target
        calls.append(kwargs)
        return computer

    runtime = SimpleNamespace(App=_ModalApp, Sandbox=_ModalSandboxType, Probe=None)
    monkeypatch.setitem(sys.modules, "modal", runtime)
    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_from_name",
        lambda *_args, **_kwargs: target,
    )
    monkeypatch.setattr(
        ComputerSandbox,
        "_attach_resolved_sandbox",
        classmethod(attach_resolved),
    )
    reused = ComputerSandbox.attach_or_create(
        name="desktop-reuse",
        config=config,
        app_name="desktop-app",
        wait=False,
    )

    assert reused is computer
    assert calls[0]["ingress"] == "connect"
    assert calls[0]["http2"] is True
    handle = reused.session_handle()
    assert handle.ingress == "connect"
    assert handle.daemon_http_version == "2"


def test_borrow_live_config_mismatch_cleans_up_without_termination(monkeypatch) -> None:
    _computer, target, client = _borrowed_computer(config_hash="different")
    _ModalSandboxType.from_id_result = target
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=_ModalSandboxType),
    )

    with (
        pytest.raises(SessionTargetMismatchError),
        _handle().borrow(run_id="run-123", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert target.detach_calls == 1
    assert client.close_calls == 0
    assert target.terminate_calls == 0


@pytest.mark.parametrize(
    ("observed_region", "expected_error"),
    [
        (None, "SessionPlacementMissingError"),
        ("https://private.invalid", "SessionPlacementMalformedError"),
        ("us", "SessionPlacementUnverifiableError"),
        ("us-east-1", "SessionPlacementMismatchError"),
    ],
)
def test_borrow_rejects_invalid_observed_target_placement_before_credentials(
    monkeypatch,
    observed_region: str | None,
    expected_error: str,
) -> None:
    _computer, target, client = _borrowed_computer()
    _ModalSandboxType.from_id_result = target
    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Sandbox=_ModalSandboxType))
    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_runtime_placement",
        lambda _sandbox: {"cloud": "aws", "region": observed_region},
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle().borrow(run_id="run-123", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == expected_error
    assert target.credential_calls == 0
    assert target.detach_calls == 1
    assert client.close_calls == 0


def test_borrow_accepts_concrete_target_region_for_public_narrow_selector(
    monkeypatch,
) -> None:
    target = _OwnedSandbox(config_hash="a" * 16)
    handle = _handle(requested_modal_region="us-west")
    target._tags["computer-use.session_id"] = handle.session_id
    _ModalSandboxType.from_id_result = target
    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Sandbox=_ModalSandboxType))
    monkeypatch.setenv("MODAL_REGION", "us-west1")
    transport = _CapabilityBorrowTransport(
        [
            "screenshot-binary-metadata-v1",
            "trajectory-leases-v1",
            "trajectory-operation-receipts-v1",
            "computer-step-envelope-v1",
        ]
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.HTTPTransport",
        lambda *_args, **_kwargs: transport,
    )

    with handle.borrow(run_id="run-123", function_region="us-west"):
        pass

    assert target.credential_calls == 1
    assert target.detach_calls == 1


@pytest.mark.asyncio
async def test_async_borrow_rejects_target_region_mismatch_before_credentials(
    monkeypatch,
) -> None:
    target = _AsyncTarget()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        target.calls.append("from_id.aio")
        return target

    async def mismatched_placement(_sandbox: object) -> dict[str, str]:
        return {"cloud": "aws", "region": "us-east-1"}

    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_runtime_placement_async",
        mismatched_placement,
    )

    with pytest.raises(SessionPlacementMismatchError):
        async with _handle().borrow_async(
            run_id="async-run",
            function_region="us-west-2",
        ):
            raise AssertionError("unreachable")

    assert target.calls == ["from_id.aio", "get_tags.aio", "detach.aio"]


def test_borrow_rejects_target_that_lost_sdk_ownership_marker(monkeypatch) -> None:
    computer, target, client = _borrowed_computer()
    target._tags.pop("computer-use")
    computer._metadata = computer.metadata().model_copy(update={"tags": target.get_tags()})  # type: ignore[union-attr]
    _ModalSandboxType.from_id_result = target
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=_ModalSandboxType),
    )

    with (
        pytest.raises(SessionTargetMismatchError),
        _handle().borrow(run_id="run-123", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert target.detach_calls == 1
    assert client.close_calls == 0
    assert target.terminate_calls == 0


def test_borrow_rejects_target_with_a_different_session_id(monkeypatch) -> None:
    computer, target, client = _borrowed_computer()
    target._tags["computer-use.session_id"] = "c" * 32
    computer._metadata = computer.metadata().model_copy(update={"tags": target.get_tags()})  # type: ignore[union-attr]
    _ModalSandboxType.from_id_result = target
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=_ModalSandboxType),
    )

    with (
        pytest.raises(SessionTargetMismatchError),
        _handle().borrow(run_id="run-123", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert target.detach_calls == 1
    assert client.close_calls == 0
    assert target.terminate_calls == 0


def test_borrow_reports_unverifiable_target_tags_before_credentials(monkeypatch) -> None:
    target = _OwnedSandbox(config_hash="a" * 16)
    target.get_tags = lambda: None  # type: ignore[method-assign,return-value]
    _ModalSandboxType.from_id_result = target
    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Sandbox=_ModalSandboxType))

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle().borrow(run_id="run-123", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementUnverifiableError"
    assert target.credential_calls == 0
    assert target.detach_calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_name", "other-app"),
        ("modal_environment", "staging"),
        ("requested_modal_region", "us-east-1"),
        ("ingress", "attested-tunnel"),
        ("daemon_http_version", "2"),
        ("vnc_mode", "view_only"),
        ("config_hash", "c" * 16),
    ],
)
def test_borrow_rejects_each_policy_field_tamper_before_credentials(
    monkeypatch,
    field: str,
    value: str,
) -> None:
    target = _OwnedSandbox(config_hash="a" * 16)
    _ModalSandboxType.from_id_result = target
    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Sandbox=_ModalSandboxType))
    if field == "modal_environment":
        monkeypatch.setenv("MODAL_ENVIRONMENT", value)
    if field == "requested_modal_region":
        monkeypatch.setenv("MODAL_REGION", value)
    function_region = value if field == "requested_modal_region" else "us-west-2"
    handle = _handle(**{field: value, "session_id": _session_id()})

    with pytest.raises(SessionTargetMismatchError), handle.borrow(
        run_id="run-123", function_region=function_region
    ):
        raise AssertionError("unreachable")

    assert target.credential_calls == 0
    assert target.detach_calls == 1


def test_sync_tag_lookup_failure_detaches_before_credential_issuance(monkeypatch) -> None:
    target = _OwnedSandbox(config_hash="a" * 16)

    def fail_tags() -> dict[str, str]:
        raise ConnectionError("tag lookup failed")

    target.get_tags = fail_tags  # type: ignore[method-assign]
    _ModalSandboxType.from_id_result = target
    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Sandbox=_ModalSandboxType))

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle().borrow(run_id="run-123", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementUnverifiableError"
    assert "tag lookup failed" not in str(raised.value)
    assert target.credential_calls == 0
    assert target.detach_calls == 1


@pytest.mark.parametrize("raises", [False, True])
def test_borrow_deterministically_detaches_without_terminating(
    monkeypatch,
    raises: bool,
) -> None:
    computer, target, client = _borrowed_computer()
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda _handle, **_kwargs: _borrow_result(computer),
    )

    if raises:
        with (
            pytest.raises(RuntimeError, match="user workload failed"),
            _handle().borrow(run_id="run-123", function_region="us-west-2"),
        ):
            raise RuntimeError("user workload failed")
    else:
        with _handle().borrow(run_id="run-123", function_region="us-west-2") as borrowed:
            assert isinstance(borrowed, BorrowedComputer)

    assert target.detach_calls == 1
    assert client.close_calls == 1
    assert target.terminate_calls == 0


@pytest.mark.parametrize(
    "failure",
    [PermissionError("authorization failed"), ConnectionError("endpoint failed"), TimeoutError()],
)
def test_borrow_does_not_retry_or_fallback_on_attach_failures(
    monkeypatch,
    failure: Exception,
) -> None:
    calls = 0

    def fail(_handle: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr("modal_computer_use.sandbox._borrow_modal_function_session", fail)
    with pytest.raises(type(failure)), _handle().borrow(
        run_id="run-123", function_region="us-west-2"
    ):
        raise AssertionError("unreachable")
    assert calls == 1


def test_detach_closes_client_when_modal_detach_raises() -> None:
    computer, target, client = _borrowed_computer()

    def fail() -> None:
        target.detach_calls += 1
        raise RuntimeError("provider detach failed")

    target.detach = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="provider detach failed"):
        computer.detach()
    assert client.close_calls == 1
    assert target.terminate_calls == 0


def test_computer_session_handle_is_public() -> None:
    import modal_computer_use

    assert modal_computer_use.ComputerSessionHandle is ComputerSessionHandle
    assert modal_computer_use.BorrowedComputer is BorrowedComputer


class _AioCall:
    def __init__(self, function) -> None:
        self._function = function

    async def aio(self, *args: object, **kwargs: object):
        return await self._function(*args, **kwargs)


class _AsyncTarget:
    def __init__(self) -> None:
        self.object_id = "sb-owned"
        self.calls: list[str] = []
        self._tags = {
            "computer-use": "true",
            "computer-use.app_name": "desktop-app",
            "computer-use.config_hash": "a" * 16,
            "computer-use.session_id": _session_id(),
            "computer-use.vnc_mode": "off",
        }
        self.get_tags = _AioCall(self._get_tags)
        self.create_connect_token = _AioCall(self._create_connect_token)
        self.tunnels = _AioCall(self._tunnels)
        self.detach = _AioCall(self._detach)

    async def _get_tags(self) -> dict[str, str]:
        self.calls.append("get_tags.aio")
        return self._tags

    async def _create_connect_token(self, **kwargs: object) -> _ConnectToken:
        assert kwargs["port"] == 8080
        self.calls.append("create_connect_token.aio")
        return _ConnectToken()

    async def _tunnels(self) -> dict[int, object]:
        self.calls.append("tunnels.aio")
        return {8080: SimpleNamespace(url="https://daemon.invalid")}

    async def _detach(self) -> None:
        self.calls.append("detach.aio")

    def terminate(self, **_kwargs: object) -> None:
        self.calls.append("terminate")


class _AsyncBorrowTransport:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def request(self, _method: str, path: str, **_kwargs: object) -> httpx.Response:
        self.calls.append(path)
        request = httpx.Request("POST", f"https://daemon.invalid{path}")
        if path == "/v1/leases/acquire":
            return httpx.Response(
                200,
                json={
                    "lease_id": "lease-test",
                    "daemon_epoch": "epoch-test",
                    "fence": 1,
                    "ttl_seconds": 30.0,
                    "heartbeat_interval_seconds": 60.0,
                },
                headers={"x-computer-use-lease-token": "lease-token"},
                request=request,
            )
        if path == "/readyz":
            return httpx.Response(200, json={"ready": True}, request=request)
        if path == "/v1/version":
            return httpx.Response(200, json={"api_version": "v1"}, request=request)
        if path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "primitives": [
                        "screenshot-binary-metadata-v1",
                        "trajectory-leases-v1",
                        "trajectory-operation-receipts-v1",
                        "computer-step-envelope-v1",
                    ]
                },
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    async def aclose(self) -> None:
        self.closed = True


class _SyncBorrowTransport:
    timeout = 1.0

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    def request(self, _method: str, path: str, **_kwargs: object) -> httpx.Response:
        self.calls.append(path)
        request = httpx.Request("POST", f"https://daemon.invalid{path}")
        if path == "/v1/leases/acquire":
            return httpx.Response(
                200,
                json={
                    "lease_id": "lease-test",
                    "daemon_epoch": "epoch-test",
                    "fence": 1,
                    "ttl_seconds": 30.0,
                    "heartbeat_interval_seconds": 60.0,
                },
                headers={"x-computer-use-lease-token": "lease-token"},
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    def close(self) -> None:
        self.closed = True


class _CapabilityBorrowTransport(_SyncBorrowTransport):
    def __init__(self, primitives: object) -> None:
        super().__init__()
        self.primitives = primitives
        self.base_url = "https://private.invalid"

    def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        self.calls.append(path)
        request = httpx.Request(method, f"https://daemon.invalid{path}")
        if path == "/readyz":
            return httpx.Response(200, json={"ready": True}, request=request)
        if path == "/v1/version":
            return httpx.Response(200, json={"api_version": "v1"}, request=request)
        if path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={"primitives": self.primitives},
                request=request,
            )
        if path == "/v1/leases/acquire":
            return httpx.Response(
                200,
                json={
                    "lease_id": "lease-test",
                    "daemon_epoch": "epoch-test",
                    "fence": 1,
                    "ttl_seconds": 30.0,
                    "heartbeat_interval_seconds": 60.0,
                },
                headers={"x-computer-use-lease-token": "lease-token"},
                request=request,
            )
        if path == "/v1/leases/release":
            return httpx.Response(200, json={"state": "released"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)


def test_borrow_rejects_missing_required_daemon_capability_before_lease(
    monkeypatch,
) -> None:
    target = _OwnedSandbox(config_hash="a" * 16)
    _ModalSandboxType.from_id_result = target
    transport = _CapabilityBorrowTransport(
        ["trajectory-leases-v1", "trajectory-operation-receipts-v1"]
    )
    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Sandbox=_ModalSandboxType))
    monkeypatch.setattr(
        "modal_computer_use.sandbox.HTTPTransport",
        lambda *_args, **_kwargs: transport,
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle().borrow(run_id="sync-run", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionDaemonProtocolError"
    assert transport.calls == ["/readyz", "/v1/version", "/v1/capabilities"]
    assert target.detach_calls == 1


def test_borrow_rejects_unsupported_daemon_api_before_lease(
    monkeypatch,
) -> None:
    target = _OwnedSandbox(config_hash="a" * 16)
    _ModalSandboxType.from_id_result = target

    class UnsupportedApiTransport(_CapabilityBorrowTransport):
        def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
            if path == "/v1/version":
                self.calls.append(path)
                request = httpx.Request(method, f"https://daemon.invalid{path}")
                return httpx.Response(200, json={"api_version": "v2"}, request=request)
            return super().request(method, path, **kwargs)

    transport = UnsupportedApiTransport(
        [
            "screenshot-binary-metadata-v1",
            "trajectory-leases-v1",
            "trajectory-operation-receipts-v1",
            "computer-step-envelope-v1",
        ]
    )
    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Sandbox=_ModalSandboxType))
    monkeypatch.setattr(
        "modal_computer_use.sandbox.HTTPTransport",
        lambda *_args, **_kwargs: transport,
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle().borrow(run_id="sync-run", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionDaemonProtocolError"
    assert transport.calls == ["/readyz", "/v1/version", "/v1/capabilities"]
    assert target.detach_calls == 1


def test_borrow_redacts_unverifiable_daemon_capabilities(monkeypatch) -> None:
    target = _OwnedSandbox(config_hash="a" * 16)
    _ModalSandboxType.from_id_result = target

    class UnverifiableCapabilitiesTransport(_CapabilityBorrowTransport):
        def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
            if path == "/v1/capabilities":
                self.calls.append(path)
                raise ConnectionError(
                    "https://credential-value.invalid/private capabilities unavailable"
                )
            return super().request(method, path, **kwargs)

    transport = UnverifiableCapabilitiesTransport([])
    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Sandbox=_ModalSandboxType))
    monkeypatch.setattr(
        "modal_computer_use.sandbox.HTTPTransport",
        lambda *_args, **_kwargs: transport,
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle().borrow(run_id="sync-run", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionDaemonProtocolError"
    assert "credential-value" not in str(raised.value)
    assert "private" not in str(raised.value)
    assert transport.calls == ["/readyz", "/v1/version", "/v1/capabilities"]
    assert target.detach_calls == 1


@pytest.mark.asyncio
async def test_async_borrow_rejects_malformed_daemon_capabilities_before_lease(
    monkeypatch,
) -> None:
    target = _AsyncTarget()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        return target

    class MalformedCapabilitiesTransport(_AsyncBorrowTransport):
        async def request(
            self, method: str, path: str, **kwargs: object
        ) -> httpx.Response:
            if path == "/v1/capabilities":
                self.calls.append(path)
                request = httpx.Request(method, f"https://daemon.invalid{path}")
                return httpx.Response(
                    200,
                    json={"primitives": {"token": "credential-value"}},
                    request=request,
                )
            return await super().request(method, path, **kwargs)

    transport = MalformedCapabilitiesTransport()
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.AsyncHTTPTransport",
        lambda *_args, **_kwargs: transport,
    )

    with pytest.raises(SessionCompatibilityError) as raised:
        async with _handle().borrow_async(
            run_id="async-run",
            function_region="us-west-2",
        ):
            raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionDaemonProtocolError"
    assert "credential-value" not in str(raised.value)
    assert transport.calls == ["/readyz", "/v1/version", "/v1/capabilities"]
    assert transport.closed
    assert target.calls[-1] == "detach.aio"


@pytest.mark.asyncio
async def test_async_borrow_uses_only_modal_aio_and_cleans_up(monkeypatch) -> None:
    target = _AsyncTarget()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        target.calls.append("from_id.aio")
        return target

    transport = _AsyncBorrowTransport()
    heartbeat_transport = _SyncBorrowTransport()
    transport_configuration: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def async_transport(*args: object, **kwargs: object) -> _AsyncBorrowTransport:
        transport_configuration.append(("async", args, kwargs))
        return transport

    def heartbeat(
        *args: object, **kwargs: object
    ) -> _SyncBorrowTransport:
        transport_configuration.append(("heartbeat", args, kwargs))
        return heartbeat_transport

    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.AsyncHTTPTransport",
        async_transport,
    )
    monkeypatch.setattr("modal_computer_use.sandbox.HTTPTransport", heartbeat)

    async with _handle().borrow_async(
        run_id="async-run", function_region="us-west-2"
    ) as borrowed:
        assert repr(borrowed) == "AsyncBorrowedComputer()"

    assert target.calls == [
        "from_id.aio",
        "get_tags.aio",
        "create_connect_token.aio",
        "detach.aio",
    ]
    assert transport.calls.count("/v1/leases/acquire") == 1
    assert transport.calls.count("/v1/leases/release") == 1
    assert transport.closed
    assert heartbeat_transport.calls == []
    assert heartbeat_transport.closed
    assert transport_configuration == [
        (
            "async",
            ("https://connect.invalid",),
            {
                "token": "credential-value",
                "timeout": 30.0,
                "http2": False,
            },
        ),
        (
            "heartbeat",
            ("https://connect.invalid",),
            {
                "token": "credential-value",
                "timeout": 30.0,
                "http2": False,
            },
        ),
    ]
    assert "terminate" not in target.calls


@pytest.mark.asyncio
async def test_async_user_exception_remains_primary_when_cleanup_fails(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Borrowed:
        def _invalidate(self) -> None:
            calls.append("invalidate")

    class Coordinator:
        async def aclose(self) -> None:
            calls.append("coordinator.aclose")
            raise SessionLeaseLostError("private endpoint and credential")

    class Client:
        async def aclose(self) -> None:
            calls.append("client.aclose")

    class Target:
        def __init__(self) -> None:
            self.detach = _AioCall(self._detach)

        async def _detach(self) -> None:
            calls.append("detach.aio")

    async def borrow(*_args: object, **_kwargs: object):
        return Borrowed(), Target(), Client(), Coordinator()

    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session_async",
        borrow,
    )

    with pytest.raises(RuntimeError, match="user workload failed") as raised:
        async with _handle().borrow_async(
            run_id="async-run", function_region="us-west-2"
        ):
            raise RuntimeError("user workload failed")

    assert raised.value.__notes__ == [
        "borrowed session cleanup also failed: lease_coordinator.aclose "
        "(SessionLeaseLostError)"
    ]
    assert "private" not in " ".join(raised.value.__notes__)
    assert calls == [
        "invalidate",
        "coordinator.aclose",
        "client.aclose",
        "detach.aio",
    ]


@pytest.mark.asyncio
async def test_async_target_mismatch_precedes_credential_issuance(monkeypatch) -> None:
    target = _AsyncTarget()
    target._tags["computer-use.session_id"] = "c" * 32

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        target.calls.append("from_id.aio")
        return target

    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )

    with pytest.raises(SessionTargetMismatchError):
        async with _handle().borrow_async(
            run_id="async-run", function_region="us-west-2"
        ):
            raise AssertionError("unreachable")

    assert target.calls == ["from_id.aio", "get_tags.aio", "detach.aio"]


@pytest.mark.asyncio
async def test_async_tag_lookup_failure_detaches_before_credential_issuance(monkeypatch) -> None:
    target = _AsyncTarget()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        target.calls.append("from_id.aio")
        return target

    async def fail_tags() -> dict[str, str]:
        target.calls.append("get_tags.aio")
        raise ConnectionError("tag lookup failed")

    target.get_tags = _AioCall(fail_tags)
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )

    with pytest.raises(SessionCompatibilityError) as raised:
        async with _handle().borrow_async(
            run_id="async-run", function_region="us-west-2"
        ):
            raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementUnverifiableError"
    assert "tag lookup failed" not in str(raised.value)
    assert target.calls == ["from_id.aio", "get_tags.aio", "detach.aio"]


@pytest.mark.asyncio
async def test_async_tag_lookup_cancellation_finishes_detach(monkeypatch) -> None:
    target = _AsyncTarget()
    lookup_started = asyncio.Event()
    detach_finished = asyncio.Event()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        return target

    async def wait_for_cancel() -> dict[str, str]:
        lookup_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def detach() -> None:
        target.calls.append("detach.aio")
        await asyncio.sleep(0)
        detach_finished.set()

    target.get_tags = _AioCall(wait_for_cancel)
    target.detach = _AioCall(detach)
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )

    async def enter() -> None:
        async with _handle().borrow_async(
            run_id="async-run", function_region="us-west-2"
        ):
            raise AssertionError("unreachable")

    task = asyncio.create_task(enter())
    await lookup_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert detach_finished.is_set()
    assert "create_connect_token.aio" not in target.calls


def test_sync_readiness_failure_closes_and_detaches_before_lease(monkeypatch) -> None:
    target = _OwnedSandbox(config_hash="a" * 16)
    target._tags.update(
        {
            "computer-use.app_name": "desktop-app",
            "computer-use.vnc_mode": "off",
        }
    )
    _ModalSandboxType.from_id_result = target
    transport = _SyncBorrowTransport()
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=_ModalSandboxType),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.HTTPTransport",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._wait_borrowed_ready_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    with (
        pytest.raises(TimeoutError),
        _handle().borrow(run_id="sync-run", function_region="us-west-2"),
    ):
        raise AssertionError("unreachable")

    assert transport.calls.count("/v1/leases/acquire") == 0
    assert transport.calls.count("/v1/leases/release") == 0
    assert transport.closed
    assert target.detach_calls == 1
    assert target.terminate_calls == 0


@pytest.mark.asyncio
async def test_async_readiness_failure_closes_and_detaches_before_lease(monkeypatch) -> None:
    target = _AsyncTarget()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        return target

    async def fail_ready(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError

    transport = _AsyncBorrowTransport()
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.AsyncHTTPTransport",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._wait_borrowed_ready_async",
        fail_ready,
    )

    with pytest.raises(TimeoutError):
        async with _handle().borrow_async(
            run_id="async-run", function_region="us-west-2"
        ):
            raise AssertionError("unreachable")

    assert transport.calls.count("/v1/leases/acquire") == 0
    assert transport.calls.count("/v1/leases/release") == 0
    assert transport.closed
    assert target.calls[-1] == "detach.aio"
    assert "terminate" not in target.calls


@pytest.mark.asyncio
async def test_async_acquire_failure_closes_both_transports_and_detaches(
    monkeypatch,
) -> None:
    target = _AsyncTarget()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        return target

    class AcquireFailureTransport(_AsyncBorrowTransport):
        async def request(
            self, method: str, path: str, **kwargs: object
        ) -> httpx.Response:
            if path == "/v1/leases/acquire":
                self.calls.append(path)
                raise ConnectionError("acquire failed")
            return await super().request(method, path, **kwargs)

    transport = AcquireFailureTransport()
    heartbeat_transport = _SyncBorrowTransport()
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.AsyncHTTPTransport",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.HTTPTransport",
        lambda *_args, **_kwargs: heartbeat_transport,
    )

    with pytest.raises(ConnectionError, match="acquire failed"):
        async with _handle().borrow_async(
            run_id="async-run", function_region="us-west-2"
        ):
            raise AssertionError("unreachable")

    assert transport.closed
    assert heartbeat_transport.closed
    assert target.calls[-1] == "detach.aio"


@pytest.mark.asyncio
async def test_async_preflight_cancellation_closes_and_detaches_without_a_lease(
    monkeypatch,
) -> None:
    target = _AsyncTarget()
    readiness_started = asyncio.Event()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        return target

    async def blocked_readiness(*_args: object, **_kwargs: object) -> None:
        readiness_started.set()
        await asyncio.Event().wait()

    transport = _AsyncBorrowTransport()
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.AsyncHTTPTransport",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._wait_borrowed_ready_async",
        blocked_readiness,
    )

    async def enter() -> None:
        async with _handle().borrow_async(
            run_id="async-run", function_region="us-west-2"
        ):
            raise AssertionError("unreachable")

    task = asyncio.create_task(enter())
    await readiness_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "/v1/leases/acquire" not in transport.calls
    assert "/v1/leases/release" not in transport.calls
    assert transport.closed
    assert target.calls[-1] == "detach.aio"
    assert "terminate" not in target.calls


@pytest.mark.asyncio
async def test_async_cancellation_after_acquisition_safely_closes_run_and_target(
    monkeypatch,
    tmp_path,
) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
        )
    )
    target = _AsyncTarget()
    acquired_headers: dict[str, str] = {}

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        return target

    async def ready_immediately(*_args: object, **_kwargs: object) -> None:
        return None

    async with app.router.lifespan_context(app):
        asgi = httpx.ASGITransport(app=app)
        borrowed_http = httpx.AsyncClient(
            transport=asgi,
            base_url="http://target",
            headers={"Authorization": "Bearer dev"},
        )
        verifier = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://target",
            headers={"Authorization": "Bearer dev"},
        )

        class DaemonTransport:
            timeout = 1.0

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.closed = False

            async def request(
                self, method: str, path: str, **kwargs: object
            ) -> httpx.Response:
                response = await borrowed_http.request(method, path, **kwargs)
                if path == "/v1/leases/acquire" and response.status_code == 200:
                    body = response.json()
                    acquired_headers.update(
                        {
                            LEASE_ID_HEADER: body["lease_id"],
                            LEASE_EPOCH_HEADER: body["daemon_epoch"],
                            LEASE_FENCE_HEADER: str(body["fence"]),
                            LEASE_TOKEN_HEADER: response.headers[LEASE_TOKEN_HEADER],
                        }
                    )
                return response

            async def aclose(self) -> None:
                self.closed = True
                await borrowed_http.aclose()

        daemon_transport = DaemonTransport()
        heartbeat_transport = _SyncBorrowTransport()
        monkeypatch.setitem(
            sys.modules,
            "modal",
            SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
        )
        monkeypatch.setattr(
            "modal_computer_use.sandbox.AsyncHTTPTransport",
            lambda *_args, **_kwargs: daemon_transport,
        )
        monkeypatch.setattr(
            "modal_computer_use.sandbox.HTTPTransport",
            lambda *_args, **_kwargs: heartbeat_transport,
        )
        monkeypatch.setattr(
            "modal_computer_use.sandbox._wait_borrowed_ready_async",
            ready_immediately,
        )

        entered = asyncio.Event()

        async def borrow_until_cancelled() -> None:
            async with _handle().borrow_async(
                run_id="cancelled-run",
                function_region="us-west-2",
            ):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(borrow_until_cancelled())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        sealed = await verifier.post(
            "/v1/leases/acquire",
            json={"run_id": "cancelled-run"},
        )
        fresh = await verifier.post(
            "/v1/leases/acquire",
            json={"run_id": "fresh-run"},
        )
        fresh_body = fresh.json()
        stale = await verifier.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers={**acquired_headers, OPERATION_SEQUENCE_HEADER: "0"},
        )
        fresh_headers = {
            LEASE_ID_HEADER: fresh_body["lease_id"],
            LEASE_EPOCH_HEADER: fresh_body["daemon_epoch"],
            LEASE_FENCE_HEADER: str(fresh_body["fence"]),
            LEASE_TOKEN_HEADER: fresh.headers[LEASE_TOKEN_HEADER],
            OPERATION_SEQUENCE_HEADER: "0",
        }
        fresh_mutation = await verifier.post(
            "/v1/mouse/move",
            json={"x": 3, "y": 4},
            headers=fresh_headers,
        )
        await verifier.aclose()

    assert daemon_transport.closed
    assert heartbeat_transport.closed
    assert target.calls[-1] == "detach.aio"
    assert "terminate" not in target.calls
    assert sealed.json()["code"] == "run_sealed"
    assert fresh.status_code == 200
    assert stale.json()["code"] == "lease_stale"
    assert fresh_mutation.status_code == 200


@pytest.mark.asyncio
async def test_async_borrowed_facade_cannot_resurrect_connections_after_exit(monkeypatch) -> None:
    target = _AsyncTarget()

    async def from_id(_sandbox_id: str) -> _AsyncTarget:
        return target

    transport = _AsyncBorrowTransport()
    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(Sandbox=SimpleNamespace(from_id=_AioCall(from_id))),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox.AsyncHTTPTransport",
        lambda *_args, **_kwargs: transport,
    )

    async with _handle().borrow_async(
        run_id="async-run", function_region="us-west-2"
    ) as retained:
        pass
    calls_after_exit = list(transport.calls)

    with pytest.raises(SessionLeaseLostError):
        await retained.mouse.move(1, 2)
    with pytest.raises(SessionLeaseLostError):
        retained.hot_session()
    with pytest.raises(SessionLeaseLostError):
        retained.observation_stream()
    assert transport.calls == calls_after_exit


def test_public_session_errors_redact_caller_supplied_identity_and_content() -> None:
    import modal_computer_use

    error_names = (
        "SessionBorrowError",
        "SessionCompatibilityError",
        "SessionDaemonProtocolError",
        "SessionEnvironmentMismatchError",
        "SessionPlacementMalformedError",
        "SessionPlacementMissingError",
        "SessionPlacementMismatchError",
        "SessionPlacementUnverifiableError",
        "SessionTargetMismatchError",
        "SessionBusyError",
        "SessionLeaseLostError",
        "RunSequenceConflictError",
        "ActionOutcomeUnknownError",
        "OperationNotAppliedError",
        "SessionRecoveryRequiredError",
    )
    for error_name in error_names:
        error_type = getattr(modal_computer_use, error_name)
        error = error_type("sb-secret", content="private task")
        rendered = f"{error!s} {error!r}"
        assert "sb-secret" not in rendered
        assert "private task" not in rendered

    unavailable = modal_computer_use.OperationResultUnavailableError(
        sequence=3,
        operation_kind="actions.run",
    )
    rendered = f"{unavailable!s} {unavailable!r}"
    assert "sb-secret" not in rendered
    assert "private task" not in rendered


def test_deployed_handoff_smoke_source_is_bounded_and_returns_only_safe_aggregates() -> None:
    path = ROOT / "tests" / "modal_function_session_handoff_smoke_app.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    body = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "run_handoff_smoke_body"
    )
    returned = next(
        node.value
        for node in ast.walk(body)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    )
    return_keys = {
        key.value
        for key in returned.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }

    assert return_keys == {
        "borrow_succeeded",
        "screenshot_succeeded",
        "action_succeeded",
        "width",
        "height",
        "function_cloud",
        "function_region",
    }
    assert '.pip_install_from_pyproject(' in source
    assert 'optional_dependencies=["modal"]' in source
    assert '.add_local_python_source("modal_computer_use", copy=True)' in source
    assert "retries=0" in source
    assert "min_containers=0" in source
    assert "max_containers=1" in source
    assert "FUNCTION_TIMEOUT_SECONDS = 300" in source
    assert '"MODAL_COMPUTER_USE_HANDOFF_REGION", "us-west"' in source
    assert "one narrow or granted granular Modal region selector" in source
    assert "timeout=FUNCTION_TIMEOUT_SECONDS" in source
    assert "restrict_modal_access=False" in source
    assert source.count("handle.borrow_async(") == 1
    assert source.count("computer.step(") == 1
    assert "computer.screenshots.full(" not in source
    assert "computer.actions.run(" not in source
    assert 'storage="inline"' in source
    assert '{"type": "wait", "duration_ms": 50}' in source


def test_deployed_handoff_smoke_owner_round_trips_and_invokes_once() -> None:
    source = (ROOT / "tests" / "test_modal_integration.py").read_text(
        encoding="utf-8"
    )
    test_source = source.split(
        "def test_modal_deployed_function_session_handoff_smoke() -> None:",
        maxsplit=1,
    )[1].split("\n\n@pytest.mark.modal", maxsplit=1)[0]

    assert 'os.getenv("MODAL_COMPUTER_USE_RUN_HANDOFF_SMOKE")' in source
    assert "ComputerSessionHandle.model_validate_json(" in test_source
    assert "computer.session_handle().model_dump_json()" in test_source
    assert test_source.count("deployed.remote(") == 1
    assert 'expose_vnc="off"' in test_source
    assert 'ingress="attested-tunnel"' in test_source
    assert "timeout_seconds=600" in test_source
    assert "idle_timeout_seconds=180" in test_source
    assert 'lease_status.get("state") == "released"' in test_source
    assert "assert computer.poll() is None" in test_source
    assert "computer.terminate(wait=True)" in test_source
    assert "target_placement[\"cloud\"]" in test_source
    assert "target_placement[\"region\"]" in test_source
    assert "target_placement == result" not in test_source


def test_protected_workflow_scopes_and_cleans_up_handoff_smoke() -> None:
    source = (ROOT / ".github" / "workflows" / "modal-handoff-smoke.yml").read_text(
        encoding="utf-8"
    )
    release = (
        ROOT / ".github" / "workflows" / "release-validation.yml"
    ).read_text(encoding="utf-8")

    assert source.count("MODAL_COMPUTER_USE_RUN_HANDOFF_SMOKE") == 1
    assert "workflow_dispatch:" in source
    assert "environment: modal-smoke" in source
    assert "timeout-minutes:" in source
    assert "concurrency:" in source
    assert "uv run modal deploy" in source
    assert "if: always()" in source
    assert "uv run modal app stop" in source
    assert "MODAL_COMPUTER_USE_HANDOFF_DEPLOYED=1" in source
    assert "--yes" in source
    assert '"computer-use.owner": owner' in source
    assert "sandbox.terminate(wait=True)" in source
    assert ">/dev/null 2>&1" in source
    assert "MODAL_COMPUTER_USE_RUN_HANDOFF_SMOKE" not in release
