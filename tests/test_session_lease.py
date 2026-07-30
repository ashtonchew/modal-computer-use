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


def _grant(*, heartbeat_interval_seconds: float = 60.0) -> httpx.Response:
    return _response(
        {
            "lease_id": "lease-test",
            "daemon_epoch": "epoch-test",
            "fence": 4,
            "ttl_seconds": 30.0,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
        },
        headers={"x-computer-use-lease-token": "protected-token"},
    )


class _SyncTransport:
    def __init__(self) -> None:
        self.timeout = 0.05
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status: dict[int, dict[str, Any]] = {}
        self.resolved: dict[int, dict[str, Any]] = {}
        self.closed = False

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

    def close(self) -> None:
        self.closed = True


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
    heartbeat_transport = _SyncTransport()
    transport.resolved[0] = {
        "state": "MISSING",
        "sequence": 0,
        "proven_not_applied": True,
        "run_sealed": True,
    }
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.1,
    )
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
    heartbeat_transport = _SyncTransport()
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.1,
    )
    await coordinator.acquire()
    await coordinator.aclose()
    calls_after_close = list(transport.calls)

    async def operation(_headers: Any) -> None:
        raise AssertionError("must not dispatch")

    with pytest.raises(SessionLeaseLostError):
        await coordinator.execute(operation)

    assert transport.calls == calls_after_close


class _ShortGrantAsyncTransport(_AsyncTransport):
    def __init__(self, *, heartbeat_interval_seconds: float = 0.005) -> None:
        super().__init__()
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.release_observation: tuple[bool, object, bool | None] | None = None
        self.worker: object | None = None
        self.resource: object | None = None

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if path == "/v1/leases/acquire":
            self.calls.append((path, kwargs))
            return _grant(
                heartbeat_interval_seconds=self.heartbeat_interval_seconds
            )
        if path == "/v1/leases/release" and self.worker is not None:
            thread = self.worker._thread
            credentials = self.worker._credentials_value
            resource_closed = (
                self.resource.closed if self.resource is not None else None
            )
            self.release_observation = (
                thread.is_alive(),
                credentials,
                resource_closed,
            )
        return await super().request(method, path, **kwargs)


class _HeartbeatTransport:
    def __init__(
        self,
        *,
        timeout: float = 0.01,
        fail: bool = False,
        block: bool = False,
    ) -> None:
        self.timeout = timeout
        self.fail = fail
        self.block = block
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.started = threading.Event()
        self.unblock = threading.Event()
        self.closed = False

    def request(self, _method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((path, kwargs))
        self.started.set()
        if self.block:
            self.unblock.wait()
        if self.fail:
            raise httpx.ConnectError("secret https://daemon.invalid?token=protected")
        return _response({"ok": True})

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_async_heartbeat_renews_while_event_loop_is_blocked() -> None:
    transport = _ShortGrantAsyncTransport()
    heartbeat_transport = _HeartbeatTransport()
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.05,
    )
    await coordinator.acquire()

    time.sleep(0.04)

    assert len(heartbeat_transport.calls) >= 2
    assert {path for path, _ in heartbeat_transport.calls} == {
        "/v1/leases/heartbeat"
    }
    assert "/v1/leases/heartbeat" not in [path for path, _ in transport.calls]
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_async_heartbeat_failure_blocks_next_mutation_and_redacts_error() -> None:
    transport = _ShortGrantAsyncTransport()
    heartbeat_transport = _HeartbeatTransport(fail=True)
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.05,
    )
    await coordinator.acquire()
    assert await asyncio.to_thread(heartbeat_transport.started.wait, 1.0)
    dispatched = False

    async def operation(_headers: Any) -> None:
        nonlocal dispatched
        dispatched = True

    with pytest.raises(SessionLeaseLostError) as raised:
        await coordinator.execute(operation)

    rendered = f"{raised.value!s} {raised.value!r} {raised.value.__cause__!r}"
    assert not dispatched
    assert "daemon.invalid" not in rendered
    assert "protected" not in rendered
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_async_heartbeat_request_timeout_must_be_below_join_budget() -> None:
    transport = _ShortGrantAsyncTransport()
    heartbeat_transport = _HeartbeatTransport(timeout=0.05)
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.05,
    )

    with pytest.raises(ValueError, match="request timeout"):
        await coordinator.acquire()
    await coordinator.aclose()

    assert heartbeat_transport.closed


