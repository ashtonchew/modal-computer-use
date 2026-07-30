"""Application-owned dependency ports for the run gateway."""

from __future__ import annotations

from typing import Protocol

from fastapi import Request

from .domain import (
    FunctionCallIdentity,
    PollOutcome,
    PollState,
    Principal,
    Reservation,
    ResolvedDesktop,
    ResolvedTask,
    RunRecord,
)


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


class TrajectoryDispatcher(Protocol):
    async def spawn(
        self,
        *,
        desktop: ResolvedDesktop,
        task: ResolvedTask,
        run_id: str,
    ) -> FunctionCallIdentity: ...

    async def poll(self, call_id: FunctionCallIdentity) -> PollOutcome | PollState: ...

    async def cancel(self, call_id: FunctionCallIdentity) -> None: ...
