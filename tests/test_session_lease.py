from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx
import pytest

from modal_computer_use.errors import (
    ActionOutcomeUnknownError,
    DaemonHTTPError,
    OperationNotAppliedError,
    OperationResultUnavailableError,
    SessionLeaseLostError,
)
from modal_computer_use.session_lease import (
    AsyncSessionLeaseCoordinator,
    SessionLeaseCoordinator,
)


def _response(
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        headers=headers,
        request=httpx.Request("POST", "https://daemon.invalid"),
    )


def _grant() -> httpx.Response:
    return _response(
        {
            "lease_id": "lease-test",
            "daemon_epoch": "epoch-test",
            "fence": 4,
            "ttl_seconds": 30.0,
            "heartbeat_interval_seconds": 60.0,
        },
        headers={"x-computer-use-lease-token": "protected-token"},
    )


class _SyncTransport:
    def __init__(self) -> None:
        self.timeout = 0.05
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status: dict[int, dict[str, Any]] = {}
        self.resolved: dict[int, dict[str, Any]] = {}

    def request(self, _method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((path, kwargs))
        if path == "/v1/leases/acquire":
            return _grant()
        if path == "/v1/receipts/status":
            sequence = kwargs["json"]["sequence"]
            return _response(self.status[sequence])
        if path == "/v1/receipts/resolve":
            sequence = kwargs["json"]["sequence"]
            return _response(self.resolved[sequence])
        return _response({"ok": True})


class _AsyncTransport(_SyncTransport):
    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return super().request(method, path, **kwargs)


def test_sync_sequence_is_gap_free_and_shared_by_all_executors() -> None:
    transport = _SyncTransport()
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()
    emitted: list[int] = []

    def successful(headers: Any) -> str:
        emitted.append(int(headers["x-computer-use-operation-sequence"]))
        return "ok"

    assert coordinator.execute(successful) == "ok"
    assert coordinator.execute(successful) == "ok"
    coordinator.close()

    assert emitted == [0, 1]
    assert [path for path, _ in transport.calls].count("/v1/leases/acquire") == 1
    assert [path for path, _ in transport.calls].count("/v1/leases/release") == 1


def test_sync_closed_coordinator_rejects_retained_mutation_without_dispatch() -> None:
    transport = _SyncTransport()
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()
    coordinator.close()
    calls_after_close = list(transport.calls)

    with pytest.raises(SessionLeaseLostError):
        coordinator.execute(lambda _headers: None)

    assert transport.calls == calls_after_close


def test_received_missing_error_does_not_consume_sequence() -> None:
    transport = _SyncTransport()
    transport.status[0] = {"state": "MISSING", "sequence": 0}
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()

    def rejected(_headers: Any) -> None:
        raise DaemonHTTPError("rejected", code="action_validation_failed")

    with pytest.raises(DaemonHTTPError, match="rejected"):
        coordinator.execute(rejected)
    assert coordinator.execute(
        lambda headers: int(headers["x-computer-use-operation-sequence"])
    ) == 0
    coordinator.close()


def test_completed_delivered_error_advances_but_only_result_unavailable_poisons() -> None:
    transport = _SyncTransport()
    transport.status[0] = {"state": "COMPLETED", "sequence": 0}
    transport.status[2] = {"state": "COMPLETED", "sequence": 2}
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()

    with pytest.raises(DaemonHTTPError):
        coordinator.execute(
            lambda _headers: (_ for _ in ()).throw(
                DaemonHTTPError("application error", code="application_error")
            )
        )
    assert coordinator.execute(
        lambda headers: int(headers["x-computer-use-operation-sequence"])
    ) == 1

    with pytest.raises(OperationResultUnavailableError):
        coordinator.execute(
            lambda _headers: (_ for _ in ()).throw(
                DaemonHTTPError(
                    "result gone", code="operation_result_unavailable"
                )
            )
        )
    with pytest.raises(ActionOutcomeUnknownError):
        coordinator.execute(lambda _headers: None)
    coordinator.close()


def test_received_stale_lease_maps_without_receipt_query() -> None:
    transport = _SyncTransport()
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()

    with pytest.raises(SessionLeaseLostError):
        coordinator.execute(
            lambda _headers: (_ for _ in ()).throw(
                DaemonHTTPError("stale", code="lease_stale")
            )
        )
    assert "/v1/receipts/status" not in [path for path, _ in transport.calls]
    coordinator.close()


def test_transport_loss_missing_is_not_replayed_and_seals_current_borrow() -> None:
    transport = _SyncTransport()
    transport.resolved[0] = {
        "state": "MISSING",
        "sequence": 0,
        "proven_not_applied": True,
        "run_sealed": True,
    }
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()
    dispatches = 0

    def lost(_headers: Any) -> None:
        nonlocal dispatches
        dispatches += 1
        raise httpx.ReadTimeout("lost")

    with pytest.raises(OperationNotAppliedError):
        coordinator.execute(lost)
    with pytest.raises(ActionOutcomeUnknownError):
        coordinator.execute(lost)
    coordinator.close()
    assert dispatches == 1


@pytest.mark.asyncio
async def test_async_cancellation_resolves_before_propagation_without_replay() -> None:
    transport = _AsyncTransport()
    transport.resolved[0] = {
        "state": "MISSING",
        "sequence": 0,
        "proven_not_applied": True,
        "run_sealed": True,
    }
    coordinator = AsyncSessionLeaseCoordinator(transport, run_id="run-a")
    await coordinator.acquire()
    dispatched = asyncio.Event()

    async def blocked(_headers: Any) -> None:
        dispatched.set()
        await asyncio.Future()

    task = asyncio.create_task(coordinator.execute(blocked))
    await dispatched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "/v1/receipts/resolve" in [path for path, _ in transport.calls]
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_async_closed_coordinator_rejects_retained_mutation_without_dispatch() -> None:
    transport = _AsyncTransport()
    coordinator = AsyncSessionLeaseCoordinator(transport, run_id="run-a")
    await coordinator.acquire()
    await coordinator.aclose()
    calls_after_close = list(transport.calls)

    async def operation(_headers: Any) -> None:
        raise AssertionError("must not dispatch")

    with pytest.raises(SessionLeaseLostError):
        await coordinator.execute(operation)

    assert transport.calls == calls_after_close


def test_sync_close_is_bounded_when_heartbeat_transport_is_stuck() -> None:
    class StuckHeartbeatTransport(_SyncTransport):
        def __init__(self) -> None:
            super().__init__()
            self.timeout = 0.02
            self.started = threading.Event()
            self.unblock = threading.Event()

        def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
            if path == "/v1/leases/acquire":
                response = _grant()
                response._content = response._content.replace(b"60.0", b"0.001")
                return response
            if path == "/v1/leases/heartbeat":
                self.started.set()
                self.unblock.wait()
            return super().request(method, path, **kwargs)

    transport = StuckHeartbeatTransport()
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()
    assert transport.started.wait(timeout=1.0)
    started = time.monotonic()
    coordinator.close()
    elapsed = time.monotonic() - started
    transport.unblock.set()
    assert elapsed < 0.2
