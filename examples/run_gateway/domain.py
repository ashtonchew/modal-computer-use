"""Secret-safe immutable types and run-state invariants."""

from __future__ import annotations

import hashlib
import hmac
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
CAPACITY_RELEASING_STATES = frozenset(
    {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
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


class IdentityKeyUnavailable(RuntimeError):
    """A retained binding cannot be verified by the configured keyring."""

    def __init__(self) -> None:
        super().__init__("retained identity key version is unavailable")


class IdentityKind(StrEnum):
    IDEMPOTENCY = "idempotency"
    DESKTOP = "desktop"
    TASK = "task"


def _validate_key_id(value: str) -> None:
    if not 1 <= len(value) <= 64 or not value.isascii():
        raise ValueError("identity key ID must be 1-64 ASCII characters")
    if any(not (character.isalnum() or character in "._-") for character in value):
        raise ValueError("identity key ID contains an unsupported character")


@dataclass(frozen=True)
class IdentityKey:
    """One versioned HMAC key. Secret material is never printable."""

    key_id: str
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_key_id(self.key_id)
        if type(self.secret) is not bytes or len(self.secret) < 32:
            raise ValueError("identity HMAC secret must contain at least 32 bytes")


@dataclass(frozen=True)
class KeyedDigest:
    """A persistable key-versioned HMAC digest."""

    key_id: str
    digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_key_id(self.key_id)
        if type(self.digest) is not bytes or len(self.digest) != hashlib.sha256().digest_size:
            raise ValueError("keyed digest must be exactly 32 bytes")

    def matches(self, candidate: KeyedDigest) -> bool:
        return hmac.compare_digest(self.key_id, candidate.key_id) and hmac.compare_digest(
            self.digest, candidate.digest
        )


@dataclass(frozen=True)
class IdentityProof:
    """Active-key mint plus all active/retiring verification candidates."""

    mint: KeyedDigest = field(repr=False)
    verification_candidates: tuple[KeyedDigest, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.verification_candidates:
            raise ValueError("identity proof requires verification candidates")
        ids = tuple(candidate.key_id for candidate in self.verification_candidates)
        if len(ids) != len(set(ids)) or self.mint.key_id != ids[0]:
            raise ValueError("identity proof candidates must be unique and active-first")
        if not self.mint.matches(self.verification_candidates[0]):
            raise ValueError("identity proof mint must equal its active candidate")

    def matches(self, stored: KeyedDigest) -> bool:
        """Match a stored active or retiring digest in constant time."""

        matched = False
        for candidate in self.verification_candidates:
            same_key = hmac.compare_digest(candidate.key_id, stored.key_id)
            same_digest = hmac.compare_digest(candidate.digest, stored.digest)
            matched = matched or (same_key and same_digest)
        return matched

    def recognizes_key(self, stored: KeyedDigest) -> bool:
        """Whether this proof can verify a retained binding's key version."""

        return any(
            hmac.compare_digest(candidate.key_id, stored.key_id)
            for candidate in self.verification_candidates
        )


@dataclass(frozen=True)
class IdentityKeyring:
    """Strict HMAC-SHA256 keyring with exactly one active minting key."""

    active: IdentityKey = field(repr=False)
    retiring: tuple[IdentityKey, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.active, IdentityKey):
            raise ValueError("identity keyring requires exactly one active key")
        if not isinstance(self.retiring, tuple) or any(
            not isinstance(key, IdentityKey) for key in self.retiring
        ):
            raise ValueError("retiring identity keys must be a tuple of IdentityKey values")
        ids = (self.active.key_id, *(key.key_id for key in self.retiring))
        if len(ids) != len(set(ids)):
            raise ValueError("identity key IDs must be unique")

    def prove(self, *, tenant_id: str, kind: IdentityKind, value: str) -> IdentityProof:
        if not tenant_id or not value:
            raise ValueError("identity binding inputs must be non-empty")
        if not isinstance(kind, IdentityKind):
            raise ValueError("identity binding kind must be explicit")
        candidates = tuple(
            _keyed_digest(key, tenant_id=tenant_id, kind=kind, value=value)
            for key in (self.active, *self.retiring)
        )
        return IdentityProof(mint=candidates[0], verification_candidates=candidates)


def _keyed_digest(
    key: IdentityKey, *, tenant_id: str, kind: IdentityKind, value: str
) -> KeyedDigest:
    # Fixed domain plus length-prefixed components prevents concatenation and
    # cross-purpose ambiguity without persisting any source identity.
    components = (
        b"modal-computer-use/run-admission/v1",
        tenant_id.encode(),
        kind.value.encode(),
        value.encode(),
    )
    message = b"".join(len(component).to_bytes(8, "big") + component for component in components)
    return KeyedDigest(
        key_id=key.key_id,
        digest=hmac.digest(key.secret, message, "sha256"),
    )


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
    internal_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.internal_id:
            raise ValueError("resolved desktop requires a stable application identity")


@dataclass(frozen=True)
class ResolvedTask:
    text: str = field(repr=False)
    internal_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.text or not self.internal_id:
            raise ValueError("resolved task requires text and a stable application identity")


@dataclass(frozen=True)
class RunRecord:
    """Private durable record. Source identities and task/desktop content are absent."""

    run_id: str
    tenant_id: str
    idempotency_binding: KeyedDigest = field(repr=False)
    desktop_binding: KeyedDigest = field(repr=False)
    task_binding: KeyedDigest = field(repr=False)
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
        idempotency_binding: KeyedDigest,
        desktop_binding: KeyedDigest,
        task_binding: KeyedDigest,
        now: datetime,
    ) -> RunRecord:
        return cls(
            run_id=run_id,
            tenant_id=tenant_id,
            idempotency_binding=idempotency_binding,
            desktop_binding=desktop_binding,
            task_binding=task_binding,
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


class DispatchIntentState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"


@dataclass(frozen=True)
class DispatchIntent:
    """Payload-free durable permission candidate for one admitted run."""

    run_id: str
    state: DispatchIntentState

    @classmethod
    def pending(cls, run_id: str) -> DispatchIntent:
        return cls(run_id=run_id, state=DispatchIntentState.PENDING)


@dataclass(frozen=True)
class DispatchClaim:
    record: RunRecord
    intent: DispatchIntent

    def __post_init__(self) -> None:
        if self.record.state is not RunState.DISPATCHING:
            raise ValueError("dispatch claim record must be dispatching")
        if self.intent.state is not DispatchIntentState.CLAIMED:
            raise ValueError("dispatch claim intent must be claimed")
        if self.record.run_id != self.intent.run_id:
            raise ValueError("dispatch claim run identities must agree")


class AdmissionDisposition(StrEnum):
    ADMITTED = "admitted"
    REPLAYED = "replayed"


class AdmissionRejection(StrEnum):
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    DESKTOP_BUSY = "desktop_busy"
    TENANT_QUOTA_EXCEEDED = "tenant_quota_exceeded"


@dataclass(frozen=True)
class AdmissionCommand:
    proposed: RunRecord
    idempotency: IdentityProof = field(repr=False)
    desktop: IdentityProof = field(repr=False)
    task: IdentityProof = field(repr=False)
    pending_intent: DispatchIntent

    def __post_init__(self) -> None:
        if self.pending_intent.run_id != self.proposed.run_id:
            raise ValueError("pending intent must belong to proposed run")
        if self.pending_intent.state is not DispatchIntentState.PENDING:
            raise ValueError("admission requires a pending dispatch intent")
        bindings = (
            (self.idempotency.mint, self.proposed.idempotency_binding),
            (self.desktop.mint, self.proposed.desktop_binding),
            (self.task.mint, self.proposed.task_binding),
        )
        if any(not proof.matches(stored) for proof, stored in bindings):
            raise ValueError("proposed record must use active identity bindings")


@dataclass(frozen=True)
class AdmissionAccepted:
    disposition: AdmissionDisposition
    record: RunRecord
    intent: DispatchIntent


@dataclass(frozen=True)
class AdmissionDenied:
    rejection: AdmissionRejection


type AdmissionResult = AdmissionAccepted | AdmissionDenied


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
    CANCELLED = "terminated"


class PollReason(StrEnum):
    FUNCTION_TIMEOUT = "function_timeout"
    OUTPUT_EXPIRED = "output_expired"
    MISSING_CALL = "missing_call"
    CALL_GRAPH_UNAVAILABLE = "call_graph_unavailable"
    INVALID_OUTCOME = "invalid_outcome"
    RESULT_DATA_LOSS = "result_data_loss"
    TRANSIENT_PROVIDER_ERROR = "transient_provider_error"


@dataclass(frozen=True)
class PollOutcome:
    state: PollState
    reason: PollReason | None = None
