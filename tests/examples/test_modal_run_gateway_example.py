from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def _load_example():
    path = Path(__file__).resolve().parents[2] / "examples" / "modal_run_gateway.py"
    spec = importlib.util.spec_from_file_location("modal_run_gateway_example", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gateway = _load_example()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakePrincipalResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, request):
        self.calls += 1
        authorization = request.headers.get("authorization")
        if authorization == "Bearer tenant-a":
            return gateway.Principal(tenant_id="tenant-a", principal_id="principal-a")
        if authorization == "Bearer tenant-b":
            return gateway.Principal(tenant_id="tenant-b", principal_id="principal-b")
        raise gateway.AuthenticationRequired()


class FakeSessionCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.handle = SimpleNamespace(sandbox_id="sandbox-secret")

    async def resolve(self, principal, desktop_key):
        self.calls.append((principal.tenant_id, desktop_key))
        allowed = {
            f"desktop-for-{principal.tenant_id}",
            f"alternate-desktop-for-{principal.tenant_id}",
        }
        if desktop_key not in allowed:
            raise gateway.ObjectNotFound()
        return gateway.ResolvedDesktop(self.handle, f"desktop-internal-{desktop_key}")


class FakeTaskCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, principal, task_key):
        self.calls.append((principal.tenant_id, task_key))
        allowed = {
            f"task-for-{principal.tenant_id}",
            f"alternate-task-for-{principal.tenant_id}",
        }
        if task_key not in allowed:
            raise gateway.ObjectNotFound()
        return gateway.ResolvedTask("task-text-secret", f"task-internal-{task_key}")


