from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

import httpx

from .errors import (
    ActionOutcomeUnknownError,
    AuthenticationError,
    DaemonHTTPError,
    OperationNotAppliedError,
    OperationResultUnavailableError,
    RunSequenceConflictError,
    SessionBusyError,
    SessionLeaseLostError,
    SessionRecoveryRequiredError,
)
from .operation_kinds import stable_operation_kind

LEASE_ID_HEADER = "x-computer-use-lease-id"
LEASE_EPOCH_HEADER = "x-computer-use-lease-epoch"
LEASE_FENCE_HEADER = "x-computer-use-lease-fence"
LEASE_TOKEN_HEADER = "x-computer-use-lease-token"  # noqa: S105
OPERATION_SEQUENCE_HEADER = "x-computer-use-operation-sequence"

T = TypeVar("T")
_CoordinatorState = Literal["active", "result_unavailable", "unsafe", "closed"]

_ASYNC_HEARTBEAT_JOIN_TIMEOUT_SECONDS = 31.0


@dataclass(frozen=True, slots=True)
class LeaseCredentials:
    lease_id: str
    epoch: str
    fence: int
    token: str = field(repr=False)

    def headers(self) -> dict[str, str]:
        return {
            LEASE_ID_HEADER: self.lease_id,
            LEASE_EPOCH_HEADER: self.epoch,
            LEASE_FENCE_HEADER: str(self.fence),
            LEASE_TOKEN_HEADER: self.token,
        }


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    credentials: LeaseCredentials = field(repr=False)
    ttl_seconds: float
    heartbeat_interval_seconds: float


def _grant(response: httpx.Response) -> LeaseGrant:
    payload = response.json()
    token = response.headers.get(LEASE_TOKEN_HEADER)
    lease_id = payload.get("lease_id")
    epoch = payload.get("daemon_epoch")
    fence = payload.get("fence")
    ttl = payload.get("ttl_seconds")
    interval = payload.get("heartbeat_interval_seconds")
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(lease_id, str)
        or not lease_id
        or not isinstance(epoch, str)
        or not epoch
        or isinstance(fence, bool)
        or not isinstance(fence, int)
        or isinstance(ttl, bool)
        or not isinstance(ttl, int | float)
        or ttl <= 0
        or isinstance(interval, bool)
        or not isinstance(interval, int | float)
        or interval <= 0
    ):
        raise SessionLeaseLostError()
    return LeaseGrant(
        credentials=LeaseCredentials(
            lease_id=lease_id,
            epoch=epoch,
            fence=fence,
            token=token,
        ),
        ttl_seconds=float(ttl),
        heartbeat_interval_seconds=float(interval),
    )


def _mapped_error(exc: BaseException) -> BaseException:
    if isinstance(exc, AuthenticationError):
        return SessionLeaseLostError()
    if not isinstance(exc, DaemonHTTPError):
        return exc
    code = exc.code
    if code == "session_busy":
        return SessionBusyError()
    if code in {"lease_required", "lease_stale", "lease_expired", "lease_released"}:
        return SessionLeaseLostError()
    if code in {
        "operation_sequence_required",
        "operation_sequence_gap",
        "operation_sequence_reused",
        "run_sequence_conflict",
        "run_sealed",
    }:
        return RunSequenceConflictError()
    if code in {"recovery_required", "recovery_incident_mismatch"}:
        return SessionRecoveryRequiredError()
    return exc


def _receipt_state(payload: Any, sequence: int) -> str:
    if not isinstance(payload, dict) or payload.get("sequence") != sequence:
        raise ActionOutcomeUnknownError()
    state = payload.get("state")
    if state not in {"MISSING", "COMPLETED", "INDETERMINATE", "IN_PROGRESS"}:
        raise ActionOutcomeUnknownError()
    return str(state)


def _result_unavailable_error(
    payload: Mapping[str, Any],
    sequence: int,
) -> OperationResultUnavailableError:
    return OperationResultUnavailableError(
        sequence=sequence,
        operation_kind=stable_operation_kind(payload.get("operation_kind")),
    )


