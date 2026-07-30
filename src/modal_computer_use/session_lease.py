from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, TypeVar

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

LEASE_ID_HEADER = "x-computer-use-lease-id"
LEASE_EPOCH_HEADER = "x-computer-use-lease-epoch"
LEASE_FENCE_HEADER = "x-computer-use-lease-fence"
LEASE_TOKEN_HEADER = "x-computer-use-lease-token"  # noqa: S105
OPERATION_SEQUENCE_HEADER = "x-computer-use-operation-sequence"

T = TypeVar("T")


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
    if code == "operation_result_unavailable":
        return OperationResultUnavailableError()
    return exc


def _receipt_state(payload: Any, sequence: int) -> str:
    if not isinstance(payload, dict) or payload.get("sequence") != sequence:
        raise ActionOutcomeUnknownError()
    state = payload.get("state")
    if state not in {"MISSING", "COMPLETED", "INDETERMINATE", "IN_PROGRESS"}:
        raise ActionOutcomeUnknownError()
    return str(state)


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
        self._poisoned = False
        self._released = False
        self._closed = False
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
                self._handle_received_error(exc, sequence)
                raise AssertionError("unreachable") from None
            except AuthenticationError as exc:
                raise SessionLeaseLostError() from exc
            except BaseException as exc:
                self._resolve_transport_loss(sequence, exc)
                raise AssertionError("unreachable") from None
            self._sequence += 1
            return result

    def track(self, resource: T) -> T:
        self.ensure_open()
        self._resources.append(resource)
        return resource

    def ensure_open(self) -> None:
        if self._closed:
            raise SessionLeaseLostError()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
        if self._poisoned:
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

    def _handle_received_error(self, exc: DaemonHTTPError, sequence: int) -> None:
        if exc.code in {
            "lease_required",
            "lease_stale",
            "lease_expired",
            "lease_released",
        }:
            raise SessionLeaseLostError() from exc
        try:
            state = _receipt_state(self._receipt("/v1/receipts/status", sequence), sequence)
        except BaseException as status_exc:
            self._poisoned = True
            raise ActionOutcomeUnknownError() from status_exc
        if state == "MISSING":
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise exc
            raise mapped from exc
        if state == "COMPLETED":
            self._sequence += 1
            if exc.code == "operation_result_unavailable":
                self._poisoned = True
                raise OperationResultUnavailableError() from exc
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise exc
            raise mapped from exc
        self._poisoned = True
        raise SessionRecoveryRequiredError() from exc

    def _resolve_transport_loss(self, sequence: int, cause: BaseException) -> None:
        self._poisoned = True
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
            raise OperationResultUnavailableError() from cause
        if state == "INDETERMINATE":
            raise SessionRecoveryRequiredError() from cause
        raise ActionOutcomeUnknownError() from cause


class AsyncSessionLeaseCoordinator:
    """Native-async counterpart to :class:`SessionLeaseCoordinator`."""

    def __init__(self, transport: Any, *, run_id: str) -> None:
        self._transport = transport
        self._run_id = run_id
        self._grant: LeaseGrant | None = None
        self._sequence = 0
        self._operation_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._heartbeat: asyncio.Task[None] | None = None
        self._heartbeat_error: BaseException | None = None
        self._poisoned = False
        self._released = False
        self._closed = False
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
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())

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
                await self._handle_received_error(exc, sequence)
                raise AssertionError("unreachable") from None
            except AuthenticationError as exc:
                raise SessionLeaseLostError() from exc
            except BaseException as exc:
                await self._resolve_transport_loss(sequence, exc)
                raise AssertionError("unreachable") from None
            self._sequence += 1
            return result

    def track(self, resource: T) -> T:
        self.ensure_open()
        self._resources.append(resource)
        return resource

    def ensure_open(self) -> None:
        if self._closed:
            raise SessionLeaseLostError()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        self._stop.set()
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                errors.append(exc)
        for resource in reversed(self._resources):
            try:
                await resource.aclose()
            except BaseException as exc:
                errors.append(exc)
        self._resources.clear()
        if self._grant is not None and not self._released:
            try:
                await self._transport.request(
                    "POST",
                    "/v1/leases/release",
                    headers=self.metadata_headers(),
                )
                self._released = True
            except BaseException as exc:
                errors.append(_mapped_error(exc))
        if errors:
            raise SessionLeaseLostError() from errors[0]

    async def _heartbeat_loop(self) -> None:
        assert self._grant is not None
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._grant.heartbeat_interval_seconds,
                    )
                    return
                except TimeoutError:
                    pass
                await self._transport.request(
                    "POST",
                    "/v1/leases/heartbeat",
                    headers=self.metadata_headers(),
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._heartbeat_error = _mapped_error(exc)
            self._stop.set()

    def _credentials(self) -> LeaseCredentials:
        if self._grant is None:
            raise SessionLeaseLostError()
        return self._grant.credentials

    def _ensure_mutable(self) -> None:
        self.ensure_open()
        if self._heartbeat_error is not None:
            raise SessionLeaseLostError() from self._heartbeat_error
        if self._poisoned:
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

    async def _handle_received_error(self, exc: DaemonHTTPError, sequence: int) -> None:
        if exc.code in {
            "lease_required",
            "lease_stale",
            "lease_expired",
            "lease_released",
        }:
            raise SessionLeaseLostError() from exc
        try:
            state = _receipt_state(
                await self._receipt("/v1/receipts/status", sequence), sequence
            )
        except BaseException as status_exc:
            self._poisoned = True
            raise ActionOutcomeUnknownError() from status_exc
        if state == "MISSING":
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise exc
            raise mapped from exc
        if state == "COMPLETED":
            self._sequence += 1
            if exc.code == "operation_result_unavailable":
                self._poisoned = True
                raise OperationResultUnavailableError() from exc
            mapped = _mapped_error(exc)
            if mapped is exc:
                raise exc
            raise mapped from exc
        self._poisoned = True
        raise SessionRecoveryRequiredError() from exc

    async def _resolve_transport_loss(self, sequence: int, cause: BaseException) -> None:
        self._poisoned = True
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
            raise OperationResultUnavailableError() from cause
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
