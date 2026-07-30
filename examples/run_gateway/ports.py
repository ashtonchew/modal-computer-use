"""Application-owned dependency ports for the run gateway."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from fastapi import Request

from .domain import (
    AdmissionCommand,
    AdmissionResult,
    CancelOutcome,
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

if TYPE_CHECKING:
    from .recovery import (
        OperatorResolution,
        OperatorResolutionResult,
        PruneResult,
        ReconcileClaim,
        ReconcileCursor,
        ReconcilePage,
        RetentionPolicy,
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
    entering SUCCEEDED, FAILED, or CANCELLED. A RESERVED -> CANCELLED write also
    revokes its pending dispatch intent in that same transaction. INDETERMINATE
    deliberately retains both claims because ownership is not known to be safe.
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

    async def claim_reconcile_page(
        self,
        *,
        cursor: ReconcileCursor | None,
        now: datetime,
        limit: int,
        lease_duration: timedelta,
    ) -> ReconcilePage:
        """Atomically keyset-scan due runs and lease each returned record."""
        ...

    async def reconcile_compare_and_set(
        self,
        *,
        claim: ReconcileClaim,
        next_record: RunRecord,
    ) -> RunRecord | None:
        """Write only while both the opaque lease and expected version remain current.

        Expiring RESERVED -> CANCELLED also revokes its pending dispatch intent in
        this transaction.
        """
        ...

    async def release_reconcile_lease(self, *, claim: ReconcileClaim) -> bool: ...

    async def resolve_indeterminate(
        self, *, resolution: OperatorResolution, now: datetime
    ) -> OperatorResolutionResult | None:
        """Atomically seal an operator SAFE_RELEASE or SAFE_REPLACE decision."""
        ...

    async def prune_safe_terminal_page(
        self,
        *,
        cursor: ReconcileCursor | None,
        now: datetime,
        policy: RetentionPolicy,
    ) -> PruneResult:
        """Prune only released terminal rows while retaining replay tombstones."""
        ...


class TrajectoryDispatcher(Protocol):
    async def spawn(
        self,
        *,
        desktop: ResolvedDesktop,
        task: ResolvedTask,
        run_id: str,
        deadline_at: datetime,
    ) -> FunctionCallIdentity: ...

    async def poll(self, call_id: FunctionCallIdentity) -> PollOutcome | PollState: ...

    async def cancel(self, call_id: FunctionCallIdentity) -> CancelOutcome: ...