class FakeRunStore:
    """Test-only atomic store; the example intentionally ships no production default."""

    def __init__(
        self, *, quota: int = 10, fail_after: str | None = None, types=gateway
    ) -> None:
        self._lock = asyncio.Lock()
        self.by_id: dict[str, object] = {}
        self.intents: dict[str, object] = {}
        self.quota = quota
        self.fail_after = fail_after
        self.capacity: set[str] = set()
        self.desktop_claims: dict[str, object] = {}
        self.transitions: list[tuple[object, object]] = []
        self.types = types

    async def admit(self, command):
        async with self._lock:
            snapshot = (
                dict(self.by_id),
                dict(self.intents),
                set(self.capacity),
                dict(self.desktop_claims),
            )
            try:
                for record in self.by_id.values():
                    if record.tenant_id != command.proposed.tenant_id:
                        continue
                    if command.idempotency.matches(record.idempotency_binding):
                        if not command.desktop.matches(
                            record.desktop_binding
                        ) or not command.task.matches(record.task_binding):
                            return self.types.AdmissionDenied(
                                self.types.AdmissionRejection.IDEMPOTENCY_CONFLICT
                            )
                        return self.types.AdmissionAccepted(
                            self.types.AdmissionDisposition.REPLAYED,
                            record,
                            self.intents[record.run_id],
                        )
                self._fault("replay")
                tenant_active = sum(
                    self.by_id[run_id].tenant_id == command.proposed.tenant_id
                    for run_id in self.capacity
                )
                if tenant_active >= self.quota:
                    return self.types.AdmissionDenied(
                        self.types.AdmissionRejection.TENANT_QUOTA_EXCEEDED
                    )
                self.capacity.add(command.proposed.run_id)
                self._fault("quota")
                for binding in self.desktop_claims.values():
                    if command.desktop.matches(binding):
                        self.capacity.remove(command.proposed.run_id)
                        return self.types.AdmissionDenied(
                            self.types.AdmissionRejection.DESKTOP_BUSY
                        )
                self.desktop_claims[command.proposed.run_id] = (
                    command.proposed.desktop_binding
                )
                self._fault("desktop")
                self.by_id[command.proposed.run_id] = command.proposed
                self._fault("run")
                self.intents[command.proposed.run_id] = command.pending_intent
                self._fault("intent")
                return self.types.AdmissionAccepted(
                    self.types.AdmissionDisposition.ADMITTED,
                    command.proposed,
                    command.pending_intent,
                )
            except Exception:
                self.by_id, self.intents, self.capacity, self.desktop_claims = snapshot
                raise

    def _fault(self, phase):
        if self.fail_after == phase:
            raise RuntimeError("admission-fault-secret")

    async def claim_dispatch(self, *, current, pending_intent, now):
        async with self._lock:
            stored = self.by_id.get(current.run_id)
            intent = self.intents.get(current.run_id)
            if (
                stored != current
                or intent != pending_intent
                or current.state is not self.types.RunState.RESERVED
                or intent.state is not self.types.DispatchIntentState.PENDING
            ):
                return None
            next_record = current.transition(self.types.RunState.DISPATCHING, now=now)
            next_intent = self.types.DispatchIntent(
                run_id=current.run_id,
                state=self.types.DispatchIntentState.CLAIMED,
            )
            self.by_id[current.run_id] = next_record
            self.intents[current.run_id] = next_intent
            self.transitions.append((current.state, next_record.state))
            return self.types.DispatchClaim(next_record, next_intent)

    async def get_authorized(self, *, tenant_id, run_id):
        record = self.by_id.get(run_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    async def compare_and_set(self, *, current, next_record):
        async with self._lock:
            stored = self.by_id.get(current.run_id)
            if stored != current:
                return None
            self.by_id[current.run_id] = next_record
            self.transitions.append((current.state, next_record.state))
            if (
                next_record.state in self.types.CAPACITY_RELEASING_STATES
                and current.run_id in self.capacity
            ):
                self.capacity.remove(current.run_id)
                self.desktop_claims.pop(current.run_id)
            return next_record

    def insert(self, record, *, intent=None) -> None:
        self.by_id[record.run_id] = record
        self.intents[record.run_id] = intent or self.types.DispatchIntent(
            record.run_id,
            self.types.DispatchIntentState.CLAIMED,
        )
        self.capacity.add(record.run_id)
        self.desktop_claims[record.run_id] = record.desktop_binding


class FakeDispatcher:
    def __init__(self) -> None:
        self.spawn_calls: list[tuple[object, object, str]] = []
        self.poll_calls: list[object] = []
        self.cancel_calls: list[object] = []
        self.poll_state = gateway.PollState.PENDING

    async def spawn(self, *, desktop, task, run_id):
        self.spawn_calls.append((desktop, task, run_id))
        await asyncio.sleep(0)
        return gateway.FunctionCallIdentity("fc-provider-secret")

    async def poll(self, call_id):
        self.poll_calls.append(call_id)
        return self.poll_state

    async def cancel(self, call_id):
        self.cancel_calls.append(call_id)


def _service(
    *,
    clock: Clock | None = None,
    store: FakeRunStore | None = None,
    dispatcher: FakeDispatcher | None = None,
):
    clock = clock or Clock()
    store = store or FakeRunStore()
    dispatcher = dispatcher or FakeDispatcher()
    service = gateway.RunGatewayService(
        principal_resolver=FakePrincipalResolver(),
        session_catalog=FakeSessionCatalog(),
        task_catalog=FakeTaskCatalog(),
        run_store=store,
        dispatcher=dispatcher,
        identity_keyring=gateway.IdentityKeyring(
            gateway.IdentityKey("active", b"a" * 32)
        ),
        stale_after=timedelta(seconds=10),
        clock=clock,
        run_id_factory=lambda: "run-application-stable",
    )
    return service, store, dispatcher


def _headers(tenant: str = "tenant-a") -> dict[str, str]:
    return {"authorization": f"Bearer {tenant}"}


def _body(tenant: str = "tenant-a") -> dict[str, str]:
    return {
        "desktop_key": f"desktop-for-{tenant}",
        "task_key": f"task-for-{tenant}",
        "idempotency_key": "request-opaque-key",
    }


def _command(
    *,
    run_id: str,
    idempotency: str,
    desktop: str,
    task: str,
    keyring=None,
    tenant: str = "tenant-a",
):
    keyring = keyring or gateway.IdentityKeyring(
        gateway.IdentityKey("active", b"a" * 32)
    )
    idempotency_proof = keyring.prove(
        tenant_id=tenant,
        kind=gateway.IdentityKind.IDEMPOTENCY,
        value=idempotency,
    )
    desktop_proof = keyring.prove(
        tenant_id=tenant,
        kind=gateway.IdentityKind.DESKTOP,
        value=desktop,
    )
    task_proof = keyring.prove(
        tenant_id=tenant,
        kind=gateway.IdentityKind.TASK,
        value=task,
    )
    now = datetime(2026, 7, 30, tzinfo=UTC)
    record = gateway.RunRecord.reserve(
        run_id=run_id,
        tenant_id=tenant,
        idempotency_binding=idempotency_proof.mint,
        desktop_binding=desktop_proof.mint,
        task_binding=task_proof.mint,
        now=now,
    )
    return gateway.AdmissionCommand(
        proposed=record,
        idempotency=idempotency_proof,
        desktop=desktop_proof,
        task=task_proof,
        pending_intent=gateway.DispatchIntent.pending(run_id),
    )


def _reserved_record(
    clock: Clock,
    *,
    tenant: str = "tenant-a",
    run_id: str = "run-owned",
    idempotency_key: str = "request-opaque-key",
    desktop_key: str | None = None,
    task_key: str | None = None,
):
    keyring = gateway.IdentityKeyring(gateway.IdentityKey("active", b"a" * 32))
    desktop_key = desktop_key or f"desktop-for-{tenant}"
    task_key = task_key or f"task-for-{tenant}"
    return gateway.RunRecord.reserve(
        run_id=run_id,
        tenant_id=tenant,
        idempotency_binding=keyring.prove(
            tenant_id=tenant,
            kind=gateway.IdentityKind.IDEMPOTENCY,
            value=idempotency_key,
        ).mint,
        desktop_binding=keyring.prove(
            tenant_id=tenant,
            kind=gateway.IdentityKind.DESKTOP,
            value=f"desktop-internal-{desktop_key}",
        ).mint,
        task_binding=keyring.prove(
            tenant_id=tenant,
            kind=gateway.IdentityKind.TASK,
            value=f"task-internal-{task_key}",
        ).mint,
        now=clock(),
    )


def _running_record(clock: Clock, *, tenant: str = "tenant-a", run_id: str = "run-owned"):
    reserved = _reserved_record(clock, tenant=tenant, run_id=run_id)
    dispatching = reserved.transition(gateway.RunState.DISPATCHING, now=clock())
    return dispatching.transition(
        gateway.RunState.RUNNING,
        now=clock(),
        function_call_id=gateway.FunctionCallIdentity("fc-provider-secret"),
    )


@pytest.mark.parametrize(
    ("field", "secret"),
    [
        ("sandbox_id", "sandbox-client-secret"),
        ("session_handle", "handle-client-secret"),
        ("function_name", "function-client-secret"),
        ("function_call_id", "fc-client-secret"),
        ("owner", "owner-client-secret"),
        ("endpoint", "https://endpoint.client.secret"),
        ("token", "token-client-secret"),
    ],
)
def test_http_authentication_object_authorization_and_input_fail_closed(
    field: str,
    secret: str,
) -> None:
    service, _store, dispatcher = _service()
    client = TestClient(gateway.build_run_gateway_app(service))

    assert client.post("/v1/runs", json=_body()).status_code == 401
    assert client.post(
        "/v1/runs", json={**_body(), "desktop_key": "unknown"}, headers=_headers()
    ).status_code == 404
    assert client.post(
        "/v1/runs", json={**_body(), "task_key": "unknown"}, headers=_headers()
    ).status_code == 404
    response = client.post(
        "/v1/runs",
        json={**_body(), field: secret},
        headers=_headers(),
    )
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}
    assert secret not in response.text
    assert dispatcher.spawn_calls == []


def test_required_idempotency_and_internal_errors_are_sanitized(caplog) -> None:
    service, _store, _dispatcher = _service()
    client = TestClient(gateway.build_run_gateway_app(service))
    missing_idempotency = _body()
    del missing_idempotency["idempotency_key"]

    missing_response = client.post(
        "/v1/runs", json=missing_idempotency, headers=_headers()
    )

    async def fail_with_secret(_request):
        raise RuntimeError("bearer-token-secret")

    service.principal_resolver.resolve = fail_with_secret
    error_response = client.post("/v1/runs", json=_body(), headers=_headers())

    assert missing_response.status_code == 422
    assert missing_response.json() == {"error": "invalid_request"}
    assert error_response.status_code == 500
    assert error_response.json() == {"error": "internal_error"}
    assert "bearer-token-secret" not in error_response.text
    assert "bearer-token-secret" not in caplog.text


