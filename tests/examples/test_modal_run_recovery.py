from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _load_example():
    path = Path(__file__).resolve().parents[2] / "examples" / "modal_run_gateway.py"
    spec = importlib.util.spec_from_file_location("modal_run_recovery_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gateway = _load_example()


NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _binding(byte: bytes = b"b") -> gateway.KeyedDigest:
    return gateway.KeyedDigest("active", byte * 32)


def _reserved(
    run_id: str,
    *,
    deadline_at: datetime | None = None,
    reconcile_at: datetime | None = None,
    idempotency_binding: gateway.KeyedDigest | None = None,
) -> gateway.RunRecord:
    binding = _binding()
    return gateway.RunRecord.reserve(
        run_id=run_id,
        tenant_id="tenant-a",
        idempotency_binding=idempotency_binding or binding,
        desktop_binding=binding,
        task_binding=binding,
        now=NOW,
        deadline_at=deadline_at or NOW + timedelta(hours=1),
    ).reschedule(now=NOW, reconcile_at=reconcile_at or NOW)


def _running(
    run_id: str,
    *,
    deadline_at: datetime | None = None,
    provider_errors: int = 0,
) -> gateway.RunRecord:
    reserved = _reserved(run_id, deadline_at=deadline_at)
    dispatching = reserved.transition(gateway.RunState.DISPATCHING, now=NOW)
    running = dispatching.transition(
        gateway.RunState.RUNNING,
        now=NOW,
        function_call_id=gateway.FunctionCallIdentity("provider-identity-secret"),
        reconcile_at=NOW,
    )
    return replace(running, provider_error_count=provider_errors)


class LockedRecoveryStore:
    """Test-only transactional model of the recovery storage contract."""

    def __init__(self, records: tuple[gateway.RunRecord, ...] = ()) -> None:
        self._lock = asyncio.Lock()
        self.records = {record.run_id: record for record in records}
        self.intents = {
            record.run_id: (
                gateway.DispatchIntent.pending(record.run_id)
                if record.state is gateway.RunState.RESERVED
                else gateway.DispatchIntent(
                    record.run_id,
                    gateway.DispatchIntentState.CLAIMED,
                )
            )
            for record in records
        }
        self.capacity = {
            run_id
            for run_id, record in self.records.items()
            if record.state not in gateway.CAPACITY_RELEASING_STATES
        }
        self.desktop_claims = {
            run_id: self.records[run_id].desktop_binding for run_id in self.capacity
        }
        self.leases: dict[str, tuple[gateway.LeaseToken, datetime, int]] = {}
        self.release_count: dict[str, int] = {}
        self.audit: dict[str, gateway.OperatorResolution] = {}
        self.tombstones: dict[str, gateway.ReplayTombstone] = {}
        self.now = NOW
        self._token_counter = 0

    async def claim_reconcile_page(self, *, cursor, now, limit, lease_duration):
        async with self._lock:
            self.now = now
            after = self._decode_cursor(cursor)
            due = sorted(
                (
                    record
                    for record in self.records.values()
                    if record.reconcile_at is not None
                    and record.reconcile_at <= now
                    and (record.reconcile_at, record.run_id) > after
                ),
                key=lambda record: (record.reconcile_at, record.run_id),
            )
            claims = []
            scanned = []
            for record in due:
                scanned.append(record)
                lease = self.leases.get(record.run_id)
                if lease is not None and lease[1] > now:
                    continue
                self._token_counter += 1
                token = gateway.LeaseToken(self._token_counter.to_bytes(16, "big"))
                expiry = now + lease_duration
                self.leases[record.run_id] = (token, expiry, record.version)
                claims.append(gateway.ReconcileClaim(record, record.version, token, expiry))
                if len(claims) == limit:
                    break
            has_more = len(scanned) < len(due)
            next_cursor = None
            if has_more and scanned:
                last = scanned[-1]
                value = f"{last.reconcile_at.isoformat()}\0{last.run_id}".encode()
                next_cursor = gateway.ReconcileCursor(value)
            return gateway.ReconcilePage(tuple(claims), next_cursor)

    @staticmethod
    def _decode_cursor(cursor):
        if cursor is None:
            return (datetime.min.replace(tzinfo=UTC), "")
        timestamp, run_id = cursor.reveal_to_store().decode().split("\0", 1)
        return (datetime.fromisoformat(timestamp), run_id)

    async def reconcile_compare_and_set(self, *, claim, next_record):
        async with self._lock:
            lease = self.leases.get(claim.record.run_id)
            stored = self.records.get(claim.record.run_id)
            if (
                lease is None
                or lease[0] != claim.lease_token
                or lease[1] <= self.now
                or lease[2] != claim.expected_version
                or stored is None
                or stored.version != claim.expected_version
                or next_record.version != stored.version + 1
            ):
                return None
            self.records[stored.run_id] = next_record
            self.leases[stored.run_id] = (lease[0], lease[1], next_record.version)
            if (
                stored.state is gateway.RunState.RESERVED
                and next_record.state is gateway.RunState.CANCELLED
            ):
                self.intents[stored.run_id] = gateway.DispatchIntent.revoked(stored.run_id)
            if next_record.state in gateway.CAPACITY_RELEASING_STATES:
                self._release(stored.run_id)
            return next_record

    async def release_reconcile_lease(self, *, claim):
        async with self._lock:
            lease = self.leases.get(claim.record.run_id)
            if lease is None or lease[0] != claim.lease_token:
                return False
            del self.leases[claim.record.run_id]
            return True

    def _release(self, run_id: str) -> None:
        if run_id in self.capacity:
            self.capacity.remove(run_id)
            self.desktop_claims.pop(run_id)
            self.release_count[run_id] = self.release_count.get(run_id, 0) + 1

    async def resolve_indeterminate(self, *, resolution, now):
        async with self._lock:
            current = self.records.get(resolution.run_id)
            if (
                current is None
                or current.state is not gateway.RunState.INDETERMINATE
                or current.version != resolution.expected_version
                or resolution.run_id in self.leases
                or resolution.audit_identity in self.audit
            ):
                return None
            replacement = resolution.replacement
            if replacement is not None and (
                replacement.proposed.run_id in self.records
                or replacement.proposed.idempotency_binding.matches(
                    current.idempotency_binding
                )
                or any(
                    replacement.idempotency.matches(record.idempotency_binding)
                    for run_id, record in self.records.items()
                    if run_id != current.run_id
                )
                or any(
                    replacement.desktop.matches(binding)
                    for run_id, binding in self.desktop_claims.items()
                    if run_id != current.run_id
                )
            ):
                return None
            reason = (
                gateway.TerminalReason.OPERATOR_SAFE_RELEASE
                if resolution.action is gateway.OperatorResolutionAction.SAFE_RELEASE
                else gateway.TerminalReason.OPERATOR_SAFE_REPLACE
            )
            sealed = replace(
                current,
                version=current.version + 1,
                updated_at=now,
                terminal_reason=reason,
            )
            self.records[current.run_id] = sealed
            self._release(current.run_id)
            successor = None
            if replacement is not None:
                successor = replacement.proposed
                self.records[successor.run_id] = successor
                self.intents[successor.run_id] = replacement.pending_intent
                self.capacity.add(successor.run_id)
                self.desktop_claims[successor.run_id] = successor.desktop_binding
            self.audit[resolution.audit_identity] = resolution
            return gateway.OperatorResolutionResult(sealed, successor)

    async def prune_safe_terminal_page(self, *, cursor, now, policy):
        async with self._lock:
            after = self._decode_cursor(cursor)
            eligible = sorted(
                (
                    record
                    for record in self.records.values()
                    if record.state in gateway.CAPACITY_RELEASING_STATES
                    and record.terminal_at is not None
                    and record.terminal_at + policy.terminal_retention <= now
                    and record.run_id not in self.leases
                    and (record.terminal_at, record.run_id) > after
                ),
                key=lambda record: (record.terminal_at, record.run_id),
            )
            selected = eligible[: policy.page_size]
            for record in selected:
                self.tombstones[record.run_id] = gateway.ReplayTombstone.from_record(
                    record,
                    expires_at=now + policy.replay_fence_window,
                )
                del self.records[record.run_id]
            next_cursor = None
            if len(eligible) > len(selected) and selected:
                last = selected[-1]
                value = f"{last.terminal_at.isoformat()}\0{last.run_id}".encode()
                next_cursor = gateway.ReconcileCursor(value)
            return gateway.PruneResult(len(selected), next_cursor)

    async def admit(self, command):
        async with self._lock:
            for tombstone in self.tombstones.values():
                if tombstone.expires_at > self.now and command.idempotency.matches(
                    tombstone.idempotency_binding
                ):
                    desktop_mismatch = not command.desktop.matches(
                        tombstone.desktop_binding
                    )
                    task_mismatch = not command.task.matches(tombstone.task_binding)
                    if desktop_mismatch or task_mismatch:
                        return gateway.AdmissionDenied(
                            gateway.AdmissionRejection.IDEMPOTENCY_CONFLICT
                        )
                    return gateway.AdmissionAccepted(
                        gateway.AdmissionDisposition.REPLAYED,
                        tombstone,
                        gateway.DispatchIntent(
                            tombstone.run_id,
                            gateway.DispatchIntentState.CLAIMED,
                        ),
                    )
            self.records[command.proposed.run_id] = command.proposed
            self.intents[command.proposed.run_id] = command.pending_intent
            self.capacity.add(command.proposed.run_id)
            self.desktop_claims[command.proposed.run_id] = command.proposed.desktop_binding
            return gateway.AdmissionAccepted(
                gateway.AdmissionDisposition.ADMITTED,
                command.proposed,
                command.pending_intent,
            )


class Dispatcher:
    def __init__(self, outcome=gateway.PollState.PENDING) -> None:
        self.outcome = outcome
        self.poll_calls = 0
        self.cancel_calls = 0
        self.events: list[str] = []
        self.cancel_outcome = gateway.CancelOutcome(gateway.CancelState.ACCEPTED)
        self.cancel_error: Exception | None = None
        self.store: LockedRecoveryStore | None = None
        self.run_id: str | None = None

    async def poll(self, _call_id):
        self.poll_calls += 1
        self.events.append("poll")
        return self.outcome

    async def cancel(self, _call_id):
        self.cancel_calls += 1
        self.events.append("cancel")
        if self.store is not None and self.run_id is not None:
            assert (
                self.store.records[self.run_id].state
                is gateway.RunState.CANCELLATION_REQUESTED
            )
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_outcome


def _policy(**changes) -> gateway.RecoveryPolicy:
    defaults = {
        "page_size": 2,
        "max_pages_per_tick": 2,
        "poll_interval": timedelta(seconds=10),
        "provider_backoff_initial": timedelta(seconds=2),
        "provider_backoff_max": timedelta(seconds=8),
        "max_provider_errors": 3,
        "cancel_backoff_initial": timedelta(seconds=2),
        "cancel_backoff_max": timedelta(seconds=8),
        "max_cancel_errors": 2,
        "cancellation_grace": timedelta(seconds=30),
        "lease_duration": timedelta(seconds=20),
        "dispatch_stale_after": timedelta(seconds=10),
    }
    defaults.update(changes)
    return gateway.RecoveryPolicy(**defaults)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_size", 0),
        ("max_pages_per_tick", 0),
        ("max_provider_errors", 0),
        ("poll_interval", timedelta(0)),
        ("lease_duration", timedelta(0)),
    ],
)
def test_recovery_policy_is_strictly_bounded(field, value) -> None:
    with pytest.raises(ValueError):
        _policy(**{field: value})


