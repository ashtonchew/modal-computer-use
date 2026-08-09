from __future__ import annotations

import pytest

from modal_computer_use import ComputerSandbox, ComputerSessionHandle
from modal_computer_use.client import DaemonClient
from modal_computer_use.errors import SessionLeaseLostError


class _RecordingClient(DaemonClient):
    def __init__(self, events: list[str]) -> None:
        super().__init__("https://daemon.invalid")
        self._events = events

    def close(self) -> None:
        self._events.append("client.close")


class _RecordingSandbox:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def terminate(self, *, wait: bool) -> None:
        assert wait is True
        self._events.append("sandbox.terminate")

    def detach(self) -> None:
        self._events.append("sandbox.detach")


class _AsyncOperation:
    def __init__(self, operation: object) -> None:
        self.aio = operation


def _session_handle() -> ComputerSessionHandle:
    return ComputerSessionHandle(
        sandbox_id="sb-owned",
        session_id="a" * 32,
        app_name="desktop-app",
        modal_environment="test",
        requested_modal_region="us-west-2",
        ingress="connect",
        daemon_http_version="1.1",
        vnc_mode="off",
        config_hash="b" * 16,
    )


def test_owned_context_closes_children_before_terminating_target() -> None:
    events: list[str] = []
    computer = ComputerSandbox(
        _RecordingClient(events),
        sandbox=_RecordingSandbox(events),
    )

    with computer:
        pass

    assert events == [
        "client.close",
        "sandbox.terminate",
        "sandbox.detach",
    ]


def test_attached_context_closes_children_before_detaching_without_termination() -> None:
    events: list[str] = []
    computer = ComputerSandbox(
        _RecordingClient(events),
        sandbox=_RecordingSandbox(events),
        _lifecycle_mode="attached",
    )

    with computer:
        pass

    assert events == ["client.close", "sandbox.detach"]


def test_borrowed_context_aggregates_secret_free_cleanup_failures_on_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Borrowed:
        def _invalidate(self) -> None:
            events.append("borrowed.invalidate")

    class Coordinator:
        def close(self) -> None:
            events.append("lease_coordinator.close")
            raise SessionLeaseLostError("lease-token-value")

    class Client:
        def close(self) -> None:
            events.append("client.close")
            raise ConnectionError("https://private.invalid")

    class Target:
        def detach(self) -> None:
            events.append("sandbox.detach")
            raise RuntimeError("typed-secret-value")

    monkeypatch.setenv("MODAL_IS_REMOTE", "1")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "test")
    monkeypatch.setenv("MODAL_REGION", "us-west-2")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda *_args, **_kwargs: (Borrowed(), Target(), Client(), Coordinator()),
    )
    trajectory_error = ValueError("trajectory failed")

    with (
        pytest.raises(ValueError, match="trajectory failed") as raised,
        _session_handle().borrow(
            run_id="trajectory-run",
            function_region="us-west-2",
        ),
    ):
        raise trajectory_error

    assert raised.value is trajectory_error
    assert events == [
        "borrowed.invalidate",
        "lease_coordinator.close",
        "client.close",
        "sandbox.detach",
    ]
    assert raised.value.__notes__ == [
        "borrowed session cleanup also failed: lease_coordinator.close "
        "(SessionLeaseLostError)",
        "borrowed session cleanup also failed: client.close (ConnectionError)",
        "borrowed session cleanup also failed: sandbox.detach (RuntimeError)",
    ]
    notes = " ".join(raised.value.__notes__)
    assert "lease-token-value" not in notes
    assert "private.invalid" not in notes
    assert "typed-secret-value" not in notes


