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


def _exception_graph(error: BaseException) -> list[BaseException]:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    graph: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        graph.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return graph


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
    transport.status[0] = {
        "state": "COMPLETED",
        "sequence": 0,
        "operation_kind": "actions.run",
    }
    transport.status[2] = {
        "state": "COMPLETED",
        "sequence": 2,
        "operation_kind": "/v1/mouse/click",
    }
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

    with pytest.raises(OperationResultUnavailableError) as raised:
        coordinator.execute(
            lambda _headers: (_ for _ in ()).throw(
                DaemonHTTPError(
                    "result gone", code="operation_result_unavailable"
                )
            )
        )
    assert raised.value.sequence == 2
    assert raised.value.operation_kind == "/v1/mouse/click"
    with pytest.raises(ActionOutcomeUnknownError):
        coordinator.execute(lambda _headers: None)
    coordinator.close()


def test_result_unavailable_error_exposes_only_safe_validated_metadata() -> None:
    error = OperationResultUnavailableError(
        sequence=7,
        operation_kind="actions.run",
    )

    assert vars(error) == {"sequence": 7, "operation_kind": "actions.run"}
    rendered = f"{error!s} {error!r}"
    assert rendered == (
        "the operation result is unavailable "
        "OperationResultUnavailableError('the operation result is unavailable')"
    )
    assert "daemon.invalid" not in rendered
    assert "protected" not in rendered

    for sequence in (-1, True, 1.5):
        with pytest.raises(ValueError, match="sequence"):
            OperationResultUnavailableError(
                sequence=sequence,  # type: ignore[arg-type]
                operation_kind="actions.run",
            )
    with pytest.raises(ValueError, match="operation_kind") as raised:
        OperationResultUnavailableError(
            sequence=0,
            operation_kind="https://daemon.invalid/private-operation",
        )
    assert "daemon.invalid" not in str(raised.value)


def test_unallowlisted_daemon_operation_kind_is_redacted_to_none() -> None:
    transport = _SyncTransport()
    transport.resolved[0] = {
        "state": "COMPLETED",
        "sequence": 0,
        "operation_kind": "https://daemon.invalid?token=protected",
    }
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()

    with pytest.raises(OperationResultUnavailableError) as raised:
        coordinator.execute(
            lambda _headers: (_ for _ in ()).throw(
                httpx.ReadTimeout("private endpoint and token")
            )
        )

    assert raised.value.sequence == 0
    assert raised.value.operation_kind is None
    rendered = f"{raised.value!s} {raised.value!r}"
    assert "daemon.invalid" not in rendered
    assert "protected" not in rendered
    coordinator.close()