@pytest.mark.parametrize(
    ("method_name", "http_method", "path", "json_body"),
    [
        ("create_run", "POST", "/v1/runs", _body()),
        ("get_run", "GET", "/v1/runs/run-owned", None),
        ("cancel_run", "POST", "/v1/runs/run-owned/cancel", None),
    ],
)
def test_each_route_contains_unexpected_exceptions_before_asgi_logging(
    method_name: str,
    http_method: str,
    path: str,
    json_body: dict[str, str] | None,
    caplog,
) -> None:
    service, _store, _dispatcher = _service()
    sentinel = f"private-{method_name}-exception"

    async def fail(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    setattr(service, method_name, fail)
    client = TestClient(gateway.build_run_gateway_app(service))

    response = client.request(http_method, path, json=json_body, headers=_headers())

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert sentinel not in response.text
    assert sentinel not in caplog.text


def test_idempotent_submissions_return_one_run_and_spawn_once() -> None:
    service, _store, dispatcher = _service()
    client = TestClient(gateway.build_run_gateway_app(service))

    first = client.post("/v1/runs", json=_body(), headers=_headers())
    second = client.post("/v1/runs", json=_body(), headers=_headers())

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {
        "run_id": "run-application-stable",
        "state": "running",
    }
    assert len(dispatcher.spawn_calls) == 1
    assert dispatcher.spawn_calls[0][2] == "run-application-stable"
    assert service.session_catalog.calls == [
        ("tenant-a", "desktop-for-tenant-a"),
        ("tenant-a", "desktop-for-tenant-a"),
    ]
    assert service.task_catalog.calls == [
        ("tenant-a", "task-for-tenant-a"),
        ("tenant-a", "task-for-tenant-a"),
    ]


@pytest.mark.asyncio
async def test_many_concurrent_identical_submissions_create_one_run_intent_and_spawn() -> None:
    service, store, dispatcher = _service()

    async def submit():
        request = SimpleNamespace(headers={"authorization": "Bearer tenant-a"})
        return await service.create_run(request, gateway.CreateRunRequest(**_body()))

    records = await asyncio.gather(*(submit() for _ in range(40)))

    assert {record.run_id for record in records} == {"run-application-stable"}
    assert len(store.by_id) == len(store.intents) == 1
    assert store.intents["run-application-stable"].state is gateway.DispatchIntentState.CLAIMED
    assert len(dispatcher.spawn_calls) == 1


@pytest.mark.asyncio
async def test_admission_conflicts_and_quota_do_not_leak_capacity() -> None:
    store = FakeRunStore(quota=2)
    first = _command(run_id="run-1", idempotency="idem-1", desktop="desktop-1", task="task")
    busy = _command(run_id="run-2", idempotency="idem-2", desktop="desktop-1", task="task")

    accepted = await store.admit(first)
    rejected = await store.admit(busy)

    assert isinstance(accepted, gateway.AdmissionAccepted)
    assert rejected == gateway.AdmissionDenied(gateway.AdmissionRejection.DESKTOP_BUSY)
    assert store.capacity == {"run-1"}
    assert set(store.desktop_claims) == {"run-1"}
    assert set(store.by_id) == set(store.intents) == {"run-1"}

    quota_store = FakeRunStore(quota=1)
    assert isinstance(await quota_store.admit(first), gateway.AdmissionAccepted)
    quota = await quota_store.admit(
        _command(run_id="run-3", idempotency="idem-3", desktop="desktop-2", task="task")
    )
    assert quota == gateway.AdmissionDenied(
        gateway.AdmissionRejection.TENANT_QUOTA_EXCEEDED
    )
    assert quota_store.capacity == {"run-1"}


@pytest.mark.asyncio
async def test_mismatched_replay_precedes_capacity_checks_without_writes() -> None:
    store = FakeRunStore(quota=1)
    original = _command(
        run_id="run-1", idempotency="same-idem", desktop="desktop-1", task="task-1"
    )
    assert isinstance(await store.admit(original), gateway.AdmissionAccepted)
    before = (dict(store.by_id), dict(store.intents), set(store.capacity))

    result = await store.admit(
        _command(
            run_id="run-2",
            idempotency="same-idem",
            desktop="desktop-2",
            task="task-2",
        )
    )

    assert result == gateway.AdmissionDenied(
        gateway.AdmissionRejection.IDEMPOTENCY_CONFLICT
    )
    assert (store.by_id, store.intents, store.capacity) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["replay", "quota", "desktop", "run", "intent"])
async def test_admission_fault_after_each_phase_rolls_back(phase: str) -> None:
    store = FakeRunStore(fail_after=phase)

    with pytest.raises(RuntimeError, match="admission-fault-secret"):
        await store.admit(
            _command(run_id="run-fault", idempotency="idem", desktop="desktop", task="task")
        )

    assert store.by_id == {}
    assert store.intents == {}
    assert store.capacity == set()
    assert store.desktop_claims == {}


@pytest.mark.asyncio
async def test_replay_after_admission_commit_can_claim_and_dispatch() -> None:
    service, store, dispatcher = _service()
    command = _command(
        run_id="run-application-stable",
        idempotency="request-opaque-key",
        desktop="desktop-internal-desktop-for-tenant-a",
        task="task-internal-task-for-tenant-a",
    )
    committed = await store.admit(command)
    assert isinstance(committed, gateway.AdmissionAccepted)

    request = SimpleNamespace(headers={"authorization": "Bearer tenant-a"})
    record = await service.create_run(request, gateway.CreateRunRequest(**_body()))

    assert record.state is gateway.RunState.RUNNING
    assert len(dispatcher.spawn_calls) == 1
    assert store.intents[record.run_id].state is gateway.DispatchIntentState.CLAIMED


@pytest.mark.asyncio
async def test_concurrent_dispatch_claims_have_exactly_one_winner() -> None:
    store = FakeRunStore()
    command = _command(run_id="run-claim", idempotency="idem", desktop="desktop", task="task")
    admitted = await store.admit(command)
    assert isinstance(admitted, gateway.AdmissionAccepted)

    claims = await asyncio.gather(
        *(
            store.claim_dispatch(
                current=admitted.record,
                pending_intent=admitted.intent,
                now=admitted.record.created_at,
            )
            for _ in range(30)
        )
    )

    assert sum(claim is not None for claim in claims) == 1
    assert store.intents["run-claim"].state is gateway.DispatchIntentState.CLAIMED