@pytest.mark.asyncio
async def test_keyset_tick_is_bounded_by_page_size_and_page_count() -> None:
    records = tuple(_reserved(f"run-{index:02}") for index in range(7))
    store = LockedRecoveryStore(records)
    reconciler = gateway.RunReconciler(store, Dispatcher(), _policy())

    processed = await reconciler.reconcile(now=NOW)

    assert processed == 4
    assert sum(record.version > records[0].version for record in store.records.values()) == 4


@pytest.mark.asyncio
async def test_overlapping_claims_have_one_lease_winner_and_stale_cas_is_rejected() -> None:
    record = _running("run-overlap")
    store = LockedRecoveryStore((record,))
    first, second = await asyncio.gather(
        store.claim_reconcile_page(
            cursor=None, now=NOW, limit=1, lease_duration=timedelta(seconds=10)
        ),
        store.claim_reconcile_page(
            cursor=None, now=NOW, limit=1, lease_duration=timedelta(seconds=10)
        ),
    )
    claims = (*first.claims, *second.claims)
    assert len(claims) == 1
    claim = claims[0]

    store.now = NOW + timedelta(seconds=10)
    next_record = record.reschedule(now=store.now, reconcile_at=store.now)
    assert await store.reconcile_compare_and_set(claim=claim, next_record=next_record) is None