def test_borrowed_context_reports_all_cleanup_failures_without_secret_causes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Borrowed:
        def _invalidate(self) -> None:
            return None

    class Coordinator:
        def close(self) -> None:
            raise SessionLeaseLostError("lease-token-value")

    class Client:
        def close(self) -> None:
            raise ConnectionError("https://private.invalid")

    class Target:
        def detach(self) -> None:
            raise RuntimeError("typed-secret-value")

    monkeypatch.setenv("MODAL_IS_REMOTE", "1")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "test")
    monkeypatch.setenv("MODAL_REGION", "us-west-2")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda *_args, **_kwargs: (Borrowed(), Target(), Client(), Coordinator()),
    )

    with (
        pytest.raises(SessionLeaseLostError) as raised,
        _session_handle().borrow(
            run_id="trajectory-run",
            function_region="us-west-2",
        ),
    ):
        pass

    assert raised.value.__cause__ is None
    assert raised.value.__notes__ == [
        "borrowed session cleanup failed: lease_coordinator.close "
        "(SessionLeaseLostError)",
        "borrowed session cleanup failed: client.close (ConnectionError)",
        "borrowed session cleanup failed: sandbox.detach (RuntimeError)",
    ]
    rendered = f"{raised.value!s} {raised.value!r} {raised.value.__cause__!r}"
    assert "lease-token-value" not in rendered
    assert "private.invalid" not in rendered
    assert "typed-secret-value" not in rendered


@pytest.mark.asyncio
async def test_async_borrow_aggregates_secret_free_cleanup_failures_on_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Borrowed:
        def _invalidate(self) -> None:
            events.append("borrowed.invalidate")

    class Coordinator:
        async def aclose(self) -> None:
            events.append("lease_coordinator.aclose")
            raise SessionLeaseLostError("lease-token-value")

    class Client:
        async def aclose(self) -> None:
            events.append("client.aclose")
            raise ConnectionError("https://private.invalid")

    class Target:
        def __init__(self) -> None:
            self.detach = _AsyncOperation(self._detach)

        async def _detach(self) -> None:
            events.append("sandbox.detach.aio")
            raise RuntimeError("clipboard-secret-value")

    async def borrow(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        return Borrowed(), Target(), Client(), Coordinator()

    monkeypatch.setenv("MODAL_IS_REMOTE", "1")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "test")
    monkeypatch.setenv("MODAL_REGION", "us-west-2")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session_async",
        borrow,
    )
    trajectory_error = ValueError("trajectory failed")

    with pytest.raises(ValueError, match="trajectory failed") as raised:
        async with _session_handle().borrow_async(
            run_id="trajectory-run",
            function_region="us-west-2",
        ):
            raise trajectory_error

    assert raised.value is trajectory_error
    assert events == [
        "borrowed.invalidate",
        "lease_coordinator.aclose",
        "client.aclose",
        "sandbox.detach.aio",
    ]
    assert raised.value.__notes__ == [
        "borrowed session cleanup also failed: lease_coordinator.aclose "
        "(SessionLeaseLostError)",
        "borrowed session cleanup also failed: client.aclose (ConnectionError)",
        "borrowed session cleanup also failed: sandbox.detach.aio (RuntimeError)",
    ]
    notes = " ".join(raised.value.__notes__)
    assert "lease-token-value" not in notes
    assert "private.invalid" not in notes
    assert "clipboard-secret-value" not in notes


@pytest.mark.asyncio
async def test_async_borrow_reports_all_cleanup_failures_without_secret_causes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Borrowed:
        def _invalidate(self) -> None:
            return None

    class Coordinator:
        async def aclose(self) -> None:
            raise SessionLeaseLostError("lease-token-value")

    class Client:
        async def aclose(self) -> None:
            raise ConnectionError("https://private.invalid")

    class Target:
        def __init__(self) -> None:
            self.detach = _AsyncOperation(self._detach)

        async def _detach(self) -> None:
            raise RuntimeError("clipboard-secret-value")

    async def borrow(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        return Borrowed(), Target(), Client(), Coordinator()

    monkeypatch.setenv("MODAL_IS_REMOTE", "1")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "test")
    monkeypatch.setenv("MODAL_REGION", "us-west-2")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session_async",
        borrow,
    )

    with pytest.raises(SessionLeaseLostError) as raised:
        async with _session_handle().borrow_async(
            run_id="trajectory-run",
            function_region="us-west-2",
        ):
            pass

    assert raised.value.__cause__ is None
    assert raised.value.__notes__ == [
        "borrowed session cleanup failed: lease_coordinator.aclose "
        "(SessionLeaseLostError)",
        "borrowed session cleanup failed: client.aclose (ConnectionError)",
        "borrowed session cleanup failed: sandbox.detach.aio (RuntimeError)",
    ]
    rendered = f"{raised.value!s} {raised.value!r} {raised.value.__cause__!r}"
    assert "lease-token-value" not in rendered
    assert "private.invalid" not in rendered
    assert "clipboard-secret-value" not in rendered