@pytest.mark.asyncio
async def test_terminal_release_is_once_and_indeterminate_retains_ownership() -> None:
    store = FakeRunStore()
    admitted = await store.admit(
        _command(run_id="run-release", idempotency="idem", desktop="desktop", task="task")
    )
    assert isinstance(admitted, gateway.AdmissionAccepted)
    claim = await store.claim_dispatch(
        current=admitted.record,
        pending_intent=admitted.intent,
        now=admitted.record.created_at,
    )
    assert claim is not None
    running = claim.record.transition(
        gateway.RunState.RUNNING,
        now=claim.record.updated_at,
        function_call_id=gateway.FunctionCallIdentity("call-secret"),
    )
    assert await store.compare_and_set(current=claim.record, next_record=running) == running
    succeeded = running.transition(gateway.RunState.SUCCEEDED, now=running.updated_at)
    assert await store.compare_and_set(current=running, next_record=succeeded) == succeeded
    assert await store.compare_and_set(current=running, next_record=succeeded) is None
    assert store.capacity == set()
    assert store.desktop_claims == {}

    uncertain_store = FakeRunStore()
    uncertain = await uncertain_store.admit(
        _command(run_id="run-uncertain", idempotency="idem", desktop="desktop", task="task")
    )
    assert isinstance(uncertain, gateway.AdmissionAccepted)
    uncertain_claim = await uncertain_store.claim_dispatch(
        current=uncertain.record,
        pending_intent=uncertain.intent,
        now=uncertain.record.created_at,
    )
    assert uncertain_claim is not None
    indeterminate = uncertain_claim.record.transition(
        gateway.RunState.INDETERMINATE, now=uncertain_claim.record.updated_at
    )
    assert (
        await uncertain_store.compare_and_set(
            current=uncertain_claim.record, next_record=indeterminate
        )
        == indeterminate
    )
    assert uncertain_store.capacity == {"run-uncertain"}
    assert set(uncertain_store.desktop_claims) == {"run-uncertain"}


def test_identity_hmac_is_domain_separated_rotatable_and_strict() -> None:
    old = gateway.IdentityKey("old", b"o" * 32)
    active = gateway.IdentityKey("active", b"a" * 32)
    rotated = gateway.IdentityKeyring(active, (old,))
    old_ring = gateway.IdentityKeyring(old)
    old_digest = old_ring.prove(
        tenant_id="tenant-a", kind=gateway.IdentityKind.DESKTOP, value="same"
    ).mint

    proof = rotated.prove(
        tenant_id="tenant-a", kind=gateway.IdentityKind.DESKTOP, value="same"
    )
    assert proof.matches(old_digest)
    assert not rotated.prove(
        tenant_id="tenant-b", kind=gateway.IdentityKind.DESKTOP, value="same"
    ).matches(old_digest)
    assert not rotated.prove(
        tenant_id="tenant-a", kind=gateway.IdentityKind.TASK, value="same"
    ).matches(old_digest)
    assert not gateway.IdentityKeyring(active).prove(
        tenant_id="tenant-a", kind=gateway.IdentityKind.DESKTOP, value="same"
    ).matches(old_digest)
    assert proof.mint != rotated.prove(
        tenant_id="tenant-a", kind=gateway.IdentityKind.DESKTOP, value="same\x00suffix"
    ).mint

    with pytest.raises(ValueError, match="at least 32"):
        gateway.IdentityKey("weak", b"short")
    with pytest.raises(ValueError, match="ASCII"):
        gateway.IdentityKey("non-\N{LATIN SMALL LETTER N WITH TILDE}-ascii", b"a" * 32)
    with pytest.raises(ValueError, match="unique"):
        gateway.IdentityKeyring(active, (gateway.IdentityKey("active", b"b" * 32),))
    with pytest.raises(ValueError, match="exactly one active"):
        gateway.IdentityKeyring(None)
    with pytest.raises(ValueError, match="1-64 ASCII"):
        gateway.IdentityKey("x" * 65, b"a" * 32)
    with pytest.raises(ValueError, match="unique and active-first"):
        gateway.IdentityProof(proof.mint, (proof.mint, proof.mint))


@pytest.mark.parametrize(
    "changed_field",
    ["desktop_key", "task_key"],
)
def test_idempotency_key_reuse_rejects_different_authorized_admission(
    changed_field: str,
    caplog,
) -> None:
    service, store, dispatcher = _service()
    client = TestClient(gateway.build_run_gateway_app(service))
    first = client.post("/v1/runs", json=_body(), headers=_headers())
    changed = _body()
    sentinel = f"alternate-{changed_field.replace('_key', '')}-for-tenant-a"
    changed[changed_field] = sentinel

    replay = client.post("/v1/runs", json=changed, headers=_headers())

    assert first.status_code == 202
    assert replay.status_code == 409
    assert replay.json() == {"error": "idempotency_conflict"}
    assert sentinel not in replay.text
    assert sentinel not in caplog.text
    assert len(dispatcher.spawn_calls) == 1
    stored = store.by_id["run-application-stable"]
    assert isinstance(stored.idempotency_binding, gateway.KeyedDigest)
    assert "desktop-for-tenant-a" not in repr(stored)
    assert "task-for-tenant-a" not in repr(stored)


def test_http_capacity_errors_are_sanitized_and_do_not_echo_identities(caplog) -> None:
    busy_service, _store, _dispatcher = _service(store=FakeRunStore(quota=2))
    busy_client = TestClient(gateway.build_run_gateway_app(busy_service))
    assert busy_client.post("/v1/runs", json=_body(), headers=_headers()).status_code == 202
    busy_body = {**_body(), "idempotency_key": "busy-idempotency-sentinel"}
    busy = busy_client.post("/v1/runs", json=busy_body, headers=_headers())

    quota_service, _store, _dispatcher = _service(store=FakeRunStore(quota=1))
    quota_client = TestClient(gateway.build_run_gateway_app(quota_service))
    assert quota_client.post("/v1/runs", json=_body(), headers=_headers()).status_code == 202
    quota_body = {
        **_body(),
        "desktop_key": "alternate-desktop-for-tenant-a",
        "idempotency_key": "quota-idempotency-sentinel",
    }
    quota = quota_client.post("/v1/runs", json=quota_body, headers=_headers())

    assert busy.status_code == 409
    assert busy.json() == {"error": "desktop_busy"}
    assert quota.status_code == 429
    assert quota.json() == {"error": "tenant_quota_exceeded"}
    assert "busy-idempotency-sentinel" not in busy.text
    assert "quota-idempotency-sentinel" not in quota.text
    assert "idempotency-sentinel" not in caplog.text