@pytest.mark.asyncio
async def test_reserved_expiry_releases_once_and_stale_dispatch_is_never_respawned() -> None:
    reserved = _reserved("run-reserved", deadline_at=NOW)
    dispatching = _reserved("run-dispatch").transition(
        gateway.RunState.DISPATCHING,
        now=NOW - timedelta(seconds=11),
        reconcile_at=NOW,
    )
    store = LockedRecoveryStore((reserved, dispatching))
    dispatcher = Dispatcher()

    assert await gateway.RunReconciler(store, dispatcher, _policy()).reconcile(now=NOW) == 2

    assert store.records[reserved.run_id].state is gateway.RunState.CANCELLED
    assert (
        store.intents[reserved.run_id].state
        is gateway.DispatchIntentState.REVOKED
    )
    assert store.release_count == {reserved.run_id: 1}
    assert store.records[dispatching.run_id].state is gateway.RunState.INDETERMINATE
    assert dispatching.run_id in store.capacity
    assert not hasattr(dispatcher, "spawn_calls")


@pytest.mark.asyncio
async def test_running_deadline_durably_requests_cancellation_before_provider_call() -> None:
    record = _running("run-deadline", deadline_at=NOW)
    store = LockedRecoveryStore((record,))
    dispatcher = Dispatcher()
    dispatcher.store = store
    dispatcher.run_id = record.run_id

    await gateway.RunReconciler(store, dispatcher, _policy()).reconcile(now=NOW)

    recovered = store.records[record.run_id]
    assert recovered.state is gateway.RunState.CANCELLATION_REQUESTED
    assert recovered.cancellation_requested_at == NOW
    assert recovered.cancellation_deadline_at == NOW + timedelta(seconds=30)
    assert dispatcher.events == ["poll", "cancel"]


