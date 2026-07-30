"""Application-owned async run gateway for one deployed Modal trajectory Function.

This is a reference control plane, not a production multi-tenant service. The
application must inject authentication, object-ownership catalogs, and an atomic
durable run store. Modal proxy authentication is only an outer Workspace or
Environment guard; it is not object-level authorization.

The HTTP surface accepts opaque application keys and never accepts or returns a
Sandbox ID, session handle, Function name, FunctionCall ID, endpoint, or token.
It deliberately does not proxy screenshot, action, task, or result content.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from modal_computer_use import ComputerSessionHandle


class RunState(StrEnum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


TERMINAL_STATES = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.INDETERMINATE,
    }
)

LEGAL_TRANSITIONS = frozenset(
    {
        (RunState.RESERVED, RunState.DISPATCHING),
        (RunState.DISPATCHING, RunState.RUNNING),
        (RunState.DISPATCHING, RunState.INDETERMINATE),
        (RunState.RUNNING, RunState.SUCCEEDED),
        (RunState.RUNNING, RunState.FAILED),
        (RunState.RUNNING, RunState.CANCELLATION_REQUESTED),
        (RunState.RUNNING, RunState.INDETERMINATE),
        (RunState.CANCELLATION_REQUESTED, RunState.CANCELLED),
        (RunState.CANCELLATION_REQUESTED, RunState.SUCCEEDED),
        (RunState.CANCELLATION_REQUESTED, RunState.FAILED),
        (RunState.CANCELLATION_REQUESTED, RunState.INDETERMINATE),
    }
)


class StateTransitionError(RuntimeError):
    """Raised before storage when code attempts an edge outside the closed graph."""


@dataclass(frozen=True)
class FunctionCallIdentity:
    """Private provider identity whose string representations are always redacted."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self._value:
            raise ValueError("provider call identity must not be empty")

    def reveal_to_backend(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "FunctionCallIdentity(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    principal_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.principal_id:
            raise ValueError("principal identity must be stable and non-empty")


@dataclass(frozen=True)
class ResolvedDesktop:
    handle: ComputerSessionHandle = field(repr=False)


@dataclass(frozen=True)
class ResolvedTask:
    text: str = field(repr=False)


@dataclass(frozen=True)
class RunRecord:
    """Private durable record. Task and desktop content are intentionally absent."""

    run_id: str
    tenant_id: str
    idempotency_key: str
    admission_fingerprint: str = field(repr=False)
    state: RunState
    created_at: datetime
    updated_at: datetime
    version: int = 0
    function_call_id: FunctionCallIdentity | None = field(default=None, repr=False)

    @classmethod
    def reserve(
        cls,
        *,
        run_id: str,
        tenant_id: str,
        idempotency_key: str,
        admission_fingerprint: str,
        now: datetime,
    ) -> RunRecord:
        return cls(
            run_id=run_id,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            admission_fingerprint=admission_fingerprint,
            state=RunState.RESERVED,
            created_at=now,
            updated_at=now,
        )

    def transition(
        self,
        next_state: RunState,
        *,
        now: datetime,
        function_call_id: FunctionCallIdentity | None = None,
    ) -> RunRecord:
        if (self.state, next_state) not in LEGAL_TRANSITIONS:
            raise StateTransitionError(
                f"illegal run state transition: {self.state.value} -> {next_state.value}"
            )
        next_call_id = self.function_call_id
        if self.state is RunState.DISPATCHING and next_state is RunState.RUNNING:
            if function_call_id is None:
                raise StateTransitionError(
                    "dispatching -> running requires a private call identity"
                )
            next_call_id = function_call_id
        elif function_call_id is not None:
            raise StateTransitionError(
                "a private call identity may only be set when entering running"
            )
        return replace(
            self,
            state=next_state,
            updated_at=now,
            version=self.version + 1,
            function_call_id=next_call_id,
        )


@dataclass(frozen=True)
class Reservation:
    record: RunRecord
    created: bool


class PrincipalResolver(Protocol):
    async def resolve(self, request: Request) -> Principal: ...


class SessionCatalog(Protocol):
    async def resolve(self, principal: Principal, desktop_key: str) -> ResolvedDesktop: ...


class TaskCatalog(Protocol):
    async def resolve(self, principal: Principal, task_key: str) -> ResolvedTask: ...


class RunStore(Protocol):
    """Application-supplied durable store with atomic reservation and CAS."""

    async def reserve_if_absent(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        proposed: RunRecord,
    ) -> Reservation: ...

    async def get_authorized(self, *, tenant_id: str, run_id: str) -> RunRecord | None: ...

    async def compare_and_set(
        self,
        *,
        current: RunRecord,
        next_record: RunRecord,
    ) -> RunRecord | None: ...


class PollState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


class TrajectoryDispatcher(Protocol):
    async def spawn(
        self,
        *,
        desktop: ResolvedDesktop,
        task: ResolvedTask,
        run_id: str,
    ) -> FunctionCallIdentity: ...

    async def poll(self, call_id: FunctionCallIdentity) -> PollState: ...

    async def cancel(self, call_id: FunctionCallIdentity) -> None: ...


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


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desktop_key: str = Field(min_length=1, max_length=256)
    task_key: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: RunState


class CancelRunResponse(RunResponse):
    cancellation_requested: bool


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

    async def create_run(self, request: Request, body: CreateRunRequest) -> RunRecord:
        principal = await self.principal_resolver.resolve(request)
        desktop = await self.session_catalog.resolve(principal, body.desktop_key)
        task = await self.task_catalog.resolve(principal, body.task_key)
        now = self.clock()
        proposed = RunRecord.reserve(
            run_id=self.run_id_factory(),
            tenant_id=principal.tenant_id,
            idempotency_key=body.idempotency_key,
            admission_fingerprint=_admission_fingerprint(
                desktop_key=body.desktop_key,
                task_key=body.task_key,
            ),
            now=now,
        )
        reservation = await self.run_store.reserve_if_absent(
            tenant_id=principal.tenant_id,
            idempotency_key=body.idempotency_key,
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
        outcome = await self.dispatcher.poll(call_id)
        if outcome is PollState.PENDING:
            return record
        next_state = {
            PollState.SUCCEEDED: RunState.SUCCEEDED,
            PollState.FAILED: RunState.FAILED,
            PollState.CANCELLED: (
                RunState.CANCELLED
                if record.state is RunState.CANCELLATION_REQUESTED
                else RunState.FAILED
            ),
            PollState.INDETERMINATE: RunState.INDETERMINATE,
        }[outcome]
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
            # The provider request may have crossed its boundary. Keep this durable
            # state for an explicit later poll; never infer rollback or retry.
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


class ModalTrajectoryDispatcher:
    """Modal 1.5.2 adapter for one application-owned deployed Function."""

    def __init__(self, function: object) -> None:
        if function is None:
            raise ValueError("a deployed trajectory Function is required")
        self._function = function

    @classmethod
    def from_name(cls, app_name: str, function_name: str) -> ModalTrajectoryDispatcher:
        if modal is None:
            raise ImportError("Modal is required for hosted trajectory dispatch")
        return cls(modal.Function.from_name(app_name, function_name))

    async def spawn(
        self,
        *,
        desktop: ResolvedDesktop,
        task: ResolvedTask,
        run_id: str,
    ) -> FunctionCallIdentity:
        call = await self._function.spawn.aio(desktop.handle, task.text, run_id)
        return FunctionCallIdentity(call.object_id)

    async def poll(self, call_id: FunctionCallIdentity) -> PollState:
        if modal is None:
            raise ImportError("Modal is required for hosted trajectory polling")
        call = modal.FunctionCall.from_id(call_id.reveal_to_backend())
        try:
            await call.get.aio(timeout=0)
        except modal.exception.OutputExpiredError:
            return PollState.INDETERMINATE
        except modal.exception.TimeoutError:
            return PollState.PENDING
        except modal.exception.InputCancellation:
            return PollState.CANCELLED
        except Exception:
            return PollState.FAILED
        return PollState.SUCCEEDED

    async def cancel(self, call_id: FunctionCallIdentity) -> None:
        if modal is None:
            raise ImportError("Modal is required for hosted trajectory cancellation")
        call = modal.FunctionCall.from_id(call_id.reveal_to_backend())
        await call.cancel.aio(terminate_containers=False)


def build_run_gateway_app(service: RunGatewayService) -> FastAPI:
    if not isinstance(service, RunGatewayService):
        raise ValueError("an application-configured RunGatewayService is required")
    app = FastAPI(title="application-owned Modal run gateway", version="1.0.0")

    @app.exception_handler(GatewayError)
    async def gateway_error(_request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default validation response can echo rejected secret input.
        return JSONResponse(status_code=422, content={"error": "invalid_request"})

    @app.post("/v1/runs", response_model=RunResponse, status_code=202)
    async def create_run(body: CreateRunRequest, request: Request) -> RunResponse | JSONResponse:
        try:
            record = await service.create_run(request, body)
            return _run_response(record)
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception:
            return _internal_error_response()

    @app.get("/v1/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str, request: Request) -> RunResponse | JSONResponse:
        try:
            return _run_response(await service.get_run(request, run_id))
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception:
            return _internal_error_response()

    @app.post("/v1/runs/{run_id}/cancel", response_model=CancelRunResponse)
    async def cancel_run(
        run_id: str,
        request: Request,
    ) -> CancelRunResponse | JSONResponse:
        try:
            record, requested = await service.cancel_run(request, run_id)
            return CancelRunResponse(
                run_id=record.run_id,
                state=record.state,
                cancellation_requested=requested,
            )
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception:
            return _internal_error_response()

    return app


def build_default_service() -> RunGatewayService:
    raise RuntimeError(
        "inject the application's PrincipalResolver, SessionCatalog, TaskCatalog, "
        "durable RunStore, and TrajectoryDispatcher before deploying this example"
    )


def _run_response(record: RunRecord) -> RunResponse:
    return RunResponse(run_id=record.run_id, state=record.state)


def _internal_error_response() -> JSONResponse:
    # Dependency and provider exception text may contain application secrets.
    return JSONResponse(status_code=500, content={"error": "internal_error"})


def _admission_fingerprint(*, desktop_key: str, task_key: str) -> str:
    canonical = json.dumps(
        [desktop_key, task_key],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


try:
    import modal
except ImportError:
    modal = None
    app = None
else:
    app = modal.App("application-owned-run-gateway")
    _image = modal.Image.debian_slim().pip_install("modal-computer-use[modal]")

    @app.cls(
        image=_image,
        min_containers=int(os.environ.get("RUN_GATEWAY_MIN_CONTAINERS", "0")),
        scaledown_window=int(os.environ.get("RUN_GATEWAY_SCALEDOWN_WINDOW", "300")),
    )
    @modal.concurrent(max_inputs=100, target_inputs=80)
    class RunGateway:
        @modal.enter()
        def setup(self) -> None:
            self.service = build_default_service()

        @modal.asgi_app(requires_proxy_auth=True)
        def web(self) -> FastAPI:
            return build_run_gateway_app(self.service)


def build_modal_app() -> object:
    if app is None:
        raise ImportError("Modal is required to build the hosted run gateway")
    return app


if __name__ == "__main__":
    raise SystemExit(
        "Adapt build_default_service() with application-owned durable dependencies, "
        "then deploy with Modal."
    )