def test_cross_tenant_get_poll_and_cancel_are_denied_without_provider_calls() -> None:
    clock = Clock()
    store = FakeRunStore()
    dispatcher = FakeDispatcher()
    record = _running_record(clock)
    store.insert(record)
    service, _store, _dispatcher = _service(clock=clock, store=store, dispatcher=dispatcher)
    client = TestClient(gateway.build_run_gateway_app(service))

    get_response = client.get(f"/v1/runs/{record.run_id}", headers=_headers("tenant-b"))
    cancel_response = client.post(
        f"/v1/runs/{record.run_id}/cancel", headers=_headers("tenant-b")
    )

    assert get_response.status_code == cancel_response.status_code == 404
    assert get_response.json() == cancel_response.json() == {"error": "not_found"}
    assert dispatcher.poll_calls == []
    assert dispatcher.cancel_calls == []


def test_closed_state_machine_permits_every_documented_edge_and_rejects_all_others() -> None:
    clock = Clock()
    call_id = gateway.FunctionCallIdentity("fc-private")
    binding = gateway.KeyedDigest("active", b"d" * 32)
    all_states = set(gateway.RunState)
    for current, next_state in gateway.LEGAL_TRANSITIONS:
        record = gateway.RunRecord(
            run_id="run-state",
            tenant_id="tenant-a",
            idempotency_binding=binding,
            desktop_binding=binding,
            task_binding=binding,
            state=current,
            created_at=clock(),
            updated_at=clock(),
            function_call_id=(
                call_id
                if current in {gateway.RunState.RUNNING, gateway.RunState.CANCELLATION_REQUESTED}
                else None
            ),
        )
        transitioned = record.transition(
            next_state,
            now=clock(),
            function_call_id=(
                call_id
                if (current, next_state)
                == (gateway.RunState.DISPATCHING, gateway.RunState.RUNNING)
                else None
            ),
        )
        assert transitioned.state is next_state
        assert transitioned.version == record.version + 1

    for current in all_states:
        for next_state in all_states:
            if (current, next_state) in gateway.LEGAL_TRANSITIONS:
                continue
            record = gateway.RunRecord(
                run_id="run-state",
                tenant_id="tenant-a",
                idempotency_binding=binding,
                desktop_binding=binding,
                task_binding=binding,
                state=current,
                created_at=clock(),
                updated_at=clock(),
            )
            with pytest.raises(gateway.StateTransitionError):
                record.transition(next_state, now=clock())

    reserved = _reserved_record(clock, run_id="run-state")
    with pytest.raises(gateway.StateTransitionError, match="requires a private call identity"):
        reserved.transition(gateway.RunState.DISPATCHING, now=clock()).transition(
            gateway.RunState.RUNNING, now=clock()
        )


@pytest.mark.asyncio
async def test_pending_reserved_is_claimed_once_under_concurrent_duplicate_requests() -> None:
    clock = Clock()
    store = FakeRunStore()
    dispatcher = FakeDispatcher()
    stale = _reserved_record(clock, run_id="run-stale")
    store.insert(stale, intent=gateway.DispatchIntent.pending(stale.run_id))
    service, _store, _dispatcher = _service(clock=clock, store=store, dispatcher=dispatcher)

    async def submit():
        request = SimpleNamespace(headers={"authorization": "Bearer tenant-a"})
        return await service.create_run(request, gateway.CreateRunRequest(**_body()))

    first, second = await asyncio.gather(submit(), submit())

    assert first.run_id == second.run_id == "run-stale"
    assert len(dispatcher.spawn_calls) == 1
    assert store.by_id["run-stale"].state is gateway.RunState.RUNNING


def test_stale_dispatching_becomes_indeterminate_without_respawn() -> None:
    clock = Clock()
    store = FakeRunStore()
    dispatcher = FakeDispatcher()
    stale = _reserved_record(clock, run_id="run-stale-dispatch").transition(
        gateway.RunState.DISPATCHING,
        now=clock() - timedelta(seconds=20),
    )
    store.insert(stale)
    service, _store, _dispatcher = _service(clock=clock, store=store, dispatcher=dispatcher)
    client = TestClient(gateway.build_run_gateway_app(service))

    response = client.post("/v1/runs", json=_body(), headers=_headers())

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-stale-dispatch", "state": "indeterminate"}
    assert dispatcher.spawn_calls == []


def test_mismatched_stale_reservation_is_rejected_before_reclaim() -> None:
    clock = Clock()
    store = FakeRunStore()
    dispatcher = FakeDispatcher()
    stale = _reserved_record(clock, run_id="run-stale")
    store.insert(stale, intent=gateway.DispatchIntent.pending(stale.run_id))
    service, _store, _dispatcher = _service(clock=clock, store=store, dispatcher=dispatcher)
    client = TestClient(gateway.build_run_gateway_app(service))
    changed = _body()
    changed["task_key"] = "alternate-task-for-tenant-a"

    response = client.post("/v1/runs", json=changed, headers=_headers())

    assert response.status_code == 409
    assert response.json() == {"error": "idempotency_conflict"}
    assert store.by_id["run-stale"].state is gateway.RunState.RESERVED
    assert dispatcher.spawn_calls == []


@pytest.mark.parametrize(
    ("initial", "outcome", "expected"),
    [
        (gateway.RunState.RUNNING, gateway.PollState.PENDING, gateway.RunState.RUNNING),
        (gateway.RunState.RUNNING, gateway.PollState.UNAVAILABLE, gateway.RunState.RUNNING),
        (gateway.RunState.RUNNING, gateway.PollState.SUCCEEDED, gateway.RunState.SUCCEEDED),
        (gateway.RunState.RUNNING, gateway.PollState.FAILED, gateway.RunState.FAILED),
        (gateway.RunState.RUNNING, gateway.PollState.CANCELLED, gateway.RunState.FAILED),
        (
            gateway.RunState.RUNNING,
            gateway.PollState.INDETERMINATE,
            gateway.RunState.INDETERMINATE,
        ),
        (
            gateway.RunState.CANCELLATION_REQUESTED,
            gateway.PollState.CANCELLED,
            gateway.RunState.CANCELLED,
        ),
        (
            gateway.RunState.CANCELLATION_REQUESTED,
            gateway.PollState.SUCCEEDED,
            gateway.RunState.SUCCEEDED,
        ),
        (
            gateway.RunState.CANCELLATION_REQUESTED,
            gateway.PollState.FAILED,
            gateway.RunState.FAILED,
        ),
    ],
)
def test_poll_maps_only_sanitized_terminal_state(initial, outcome, expected) -> None:
    clock = Clock()
    store = FakeRunStore()
    dispatcher = FakeDispatcher()
    dispatcher.poll_state = outcome
    record = _running_record(clock)
    if initial is gateway.RunState.CANCELLATION_REQUESTED:
        record = record.transition(initial, now=clock())
    store.insert(record)
    service, _store, _dispatcher = _service(clock=clock, store=store, dispatcher=dispatcher)
    client = TestClient(gateway.build_run_gateway_app(service))

    response = client.get(f"/v1/runs/{record.run_id}", headers=_headers())

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-owned", "state": expected.value}
    assert "fc-provider-secret" not in response.text


