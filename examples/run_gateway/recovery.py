"""Bounded, lease-fenced application recovery for hosted runs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .domain import (
    AdmissionCommand,
    CancelOutcome,
    CancelState,
    FunctionCallIdentity,
    PollOutcome,
    PollReason,
    PollState,
    RunRecord,
    RunState,
    TerminalReason,
)
from .ports import RunStore, TrajectoryDispatcher


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _positive(value: int | timedelta, name: str) -> None:
    if value <= (0 if isinstance(value, int) else timedelta(0)):
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class RecoveryPolicy:
    page_size: int = 100
    max_pages_per_tick: int = 10
    poll_interval: timedelta = timedelta(seconds=15)
    provider_backoff_initial: timedelta = timedelta(seconds=5)
    provider_backoff_max: timedelta = timedelta(minutes=2)
    max_provider_errors: int = 8
    cancel_backoff_initial: timedelta = timedelta(seconds=5)
    cancel_backoff_max: timedelta = timedelta(minutes=1)
    max_cancel_errors: int = 6
    cancellation_grace: timedelta = timedelta(minutes=5)
    lease_duration: timedelta = timedelta(seconds=55)
    dispatch_stale_after: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        for name in ("page_size", "max_pages_per_tick", "max_provider_errors", "max_cancel_errors"):
            _positive(getattr(self, name), name)
        for name in (
            "poll_interval",
            "provider_backoff_initial",
            "provider_backoff_max",
            "cancel_backoff_initial",
            "cancel_backoff_max",
            "cancellation_grace",
            "lease_duration",
            "dispatch_stale_after",
        ):
            _positive(getattr(self, name), name)
        if self.provider_backoff_initial > self.provider_backoff_max:
            raise ValueError("provider backoff initial must not exceed its maximum")
        if self.cancel_backoff_initial > self.cancel_backoff_max:
            raise ValueError("cancel backoff initial must not exceed its maximum")


@dataclass(frozen=True)
class ReconcileCursor:
    """Store-defined keyset position; its contents are intentionally opaque."""

    _value: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self._value:
            raise ValueError("reconcile cursor must not be empty")

    def reveal_to_store(self) -> bytes:
        return self._value

    def __repr__(self) -> str:
        return "ReconcileCursor(<redacted>)"


@dataclass(frozen=True)
class LeaseToken:
    _value: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self._value) < 16:
            raise ValueError("lease token must contain at least 16 bytes")

    def reveal_to_store(self) -> bytes:
        return self._value

    def __repr__(self) -> str:
        return "LeaseToken(<redacted>)"


@dataclass(frozen=True)
class ReconcileClaim:
    record: RunRecord
    expected_version: int
    lease_token: LeaseToken = field(repr=False)
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if self.expected_version != self.record.version:
            raise ValueError("claim version must match its record")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("lease expiry must be timezone-aware")

    def advanced(self, record: RunRecord) -> ReconcileClaim:
        if record.run_id != self.record.run_id:
            raise ValueError("advanced claim must retain its run identity")
        return ReconcileClaim(record, record.version, self.lease_token, self.lease_expires_at)


@dataclass(frozen=True)
class ReconcilePage:
    claims: tuple[ReconcileClaim, ...]
    next_cursor: ReconcileCursor | None


class OperatorResolutionAction(StrEnum):
    SAFE_RELEASE = "safe_release"
    SAFE_REPLACE = "safe_replace"


@dataclass(frozen=True)
class OperatorResolution:
    run_id: str
    expected_version: int
    action: OperatorResolutionAction
    actor: str = field(repr=False)
    reason: str = field(repr=False)
    audit_identity: str = field(repr=False)
    replacement: AdmissionCommand | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id or self.expected_version < 0:
            raise ValueError("operator resolution requires a run and expected version")
        if not self.actor or not self.reason or not self.audit_identity:
            raise ValueError("operator resolution requires actor, reason, and audit identity")
        if (self.action is OperatorResolutionAction.SAFE_REPLACE) != (
            self.replacement is not None
        ):
            raise ValueError("SAFE_REPLACE requires exactly one fresh replacement admission")
        if self.replacement is not None and self.replacement.proposed.run_id == self.run_id:
            raise ValueError("SAFE_REPLACE must use a fresh successor run identity")


@dataclass(frozen=True)
class OperatorResolutionResult:
    sealed: RunRecord
    successor: RunRecord | None = None


@dataclass(frozen=True)
class RetentionPolicy:
    terminal_retention: timedelta
    replay_fence_window: timedelta
    page_size: int = 100

    def __post_init__(self) -> None:
        _positive(self.terminal_retention, "terminal_retention")
        _positive(self.replay_fence_window, "replay_fence_window")
        _positive(self.page_size, "page_size")


@dataclass(frozen=True)
class PruneResult:
    pruned: int
    next_cursor: ReconcileCursor | None


@dataclass
class RunReconciler:
    store: RunStore
    dispatcher: TrajectoryDispatcher
    policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    clock: Callable[[], datetime] = _utc_now

    async def reconcile(self) -> int:
        cursor: ReconcileCursor | None = None
        processed = 0
        scan_now = self.clock()
        for _ in range(self.policy.max_pages_per_tick):
            page = await self.store.claim_reconcile_page(
                cursor=cursor,
                now=scan_now,
                limit=self.policy.page_size,
                lease_duration=self.policy.lease_duration,
            )
            for claim in page.claims:
                try:
                    await self._reconcile_claim(claim)
                finally:
                    await self.store.release_reconcile_lease(claim=claim)
                processed += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        return processed

    async def _reconcile_claim(self, claim: ReconcileClaim) -> None:
        record = claim.record
        now = self.clock()
        if record.state is RunState.RESERVED:
            if now >= record.deadline_at:
                await self._commit(
                    claim,
                    record.transition(
                        RunState.CANCELLED,
                        now=now,
                        terminal_reason=TerminalReason.EXPIRED_BEFORE_DISPATCH,
                    ),
                )
            else:
                await self._commit(
                    claim,
                    record.reschedule(now=now, reconcile_at=record.deadline_at),
                )
            return
        if record.state is RunState.DISPATCHING:
            if (
                now >= record.deadline_at
                or now - record.state_changed_at >= self.policy.dispatch_stale_after
            ):
                await self._commit(
                    claim,
                    record.transition(
                        RunState.INDETERMINATE,
                        now=now,
                        terminal_reason=TerminalReason.DISPATCH_AMBIGUOUS,
                    ),
                )
            else:
                # The atomic dispatch claim schedules the stale boundary. A fresh
                # in-flight spawn must retain its version so its call ID can commit.
                return
            return
        if record.state is RunState.RUNNING:
            if now >= record.deadline_at:
                requested = record.transition(
                    RunState.CANCELLATION_REQUESTED,
                    now=now,
                    reconcile_at=now,
                    cancellation_requested_at=now,
                    cancellation_deadline_at=now + self.policy.cancellation_grace,
                )
                committed = await self._commit(claim, requested)
                if committed is not None:
                    await self._poll_cancellation(claim.advanced(committed))
                return
            await self._poll(claim)
            return
        if record.state is RunState.CANCELLATION_REQUESTED:
            await self._poll_cancellation(claim)

    async def _poll(self, claim: ReconcileClaim) -> None:
        record = claim.record
        call_id = record.function_call_id
        if call_id is None:
            await self._indeterminate(
                claim,
                self.clock(),
                TerminalReason.PROVIDER_AMBIGUOUS,
            )
            return
        outcome = await self._safe_poll(call_id)
        now = self.clock()
        if (
            outcome.state in {PollState.PENDING, PollState.UNAVAILABLE}
            and now >= record.deadline_at
        ):
            requested = record.transition(
                RunState.CANCELLATION_REQUESTED,
                now=now,
                reconcile_at=now,
                cancellation_requested_at=now,
                cancellation_deadline_at=now + self.policy.cancellation_grace,
            )
            committed = await self._commit(claim, requested)
            if committed is not None:
                await self._cancel(claim.advanced(committed))
            return
        if outcome.state is PollState.PENDING:
            await self._commit(
                claim,
                record.reschedule(
                    now=now,
                    reconcile_at=min(record.deadline_at, now + self.policy.poll_interval),
                    provider_error_count=0,
                ),
            )
        elif outcome.state is PollState.UNAVAILABLE:
            await self._provider_unavailable(claim, now=now)
        else:
            await self._finish_from_poll(claim, outcome, now=now)

    async def _poll_cancellation(self, claim: ReconcileClaim) -> None:
        record = claim.record
        call_id = record.function_call_id
        if call_id is None:
            await self._indeterminate(
                claim,
                self.clock(),
                TerminalReason.PROVIDER_AMBIGUOUS,
            )
            return
        outcome = await self._safe_poll(call_id)
        now = self.clock()
        if outcome.state not in {PollState.PENDING, PollState.UNAVAILABLE}:
            await self._finish_from_poll(claim, outcome, now=now)
            return
        deadline = record.cancellation_deadline_at
        if deadline is None or now >= deadline:
            await self._indeterminate(claim, now, TerminalReason.CANCELLATION_DEADLINE)
            return
        if outcome.state is PollState.UNAVAILABLE:
            advanced = await self._provider_unavailable(claim, now=now, keep_due=now)
            if advanced is None or advanced.state is RunState.INDETERMINATE:
                return
            claim = claim.advanced(advanced)
        elif record.provider_error_count:
            reset = record.reschedule(now=now, reconcile_at=now, provider_error_count=0)
            advanced = await self._commit(claim, reset)
            if advanced is None:
                return
            claim = claim.advanced(advanced)
        await self._cancel(claim)

    async def _cancel(self, claim: ReconcileClaim) -> None:
        record = claim.record
        now = self.clock()
        if record.cancellation_deadline_at is None or now >= record.cancellation_deadline_at:
            await self._indeterminate(claim, now, TerminalReason.CANCELLATION_DEADLINE)
            return
        call_id = record.function_call_id
        if call_id is None:
            await self._indeterminate(claim, now, TerminalReason.PROVIDER_AMBIGUOUS)
            return
        try:
            outcome = await self.dispatcher.cancel(call_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = CancelOutcome(CancelState.UNAVAILABLE)
        now = self.clock()
        if now >= record.cancellation_deadline_at:
            await self._indeterminate(claim, now, TerminalReason.CANCELLATION_DEADLINE)
            return
        if not isinstance(outcome, CancelOutcome):
            outcome = CancelOutcome(CancelState.INDETERMINATE)
        if outcome.state is CancelState.INDETERMINATE:
            await self._indeterminate(claim, now, TerminalReason.CANCELLATION_AMBIGUOUS)
            return
        if outcome.state is CancelState.UNAVAILABLE:
            count = record.cancel_error_count + 1
            if count >= self.policy.max_cancel_errors:
                await self._indeterminate(claim, now, TerminalReason.CANCELLATION_ERROR_CAP)
                return
            delay = _backoff(
                self.policy.cancel_backoff_initial, self.policy.cancel_backoff_max, count
            )
            due = now + delay
            if record.cancellation_deadline_at is not None:
                due = min(due, record.cancellation_deadline_at)
            await self._commit(
                claim,
                record.reschedule(
                    now=now,
                    reconcile_at=due,
                    cancel_error_count=count,
                    cancel_last_attempt_at=now,
                ),
            )
            return
        due = now + self.policy.poll_interval
        if record.cancellation_deadline_at is not None:
            due = min(due, record.cancellation_deadline_at)
        await self._commit(
            claim,
            record.reschedule(
                now=now,
                reconcile_at=due,
                cancel_error_count=0,
                cancel_last_attempt_at=now,
            ),
        )

    async def _provider_unavailable(
        self, claim: ReconcileClaim, *, now: datetime, keep_due: datetime | None = None
    ) -> RunRecord | None:
        record = claim.record
        count = record.provider_error_count + 1
        if count >= self.policy.max_provider_errors:
            return await self._indeterminate(claim, now, TerminalReason.PROVIDER_ERROR_CAP)
        delay = _backoff(
            self.policy.provider_backoff_initial, self.policy.provider_backoff_max, count
        )
        due = keep_due or min(record.deadline_at, now + delay)
        return await self._commit(
            claim,
            record.reschedule(
                now=now,
                reconcile_at=due,
                provider_error_count=count,
            ),
        )

    async def _finish_from_poll(
        self, claim: ReconcileClaim, outcome: PollOutcome, *, now: datetime
    ) -> None:
        if outcome.state is PollState.SUCCEEDED:
            state = RunState.SUCCEEDED
            reason = TerminalReason.TRAJECTORY_SUCCEEDED
        elif outcome.state is PollState.FAILED:
            state = RunState.FAILED
            reason = (
                TerminalReason.FUNCTION_TIMEOUT
                if outcome.reason is PollReason.FUNCTION_TIMEOUT
                else TerminalReason.TRAJECTORY_FAILED
            )
        elif outcome.state is PollState.TERMINATED:
            state = (
                RunState.CANCELLED
                if claim.record.state is RunState.CANCELLATION_REQUESTED
                else RunState.FAILED
            )
            reason = TerminalReason.TERMINATED
        else:
            await self._indeterminate(claim, now, TerminalReason.PROVIDER_AMBIGUOUS)
            return
        await self._commit(
            claim,
            claim.record.transition(state, now=now, terminal_reason=reason),
        )

    async def _safe_poll(self, call_id: FunctionCallIdentity) -> PollOutcome:
        try:
            observed = await self.dispatcher.poll(call_id)
            return observed if isinstance(observed, PollOutcome) else PollOutcome(observed)
        except asyncio.CancelledError:
            raise
        except Exception:
            return PollOutcome(PollState.UNAVAILABLE)

    async def _indeterminate(
        self, claim: ReconcileClaim, now: datetime, reason: TerminalReason
    ) -> RunRecord | None:
        return await self._commit(
            claim,
            claim.record.transition(RunState.INDETERMINATE, now=now, terminal_reason=reason),
        )

    async def _commit(
        self, claim: ReconcileClaim, next_record: RunRecord
    ) -> RunRecord | None:
        return await self.store.reconcile_compare_and_set(
            claim=claim,
            next_record=next_record,
        )


def _backoff(initial: timedelta, maximum: timedelta, count: int) -> timedelta:
    return min(maximum, initial * (2 ** (count - 1)))


__all__ = [
    "LeaseToken",
    "OperatorResolution",
    "OperatorResolutionAction",
    "OperatorResolutionResult",
    "PruneResult",
    "ReconcileClaim",
    "ReconcileCursor",
    "ReconcilePage",
    "RecoveryPolicy",
    "RetentionPolicy",
    "RunReconciler",
]