@pytest.mark.asyncio
async def test_pending_resets_provider_errors_and_transient_errors_hit_cap() -> None:
    pending = _running("run-pending", provider_errors=2)
    pending_store = LockedRecoveryStore((pending,))
    await gateway.RunReconciler(
        pending_store, Dispatcher(gateway.PollState.PENDING), _policy()
    ).reconcile(now=NOW)
    assert pending_store.records[pending.run_id].provider_error_count == 0

    unavailable = _running("run-unavailable", provider_errors=2)
    unavailable_store = LockedRecoveryStore((unavailable,))
    await gateway.RunReconciler(
        unavailable_store, Dispatcher(gateway.PollState.UNAVAILABLE), _policy()
    ).reconcile(now=NOW)
    assert unavailable_store.records[unavailable.run_id].state is gateway.RunState.INDETERMINATE
    assert unavailable.run_id in unavailable_store.capacity

    first_error = _running("run-backoff")
    backoff_store = LockedRecoveryStore((first_error,))
    await gateway.RunReconciler(
        backoff_store, Dispatcher(gateway.PollState.UNAVAILABLE), _policy()
    ).reconcile(now=NOW)
    assert backoff_store.records[first_error.run_id].reconcile_at == NOW + timedelta(
        seconds=2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "outcome", "expected"),
    [
        (gateway.RunState.RUNNING, gateway.PollState.SUCCEEDED, gateway.RunState.SUCCEEDED),
        (gateway.RunState.RUNNING, gateway.PollState.FAILED, gateway.RunState.FAILED),
        (gateway.RunState.RUNNING, gateway.PollState.TERMINATED, gateway.RunState.FAILED),
        (
            gateway.RunState.RUNNING,
            gateway.PollState.INDETERMINATE,
            gateway.RunState.INDETERMINATE,
        ),
        (
            gateway.RunState.CANCELLATION_REQUESTED,
            gateway.PollState.TERMINATED,
            gateway.RunState.CANCELLED,
        ),
    ],
)
async def test_reconciler_maps_provider_terminal_states(initial, outcome, expected) -> None:
    record = _running("run-terminal")
    if initial is gateway.RunState.CANCELLATION_REQUESTED:
        record = record.transition(
            initial,
            now=NOW,
            reconcile_at=NOW,
            cancellation_requested_at=NOW,
            cancellation_deadline_at=NOW + timedelta(seconds=30),
        )
    store = LockedRecoveryStore((record,))

    await gateway.RunReconciler(store, Dispatcher(outcome), _policy()).reconcile(now=NOW)

    assert store.records[record.run_id].state is expected
    if expected in gateway.CAPACITY_RELEASING_STATES:
        assert record.run_id not in store.capacity
        assert store.release_count == {record.run_id: 1}
    else:
        assert record.run_id in store.capacity


