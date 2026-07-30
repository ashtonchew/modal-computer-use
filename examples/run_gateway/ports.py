"""Application-owned dependency ports for the run gateway."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fastapi import Request

from .domain import (
    AdmissionCommand,
    AdmissionResult,
    DispatchClaim,
    DispatchIntent,
    FunctionCallIdentity,
    PollOutcome,
    PollState,
    Principal,
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
    """Application durable store owning admission, capacity, and state CAS.

    ``admit`` is one transaction in this fixed conceptual order: retained-key
    availability guard, replay lookup, tenant quota check, exclusive desktop claim,
    run insert, pending intent insert. Before any decision, every retained binding
    relevant to the tenant must use a key version present in the matching command
    proof. An unknown retained version raises ``IdentityKeyUnavailable`` and changes
    nothing; it is an internal configuration/migration failure, not a user conflict.
    A rejection rolls back every phase. ``claim_dispatch`` atomically changes both
    RESERVED -> DISPATCHING and PENDING -> CLAIMED; only its winner may spawn.

    ``compare_and_set`` releases a run's desktop claim and quota exactly once when
    entering SUCCEEDED, FAILED, or CANCELLED. INDETERMINATE deliberately retains
    both because application ownership is not known to be safe to reuse.
    """

    async def admit(self, command: AdmissionCommand) -> AdmissionResult: ...

    async def claim_dispatch(
        self,
        *,
        current: RunRecord,
        pending_intent: DispatchIntent,
        now: datetime,
    ) -> DispatchClaim | None: ...

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