def test_cancellation_records_intent_before_remote_call_and_never_terminates_containers() -> None:
    clock = Clock()
    store = FakeRunStore()
    dispatcher = FakeDispatcher()
    record = _running_record(clock)
    store.insert(record)
    service, _store, _dispatcher = _service(clock=clock, store=store, dispatcher=dispatcher)
    observed_states: list[object] = []

    async def cancel(call_id):
        observed_states.append(store.by_id[record.run_id].state)
        dispatcher.cancel_calls.append(call_id)

    dispatcher.cancel = cancel
    client = TestClient(gateway.build_run_gateway_app(service))

    first = client.post(f"/v1/runs/{record.run_id}/cancel", headers=_headers())
    second = client.post(f"/v1/runs/{record.run_id}/cancel", headers=_headers())

    assert first.json() == second.json() == {
        "run_id": "run-owned",
        "state": "cancellation_requested",
        "cancellation_requested": True,
    }
    assert observed_states == [gateway.RunState.CANCELLATION_REQUESTED]
    assert len(dispatcher.cancel_calls) == 1


@pytest.mark.asyncio
async def test_cancelling_poll_task_leaves_running_state_reconcilable() -> None:
    clock = Clock()
    store = FakeRunStore()
    dispatcher = FakeDispatcher()
    record = _running_record(clock)
    store.insert(record)
    service, _store, _dispatcher = _service(clock=clock, store=store, dispatcher=dispatcher)
    entered = asyncio.Event()

    async def blocked_poll(_call_id):
        entered.set()
        await asyncio.Future()

    dispatcher.poll = blocked_poll
    request = SimpleNamespace(headers={"authorization": "Bearer tenant-a"})
    task = asyncio.create_task(service.get_run(request, record.run_id))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.by_id[record.run_id].state is gateway.RunState.RUNNING
    assert dispatcher.cancel_calls == []


@pytest.mark.asyncio
async def test_cancelling_remote_cancel_task_retains_requested_state_without_retry() -> None:
    clock = Clock()
    store = FakeRunStore()
    dispatcher = FakeDispatcher()
    record = _running_record(clock)
    store.insert(record)
    service, _store, _dispatcher = _service(clock=clock, store=store, dispatcher=dispatcher)
    entered = asyncio.Event()

    async def blocked_cancel(call_id):
        dispatcher.cancel_calls.append(call_id)
        entered.set()
        await asyncio.Future()

    dispatcher.cancel = blocked_cancel
    request = SimpleNamespace(headers={"authorization": "Bearer tenant-a"})
    task = asyncio.create_task(service.cancel_run(request, record.run_id))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.by_id[record.run_id].state is gateway.RunState.CANCELLATION_REQUESTED
    assert len(dispatcher.cancel_calls) == 1


def test_private_values_are_absent_from_models_errors_and_representations() -> None:
    clock = Clock()
    record = _running_record(clock)
    secrets = {
        "fc-provider-secret",
        "sandbox-secret",
        "desktop-id-secret",
        "task-text-secret",
        "task-id-secret",
        "https://daemon.secret",
        "bearer-token-secret",
        "result-content-secret",
    }
    representations = [
        repr(record),
        str(record),
        repr(record.function_call_id),
        str(record.function_call_id),
        repr(
            gateway.ResolvedDesktop(
                SimpleNamespace(sandbox_id="sandbox-secret"), "desktop-id-secret"
            )
        ),
        repr(gateway.ResolvedTask("task-text-secret", "task-id-secret")),
    ]
    for representation in representations:
        assert all(secret not in representation for secret in secrets)
    assert repr(record.function_call_id) == "FunctionCallIdentity(<redacted>)"


@pytest.mark.asyncio
async def test_modal_adapter_uses_native_aio_spawn_poll_and_non_terminating_cancel(
    monkeypatch,
) -> None:
    events: list[tuple[object, ...]] = []

    class AioMethod:
        def __init__(self, name, result=None):
            self.name = name
            self.result = result

        async def aio(self, *args, **kwargs):
            events.append((self.name, args, kwargs))
            return self.result

    class FunctionCall:
        @classmethod
        def from_id(cls, call_id):
            events.append(("from_id", call_id))
            return SimpleNamespace(
                get_call_graph=AioMethod(
                    "get_call_graph",
                    [SimpleNamespace(status=SimpleNamespace(name="SUCCESS"))],
                ),
                get=AioMethod("get", {"status": "succeeded"}),
                cancel=AioMethod("cancel"),
            )

    class ModalError(Exception):
        pass

    fake_modal = SimpleNamespace(
        FunctionCall=FunctionCall,
        exception=SimpleNamespace(
            Error=ModalError,
            AuthError=type("AuthError", (ModalError,), {}),
            ConnectionError=type("ConnectionError", (ModalError,), {}),
            DataLossError=type("DataLossError", (ModalError,), {}),
            DeserializationError=type("DeserializationError", (ModalError,), {}),
            ExecutionError=type("ExecutionError", (ModalError,), {}),
            FunctionTimeoutError=type("FunctionTimeoutError", (ModalError,), {}),
            InternalError=type("InternalError", (ModalError,), {}),
            InternalFailure=type("InternalFailure", (ModalError,), {}),
            InputCancellation=type("InputCancellation", (BaseException,), {}),
            NotFoundError=type("NotFoundError", (ModalError,), {}),
            OutputExpiredError=type("OutputExpiredError", (ModalError,), {}),
            ResourceExhaustedError=type("ResourceExhaustedError", (ModalError,), {}),
            ServiceError=type("ServiceError", (ModalError,), {}),
        ),
    )
    monkeypatch.setattr(gateway, "modal", fake_modal)
    function = SimpleNamespace(
        spawn=AioMethod("spawn", SimpleNamespace(object_id="fc-provider-secret"))
    )
    dispatcher = gateway.ModalTrajectoryDispatcher(function)
    desktop = gateway.ResolvedDesktop(
        SimpleNamespace(sandbox_id="sandbox-secret"), "desktop-id-secret"
    )
    task = gateway.ResolvedTask("task-text-secret", "task-id-secret")

    call_id = await dispatcher.spawn(desktop=desktop, task=task, run_id="run-stable")
    outcome = await dispatcher.poll(call_id)
    await dispatcher.cancel(call_id)

    assert outcome == gateway.PollOutcome(gateway.PollState.SUCCEEDED)
    assert events == [
        (
            "spawn",
            (desktop.handle, task.text, "run-stable"),
            {},
        ),
        ("from_id", "fc-provider-secret"),
        ("get_call_graph", (), {}),
        ("get", (), {"timeout": 0}),
        ("from_id", "fc-provider-secret"),
        ("cancel", (), {"terminate_containers": False}),
    ]