@pytest.mark.asyncio
async def test_cancellation_polls_first_and_accepted_or_transient_cancel_stays_requested() -> None:
    requested = _running("run-cancel").transition(
        gateway.RunState.CANCELLATION_REQUESTED,
        now=NOW,
        reconcile_at=NOW,
        cancellation_requested_at=NOW,
        cancellation_deadline_at=NOW + timedelta(seconds=30),
    )
    store = LockedRecoveryStore((requested,))
    dispatcher = Dispatcher()
    await gateway.RunReconciler(store, dispatcher, _policy()).reconcile(now=NOW)
    assert dispatcher.events == ["poll", "cancel"]
    assert store.records[requested.run_id].state is gateway.RunState.CANCELLATION_REQUESTED

    transient = replace(requested, run_id="run-cancel-error", cancel_error_count=0)
    transient_store = LockedRecoveryStore((transient,))
    transient_dispatcher = Dispatcher()
    transient_dispatcher.cancel_error = RuntimeError("credential-sentinel")
    await gateway.RunReconciler(
        transient_store, transient_dispatcher, _policy()
    ).reconcile(now=NOW)
    recovered = transient_store.records[transient.run_id]
    assert recovered.state is gateway.RunState.CANCELLATION_REQUESTED
    assert recovered.cancel_error_count == 1

    unavailable = replace(requested, run_id="run-cancel-unavailable")
    unavailable_store = LockedRecoveryStore((unavailable,))
    unavailable_dispatcher = Dispatcher()
    unavailable_dispatcher.cancel_outcome = gateway.CancelOutcome(
        gateway.CancelState.UNAVAILABLE,
        gateway.CancelReason.TRANSIENT_PROVIDER_ERROR,
    )
    await gateway.RunReconciler(
        unavailable_store, unavailable_dispatcher, _policy()
    ).reconcile(now=NOW)
    assert unavailable_store.records[unavailable.run_id].cancel_error_count == 1

    ambiguous = replace(requested, run_id="run-cancel-ambiguous")
    ambiguous_store = LockedRecoveryStore((ambiguous,))
    ambiguous_dispatcher = Dispatcher()
    ambiguous_dispatcher.cancel_outcome = gateway.CancelOutcome(
        gateway.CancelState.INDETERMINATE,
        gateway.CancelReason.MISSING_CALL,
    )
    await gateway.RunReconciler(
        ambiguous_store, ambiguous_dispatcher, _policy()
    ).reconcile(now=NOW)
    resolved = ambiguous_store.records[ambiguous.run_id]
    assert resolved.state is gateway.RunState.INDETERMINATE
    assert resolved.terminal_reason is gateway.TerminalReason.CANCELLATION_AMBIGUOUS
    assert ambiguous.run_id in ambiguous_store.capacity