def test_sync_result_unavailable_drops_raw_daemon_and_transport_exception_chains() -> None:
    sentinel = "sentinel-endpoint-token-sync"
    cases = (
        (
            "received",
            DaemonHTTPError(sentinel, code="operation_result_unavailable"),
        ),
        ("transport", httpx.ReadTimeout(sentinel)),
    )
    for mode, raw_error in cases:
        transport = _SyncTransport()
        receipt = {
            "state": "COMPLETED",
            "sequence": 0,
            "operation_kind": "actions.run",
        }
        if mode == "received":
            transport.status[0] = receipt
        else:
            transport.resolved[0] = receipt
        coordinator = SessionLeaseCoordinator(transport, run_id=f"run-{mode}")
        coordinator.acquire()

        with pytest.raises(OperationResultUnavailableError) as raised:
            coordinator.execute(
                lambda _headers, error=raw_error: (_ for _ in ()).throw(error)
            )

        graph = _exception_graph(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert graph == [raised.value]
        assert sentinel not in " ".join(
            f"{error!s} {error!r}" for error in graph
        )
        coordinator.close()


def test_sync_reobservation_requires_result_loss_and_never_reenables_mutation() -> None:
    transport = _SyncTransport()
    transport.resolved[0] = {
        "state": "COMPLETED",
        "sequence": 0,
        "operation_kind": "actions.run",
    }
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()

    with pytest.raises(ActionOutcomeUnknownError):
        coordinator.observe_after_result_loss(lambda: "must not run")

    dispatches = 0

    def lost(_headers: Any) -> None:
        nonlocal dispatches
        dispatches += 1
        raise httpx.ReadTimeout("private endpoint and token")

    with pytest.raises(OperationResultUnavailableError) as raised:
        coordinator.execute(lost)
    assert raised.value.sequence == 0
    assert raised.value.operation_kind == "actions.run"
    assert raised.value.__cause__ is None
    assert "private" not in f"{raised.value!s} {raised.value!r}"
    assert "token" not in f"{raised.value!s} {raised.value!r}"

    observations = 0

    def observe() -> str:
        nonlocal observations
        observations += 1
        return "frame"

    assert coordinator.observe_after_result_loss(observe) == "frame"
    assert coordinator.observe_after_result_loss(observe) == "frame"
    with pytest.raises(ActionOutcomeUnknownError):
        coordinator.execute(lost)
    assert dispatches == 1
    assert observations == 2

    coordinator.close()
    with pytest.raises(SessionLeaseLostError):
        coordinator.observe_after_result_loss(observe)


def test_sync_failed_resolution_is_unsafe_but_new_run_can_mutate() -> None:
    first_transport = _SyncTransport()
    first = SessionLeaseCoordinator(first_transport, run_id="run-a")
    first.acquire()

    with pytest.raises(ActionOutcomeUnknownError):
        first.execute(
            lambda _headers: (_ for _ in ()).throw(
                httpx.ReadTimeout("private endpoint and token")
            )
        )
    with pytest.raises(ActionOutcomeUnknownError):
        first.observe_after_result_loss(lambda: "must not run")
    first.close()

    second_transport = _SyncTransport()
    second = SessionLeaseCoordinator(second_transport, run_id="run-b")
    second.acquire()
    assert second.execute(lambda headers: headers["x-computer-use-operation-sequence"]) == "0"
    second.close()


def test_sync_cleanup_waits_for_result_loss_observation_lock() -> None:
    transport = _SyncTransport()
    transport.resolved[0] = {
        "state": "COMPLETED",
        "sequence": 0,
        "operation_kind": "actions.run",
    }
    coordinator = SessionLeaseCoordinator(transport, run_id="run-a")
    coordinator.acquire()
    with pytest.raises(OperationResultUnavailableError):
        coordinator.execute(
            lambda _headers: (_ for _ in ()).throw(httpx.ReadTimeout("lost"))
        )

    observation_started = threading.Event()
    finish_observation = threading.Event()

    def observe() -> str:
        observation_started.set()
        finish_observation.wait(timeout=1.0)
        return "frame"

    observation = threading.Thread(
        target=lambda: coordinator.observe_after_result_loss(observe)
    )
    observation.start()
    assert observation_started.wait(timeout=1.0)
    cleanup = threading.Thread(target=coordinator.close)
    cleanup.start()
    time.sleep(0.01)

    assert cleanup.is_alive()
    assert "/v1/leases/release" not in [path for path, _ in transport.calls]

    finish_observation.set()
    observation.join(timeout=1.0)
    cleanup.join(timeout=1.0)
    assert not observation.is_alive()
    assert not cleanup.is_alive()
    assert [path for path, _ in transport.calls].count("/v1/leases/release") == 1


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
async def test_async_reobservation_is_repeatable_cancellable_and_keeps_mutations_blocked() -> None:
    transport = _AsyncTransport()
    heartbeat_transport = _SyncTransport()
    transport.resolved[0] = {
        "state": "COMPLETED",
        "sequence": 0,
        "operation_kind": "actions.run",
    }
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.1,
    )
    await coordinator.acquire()

    async def lost(_headers: Any) -> None:
        raise httpx.ReadTimeout("private endpoint and token")

    with pytest.raises(OperationResultUnavailableError) as raised:
        await coordinator.execute(lost)
    assert raised.value.sequence == 0
    assert raised.value.operation_kind == "actions.run"

    observation_started = asyncio.Event()

    async def blocked_observation() -> str:
        observation_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        coordinator.observe_after_result_loss(blocked_observation)
    )
    await observation_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await coordinator.observe_after_result_loss(lambda: _async_value("frame")) == "frame"
    assert await coordinator.observe_after_result_loss(lambda: _async_value("frame")) == "frame"
    with pytest.raises(ActionOutcomeUnknownError):
        await coordinator.execute(lost)
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_async_result_unavailable_drops_raw_daemon_and_transport_exception_chains() -> None:
    sentinel = "sentinel-endpoint-token-async"
    cases = (
        (
            "received",
            DaemonHTTPError(sentinel, code="operation_result_unavailable"),
        ),
        ("transport", httpx.ReadTimeout(sentinel)),
    )
    for mode, raw_error in cases:
        transport = _AsyncTransport()
        heartbeat_transport = _SyncTransport()
        receipt = {
            "state": "COMPLETED",
            "sequence": 0,
            "operation_kind": "actions.run",
        }
        if mode == "received":
            transport.status[0] = receipt
        else:
            transport.resolved[0] = receipt
        coordinator = AsyncSessionLeaseCoordinator(
            transport,
            run_id=f"run-{mode}",
            heartbeat_transport=heartbeat_transport,
            heartbeat_join_timeout_seconds=0.1,
        )
        await coordinator.acquire()

        with pytest.raises(OperationResultUnavailableError) as raised:
            await coordinator.execute(
                lambda _headers, error=raw_error: _raise_async(error)
            )

        graph = _exception_graph(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert graph == [raised.value]
        assert sentinel not in " ".join(
            f"{error!s} {error!r}" for error in graph
        )
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_async_cleanup_waits_for_result_loss_observation_lock() -> None:
    transport = _AsyncTransport()
    heartbeat_transport = _SyncTransport()
    transport.resolved[0] = {
        "state": "COMPLETED",
        "sequence": 0,
        "operation_kind": "actions.run",
    }
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.1,
    )
    await coordinator.acquire()

    with pytest.raises(OperationResultUnavailableError):
        await coordinator.execute(
            lambda _headers: _raise_async(httpx.ReadTimeout("lost"))
        )

    observation_started = asyncio.Event()
    finish_observation = asyncio.Event()

    async def observe() -> str:
        observation_started.set()
        await finish_observation.wait()
        return "frame"

    observation = asyncio.create_task(coordinator.observe_after_result_loss(observe))
    await observation_started.wait()
    cleanup = asyncio.create_task(coordinator.aclose())
    await asyncio.sleep(0)

    assert not cleanup.done()
    assert "/v1/leases/release" not in [path for path, _ in transport.calls]

    finish_observation.set()
    assert await observation == "frame"
    await cleanup
    assert [path for path, _ in transport.calls].count("/v1/leases/release") == 1


