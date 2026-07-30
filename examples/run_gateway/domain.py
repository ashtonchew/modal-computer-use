"""Secret-safe immutable types and run-state invariants."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

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
    {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED, RunState.INDETERMINATE}
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
    """Raised before storage for an edge outside the closed transition graph."""


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


class TrajectoryStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class TrajectoryOutcome:
    """The complete allowlisted result contract for a trajectory Function."""

    status: TrajectoryStatus

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status.value}

    @classmethod
    def validate(cls, value: Any) -> TrajectoryOutcome:
        if not isinstance(value, dict) or set(value) != {"status"}:
            raise ValueError("invalid trajectory outcome")
        status = value.get("status")
        if not isinstance(status, str):
            raise ValueError("invalid trajectory outcome")
        try:
            return cls(status=TrajectoryStatus(status))
        except ValueError as exc:
            raise ValueError("invalid trajectory outcome") from exc


class PollState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TERMINATED = "terminated"
    INDETERMINATE = "indeterminate"
    UNAVAILABLE = "unavailable"
    # Compatibility name for application dispatchers predating call-graph polling.
    CANCELLED = "terminated"


class PollReason(StrEnum):
    FUNCTION_TIMEOUT = "function_timeout"
    OUTPUT_EXPIRED = "output_expired"
    MISSING_CALL = "missing_call"
    MALFORMED_CALL_GRAPH = "malformed_call_graph"
    INVALID_OUTCOME = "invalid_outcome"
    RESULT_DATA_LOSS = "result_data_loss"
    TRANSIENT_PROVIDER_ERROR = "transient_provider_error"


@dataclass(frozen=True)
class PollOutcome:
    state: PollState
    reason: PollReason | None = None
