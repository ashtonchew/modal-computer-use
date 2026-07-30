from __future__ import annotations

import json
import pickle
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from modal_computer_use import (
    ComputerConfig,
    ComputerSandbox,
    ComputerSessionHandle,
    SandboxRef,
)
from modal_computer_use.errors import ConfigConflictError, SandboxUnavailableError
from modal_computer_use.state import compute_config_hash


class _ConnectToken:
    url = "https://connect.invalid"
    token = "credential-" + "value"


class _OwnedSandbox:
    def __init__(self, *, config_hash: str | None = None) -> None:
        self.object_id = "sb-owned"
        self._tags = {
            "computer-use": "true",
            "computer-use.config_hash": config_hash or "ignored",
        }
        self.detach_calls = 0
        self.terminate_calls = 0
        self.credential_calls = 0

    def create_connect_token(self, **_kwargs: object) -> _ConnectToken:
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
        return object()


class _Client:
    def __init__(self) -> None:
        self.close_calls = 0
        self.base_url = "https://private.invalid"
        self.transport = SimpleNamespace(token="credential-value")

    def close(self) -> None:
        self.close_calls += 1


def _handle(**updates: object) -> ComputerSessionHandle:
    values: dict[str, object] = {
        "sandbox_id": "sb-owned",
        "app_name": "desktop-app",
        "requested_modal_region": "us-west",
        "ingress": "connect",
        "daemon_http_version": "1.1",
        "config_hash": "a" * 16,
    }
    values.update(updates)
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


def test_create_produces_safe_versioned_session_handle(monkeypatch) -> None:
    runtime = SimpleNamespace(App=_ModalApp, Sandbox=_ModalSandboxType, Probe=None)
    monkeypatch.setitem(sys.modules, "modal", runtime)
    config = ComputerConfig(
        run_id="run-123",
        ingress="connect",
        runtime={"modal_region": "us-west"},
        network={"daemon_http_version": "2"},
    )

    computer = ComputerSandbox.create(
        config=config,
        image=object(),
        app_name="desktop-app",
        wait=False,
    )
    handle = computer.session_handle()

    assert handle.schema_version == 1
    assert handle.sandbox_id == "sb-owned"
    assert handle.app_name == "desktop-app"
    assert handle.requested_modal_region == "us-west"
    assert handle.ingress == "connect"
    assert handle.daemon_http_version == "2"
    assert handle.config_hash == computer.metadata().config_hash  # type: ignore[union-attr]


def test_session_handle_rejects_local_unsupported_and_insufficient_targets(monkeypatch) -> None:
    with pytest.raises(SandboxUnavailableError, match="SDK-owned Modal desktop"):
        ComputerSandbox.local().session_handle()

    runtime = SimpleNamespace(App=_ModalApp, Sandbox=_ModalSandboxType, Probe=None)
    monkeypatch.setitem(sys.modules, "modal", runtime)
    no_region = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-no-region", ingress="connect"),
        image=object(),
        wait=False,
    )
    with pytest.raises(SandboxUnavailableError, match="explicit requested Modal region"):
        no_region.session_handle()

    raw_tunnel = ComputerSandbox.create(
        config=ComputerConfig(
            run_id="run-tunnel",
            ingress="tunnel",
            runtime={"modal_region": "us-west"},
        ),
        image=object(),
        wait=False,
    )
    with pytest.raises(SandboxUnavailableError, match="credential-refreshable ingress"):
        raw_tunnel.session_handle()

    unverifiable = ComputerSandbox.create(
        config=ComputerConfig(
            run_id="run-warm",
            ingress="connect",
            runtime={"modal_region": "us-west"},
        ),
        image=object(),
        tag_profile="warm_pool",
        wait=False,
    )
    with pytest.raises(SandboxUnavailableError, match="verifiable live config identity"):
        unverifiable.session_handle()

    direct_attach = ComputerSandbox.attach(base_url="https://fixture.invalid")
    with pytest.raises(SandboxUnavailableError, match="SDK-owned Modal desktop"):
        direct_attach.session_handle()