@pytest.mark.asyncio
async def test_async_normal_cleanup_joins_worker_before_release_and_clears_secrets() -> None:
    transport = _ShortGrantAsyncTransport()
    heartbeat_transport = _HeartbeatTransport()
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.05,
    )
    await coordinator.acquire()
    assert await asyncio.to_thread(heartbeat_transport.started.wait, 1.0)
    worker = coordinator._heartbeat_worker
    assert worker is not None
    transport.worker = worker

    class Resource:
        closed = False

        async def aclose(self) -> None:
            assert worker._stop.is_set()
            self.closed = True

    resource = coordinator.track(Resource())
    transport.resource = resource
    calls_before_close = len(heartbeat_transport.calls)

    await coordinator.aclose()
    time.sleep(0.02)

    assert transport.release_observation == (False, None, True)
    assert len(heartbeat_transport.calls) == calls_before_close
    assert heartbeat_transport.closed
    assert coordinator._heartbeat_worker is None
    assert coordinator._grant is None
    assert "protected-token" not in repr(worker)


@pytest.mark.asyncio
async def test_async_join_deadline_fences_releases_clears_and_reports_redacted_error() -> None:
    transport = _ShortGrantAsyncTransport()
    heartbeat_transport = _HeartbeatTransport(timeout=0.005, block=True)
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.02,
    )
    await coordinator.acquire()
    assert await asyncio.to_thread(heartbeat_transport.started.wait, 1.0)
    worker = coordinator._heartbeat_worker
    assert worker is not None
    transport.worker = worker

    started = time.monotonic()
    with pytest.raises(SessionLeaseLostError) as raised:
        await coordinator.aclose()
    elapsed = time.monotonic() - started

    rendered = f"{raised.value!s} {raised.value!r} {raised.value.__cause__!r}"
    assert elapsed < 0.2
    assert transport.release_observation == (True, None, None)
    assert [path for path, _ in transport.calls].count("/v1/leases/release") == 1
    assert heartbeat_transport.closed
    assert coordinator._heartbeat_worker is None
    assert coordinator._grant is None
    assert "daemon.invalid" not in rendered
    assert "protected" not in rendered
    heartbeat_transport.unblock.set()
    worker._thread.join(timeout=1.0)
    assert not worker._thread.is_alive()


@pytest.mark.asyncio
async def test_async_sequential_borrows_do_not_retain_workers_or_credentials() -> None:
    workers: list[object] = []
    for _ in range(2):
        transport = _ShortGrantAsyncTransport()
        heartbeat_transport = _HeartbeatTransport()
        coordinator = AsyncSessionLeaseCoordinator(
            transport,
            run_id="run-a",
            heartbeat_transport=heartbeat_transport,
            heartbeat_join_timeout_seconds=0.05,
        )
        await coordinator.acquire()
        worker = coordinator._heartbeat_worker
        assert worker is not None
        workers.append(worker)
        await coordinator.aclose()

    assert workers[0] is not workers[1]
    assert all(worker._credentials_value is None for worker in workers)
    assert all(not worker._thread.is_alive() for worker in workers)


@pytest.mark.asyncio
async def test_async_concurrent_borrows_use_isolated_workers_and_transports() -> None:
    coordinators: list[AsyncSessionLeaseCoordinator] = []
    heartbeat_transports: list[_HeartbeatTransport] = []
    for run_id in ("run-a", "run-b"):
        heartbeat_transport = _HeartbeatTransport()
        coordinator = AsyncSessionLeaseCoordinator(
            _ShortGrantAsyncTransport(),
            run_id=run_id,
            heartbeat_transport=heartbeat_transport,
            heartbeat_join_timeout_seconds=0.05,
        )
        coordinators.append(coordinator)
        heartbeat_transports.append(heartbeat_transport)

    await asyncio.gather(*(coordinator.acquire() for coordinator in coordinators))
    assert all(
        await asyncio.gather(
            *(
                asyncio.to_thread(transport.started.wait, 1.0)
                for transport in heartbeat_transports
            )
        )
    )

    workers = [coordinator._heartbeat_worker for coordinator in coordinators]
    assert workers[0] is not workers[1]
    assert heartbeat_transports[0] is not heartbeat_transports[1]
    assert all(
        {path for path, _ in transport.calls} == {"/v1/leases/heartbeat"}
        for transport in heartbeat_transports
    )
    await asyncio.gather(*(coordinator.aclose() for coordinator in coordinators))


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