def _fake_modal(
    *, roots=None, graph_exception=None, result=None, get_exception=None, get_calls=None
):
    class ModalError(Exception):
        pass

    exception_types = SimpleNamespace(
        Error=ModalError,
        AuthError=type("AuthError", (ModalError,), {}),
        ConnectionError=type("ConnectionError", (ModalError,), {}),
        DataLossError=type("DataLossError", (ModalError,), {}),
        DeserializationError=type("DeserializationError", (ModalError,), {}),
        ExecutionError=type("ExecutionError", (ModalError,), {}),
        FunctionTimeoutError=type("FunctionTimeoutError", (ModalError,), {}),
        InternalError=type("InternalError", (ModalError,), {}),
        InternalFailure=type("InternalFailure", (ModalError,), {}),
        InputCancellation=type("InputCancellation", (BaseException,), {}),
        NotFoundError=type("NotFoundError", (ModalError,), {}),
        OutputExpiredError=type("OutputExpiredError", (ModalError,), {}),
        ResourceExhaustedError=type("ResourceExhaustedError", (ModalError,), {}),
        ServiceError=type("ServiceError", (ModalError,), {}),
    )

    class Graph:
        async def aio(self):
            if graph_exception is not None:
                raise graph_exception(exception_types)
            return roots

    class Get:
        async def aio(self, *, timeout):
            assert timeout == 0
            if get_calls is not None:
                get_calls.append(timeout)
            if get_exception is not None:
                raise get_exception(exception_types)
            return result

    class FunctionCall:
        @classmethod
        def from_id(cls, call_id):
            assert call_id == "fc-provider-secret"
            return SimpleNamespace(get_call_graph=Graph(), get=Get())

    return SimpleNamespace(FunctionCall=FunctionCall, exception=exception_types)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("PENDING", gateway.PollOutcome(gateway.PollState.PENDING)),
        ("FAILURE", gateway.PollOutcome(gateway.PollState.FAILED)),
        ("INIT_FAILURE", gateway.PollOutcome(gateway.PollState.FAILED)),
        (
            "TIMEOUT",
            gateway.PollOutcome(
                gateway.PollState.FAILED,
                gateway.PollReason.FUNCTION_TIMEOUT,
            ),
        ),
        ("TERMINATED", gateway.PollOutcome(gateway.PollState.TERMINATED)),
    ],
)
async def test_modal_poll_maps_each_non_success_call_graph_status(
    monkeypatch, status, expected
) -> None:
    monkeypatch.setattr(
        gateway,
        "modal",
        _fake_modal(roots=[SimpleNamespace(status=SimpleNamespace(name=status))]),
    )
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    assert await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret")) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "roots",
    [
        [],
        [object(), object()],
        [SimpleNamespace(status=object())],
        [SimpleNamespace(status=SimpleNamespace(name="UNKNOWN"))],
    ],
)
async def test_modal_poll_incomplete_call_graph_is_unavailable_without_result_poll(
    monkeypatch, roots
) -> None:
    get_calls = []
    monkeypatch.setattr(
        gateway,
        "modal",
        _fake_modal(roots=roots, result={"status": "succeeded"}, get_calls=get_calls),
    )
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    outcome = await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))

    assert outcome == gateway.PollOutcome(
        gateway.PollState.UNAVAILABLE,
        gateway.PollReason.CALL_GRAPH_UNAVAILABLE,
    )
    assert get_calls == []


@pytest.mark.asyncio
async def test_modal_poll_lossy_graph_is_unavailable_without_result_poll(monkeypatch) -> None:
    get_calls = []
    runtime = _fake_modal(
        graph_exception=lambda exc: exc.DataLossError(),
        result={"status": "succeeded"},
        get_calls=get_calls,
    )
    monkeypatch.setattr(gateway, "modal", runtime)
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    outcome = await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))

    assert outcome == gateway.PollOutcome(
        gateway.PollState.UNAVAILABLE,
        gateway.PollReason.CALL_GRAPH_UNAVAILABLE,
    )
    assert get_calls == []


@pytest.mark.asyncio
async def test_modal_poll_success_graph_with_remote_user_error_fails(monkeypatch) -> None:
    runtime = _fake_modal(
        roots=[SimpleNamespace(status=SimpleNamespace(name="SUCCESS"))],
        get_exception=lambda _exc: ValueError("private remote failure"),
    )
    monkeypatch.setattr(gateway, "modal", runtime)
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    outcome = await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))

    assert outcome == gateway.PollOutcome(gateway.PollState.FAILED)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed", "indeterminate"])