def test_handle_is_strict_frozen_and_json_pickle_serializable() -> None:
    handle = _handle()
    assert ComputerSessionHandle.model_validate_json(handle.model_dump_json()) == handle
    assert pickle.loads(pickle.dumps(handle)) == handle  # noqa: S301 - trusted local object

    with pytest.raises(ValidationError):
        ComputerSessionHandle.model_validate({**handle.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        _handle(schema_version="1")
    with pytest.raises(ValidationError):
        _handle(requested_modal_region=" ")
    with pytest.raises(ValidationError):
        _handle(ingress="tunnel")
    with pytest.raises(ValidationError):
        _handle(daemon_http_version=2)
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
    for forbidden in ("credential-value", "connect.invalid", "private.invalid", "novnc"):
        assert forbidden not in serialized
        assert forbidden not in rendered


def test_borrow_is_lazy_and_rejects_region_before_attach(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def attach(**kwargs: object) -> ComputerSandbox:
        calls.append(kwargs)
        return _borrowed_computer()[0]

    monkeypatch.setattr(ComputerSandbox, "attach", attach)
    context = _handle().borrow(function_region="us-west")
    assert calls == []

    with context as computer:
        assert computer.metadata().sandbox_id == "sb-owned"  # type: ignore[union-attr]
        assert len(calls) == 1
        assert calls[0]["wait"] is True

    calls.clear()
    with (
        pytest.raises(ConfigConflictError, match="Function region"),
        _handle().borrow(function_region="us-east"),
    ):
        raise AssertionError("unreachable")
    assert calls == []


def test_borrow_context_rejects_second_entry_without_replacing_live_client(monkeypatch) -> None:
    computer, target, client = _borrowed_computer()
    calls = 0

    def attach(**_kwargs: object) -> ComputerSandbox:
        nonlocal calls
        calls += 1
        return computer

    monkeypatch.setattr(ComputerSandbox, "attach", attach)
    context = _handle().borrow(function_region="us-west")
    assert context.__enter__() is computer
    with pytest.raises(RuntimeError, match="only be entered once"):
        context.__enter__()
    context.__exit__(None, None, None)

    assert calls == 1
    assert target.detach_calls == 1
    assert client.close_calls == 1
    assert target.terminate_calls == 0


def test_attach_or_create_reuse_aligns_ingress_and_http_policy(monkeypatch) -> None:
    config = ComputerConfig(
        run_id="run-reuse",
        ingress="connect",
        runtime={"modal_region": "us-west"},
        network={"daemon_http_version": "2"},
    )
    config_hash = compute_config_hash(config)
    computer, _target, _client = _borrowed_computer(config_hash=config_hash)
    calls: list[dict[str, object]] = []

    def attach(**kwargs: object) -> ComputerSandbox:
        calls.append(kwargs)
        return computer

    monkeypatch.setattr(ComputerSandbox, "attach", attach)
    reused = ComputerSandbox.attach_or_create(
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
    computer, target, client = _borrowed_computer(config_hash="different")
    monkeypatch.setattr(ComputerSandbox, "attach", lambda **_kwargs: computer)

    with (
        pytest.raises(ConfigConflictError, match="live sandbox config_hash"),
        _handle().borrow(function_region="us-west"),
    ):
        raise AssertionError("unreachable")

    assert target.detach_calls == 1
    assert client.close_calls == 1
    assert target.terminate_calls == 0


def test_borrow_rejects_target_that_lost_sdk_ownership_marker(monkeypatch) -> None:
    computer, target, client = _borrowed_computer()
    target._tags.pop("computer-use")
    computer._metadata = computer.metadata().model_copy(update={"tags": target.get_tags()})  # type: ignore[union-attr]
    monkeypatch.setattr(ComputerSandbox, "attach", lambda **_kwargs: computer)

    with (
        pytest.raises(ConfigConflictError, match="live sandbox config_hash"),
        _handle().borrow(function_region="us-west"),
    ):
        raise AssertionError("unreachable")

    assert target.detach_calls == 1
    assert client.close_calls == 1
    assert target.terminate_calls == 0


@pytest.mark.parametrize("raises", [False, True])
def test_borrow_deterministically_detaches_without_terminating(
    monkeypatch,
    raises: bool,
) -> None:
    computer, target, client = _borrowed_computer()
    monkeypatch.setattr(ComputerSandbox, "attach", lambda **_kwargs: computer)

    if raises:
        with (
            pytest.raises(RuntimeError, match="user workload failed"),
            _handle().borrow(function_region="us-west"),
        ):
            raise RuntimeError("user workload failed")
    else:
        with _handle().borrow(function_region="us-west") as borrowed:
            assert borrowed is computer

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

    def fail(**_kwargs: object) -> ComputerSandbox:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(ComputerSandbox, "attach", fail)
    with pytest.raises(type(failure)), _handle().borrow(function_region="us-west"):
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