class SessionLeaseCoordinator:
    """Deep sync module for one borrowed trajectory lease.

    The interface hides credentials, sequencing, receipt reconciliation,
    heartbeat ownership, poisoning, and deterministic resource cleanup.
    """

    def __init__(self, transport: Any, *, run_id: str) -> None:
        self._transport = transport
        self._run_id = run_id
        self._grant: LeaseGrant | None = None
        self._sequence = 0
        self._operation_lock = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self._heartbeat_error: BaseException | None = None
        self._state: _CoordinatorState = "active"
        self._released = False
        self._resources: list[Any] = []
        self._close_wait_seconds = _transport_timeout_seconds(transport)

    def acquire(self) -> None:
        try:
            response = self._transport.request(
                "POST", "/v1/leases/acquire", json={"run_id": self._run_id}
            )
            self._grant = _grant(response)
        except BaseException as exc:
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="computer-use-lease-heartbeat",
            daemon=True,
        )
        self._heartbeat.start()

    def metadata_headers(self) -> dict[str, str]:
        return self._credentials().headers()

    def execute(self, request: Callable[[Mapping[str, str]], T]) -> T:
        with self._operation_lock:
            self._ensure_mutable()
            sequence = self._sequence
            headers = {
                **self.metadata_headers(),
                OPERATION_SEQUENCE_HEADER: str(sequence),
            }
            try:
                result = request(headers)
            except DaemonHTTPError as exc:
                result_unavailable = self._handle_received_error(exc, sequence)
            except AuthenticationError as exc:
                self._state = "unsafe"
                raise SessionLeaseLostError() from exc
            except BaseException as exc:
                result_unavailable = self._resolve_transport_loss(sequence, exc)
            else:
                self._sequence += 1
                return result
            raise result_unavailable

    def track(self, resource: T) -> T:
        self.ensure_open()
        self._resources.append(resource)
        return resource

    def ensure_open(self) -> None:
        if self._state == "closed":
            raise SessionLeaseLostError()

    def close(self) -> None:
        with self._operation_lock:
            if self._state == "closed":
                return
            self._state = "closed"
            errors: list[BaseException] = []
            self._stop.set()
            heartbeat, self._heartbeat = self._heartbeat, None
            if heartbeat is not None:
                heartbeat.join(timeout=self._close_wait_seconds)
            for resource in reversed(self._resources):
                try:
                    resource.close()
                except BaseException as exc:
                    errors.append(exc)
            self._resources.clear()
            if self._grant is not None and not self._released:
                try:
                    self._transport.request(
                        "POST",
                        "/v1/leases/release",
                        headers=self.metadata_headers(),
                    )
                    self._released = True
                except BaseException as exc:
                    errors.append(_mapped_error(exc))
            if errors:
                raise SessionLeaseLostError() from errors[0]

    def observe_after_result_loss(self, request: Callable[[], T]) -> T:
        with self._operation_lock:
            self._ensure_reobservable()
            return request()

    def _heartbeat_loop(self) -> None:
        assert self._grant is not None
        while not self._stop.wait(self._grant.heartbeat_interval_seconds):
            try:
                self._transport.request(
                    "POST",
                    "/v1/leases/heartbeat",
                    headers=self.metadata_headers(),
                )
            except BaseException as exc:
                self._heartbeat_error = _mapped_error(exc)
                self._stop.set()
                return

    def _credentials(self) -> LeaseCredentials:
        if self._grant is None:
            raise SessionLeaseLostError()
        return self._grant.credentials

    def _ensure_mutable(self) -> None:
        self.ensure_open()
        if self._heartbeat_error is not None:
            raise SessionLeaseLostError() from self._heartbeat_error
        if self._state != "active":
            raise ActionOutcomeUnknownError()

    def _ensure_reobservable(self) -> None:
        self.ensure_open()
        if self._heartbeat_error is not None:
            raise SessionLeaseLostError() from self._heartbeat_error
        if self._state != "result_unavailable":
            raise ActionOutcomeUnknownError()

    def _receipt(self, path: str, sequence: int) -> dict[str, Any]:
        response = self._transport.request(
            "POST",
            path,
            json={"run_id": self._run_id, "sequence": sequence},
            headers=self.metadata_headers(),
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ActionOutcomeUnknownError()
        return payload

    def _handle_received_error(
        self,
        exc: DaemonHTTPError,
        sequence: int,
    ) -> OperationResultUnavailableError:
        if exc.code in {
            "lease_required",
            "lease_stale",
            "lease_expired",
            "lease_released",
        }:
            raise SessionLeaseLostError() from exc
        try:
            payload = self._receipt("/v1/receipts/status", sequence)
            state = _receipt_state(payload, sequence)
        except BaseException as status_exc:
            self._state = "unsafe"
            raise ActionOutcomeUnknownError() from status_exc
        if state == "MISSING":
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise exc
            raise mapped from exc
        if state == "COMPLETED":
            self._sequence += 1
            if exc.code == "operation_result_unavailable":
                self._state = "result_unavailable"
                return _result_unavailable_error(payload, sequence)
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise exc
            raise mapped from exc
        self._state = "unsafe"
        raise SessionRecoveryRequiredError() from exc

    def _resolve_transport_loss(
        self,
        sequence: int,
        cause: BaseException,
    ) -> OperationResultUnavailableError:
        self._state = "unsafe"
        try:
            payload = self._receipt("/v1/receipts/resolve", sequence)
            state = _receipt_state(payload, sequence)
        except BaseException as resolve_exc:
            raise ActionOutcomeUnknownError() from resolve_exc
        if state == "MISSING" and payload.get("proven_not_applied") is True:
            self._released = bool(payload.get("run_sealed"))
            raise OperationNotAppliedError() from cause
        if state == "COMPLETED":
            self._sequence += 1
            self._state = "result_unavailable"
            return _result_unavailable_error(payload, sequence)
        if state == "INDETERMINATE":
            raise SessionRecoveryRequiredError() from cause
        raise ActionOutcomeUnknownError() from cause


class _LeaseHeartbeatWorker:
    """Private blocking heartbeat owner for one async session borrow."""

    def __init__(
        self,
        transport: Any,
        *,
        credentials: LeaseCredentials,
        heartbeat_interval_seconds: float,
        join_timeout_seconds: float,
    ) -> None:
        request_timeout_seconds = _transport_timeout_seconds(transport)
        if (
            not math.isfinite(join_timeout_seconds)
            or join_timeout_seconds <= 0
            or request_timeout_seconds >= join_timeout_seconds
        ):
            raise ValueError("heartbeat request timeout must be below its join timeout")
        self._transport: Any | None = transport
        self._credentials_value: LeaseCredentials | None = credentials
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._join_timeout_seconds = join_timeout_seconds
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._error: SessionLeaseLostError | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="computer-use-async-lease-heartbeat",
            daemon=True,
        )

    def __repr__(self) -> str:
        return "_LeaseHeartbeatWorker()"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._state_lock:
            self._stop.set()

    def join(self) -> bool:
        self._thread.join(timeout=self._join_timeout_seconds)
        return not self._thread.is_alive()

    def clear_credentials(self) -> None:
        with self._state_lock:
            self._credentials_value = None

    def error(self) -> SessionLeaseLostError | None:
        with self._state_lock:
            return self._error

    def close_transport(self) -> None:
        with self._state_lock:
            transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def _run(self) -> None:
        while not self._stop.wait(self._heartbeat_interval_seconds):
            with self._state_lock:
                if self._stop.is_set():
                    return
                credentials = self._credentials_value
                transport = self._transport
            if credentials is None or transport is None:
                return
            try:
                transport.request(
                    "POST",
                    "/v1/leases/heartbeat",
                    headers=credentials.headers(),
                )
            except BaseException:
                with self._state_lock:
                    self._error = SessionLeaseLostError()
                    self._stop.set()
                return


