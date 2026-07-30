"""Create, observe, and cancel orchestration over application-owned ports."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fastapi import Request

from .domain import (
    TERMINAL_STATES,
    FunctionCallIdentity,
    PollOutcome,
    PollState,
    Principal,
    ResolvedDesktop,
    ResolvedTask,
    RunRecord,
    RunState,
)
from .ports import PrincipalResolver, RunStore, SessionCatalog, TaskCatalog, TrajectoryDispatcher


class CreateRunBody(Protocol):
    desktop_key: str
    task_key: str
    idempotency_key: str


class GatewayError(RuntimeError):
    def __init__(self, *, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


class AuthenticationRequired(GatewayError):
    def __init__(self) -> None:
        super().__init__(status_code=401, code="authentication_required")


class ObjectNotFound(GatewayError):
    def __init__(self) -> None:
        super().__init__(status_code=404, code="not_found")


class RunConflict(GatewayError):
    def __init__(self) -> None:
        super().__init__(status_code=409, code="run_state_conflict")


class IdempotencyConflict(GatewayError):
    def __init__(self) -> None:
        super().__init__(status_code=409, code="idempotency_conflict")


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunGatewayService:
    principal_resolver: PrincipalResolver
    session_catalog: SessionCatalog
    task_catalog: TaskCatalog
    run_store: RunStore
    dispatcher: TrajectoryDispatcher
    stale_after: timedelta = timedelta(seconds=30)
    clock: Callable[[], datetime] = utc_now
    run_id_factory: Callable[[], str] = lambda: f"run_{secrets.token_urlsafe(24)}"

    def __post_init__(self) -> None:
        required = (
            self.principal_resolver,
            self.session_catalog,
            self.task_catalog,
            self.run_store,
            self.dispatcher,
        )
        if any(dependency is None for dependency in required):
            raise ValueError(
                "all authentication, authorization, storage, and dispatch dependencies "
                "are required"
            )
        if self.stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")

    async def create_run(self, request: Request, body: CreateRunBody) -> RunRecord:
        principal = await self.principal_resolver.resolve(request)
        desktop_key = body.desktop_key
        task_key = body.task_key
        idempotency_key = body.idempotency_key
        desktop = await self.session_catalog.resolve(principal, desktop_key)
        task = await self.task_catalog.resolve(principal, task_key)
        proposed = RunRecord.reserve(
            run_id=self.run_id_factory(),
            tenant_id=principal.tenant_id,
            idempotency_key=idempotency_key,
            admission_fingerprint=admission_fingerprint(
                desktop_key=desktop_key,
                task_key=task_key,
            ),
            now=self.clock(),
        )
        reservation = await self.run_store.reserve_if_absent(
            tenant_id=principal.tenant_id,
            idempotency_key=idempotency_key,
            proposed=proposed,
        )
        record = reservation.record
        if not hmac.compare_digest(
            record.admission_fingerprint,
            proposed.admission_fingerprint,
        ):
            raise IdempotencyConflict()
        if reservation.created:
            return await self._dispatch(record, desktop=desktop, task=task)
        record, claim_stale_reserved = await self._reconcile_stale_admission(record)
        if claim_stale_reserved:
            return await self._dispatch(record, desktop=desktop, task=task)
        return record

    async def get_run(self, request: Request, run_id: str) -> RunRecord:
        principal = await self.principal_resolver.resolve(request)
        record = await self._authorized_run(principal, run_id)
        record, _ = await self._reconcile_stale_admission(record)
        if record.state not in {RunState.RUNNING, RunState.CANCELLATION_REQUESTED}:
            return record
        call_id = record.function_call_id
        if call_id is None:
            return await self._transition_or_reload(record, RunState.INDETERMINATE)
        observed = await self.dispatcher.poll(call_id)
        outcome = observed if isinstance(observed, PollOutcome) else PollOutcome(observed)
        if outcome.state in {PollState.PENDING, PollState.UNAVAILABLE}:
            return record
        if outcome.state is PollState.SUCCEEDED:
            next_state = RunState.SUCCEEDED
        elif outcome.state is PollState.FAILED:
            next_state = RunState.FAILED
        elif outcome.state is PollState.TERMINATED:
            next_state = (
                RunState.CANCELLED
                if record.state is RunState.CANCELLATION_REQUESTED
                else RunState.FAILED
            )
        else:
            next_state = RunState.INDETERMINATE
        return await self._transition_or_reload(record, next_state)

    async def cancel_run(self, request: Request, run_id: str) -> tuple[RunRecord, bool]:
        principal = await self.principal_resolver.resolve(request)
        record = await self._authorized_run(principal, run_id)
        record, _ = await self._reconcile_stale_admission(record)
        if record.state is RunState.CANCELLATION_REQUESTED:
            return record, True
        if record.state in TERMINAL_STATES:
            return record, False
        if record.state is not RunState.RUNNING:
            raise RunConflict()
        claimed = await self._transition(record, RunState.CANCELLATION_REQUESTED)
        if claimed is None:
            latest = await self._authorized_run(principal, run_id)
            return latest, latest.state is RunState.CANCELLATION_REQUESTED
        call_id = claimed.function_call_id
        if call_id is None:
            return await self._transition_or_reload(claimed, RunState.INDETERMINATE), False
        try:
            await self.dispatcher.cancel(call_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self._transition_or_reload(claimed, RunState.INDETERMINATE), False
        return claimed, True

    async def _authorized_run(self, principal: Principal, run_id: str) -> RunRecord:
        record = await self.run_store.get_authorized(
            tenant_id=principal.tenant_id,
            run_id=run_id,
        )
        if record is None:
            raise ObjectNotFound()
        return record

    async def _dispatch(
        self,
        record: RunRecord,
        *,
        desktop: ResolvedDesktop,
        task: ResolvedTask,
    ) -> RunRecord:
        dispatching = await self._transition(record, RunState.DISPATCHING)
        if dispatching is None:
            latest = await self.run_store.get_authorized(
                tenant_id=record.tenant_id,
                run_id=record.run_id,
            )
            if latest is None:
                raise ObjectNotFound()
            return latest
        try:
            call_id = await self.dispatcher.spawn(
                desktop=desktop,
                task=task,
                run_id=dispatching.run_id,
            )
            running = await self._transition(
                dispatching,
                RunState.RUNNING,
                function_call_id=call_id,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._transition_or_reload(dispatching, RunState.INDETERMINATE)
            )
            raise
        except Exception:
            return await self._transition_or_reload(dispatching, RunState.INDETERMINATE)
        if running is not None:
            return running
        return await self._transition_or_reload(dispatching, RunState.INDETERMINATE)

    async def _reconcile_stale_admission(self, record: RunRecord) -> tuple[RunRecord, bool]:
        if self.clock() - record.updated_at < self.stale_after:
            return record, False
        if record.state is RunState.RESERVED:
            return record, True
        if record.state is RunState.DISPATCHING:
            return await self._transition_or_reload(record, RunState.INDETERMINATE), False
        return record, False

    async def _transition(
        self,
        record: RunRecord,
        next_state: RunState,
        *,
        function_call_id: FunctionCallIdentity | None = None,
    ) -> RunRecord | None:
        next_record = record.transition(
            next_state,
            now=self.clock(),
            function_call_id=function_call_id,
        )
        return await self.run_store.compare_and_set(current=record, next_record=next_record)

    async def _transition_or_reload(
        self,
        record: RunRecord,
        next_state: RunState,
    ) -> RunRecord:
        transitioned = await self._transition(record, next_state)
        if transitioned is not None:
            return transitioned
        latest = await self.run_store.get_authorized(
            tenant_id=record.tenant_id,
            run_id=record.run_id,
        )
        if latest is None:
            raise ObjectNotFound()
        return latest


def admission_fingerprint(*, desktop_key: str, task_key: str) -> str:
    canonical = json.dumps(
        [desktop_key, task_key],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