@pytest.mark.asyncio
async def test_cancellation_error_cap_and_grace_expiry_become_indeterminate() -> None:
    base = _running("run-cap").transition(
        gateway.RunState.CANCELLATION_REQUESTED,
        now=NOW,
        reconcile_at=NOW,
        cancellation_requested_at=NOW,
        cancellation_deadline_at=NOW + timedelta(seconds=30),
    )
    capped = replace(base, cancel_error_count=1)
    store = LockedRecoveryStore((capped,))
    dispatcher = Dispatcher()
    dispatcher.cancel_error = RuntimeError("transient")
    await gateway.RunReconciler(store, dispatcher, _policy()).reconcile(now=NOW)
    assert (
        store.records[capped.run_id].terminal_reason
        is gateway.TerminalReason.CANCELLATION_ERROR_CAP
    )

    expired = replace(
        base,
        run_id="run-grace",
        cancellation_deadline_at=NOW,
    )
    expired_store = LockedRecoveryStore((expired,))
    expired_dispatcher = Dispatcher()
    await gateway.RunReconciler(expired_store, expired_dispatcher, _policy()).reconcile(now=NOW)
    assert (
        expired_store.records[expired.run_id].terminal_reason
        is gateway.TerminalReason.CANCELLATION_DEADLINE
    )
    assert expired_dispatcher.cancel_calls == 0


def _replacement_command(run_id: str, idempotency: str) -> gateway.AdmissionCommand:
    keyring = gateway.IdentityKeyring(gateway.IdentityKey("active", b"a" * 32))
    idempotency_proof = keyring.prove(
        tenant_id="tenant-a",
        kind=gateway.IdentityKind.IDEMPOTENCY,
        value=idempotency,
    )
    desktop = keyring.prove(
        tenant_id="tenant-a", kind=gateway.IdentityKind.DESKTOP, value="desktop"
    )
    task = keyring.prove(
        tenant_id="tenant-a", kind=gateway.IdentityKind.TASK, value="task"
    )
    record = gateway.RunRecord.reserve(
        run_id=run_id,
        tenant_id="tenant-a",
        idempotency_binding=idempotency_proof.mint,
        desktop_binding=desktop.mint,
        task_binding=task.mint,
        now=NOW,
        deadline_at=NOW + timedelta(hours=1),
    )
    return gateway.AdmissionCommand(
        record,
        idempotency_proof,
        desktop,
        task,
        gateway.DispatchIntent.pending(run_id),
    )


@pytest.mark.asyncio
async def test_operator_resolution_requires_sealed_version_and_safe_replace_is_fresh() -> None:
    indeterminate = _running("run-old").transition(
        gateway.RunState.INDETERMINATE,
        now=NOW,
        terminal_reason=gateway.TerminalReason.PROVIDER_AMBIGUOUS,
    )
    store = LockedRecoveryStore((indeterminate,))
    stale = gateway.OperatorResolution(
        indeterminate.run_id,
        indeterminate.version - 1,
        gateway.OperatorResolutionAction.SAFE_RELEASE,
        "actor-secret",
        "reason-secret",
        "audit-secret",
    )
    assert await store.resolve_indeterminate(resolution=stale, now=NOW) is None

    replacement = _replacement_command("run-successor", "fresh-idempotency")
    resolution = gateway.OperatorResolution(
        indeterminate.run_id,
        indeterminate.version,
        gateway.OperatorResolutionAction.SAFE_REPLACE,
        "actor-secret",
        "reason-secret",
        "audit-secret",
        replacement,
    )
    result = await store.resolve_indeterminate(resolution=resolution, now=NOW)
    assert result is not None
    assert result.sealed.state is gateway.RunState.INDETERMINATE
    assert result.successor is not None and result.successor.run_id == "run-successor"
    assert indeterminate.run_id not in store.capacity
    assert "run-successor" in store.capacity

    safe_release_record = replace(indeterminate, run_id="run-release")
    release_store = LockedRecoveryStore((safe_release_record,))
    safe_release = gateway.OperatorResolution(
        safe_release_record.run_id,
        safe_release_record.version,
        gateway.OperatorResolutionAction.SAFE_RELEASE,
        "actor-secret",
        "reason-secret",
        "release-audit-secret",
    )
    released = await release_store.resolve_indeterminate(
        resolution=safe_release,
        now=NOW,
    )
    assert released is not None
    assert safe_release_record.run_id not in release_store.capacity
    assert release_store.release_count == {safe_release_record.run_id: 1}