class AsyncSessionLeaseCoordinator:
    """Native-async counterpart to :class:`SessionLeaseCoordinator`."""

    def __init__(
        self,
        transport: Any,
        *,
        run_id: str,
        heartbeat_transport: Any,
        heartbeat_join_timeout_seconds: float = _ASYNC_HEARTBEAT_JOIN_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._heartbeat_transport: Any | None = heartbeat_transport
        self._heartbeat_join_timeout_seconds = heartbeat_join_timeout_seconds
        self._run_id = run_id
        self._grant: LeaseGrant | None = None
        self._sequence = 0
        self._operation_lock = asyncio.Lock()
        self._heartbeat_worker: _LeaseHeartbeatWorker | None = None
        self._state: _CoordinatorState = "active"
        self._released = False
        self._resources: list[Any] = []

    async def acquire(self) -> None:
        try:
            response = await self._transport.request(
                "POST", "/v1/leases/acquire", json={"run_id": self._run_id}
            )
            self._grant = _grant(response)
        except BaseException as exc:
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        heartbeat_transport = self._heartbeat_transport
        if heartbeat_transport is None:
            raise SessionLeaseLostError()
        heartbeat_worker = _LeaseHeartbeatWorker(
            heartbeat_transport,
            credentials=self._grant.credentials,
            heartbeat_interval_seconds=self._grant.heartbeat_interval_seconds,
            join_timeout_seconds=self._heartbeat_join_timeout_seconds,
        )
        try:
            heartbeat_worker.start()
        except BaseException:
            heartbeat_worker.clear_credentials()
            raise
        self._heartbeat_worker = heartbeat_worker
        self._heartbeat_transport = None

    def metadata_headers(self) -> dict[str, str]:
        return self._credentials().headers()

    async def execute(
        self,
        request: Callable[[Mapping[str, str]], Awaitable[T]],
    ) -> T:
        async with self._operation_lock:
            self._ensure_mutable()
            sequence = self._sequence
            headers = {
                **self.metadata_headers(),
                OPERATION_SEQUENCE_HEADER: str(sequence),
            }
            try:
                result = await request(headers)
            except asyncio.CancelledError as exc:
                await self._resolve_after_cancellation(sequence, exc)
                raise
            except DaemonHTTPError as exc:
                result_unavailable = await self._handle_received_error(exc, sequence)
            except AuthenticationError as exc:
                self._state = "unsafe"
                raise SessionLeaseLostError() from exc
            except BaseException as exc:
                result_unavailable = await self._resolve_transport_loss(sequence, exc)
            else:
                self._sequence += 1
                return result
            raise result_unavailable

    def track(self, resource: T) -> T:
        self.ensure_open()
        self._resources.append(resource)
        return resource

    def ensure_open(self) -> None:
        if self._state == "closed":
            raise SessionLeaseLostError()

    async def aclose(self) -> None:
        async with self._operation_lock:
            if self._state == "closed":
                return
            self._state = "closed"
            errors: list[BaseException] = []
            heartbeat, self._heartbeat_worker = self._heartbeat_worker, None
            heartbeat_transport, self._heartbeat_transport = self._heartbeat_transport, None
            if heartbeat is not None:
                heartbeat.stop()
            for resource in reversed(self._resources):
                try:
                    await resource.aclose()
                except BaseException as exc:
                    errors.append(exc)
            self._resources.clear()
            heartbeat_terminated = True
            if heartbeat is not None:
                heartbeat_terminated = await asyncio.to_thread(heartbeat.join)
                heartbeat.clear_credentials()
            release_headers = (
                self._grant.credentials.headers() if self._grant is not None else None
            )
            if self._grant is not None and not self._released:
                try:
                    await self._transport.request(
                        "POST",
                        "/v1/leases/release",
                        headers=release_headers,
                    )
                    self._released = True
                except BaseException as exc:
                    errors.append(_mapped_error(exc))
            self._grant = None
            try:
                if heartbeat is not None:
                    heartbeat.close_transport()
                elif heartbeat_transport is not None:
                    heartbeat_transport.close()
            except BaseException:
                errors.append(SessionLeaseLostError())
            if not heartbeat_terminated:
                errors.insert(0, SessionLeaseLostError())
            if errors:
                raise SessionLeaseLostError() from errors[0]

    async def observe_after_result_loss(
        self,
        request: Callable[[], Awaitable[T]],
    ) -> T:
        async with self._operation_lock:
            self._ensure_reobservable()
            return await request()

    def _credentials(self) -> LeaseCredentials:
        if self._grant is None:
            raise SessionLeaseLostError()
        return self._grant.credentials

    def _ensure_mutable(self) -> None:
        self.ensure_open()
        heartbeat_error = (
            self._heartbeat_worker.error()
            if self._heartbeat_worker is not None
            else None
        )
        if heartbeat_error is not None:
            raise SessionLeaseLostError() from heartbeat_error
        if self._state != "active":
            raise ActionOutcomeUnknownError()

    def _ensure_reobservable(self) -> None:
        self.ensure_open()
        heartbeat_error = (
            self._heartbeat_worker.error()
            if self._heartbeat_worker is not None
            else None
        )
        if heartbeat_error is not None:
            raise SessionLeaseLostError() from heartbeat_error
        if self._state != "result_unavailable":
            raise ActionOutcomeUnknownError()

    async def _receipt(self, path: str, sequence: int) -> dict[str, Any]:
        response = await self._transport.request(
            "POST",
            path,
            json={"run_id": self._run_id, "sequence": sequence},
            headers=self.metadata_headers(),
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ActionOutcomeUnknownError()
        return payload

    async def _handle_received_error(
        self,
        exc: DaemonHTTPError,
        sequence: int,
    ) -> OperationResultUnavailableError:
        if exc.code in {
            "lease_required",
            "lease_stale",
            "lease_expired",
            "lease_released",
        }:
            raise SessionLeaseLostError() from exc
        try:
            payload = await self._receipt("/v1/receipts/status", sequence)
            state = _receipt_state(payload, sequence)
        except BaseException as status_exc:
            self._state = "unsafe"
            raise ActionOutcomeUnknownError() from status_exc
        if state == "MISSING":
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise exc
            raise mapped from exc
        if state == "COMPLETED":
            self._sequence += 1
            if exc.code == "operation_result_unavailable":
                self._state = "result_unavailable"
                return _result_unavailable_error(payload, sequence)
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise exc
            raise mapped from exc
        self._state = "unsafe"
        raise SessionRecoveryRequiredError() from exc

    async def _resolve_transport_loss(
        self,
        sequence: int,
        cause: BaseException,
    ) -> OperationResultUnavailableError:
        self._state = "unsafe"
        try:
            payload = await self._receipt("/v1/receipts/resolve", sequence)
            state = _receipt_state(payload, sequence)
        except BaseException as resolve_exc:
            raise ActionOutcomeUnknownError() from resolve_exc
        if state == "MISSING" and payload.get("proven_not_applied") is True:
            self._released = bool(payload.get("run_sealed"))
            raise OperationNotAppliedError() from cause
        if state == "COMPLETED":
            self._sequence += 1
            self._state = "result_unavailable"
            return _result_unavailable_error(payload, sequence)
        if state == "INDETERMINATE":
            raise SessionRecoveryRequiredError() from cause
        raise ActionOutcomeUnknownError() from cause

    async def _resolve_after_cancellation(
        self,
        sequence: int,
        cancellation: asyncio.CancelledError,
    ) -> None:
        task = asyncio.create_task(self._resolve_transport_loss(sequence, cancellation))
        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    with suppress(BaseException):
                        await task
                    break
                continue
            except BaseException:
                break
            else:
                break


def _transport_timeout_seconds(transport: Any) -> float:
    configured = getattr(transport, "timeout", None)
    if isinstance(configured, int | float) and not isinstance(configured, bool):
        return max(0.0, float(configured))
    timeout = getattr(getattr(transport, "_client", None), "timeout", None)
    candidates = [
        getattr(timeout, name, None)
        for name in ("connect", "read", "write", "pool")
    ]
    finite = [
        float(value)
        for value in candidates
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
    ]
    return max(finite, default=30.0)