async def _async_value(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_async_reobservation_rejects_active_unsafe_and_closed_states() -> None:
    transport = _AsyncTransport()
    heartbeat_transport = _SyncTransport()
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.1,
    )
    await coordinator.acquire()

    with pytest.raises(ActionOutcomeUnknownError):
        await coordinator.observe_after_result_loss(lambda: _async_value("must not run"))
    with pytest.raises(ActionOutcomeUnknownError):
        await coordinator.execute(
            lambda _headers: _raise_async(httpx.ReadTimeout("private endpoint and token"))
        )
    with pytest.raises(ActionOutcomeUnknownError):
        await coordinator.observe_after_result_loss(lambda: _async_value("must not run"))

    await coordinator.aclose()
    with pytest.raises(SessionLeaseLostError):
        await coordinator.observe_after_result_loss(lambda: _async_value("must not run"))


async def _raise_async(exc: BaseException) -> None:
    raise exc


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
async def test_async_heartbeat_start_failure_keeps_cleanup_ownership(
    monkeypatch,
) -> None:
    transport = _ShortGrantAsyncTransport()
    heartbeat_transport = _HeartbeatTransport()
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-a",
        heartbeat_transport=heartbeat_transport,
        heartbeat_join_timeout_seconds=0.05,
    )

    def fail_start(_worker: object) -> None:
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(
        "modal_computer_use.session_lease._LeaseHeartbeatWorker.start",
        fail_start,
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        await coordinator.acquire()
    await coordinator.aclose()

    assert coordinator._heartbeat_worker is None
    assert coordinator._grant is None
    assert heartbeat_transport.closed
    assert [path for path, _ in transport.calls].count("/v1/leases/release") == 1


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