async def test_modal_poll_validates_and_maps_strict_success_envelope(monkeypatch, status) -> None:
    runtime = _fake_modal(
        roots=[SimpleNamespace(status=SimpleNamespace(name="SUCCESS"))],
        result={"status": status},
    )
    monkeypatch.setattr(gateway, "modal", runtime)
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    outcome = await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))

    assert outcome.state.value == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [None, "succeeded", {}, {"status": "unknown"}, {"status": "succeeded", "result": "x"}],
)
async def test_modal_poll_rejects_invalid_success_envelopes(monkeypatch, result) -> None:
    monkeypatch.setattr(
        gateway,
        "modal",
        _fake_modal(
            roots=[SimpleNamespace(status=SimpleNamespace(name="SUCCESS"))],
            result=result,
        ),
    )
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    outcome = await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))

    assert outcome == gateway.PollOutcome(
        gateway.PollState.INDETERMINATE,
        gateway.PollReason.INVALID_OUTCOME,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_factory", "expected_state", "expected_reason"),
    [
        (lambda _exc: TimeoutError(), gateway.PollState.PENDING, None),
        (
            lambda exc: exc.OutputExpiredError(),
            gateway.PollState.INDETERMINATE,
            gateway.PollReason.OUTPUT_EXPIRED,
        ),
        (
            lambda exc: exc.FunctionTimeoutError(),
            gateway.PollState.FAILED,
            gateway.PollReason.FUNCTION_TIMEOUT,
        ),
        (
            lambda exc: exc.DeserializationError(),
            gateway.PollState.INDETERMINATE,
            gateway.PollReason.RESULT_DATA_LOSS,
        ),
        (
            lambda exc: exc.DataLossError(),
            gateway.PollState.INDETERMINATE,
            gateway.PollReason.RESULT_DATA_LOSS,
        ),
    ],
)
async def test_modal_success_get_classifies_specific_result_errors(
    monkeypatch, exception_factory, expected_state, expected_reason
) -> None:
    monkeypatch.setattr(
        gateway,
        "modal",
        _fake_modal(
            roots=[SimpleNamespace(status=SimpleNamespace(name="SUCCESS"))],
            get_exception=exception_factory,
        ),
    )
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    outcome = await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))

    assert outcome == gateway.PollOutcome(expected_state, expected_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception_name",
    [
        "ConnectionError",
        "ServiceError",
        "AuthError",
        "ResourceExhaustedError",
        "InternalError",
        "InternalFailure",
    ],
)
async def test_modal_poll_transient_errors_are_unavailable(monkeypatch, exception_name) -> None:
    runtime = _fake_modal(
        graph_exception=lambda exc: getattr(exc, exception_name)(),
    )
    monkeypatch.setattr(gateway, "modal", runtime)
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    outcome = await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))

    assert outcome == gateway.PollOutcome(
        gateway.PollState.UNAVAILABLE,
        gateway.PollReason.TRANSIENT_PROVIDER_ERROR,
    )


@pytest.mark.asyncio
async def test_modal_poll_missing_call_is_indeterminate(monkeypatch) -> None:
    runtime = _fake_modal(graph_exception=lambda exc: exc.NotFoundError())
    monkeypatch.setattr(gateway, "modal", runtime)
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    outcome = await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))

    assert outcome == gateway.PollOutcome(
        gateway.PollState.INDETERMINATE,
        gateway.PollReason.MISSING_CALL,
    )


@pytest.mark.asyncio
async def test_modal_poll_propagates_async_request_cancellation(monkeypatch) -> None:
    entered = asyncio.Event()

    class Graph:
        async def aio(self):
            entered.set()
            await asyncio.Future()

    class FunctionCall:
        @classmethod
        def from_id(cls, _call_id):
            return SimpleNamespace(get_call_graph=Graph())

    runtime = _fake_modal(roots=[])
    runtime.FunctionCall = FunctionCall
    monkeypatch.setattr(gateway, "modal", runtime)
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())
    task = asyncio.create_task(
        dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_modal_adapter_fails_closed_when_modal_is_absent(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "modal", None)
    dispatcher = gateway.ModalTrajectoryDispatcher(SimpleNamespace())

    with pytest.raises(ImportError, match="Modal is required"):
        await dispatcher.poll(gateway.FunctionCallIdentity("fc-provider-secret"))


def test_example_has_no_primitive_proxy_core_export_or_production_memory_store() -> None:
    root = Path(__file__).resolve().parents[2]
    entry_source = (root / "examples" / "modal_run_gateway.py").read_text()
    source = "\n".join(
        path.read_text() for path in (root / "examples" / "run_gateway").glob("*.py")
    )
    core_exports = (root / "src" / "modal_computer_use" / "__init__.py").read_text()
    handoff = (root / "examples" / "modal_function_session_handoff.py").read_text()

    assert 'app.post("/v1/runs"' in source
    assert 'app.get("/v1/runs/{run_id}"' in source
    assert 'app.post("/v1/runs/{run_id}/cancel"' in source
    assert "/screenshots" not in source
    assert "/actions" not in source
    assert "class InMemoryRunStore" not in source
    assert "reserve_if_absent" not in source
    assert "sqlalchemy" not in source.lower()
    assert "modal.Dict" not in source
    assert "modal.Queue" not in source
    assert "modal.Volume" not in source
    assert "openai" not in source.lower()
    assert "anthropic" not in source.lower()
    assert "RunGatewayService" not in core_exports
    assert len(entry_source.splitlines()) < 80
    assert "async with handle.borrow_async" in handoff
    assert "run_id=run_id" in handoff


def test_missing_dependencies_and_default_service_fail_closed() -> None:
    with pytest.raises(ValueError, match="all authentication"):
        gateway.RunGatewayService(
            principal_resolver=None,
            session_catalog=FakeSessionCatalog(),
            task_catalog=FakeTaskCatalog(),
            run_store=FakeRunStore(),
            dispatcher=FakeDispatcher(),
            identity_keyring=gateway.IdentityKeyring(
                gateway.IdentityKey("active", b"a" * 32)
            ),
        )
    with pytest.raises(ValueError, match="application-configured"):
        gateway.build_run_gateway_app(None)
    with pytest.raises(RuntimeError, match="inject the application's PrincipalResolver"):
        gateway.build_default_service()


def test_compatibility_entry_reexports_modal_run_gateway_class_when_installed() -> None:
    from run_gateway import modal_adapter

    if gateway.modal is None:
        assert not hasattr(gateway, "RunGateway")
        return
    assert gateway.RunGateway is modal_adapter.RunGateway
    assert "RunGateway" in gateway.__all__


def test_qualified_entry_shares_package_class_identity_and_accepts_package_service(
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.delitem(sys.modules, "examples", raising=False)
    importlib.invalidate_caches()
    qualified = importlib.import_module("examples.modal_run_gateway")
    package = importlib.import_module("examples.run_gateway")

    assert qualified.RunGatewayService is package.RunGatewayService
    assert qualified.RunRecord is package.RunRecord
    assert qualified.ModalTrajectoryDispatcher is package.ModalTrajectoryDispatcher
    if qualified.modal is not None:
        assert qualified.RunGateway is package.modal_adapter.RunGateway

    service = qualified.RunGatewayService(
        principal_resolver=FakePrincipalResolver(),
        session_catalog=FakeSessionCatalog(),
        task_catalog=FakeTaskCatalog(),
        run_store=FakeRunStore(types=package),
        dispatcher=FakeDispatcher(),
        identity_keyring=package.IdentityKeyring(
            package.IdentityKey("active", b"a" * 32)
        ),
        run_id_factory=lambda: "run-qualified",
    )
    client = TestClient(qualified.build_run_gateway_app(service))

    response = client.post("/v1/runs", json=_body(), headers=_headers())

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-qualified", "state": "running"}