@pytest.mark.asyncio
async def test_retention_protects_unsafe_records_and_tombstone_fences_replay() -> None:
    succeeded = _running("run-finished").transition(gateway.RunState.SUCCEEDED, now=NOW)
    succeeded = replace(succeeded, terminal_at=NOW - timedelta(days=2))
    active = _running("run-active")
    indeterminate = _running("run-uncertain").transition(
        gateway.RunState.INDETERMINATE,
        now=NOW - timedelta(days=2),
        terminal_reason=gateway.TerminalReason.PROVIDER_AMBIGUOUS,
    )
    store = LockedRecoveryStore((succeeded, active, indeterminate))
    claimed_terminal = replace(succeeded, run_id="run-claimed")
    store.records[claimed_terminal.run_id] = claimed_terminal
    token = gateway.LeaseToken(b"claimed-token-secret")
    store.leases[claimed_terminal.run_id] = (
        token,
        NOW + timedelta(minutes=1),
        claimed_terminal.version,
    )
    policy = gateway.RetentionPolicy(timedelta(days=1), timedelta(days=7), page_size=10)

    result = await store.prune_safe_terminal_page(cursor=None, now=NOW, policy=policy)

    assert result.pruned == 1
    assert set(store.records) == {
        active.run_id,
        indeterminate.run_id,
        claimed_terminal.run_id,
    }
    assert succeeded.run_id in store.tombstones
    tombstone = store.tombstones[succeeded.run_id]
    assert isinstance(tombstone, gateway.ReplayTombstone)
    assert not hasattr(tombstone, "function_call_id")
    assert "provider-identity-secret" not in repr(tombstone)
    proof = gateway.IdentityProof(
        succeeded.idempotency_binding,
        (succeeded.idempotency_binding,),
    )
    replay = await store.admit(
        gateway.AdmissionCommand(
            _reserved("run-new", idempotency_binding=succeeded.idempotency_binding),
            proof,
            gateway.IdentityProof(succeeded.desktop_binding, (succeeded.desktop_binding,)),
            gateway.IdentityProof(succeeded.task_binding, (succeeded.task_binding,)),
            gateway.DispatchIntent.pending("run-new"),
        )
    )
    assert isinstance(replay, gateway.AdmissionAccepted)
    assert replay.disposition is gateway.AdmissionDisposition.REPLAYED
    assert replay.record.run_id == succeeded.run_id
    assert "run-new" not in store.capacity

    store.now = NOW + timedelta(days=8)
    after_fence = gateway.AdmissionCommand(
        _reserved("run-after-fence", idempotency_binding=succeeded.idempotency_binding),
        proof,
        gateway.IdentityProof(
            succeeded.desktop_binding,
            (succeeded.desktop_binding,),
        ),
        gateway.IdentityProof(succeeded.task_binding, (succeeded.task_binding,)),
        gateway.DispatchIntent.pending("run-after-fence"),
    )
    admitted = await store.admit(after_fence)
    assert isinstance(admitted, gateway.AdmissionAccepted)
    assert admitted.disposition is gateway.AdmissionDisposition.ADMITTED
    assert "run-after-fence" in store.capacity


def test_recovery_tokens_and_operator_audit_representations_are_redacted() -> None:
    token = gateway.LeaseToken(b"lease-token-secret")
    cursor = gateway.ReconcileCursor(b"cursor-secret")
    resolution = gateway.OperatorResolution(
        "run-id",
        1,
        gateway.OperatorResolutionAction.SAFE_RELEASE,
        "actor-secret",
        "reason-secret",
        "audit-secret",
    )
    text = " ".join((repr(token), repr(cursor), repr(resolution)))
    secrets = (
        "lease-token-secret",
        "cursor-secret",
        "actor-secret",
        "reason-secret",
        "audit-secret",
    )
    for secret in secrets:
        assert secret not in text


def test_modal_schedule_is_explicit_bounded_integration_wiring() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "run_gateway"
        / "modal_adapter.py"
    ).read_text()
    assert "schedule=modal.Period(seconds=60)" in source
    assert '"run-gateway-store"' in source
    assert 'required_keys=["RUN_GATEWAY_STORE_DSN"]' in source
    assert "min_containers=0" in source
    assert "max_containers=1" in source
    assert "@modal.concurrent(max_inputs=1)" in source
    assert "timeout=45" in source
    assert "retries=0" in source
    assert "correctness" in source
    with pytest.raises(RuntimeError, match="inject a durable"):
        gateway.build_reconciler_from_environment()
    assert gateway.RecoveryPolicy().lease_duration > timedelta(seconds=45)
