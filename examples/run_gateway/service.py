"""Create, observe, and cancel orchestration over application-owned ports."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fastapi import Request

from .domain import (
    TERMINAL_STATES,
    AdmissionAccepted,
    AdmissionCommand,
    AdmissionRejection,
    DispatchIntent,
    FunctionCallIdentity,
    IdentityKeyring,
    IdentityKind,
    Principal,
    ReplayTombstone,
    ResolvedDesktop,
    ResolvedTask,
    RunRecord,
    RunState,
    TerminalReason,
)
from .ports import PrincipalResolver, RunStore, SessionCatalog, TaskCatalog, TrajectoryDispatcher
from .recovery import RecoveryPolicy


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


class DesktopBusy(GatewayError):
    def __init__(self) -> None:
        super().__init__(status_code=409, code="desktop_busy")


class TenantQuotaExceeded(GatewayError):
    def __init__(self) -> None:
        super().__init__(status_code=429, code="tenant_quota_exceeded")


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunGatewayService:
    principal_resolver: PrincipalResolver
    session_catalog: SessionCatalog
    task_catalog: TaskCatalog
    run_store: RunStore
    dispatcher: TrajectoryDispatcher
    identity_keyring: IdentityKeyring
    run_timeout: timedelta = timedelta(hours=1)
    recovery_policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    clock: Callable[[], datetime] = utc_now
    run_id_factory: Callable[[], str] = lambda: f"run_{secrets.token_urlsafe(24)}"

    def __post_init__(self) -> None:
        required = (
            self.principal_resolver,
            self.session_catalog,
            self.task_catalog,
            self.run_store,
            self.dispatcher,
            self.identity_keyring,
        )
        if any(dependency is None for dependency in required):
            raise ValueError(
                "all authentication, authorization, storage, and dispatch dependencies "
                "are required"
            )
        if self.run_timeout <= timedelta(0):
            raise ValueError("run_timeout must be positive")

    async def create_run(
        self, request: Request, body: CreateRunBody
    ) -> RunRecord | ReplayTombstone:
        principal = await self.principal_resolver.resolve(request)
        desktop_key = body.desktop_key
        task_key = body.task_key
        desktop = await self.session_catalog.resolve(principal, desktop_key)
        task = await self.task_catalog.resolve(principal, task_key)
        idempotency = self.identity_keyring.prove(
            tenant_id=principal.tenant_id,
            kind=IdentityKind.IDEMPOTENCY,
            value=body.idempotency_key,
        )
        desktop_identity = self.identity_keyring.prove(
            tenant_id=principal.tenant_id,
            kind=IdentityKind.DESKTOP,
            value=desktop.internal_id,
        )
        task_identity = self.identity_keyring.prove(
            tenant_id=principal.tenant_id,
            kind=IdentityKind.TASK,
            value=task.internal_id,
        )
        now = self.clock()
        proposed = RunRecord.reserve(
            run_id=self.run_id_factory(),
            tenant_id=principal.tenant_id,
            idempotency_binding=idempotency.mint,
            desktop_binding=desktop_identity.mint,
            task_binding=task_identity.mint,
            now=now,
            deadline_at=now + self.run_timeout,
        )
        admission = await self.run_store.admit(
            AdmissionCommand(
                proposed=proposed,
                idempotency=idempotency,
                desktop=desktop_identity,
                task=task_identity,
                pending_intent=DispatchIntent.pending(proposed.run_id),
            )
        )
        if not isinstance(admission, AdmissionAccepted):
            if admission.rejection is AdmissionRejection.IDEMPOTENCY_CONFLICT:
                raise IdempotencyConflict()
            if admission.rejection is AdmissionRejection.DESKTOP_BUSY:
                raise DesktopBusy()
            raise TenantQuotaExceeded()
        if isinstance(admission.record, RunRecord) and admission.record.state is RunState.RESERVED:
            return await self._claim_and_dispatch(
                admission.record,
                pending_intent=admission.intent,
                desktop=desktop,
                task=task,
            )
        return admission.record

    async def get_run(self, request: Request, run_id: str) -> RunRecord:
        principal = await self.principal_resolver.resolve(request)
        return await self._authorized_run(principal, run_id)

    async def cancel_run(self, request: Request, run_id: str) -> tuple[RunRecord, bool]:
        principal = await self.principal_resolver.resolve(request)
        record = await self._authorized_run(principal, run_id)
        if record.state is RunState.CANCELLATION_REQUESTED:
            return record, True
        if record.state in TERMINAL_STATES:
            return record, False
        if record.state is RunState.RESERVED:
            cancelled = await self._transition(
                record,
                RunState.CANCELLED,
                terminal_reason=TerminalReason.CANCELLED_BEFORE_DISPATCH,
            )
            if cancelled is not None:
                return cancelled, True
            latest = await self._authorized_run(principal, run_id)
            return latest, latest.state is RunState.CANCELLED
        if record.state is not RunState.RUNNING:
            raise RunConflict()
        now = self.clock()
        claimed = await self._transition(
            record,
            RunState.CANCELLATION_REQUESTED,
            reconcile_at=now,
            cancellation_requested_at=now,
            cancellation_deadline_at=now + self.recovery_policy.cancellation_grace,
        )
        if claimed is None:
            latest = await self._authorized_run(principal, run_id)
            return latest, latest.state is RunState.CANCELLATION_REQUESTED
        return claimed, True

    async def _authorized_run(self, principal: Principal, run_id: str) -> RunRecord:
        record = await self.run_store.get_authorized(
            tenant_id=principal.tenant_id,
            run_id=run_id,
        )
        if record is None:
            raise ObjectNotFound()
        return record

    async def _claim_and_dispatch(
        self,
        record: RunRecord,
        *,
        pending_intent: DispatchIntent,
        desktop: ResolvedDesktop,
        task: ResolvedTask,
    ) -> RunRecord:
        claim = await self.run_store.claim_dispatch(
            current=record,
            pending_intent=pending_intent,
            now=self.clock(),
        )
        if claim is None:
            latest = await self.run_store.get_authorized(
                tenant_id=record.tenant_id,
                run_id=record.run_id,
            )
            if latest is None:
                raise ObjectNotFound()
            return latest
        dispatching = claim.record
        try:
            call_id = await self.dispatcher.spawn(
                desktop=desktop,
                task=task,
                run_id=dispatching.run_id,
                deadline_at=dispatching.deadline_at,
            )
            running = await self._transition(
                dispatching,
                RunState.RUNNING,
                function_call_id=call_id,
                reconcile_at=self.clock(),
            )
        except Exception:
            return await self._transition_or_reload(
                dispatching,
                RunState.INDETERMINATE,
                terminal_reason=TerminalReason.DISPATCH_AMBIGUOUS,
            )
        if running is not None:
            return running
        return await self._transition_or_reload(
            dispatching,
            RunState.INDETERMINATE,
            terminal_reason=TerminalReason.DISPATCH_AMBIGUOUS,
        )

    async def _transition(
        self,
        record: RunRecord,
        next_state: RunState,
        *,
        function_call_id: FunctionCallIdentity | None = None,
        reconcile_at: datetime | None = None,
        cancellation_requested_at: datetime | None = None,
        cancellation_deadline_at: datetime | None = None,
        terminal_reason: TerminalReason | None = None,
    ) -> RunRecord | None:
        next_record = record.transition(
            next_state,
            now=self.clock(),
            function_call_id=function_call_id,
            reconcile_at=reconcile_at,
            cancellation_requested_at=cancellation_requested_at,
            cancellation_deadline_at=cancellation_deadline_at,
            terminal_reason=terminal_reason,
        )
        return await self.run_store.compare_and_set(current=record, next_record=next_record)

    async def _transition_or_reload(
        self,
        record: RunRecord,
        next_state: RunState,
        *,
        terminal_reason: TerminalReason | None = None,
    ) -> RunRecord:
        transitioned = await self._transition(
            record,
            next_state,
            terminal_reason=terminal_reason,
        )
        if transitioned is not None:
            return transitioned
        latest = await self.run_store.get_authorized(
            tenant_id=record.tenant_id,
            run_id=record.run_id,
        )
        if latest is None:
            raise ObjectNotFound()
        return latest
