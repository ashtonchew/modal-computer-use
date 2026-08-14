from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient

from modal_computer_use.daemon.actions import batch as action_batch
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.leases import (
    LEASE_EPOCH_HEADER,
    LEASE_FENCE_HEADER,
    LEASE_ID_HEADER,
    LEASE_TOKEN_HEADER,
    LeaseCoordinator,
    MutationLease,
)
from modal_computer_use.daemon.receipts import (
    MAX_OPERATION_SEQUENCE,
    OPERATION_SEQUENCE_HEADER,
    ReceiptJournal,
    require_operation_sequence,
)
from modal_computer_use.daemon.routes import leases as lease_routes
from modal_computer_use.daemon.routes import recovery as recovery_routes
from modal_computer_use.daemon.routes.recovery import OWNER_PROOF_HEADER
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ActionResult


def _app(tmp_path: Path, **overrides: Any):
    return create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
            tunnel_token="owner-proof",
            **overrides,
        )
    )


def _acquire(client: TestClient, run_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    response = client.post("/v1/leases/acquire", json={"run_id": run_id})
    assert response.status_code == 200, response.text
    body = response.json()
    return body, {
        LEASE_ID_HEADER: body["lease_id"],
        LEASE_EPOCH_HEADER: body["daemon_epoch"],
        LEASE_FENCE_HEADER: str(body["fence"]),
        LEASE_TOKEN_HEADER: response.headers[LEASE_TOKEN_HEADER],
    }


def _operation_headers(lease_headers: dict[str, str], sequence: int) -> dict[str, str]:
    return {**lease_headers, OPERATION_SEQUENCE_HEADER: str(sequence)}


def _owner_headers() -> dict[str, str]:
    return {OWNER_PROOF_HEADER: "owner-proof"}


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _route_request(app, path: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in headers.items()
            ],
            "client": ("testclient", 50_000),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


async def _wait_for_thread_event(event: threading.Event) -> None:
    for _ in range(2_000):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("timed out waiting for receipt worker")


def _block_after_sync_commit(original, committed: threading.Event, release: threading.Event):
    def blocking(*args):
        result = original(*args)
        committed.set()
        if not release.wait(timeout=2):
            raise AssertionError("timed out releasing receipt worker")
        return result

    return blocking


def test_only_predispatch_receipt_connection_requests_full(tmp_path, monkeypatch) -> None:
    journal = ReceiptJournal(tmp_path / "runtime")
    synchronous_modes: list[str] = []
    original_connect = journal._connect

    def recording_connect(*, synchronous="NORMAL"):
        connection = original_connect(synchronous=synchronous)
        synchronous_modes.append(synchronous)
        expected = 2 if synchronous == "FULL" else 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == expected
        return connection

    monkeypatch.setattr(journal, "_connect", recording_connect)

    async def exercise() -> None:
        await journal.start()
        synchronous_modes.clear()
        await journal.activate_run("run-synchronous-mode")
        handle = await journal.begin(
            lease=MutationLease(
                run_id="run-synchronous-mode",
                epoch="epoch",
                fence=1,
            ),
            sequence=0,
            operation_kind="test.synchronous.mode",
            semantic_data={},
        )
        await journal.complete(handle)
        await journal.receipt_status("run-synchronous-mode", 0)
        await journal.recovery_status()
        await journal.close()

    asyncio.run(exercise())

    assert synchronous_modes == ["NORMAL", "FULL", "NORMAL", "NORMAL", "NORMAL"]


def test_quarantine_persistence_failure_remains_acknowledgeable(
    tmp_path, monkeypatch
) -> None:
    journal = ReceiptJournal(tmp_path / "runtime")

    async def exercise() -> None:
        await journal.start()

        def fail_quarantine(*_args: Any) -> None:
            raise OSError("receipt database unavailable")

        def fail_recovery_status(*_args: Any) -> None:
            raise OSError("receipt database unavailable")

        monkeypatch.setattr(journal, "_quarantine_sync", fail_quarantine)
        original_recovery_status = journal._recovery_status_sync
        monkeypatch.setattr(journal, "_recovery_status_sync", fail_recovery_status)
        with pytest.raises(OSError):
            await journal.quarantine_run(
                "run-quarantine-failure",
                classification="lease_cleanup_failed",
            )

        recovery = await journal.recovery_status()
        assert recovery["recovery_required"] is True
        incident_id = recovery["incident_id"]
        assert isinstance(incident_id, str) and incident_id.startswith("incident_")
        assert recovery["classification"] == "lease_cleanup_failed"
        acknowledged = await journal.acknowledge(incident_id)
        assert acknowledged == {"recovery_required": False, "acknowledged": True}
        monkeypatch.setattr(journal, "_recovery_status_sync", original_recovery_status)
        assert (await journal.recovery_status())["recovery_required"] is False
        await journal.close()

    asyncio.run(exercise())


def test_full_receipt_commit_returns_before_mutation_dispatch(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    original_connect = sqlite3.connect

    class RecordingConnection(sqlite3.Connection):
        def commit(self) -> None:
            in_progress = self.execute(
                "SELECT 1 FROM operations WHERE state = 'IN_PROGRESS' LIMIT 1"
            ).fetchone()
            if in_progress is not None:
                assert self.execute("PRAGMA synchronous").fetchone()[0] == 2
                events.append("full_commit_started")
                super().commit()
                events.append("full_commit_returned")
                return
            super().commit()

    def recording_connect(*args, **kwargs):
        return original_connect(*args, factory=RecordingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    app = _app(tmp_path)
    original_move = app.state.backend.mouse_move

    async def tracked_move(x: int, y: int):
        events.append("mutation_dispatched")
        return await original_move(x, y)

    app.state.backend.mouse_move = tracked_move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-commit-order")
        response = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )

    assert response.status_code == 200
    assert events == [
        "full_commit_started",
        "full_commit_returned",
        "mutation_dispatched",
    ]


def test_predispatch_commit_failure_prevents_mutation(tmp_path, monkeypatch) -> None:
    original_connect = sqlite3.connect
    move_calls = 0

    class FailingConnection(sqlite3.Connection):
        def commit(self) -> None:
            in_progress = self.execute(
                "SELECT 1 FROM operations WHERE state = 'IN_PROGRESS' LIMIT 1"
            ).fetchone()
            if (
                in_progress is not None
                and self.execute("PRAGMA synchronous").fetchone()[0] == 2
            ):
                raise sqlite3.OperationalError("injected pre-dispatch commit failure")
            super().commit()

    def failing_connect(*args, **kwargs):
        return original_connect(*args, factory=FailingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", failing_connect)
    app = _app(tmp_path)
    original_move = app.state.backend.mouse_move

    async def tracked_move(x: int, y: int):
        nonlocal move_calls
        move_calls += 1
        return await original_move(x, y)

    app.state.backend.mouse_move = tracked_move
    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        _, lease = _acquire(client, "run-commit-failure")
        response = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )

    assert response.status_code == 500
    assert move_calls == 0


def test_connection_rejects_unexpected_synchronous_pragma(tmp_path, monkeypatch) -> None:
    original_connect = sqlite3.connect

    class UnexpectedPragmaResult:
        @staticmethod
        def fetchone() -> tuple[int]:
            return (1,)

    class MisreportingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA synchronous":
                return UnexpectedPragmaResult()
            return super().execute(sql, parameters)

    def misreporting_connect(*args, **kwargs):
        return original_connect(*args, factory=MisreportingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", misreporting_connect)
    journal = ReceiptJournal(tmp_path / "runtime")
    journal._runtime_dir.mkdir(parents=True)

    with pytest.raises(
        RuntimeError,
        match="receipt journal synchronous mode was not applied",
    ):
        journal._connect(synchronous="FULL")


def test_connection_rejects_unavailable_wal_mode(tmp_path, monkeypatch) -> None:
    original_connect = sqlite3.connect

    class FallbackJournalModeResult:
        @staticmethod
        def fetchone() -> tuple[str]:
            return ("delete",)

    class FallbackJournalModeConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA journal_mode=WAL":
                return FallbackJournalModeResult()
            return super().execute(sql, parameters)

    def fallback_connect(*args, **kwargs):
        return original_connect(*args, factory=FallbackJournalModeConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fallback_connect)
    journal = ReceiptJournal(tmp_path / "runtime")
    journal._runtime_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="receipt journal WAL mode was not applied"):
        journal._connect(synchronous="FULL")


def test_gap_free_sequence_replay_conflict_and_no_silent_eviction(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-sequences")
        first = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )
        same = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )
        conflict = client.post(
            "/v1/mouse/move",
            json={"x": 2, "y": 3},
            headers=_operation_headers(lease, 0),
        )
        gap = client.post(
            "/v1/mouse/move",
            json={"x": 4, "y": 5},
            headers=_operation_headers(lease, 2),
        )
        second = client.post(
            "/v1/mouse/move",
            json={"x": 4, "y": 5},
            headers=_operation_headers(lease, 1),
        )

    assert first.status_code == second.status_code == 200
    assert same.json()["code"] == "operation_result_unavailable"
    assert conflict.json()["code"] == "run_sequence_conflict"
    assert gap.json()["code"] == "operation_sequence_gap"
    assert gap.json()["details"] == {"expected_sequence": 1, "received_sequence": 2}
    with sqlite3.connect(app.state.receipt_journal.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 2


def test_validation_and_missing_sequence_do_not_consume(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-validation")
        missing = client.post("/v1/mouse/move", json={"x": 1, "y": 2}, headers=lease)
        invalid = client.post(
            "/v1/mouse/move",
            json={"x": 5000, "y": 2},
            headers=_operation_headers(lease, 0),
        )
        valid = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )

    assert missing.json()["code"] == "operation_sequence_required"
    assert invalid.status_code == 422
    assert valid.status_code == 200


def test_operation_sequence_rejects_values_above_sqlite_integer_range(tmp_path) -> None:
    app = _app(tmp_path)
    too_large = MAX_OPERATION_SEQUENCE + 1
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-sequence-range")
        mutation = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, too_large),
        )
        status = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-sequence-range", "sequence": too_large},
            headers=_owner_headers(),
        )
        very_long = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers={**lease, OPERATION_SEQUENCE_HEADER: "9" * 10_000},
        )
        valid = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )

    assert mutation.status_code == 422
    assert mutation.json()["code"] == "invalid_operation_sequence"
    assert status.status_code == 422
    assert very_long.status_code == 422
    assert very_long.json()["code"] == "invalid_operation_sequence"
    assert valid.status_code == 200
    with pytest.raises(DaemonError) as unicode_digit:
        require_operation_sequence("²")
    assert unicode_digit.value.status_code == 422
    assert unicode_digit.value.code == "invalid_operation_sequence"


def test_borrower_status_validates_lease_while_holding_input_lock(
    tmp_path, monkeypatch
) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-status-lock")
        completed = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )
        assert completed.status_code == 200
        original_validate = app.state.lease_coordinator.validate_mutation

        def validate_while_locked(credentials):
            assert app.state.input_lock.locked()
            return original_validate(credentials)

        monkeypatch.setattr(
            app.state.lease_coordinator,
            "validate_mutation",
            validate_while_locked,
        )
        recovery = client.get("/v1/recovery/status", headers=lease)
        receipt = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-status-lock", "sequence": 0},
            headers=lease,
        )

    assert recovery.status_code == 200
    assert receipt.status_code == 200


def test_local_token_can_supply_owner_proof_only_with_existing_auth(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="local-owner-proof",
            tunnel_token=None,
        )
    )
    with TestClient(app) as client:
        proof_without_auth = client.get(
            "/v1/recovery/status",
            headers={OWNER_PROOF_HEADER: "local-owner-proof"},
        )
        auth_without_proof = client.get(
            "/v1/recovery/status",
            headers={"Authorization": "Bearer local-owner-proof"},
        )
        authorized = client.get(
            "/v1/recovery/status",
            headers={
                "Authorization": "Bearer local-owner-proof",
                OWNER_PROOF_HEADER: "local-owner-proof",
            },
        )

    assert proof_without_auth.status_code == 401
    assert auth_without_proof.json()["code"] == "recovery_access_denied"
    assert authorized.status_code == 200
    assert authorized.json()["recovery_required"] is False


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("put", "/v1/clipboard/text", {"json": {"text": "secret-clipboard"}}),
        ("post", "/v1/commands/run", {"json": {"command": ["printf", "secret-output"]}}),
        (
            "post",
            "/v1/browser/open-url",
            {"json": {"url": "https://secret.example/path", "wait_for_window": False}},
        ),
        ("post", "/v1/apps/launch", {"json": {"command": "xterm"}}),
        ("post", "/v1/windows/window-1/activate", {}),
        ("put", "/v1/artifacts/secret.txt", {"content": b"secret-artifact-bytes"}),
        ("post", "/v1/recordings", {"json": {}}),
        ("post", "/v1/processes/xvfb/restart", {}),
    ],
)
def test_representative_http_mutations_require_and_consume_sequence(
    tmp_path,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-route")
        missing = getattr(client, method)(path, headers=lease, **kwargs)
        admitted = getattr(client, method)(
            path,
            headers=_operation_headers(lease, 0),
            **kwargs,
        )

    assert missing.status_code == 409
    assert missing.json()["code"] == "operation_sequence_required"
    assert admitted.status_code < 400, admitted.text


def test_actions_http_and_hot_websocket_sequence_contract(tmp_path) -> None:
    app = _app(tmp_path)
    action = {"actions": [{"type": "move", "x": 3, "y": 4}]}
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-actions")
        http = client.post(
            "/v1/actions/run",
            json=action,
            headers=_operation_headers(lease, 0),
        )
        http_replay = client.post(
            "/v1/actions/run",
            json=action,
            headers=_operation_headers(lease, 0),
        )
        with client.websocket_connect("/v1/session/hot", headers=lease) as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json({"id": "missing", "op": "run_actions", "payload": action})
            missing = websocket.receive_json()
            websocket.send_json(
                {"id": "next", "op": "run_actions", "sequence": 1, "payload": action}
            )
            admitted = websocket.receive_json()
            websocket.send_json(
                {
                    "id": "raw",
                    "op": "run_raw_screenshot",
                    "sequence": 2,
                    "payload": {
                        **action,
                        "screenshot_after": True,
                        "screenshot_options": {"format": "png", "show_cursor": False},
                    },
                }
            )
            raw = websocket.receive_json()
            assert websocket.receive_bytes()

    assert http.status_code == 200
    assert http_replay.json()["code"] == "operation_result_unavailable"
    assert missing["error"]["code"] == "operation_sequence_required"
    assert admitted["type"] == "result"
    assert raw["type"] == "binary"


def test_disappearing_second_cache_lookup_never_strands_in_progress_receipt(
    tmp_path, monkeypatch
) -> None:
    app = _app(tmp_path)
    request_body = {"actions": [{"type": "move", "x": 3, "y": 4}]}
    lookup_calls = 0
    readiness_calls = 0
    original_lookup = action_batch._cached_idempotency_result
    original_readiness = action_batch._ensure_desktop_ready

    def disappearing_second_lookup(context, key, fingerprint):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 2:
            return None
        return original_lookup(context, key, fingerprint)

    async def fail_once_after_cache_disappears(context, *, force: bool = False):
        nonlocal readiness_calls
        readiness_calls += 1
        if readiness_calls == 1:
            raise DaemonError(
                "desktop became unavailable",
                status_code=503,
                code="desktop_not_ready",
            )
        return await original_readiness(context, force=force)

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        seeded = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "cache-disappears"},
            json=request_body,
        )
        assert seeded.status_code == 200
        _, lease = _acquire(client, "run-cache-disappears")
        monkeypatch.setattr(
            action_batch,
            "_cached_idempotency_result",
            disappearing_second_lookup,
        )
        monkeypatch.setattr(
            action_batch,
            "_ensure_desktop_ready",
            fail_once_after_cache_disappears,
        )
        failed = client.post(
            "/v1/actions/run",
            headers={
                **_operation_headers(lease, 0),
                "Idempotency-Key": "cache-disappears",
            },
            json=request_body,
        )
        missing = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-cache-disappears", "sequence": 0},
            headers=lease,
        )
        retried = client.post(
            "/v1/actions/run",
            headers={
                **_operation_headers(lease, 0),
                "Idempotency-Key": "cache-disappears",
            },
            json=request_body,
        )

    assert failed.json()["code"] == "desktop_not_ready"
    assert missing.json()["state"] == "MISSING"
    assert retried.status_code == 200
    assert lookup_calls >= 4


def test_observation_websocket_revalidates_each_action_sequence(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-observation")
        with client.websocket_connect("/v1/observations/stream", headers=lease) as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "id": "start",
                    "op": "start",
                    "payload": {"fps": 0.01, "format": "png", "show_cursor": False},
                }
            )
            assert websocket.receive_json()["type"] == "started"
            assert websocket.receive_json()["type"] == "frame"
            assert websocket.receive_bytes()
            websocket.send_json(
                {
                    "id": "missing",
                    "op": "run_actions_capture",
                    "payload": {"actions": [{"type": "move", "x": 1, "y": 2}]},
                }
            )
            missing = websocket.receive_json()
            websocket.send_json(
                {
                    "id": "admitted",
                    "op": "run_actions_capture",
                    "sequence": 0,
                    "payload": {"actions": [{"type": "move", "x": 1, "y": 2}]},
                }
            )
            admitted = websocket.receive_json()
            if admitted["type"] == "frame":
                assert websocket.receive_bytes()
            websocket.send_json(
                {
                    "id": "observe",
                    "op": "run_actions_observe_change",
                    "sequence": 1,
                    "payload": {
                        "actions": [{"type": "click", "x": 1, "y": 2}],
                        "change_timeout_ms": 1,
                        "poll_interval_ms": 1,
                    },
                }
            )
            observed = websocket.receive_json()
            if observed["type"] == "frame":
                assert websocket.receive_bytes()

    assert missing["error"]["code"] == "operation_sequence_required"
    assert admitted["type"] in {"frame", "unchanged"}
    assert observed["type"] in {"frame", "unchanged"}


def test_action_timeout_stops_continue_on_error_and_quarantines(tmp_path) -> None:
    app = _app(tmp_path)
    calls = 0
    screenshot_calls = 0
    original_screenshot = app.state.backend.screenshot

    async def slow_move(x: int, y: int):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return type(app.state.backend.cursor)(x=x, y=y)

    app.state.backend.mouse_move = slow_move

    async def tracked_screenshot(*args, **kwargs):
        nonlocal screenshot_calls
        screenshot_calls += 1
        return await original_screenshot(*args, **kwargs)

    app.state.backend.screenshot = tracked_screenshot
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-timeout")
        response = client.post(
            "/v1/actions/run",
            headers=_operation_headers(lease, 0),
            json={
                "continue_on_error": True,
                "screenshot_after": True,
                "actions": [
                    {"type": "move", "x": 1, "y": 2, "timeout_ms": 1},
                    {"type": "move", "x": 3, "y": 4},
                ],
            },
        )

    assert response.json()["code"] == "recovery_required"
    assert calls == 1
    assert screenshot_calls == 0


def test_screenshot_after_timeout_completes_receipt_without_quarantine(tmp_path) -> None:
    app = _app(tmp_path)
    original = app.state.backend.screenshot

    async def slow_screenshot(*args, **kwargs):
        await asyncio.sleep(0.03)
        return await original(*args, **kwargs)

    app.state.backend.screenshot = slow_screenshot
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-observation-timeout")
        response = client.post(
            "/v1/actions/run",
            headers=_operation_headers(lease, 0),
            json={
                "actions": [{"type": "move", "x": 1, "y": 2}],
                "screenshot_after": True,
                "max_action_timeout_ms": 5,
            },
        )
        recovery = client.get("/v1/recovery/status", headers=lease)
        receipt = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-observation-timeout", "sequence": 0},
            headers=lease,
        )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert recovery.json()["recovery_required"] is False
    assert receipt.json()["state"] == "COMPLETED"


def test_partial_input_quarantines_legacy_and_leased_mutations_but_not_reads(tmp_path) -> None:
    app = _app(tmp_path)

    async def uncertain_move(_x: int, _y: int):
        raise DaemonError("partial", code="input_may_be_partial")

    app.state.backend.mouse_move = uncertain_move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-partial")
        partial = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )
        incident = partial.json()["details"]["incident_id"]
        legacy = client.post("/v1/input/release-all")
        screenshot = client.post("/v1/screenshots/full/raw", json={"storage": "inline"})
        borrower_status = client.get("/v1/recovery/status", headers=lease)
        unrelated_receipt = client.post(
            "/v1/receipts/status",
            json={"run_id": "some-other-run", "sequence": 0},
            headers=lease,
        )
        no_owner = client.post(
            "/v1/recovery/acknowledge",
            json={"incident_id": incident},
            headers=lease,
        )
        wrong = client.post(
            "/v1/recovery/acknowledge",
            json={"incident_id": "incident_wrong"},
            headers=_owner_headers(),
        )
        acknowledged = client.post(
            "/v1/recovery/acknowledge",
            json={"incident_id": incident},
            headers=_owner_headers(),
        )
        new_run = client.post("/v1/leases/acquire", json={"run_id": "run-after-recovery"})

    assert partial.json()["code"] == "recovery_required"
    assert legacy.json()["code"] == "recovery_required"
    assert screenshot.status_code == 200
    assert borrower_status.json()["incident_id"] == incident
    assert unrelated_receipt.json()["code"] == "receipt_access_denied"
    assert no_owner.json()["code"] == "owner_authorization_required"
    assert wrong.json()["code"] == "recovery_incident_mismatch"
    assert acknowledged.json() == {"recovery_required": False, "acknowledged": True}
    assert new_run.status_code == 200


def test_indeterminate_window_result_quarantines_before_receipt_completion(tmp_path) -> None:
    app = _app(tmp_path)

    async def indeterminate_activate(_window_id: str) -> ActionResult:
        return ActionResult(
            ok=False,
            message="window request outcome unknown",
            output={
                "code": "window_request_indeterminate",
                "indeterminate": True,
            },
        )

    app.state.backend.activate_window = indeterminate_activate
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-window-indeterminate")
        response = client.post(
            "/v1/windows/window-1/activate",
            headers=_operation_headers(lease, 0),
        )
        receipt = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-window-indeterminate", "sequence": 0},
            headers=_owner_headers(),
        )

    assert response.json()["code"] == "recovery_required"
    assert receipt.json()["state"] == "INDETERMINATE"


def test_batch_indeterminate_metadata_stops_and_quarantines(tmp_path) -> None:
    app = _app(tmp_path)
    move_calls = 0

    async def indeterminate_scroll(*_args, **_kwargs) -> ActionResult:
        return ActionResult(
            ok=False,
            message="request outcome unknown",
            output={
                "code": "window_request_indeterminate",
                "indeterminate": True,
            },
        )

    original_move = app.state.backend.mouse_move

    async def tracked_move(x: int, y: int):
        nonlocal move_calls
        move_calls += 1
        return await original_move(x, y)

    app.state.backend.mouse_scroll = indeterminate_scroll
    app.state.backend.mouse_move = tracked_move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-batch-indeterminate")
        response = client.post(
            "/v1/actions/run",
            headers=_operation_headers(lease, 0),
            json={
                "continue_on_error": True,
                "actions": [
                    {"type": "scroll", "direction": "down", "amount": 1},
                    {"type": "move", "x": 3, "y": 4},
                ],
            },
        )
        receipt = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-batch-indeterminate", "sequence": 0},
            headers=_owner_headers(),
        )

    assert response.json()["code"] == "recovery_required"
    assert receipt.json()["state"] == "INDETERMINATE"
    assert move_calls == 0


def test_terminal_commit_failure_immediately_quarantines_all_mutations(
    tmp_path, monkeypatch
) -> None:
    clock = _FakeClock()
    app = _app(tmp_path)
    app.state.lease_coordinator = LeaseCoordinator(clock=clock, ttl_seconds=1)
    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        _, lease = _acquire(client, "run-terminal-failure")

        def fail_terminal_commit(*_args, **_kwargs) -> None:
            raise sqlite3.OperationalError("injected terminal commit failure")

        monkeypatch.setattr(app.state.receipt_journal, "_finish_sync", fail_terminal_commit)
        failed = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )
        legacy = client.post("/v1/input/release-all")
        leased = client.post(
            "/v1/mouse/move",
            json={"x": 3, "y": 4},
            headers=_operation_headers(lease, 1),
        )
        recovery = client.get("/v1/recovery/status", headers=lease)
        clock.advance(1)
        after_expiry = client.post(
            "/v1/leases/acquire",
            json={"run_id": "run-after-terminal-failure"},
        )
        incident_id = recovery.json()["incident_id"]
        acknowledged = client.post(
            "/v1/recovery/acknowledge",
            json={"incident_id": incident_id},
            headers=_owner_headers(),
        )
        reconciled = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-terminal-failure", "sequence": 0},
            headers=_owner_headers(),
        )
        new_run = client.post(
            "/v1/leases/acquire",
            json={"run_id": "run-after-terminal-failure"},
        )

    assert failed.status_code == 500
    assert legacy.json()["code"] == "recovery_required"
    assert leased.json()["code"] == "recovery_required"
    assert recovery.json()["recovery_required"] is True
    assert recovery.json()["classification"] == "terminal_commit_failed"
    assert recovery.json()["incident_id"].startswith("incident_")
    assert after_expiry.json()["code"] == "recovery_required"
    assert acknowledged.json() == {"recovery_required": False, "acknowledged": True}
    assert reconciled.json()["state"] == "INDETERMINATE"
    assert reconciled.json()["incident_id"] == incident_id
    assert new_run.status_code == 200


def test_begin_cancellation_rolls_back_committed_in_progress_receipt(
    tmp_path, monkeypatch
) -> None:
    journal = ReceiptJournal(tmp_path / "runtime")
    committed = threading.Event()
    release = threading.Event()

    async def exercise() -> None:
        await journal.start()
        await journal.activate_run("run-cancel-begin")
        original = journal._begin_sync
        monkeypatch.setattr(
            journal,
            "_begin_sync",
            _block_after_sync_commit(original, committed, release),
        )
        task = asyncio.create_task(
            journal.begin(
                lease=MutationLease(run_id="run-cancel-begin", epoch="epoch", fence=1),
                sequence=0,
                operation_kind="test.cancel.begin",
                semantic_data={"value": 1},
            )
        )
        await _wait_for_thread_event(committed)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await journal.receipt_status("run-cancel-begin", 0))["state"] == "MISSING"
        assert (await journal.recovery_status())["recovery_required"] is False
        retry = await journal.begin(
            lease=MutationLease(run_id="run-cancel-begin", epoch="epoch", fence=1),
            sequence=0,
            operation_kind="test.cancel.begin",
            semantic_data={"value": 1},
        )
        await journal.complete(retry)
        await journal.close()

    asyncio.run(exercise())


def test_complete_cancellation_clears_phantom_memory_quarantine(
    tmp_path, monkeypatch
) -> None:
    journal = ReceiptJournal(tmp_path / "runtime")
    committed = threading.Event()
    release = threading.Event()

    async def exercise() -> None:
        await journal.start()
        await journal.activate_run("run-cancel-complete")
        handle = await journal.begin(
            lease=MutationLease(run_id="run-cancel-complete", epoch="epoch", fence=1),
            sequence=0,
            operation_kind="test.cancel.complete",
            semantic_data={},
        )
        original = journal._finish_sync
        monkeypatch.setattr(
            journal,
            "_finish_sync",
            _block_after_sync_commit(original, committed, release),
        )
        task = asyncio.create_task(journal.complete(handle))
        await _wait_for_thread_event(committed)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await journal.receipt_status("run-cancel-complete", 0))["state"] == "COMPLETED"
        assert (await journal.recovery_status())["recovery_required"] is False
        await journal.close()

    asyncio.run(exercise())


def test_abandon_cancellation_preserves_missing_retryable_sequence(
    tmp_path, monkeypatch
) -> None:
    journal = ReceiptJournal(tmp_path / "runtime")
    committed = threading.Event()
    release = threading.Event()

    async def exercise() -> None:
        await journal.start()
        await journal.activate_run("run-cancel-abandon")
        lease = MutationLease(run_id="run-cancel-abandon", epoch="epoch", fence=1)
        handle = await journal.begin(
            lease=lease,
            sequence=0,
            operation_kind="test.cancel.abandon",
            semantic_data={},
        )
        original = journal._abandon_sync
        monkeypatch.setattr(
            journal,
            "_abandon_sync",
            _block_after_sync_commit(original, committed, release),
        )
        task = asyncio.create_task(journal.abandon(handle, classification="not_started"))
        await _wait_for_thread_event(committed)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await journal.receipt_status("run-cancel-abandon", 0))["state"] == "MISSING"
        assert (await journal.recovery_status())["recovery_required"] is False
        retry = await journal.begin(
            lease=lease,
            sequence=0,
            operation_kind="test.cancel.abandon",
            semantic_data={},
        )
        await journal.complete(retry)
        await journal.close()

    asyncio.run(exercise())


def test_indeterminate_cancellation_preserves_durable_quarantine(
    tmp_path, monkeypatch
) -> None:
    journal = ReceiptJournal(tmp_path / "runtime")
    committed = threading.Event()
    release = threading.Event()

    async def exercise() -> None:
        await journal.start()
        await journal.activate_run("run-cancel-indeterminate")
        handle = await journal.begin(
            lease=MutationLease(
                run_id="run-cancel-indeterminate", epoch="epoch", fence=1
            ),
            sequence=0,
            operation_kind="test.cancel.indeterminate",
            semantic_data={},
        )
        original = journal._finish_sync
        monkeypatch.setattr(
            journal,
            "_finish_sync",
            _block_after_sync_commit(original, committed, release),
        )
        task = asyncio.create_task(
            journal.mark_indeterminate(handle, classification="cancelled_after_dispatch")
        )
        await _wait_for_thread_event(committed)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        receipt = await journal.receipt_status("run-cancel-indeterminate", 0)
        recovery = await journal.recovery_status()
        assert receipt["state"] == "INDETERMINATE"
        assert recovery["recovery_required"] is True
        assert recovery["incident_id"] == receipt["incident_id"]
        await journal.close()

    asyncio.run(exercise())


def test_recovery_route_cancellation_completes_acknowledge_and_lease_reset(
    tmp_path, monkeypatch
) -> None:
    app = _app(tmp_path)
    acknowledged = threading.Event()
    allow_reset = threading.Event()

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            state = app.state
            async with state.input_lock:
                await state.receipt_journal.activate_run("run-cancel-ack-route")
                grant = state.lease_coordinator.acquire("run-cancel-ack-route")
                handle = await state.receipt_journal.begin(
                    lease=MutationLease(
                        run_id="run-cancel-ack-route",
                        epoch=grant.epoch,
                        fence=grant.fence,
                    ),
                    sequence=0,
                    operation_kind="test.cancel.ack.route",
                    semantic_data={},
                )
                incident_id = await state.receipt_journal.mark_indeterminate(
                    handle,
                    classification="dispatch_outcome_unknown",
                )
            original_acknowledge = state.receipt_journal.acknowledge

            async def delayed_acknowledge(target_incident_id: str):
                result = await original_acknowledge(target_incident_id)
                acknowledged.set()
                while not allow_reset.is_set():
                    await asyncio.sleep(0.001)
                return result

            monkeypatch.setattr(
                state.receipt_journal,
                "acknowledge",
                delayed_acknowledge,
            )
            request = _route_request(
                app,
                "/v1/recovery/acknowledge",
                _owner_headers(),
            )
            task = asyncio.create_task(
                recovery_routes.acknowledge(
                    recovery_routes._AcknowledgeRequest(incident_id=incident_id),
                    request,
                    Response(),
                )
            )
            await _wait_for_thread_event(acknowledged)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            allow_reset.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state.input_lock.locked() is False
            status = state.lease_coordinator.status()
            assert status["state"] == "released"
            assert status["run_state"] == "interrupted"
            assert (await state.receipt_journal.recovery_status())["recovery_required"] is False
            async with state.input_lock:
                await state.receipt_journal.validate_acquire("run-after-cancelled-ack")
                await state.receipt_journal.activate_run("run-after-cancelled-ack")
                next_grant = state.lease_coordinator.acquire("run-after-cancelled-ack")
            assert next_grant.run_id == "run-after-cancelled-ack"

    asyncio.run(exercise())


def test_lease_release_route_cancellation_completes_seal_and_release(
    tmp_path, monkeypatch
) -> None:
    app = _app(tmp_path)
    sealed = threading.Event()
    allow_release = threading.Event()

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            state = app.state
            async with state.input_lock:
                await state.receipt_journal.activate_run("run-cancel-release-route")
                grant = state.lease_coordinator.acquire("run-cancel-release-route")
            headers = {
                LEASE_ID_HEADER: grant.lease_id,
                LEASE_EPOCH_HEADER: grant.epoch,
                LEASE_FENCE_HEADER: str(grant.fence),
                LEASE_TOKEN_HEADER: grant.token,
            }
            original_seal_run = state.receipt_journal.seal_run

            async def delayed_seal_run(run_id: str, reason: str):
                await original_seal_run(run_id, reason)
                sealed.set()
                while not allow_release.is_set():
                    await asyncio.sleep(0.001)

            monkeypatch.setattr(state.receipt_journal, "seal_run", delayed_seal_run)
            request = _route_request(app, "/v1/leases/release", headers)
            task = asyncio.create_task(lease_routes.release(request, Response()))
            await _wait_for_thread_event(sealed)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            allow_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state.input_lock.locked() is False
            assert state.lease_coordinator.status()["state"] == "released"
            with pytest.raises(DaemonError) as sealed_run:
                await state.receipt_journal.validate_acquire("run-cancel-release-route")
            assert sealed_run.value.code == "run_sealed"
            async with state.input_lock:
                await state.receipt_journal.validate_acquire("run-after-cancelled-release")
                await state.receipt_journal.activate_run("run-after-cancelled-release")
                next_grant = state.lease_coordinator.acquire(
                    "run-after-cancelled-release"
                )
            assert next_grant.run_id == "run-after-cancelled-release"

    asyncio.run(exercise())


def test_explicit_release_finishes_when_ttl_crosses_during_durable_seal(
    tmp_path, monkeypatch
) -> None:
    clock = _FakeClock()
    app = _app(tmp_path)
    app.state.lease_coordinator = LeaseCoordinator(clock=clock, ttl_seconds=1)

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            state = app.state
            async with state.input_lock:
                await state.receipt_journal.activate_run("run-release-crosses-ttl")
                grant = state.lease_coordinator.acquire("run-release-crosses-ttl")
            headers = {
                LEASE_ID_HEADER: grant.lease_id,
                LEASE_EPOCH_HEADER: grant.epoch,
                LEASE_FENCE_HEADER: str(grant.fence),
                LEASE_TOKEN_HEADER: grant.token,
            }
            original_seal_run = state.receipt_journal.seal_run

            async def seal_then_cross_ttl(run_id: str, reason: str):
                await original_seal_run(run_id, reason)
                clock.advance(2)

            monkeypatch.setattr(state.receipt_journal, "seal_run", seal_then_cross_ttl)
            result = await lease_routes.release(
                _route_request(app, "/v1/leases/release", headers),
                Response(),
            )
            assert result["state"] == "released"
            assert state.lease_coordinator.status()["state"] == "released"
            with sqlite3.connect(state.receipt_journal.db_path) as connection:
                reason = connection.execute(
                    "SELECT seal_reason FROM runs WHERE run_id = ?",
                    ("run-release-crosses-ttl",),
                ).fetchone()[0]
            assert reason == "lease_released"

    asyncio.run(exercise())


def test_resolve_finishes_when_ttl_crosses_during_durable_proof(
    tmp_path, monkeypatch
) -> None:
    clock = _FakeClock()
    app = _app(tmp_path)
    app.state.lease_coordinator = LeaseCoordinator(clock=clock, ttl_seconds=1)

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            state = app.state
            async with state.input_lock:
                await state.receipt_journal.activate_run("run-resolve-crosses-ttl")
                grant = state.lease_coordinator.acquire("run-resolve-crosses-ttl")
            headers = {
                LEASE_ID_HEADER: grant.lease_id,
                LEASE_EPOCH_HEADER: grant.epoch,
                LEASE_FENCE_HEADER: str(grant.fence),
                LEASE_TOKEN_HEADER: grant.token,
            }
            original_resolve = state.receipt_journal.resolve

            async def resolve_then_cross_ttl(run_id: str, sequence: int):
                result = await original_resolve(run_id, sequence)
                clock.advance(2)
                return result

            monkeypatch.setattr(state.receipt_journal, "resolve", resolve_then_cross_ttl)
            result = await recovery_routes.resolve_receipt(
                recovery_routes._ReceiptResolveRequest(
                    run_id="run-resolve-crosses-ttl",
                    sequence=0,
                ),
                _route_request(app, "/v1/receipts/resolve", headers),
                Response(),
            )
            assert result == {
                "state": "MISSING",
                "run_id": "run-resolve-crosses-ttl",
                "sequence": 0,
                "proven_not_applied": True,
                "run_sealed": True,
            }
            assert state.lease_coordinator.status()["state"] == "released"
            proof = await state.receipt_journal.resolved_missing_proof(
                "run-resolve-crosses-ttl",
                0,
            )
            assert proof == result

    asyncio.run(exercise())


def test_resolve_cancellation_leaves_idempotent_resolved_missing_proof(
    tmp_path, monkeypatch
) -> None:
    journal = ReceiptJournal(tmp_path / "runtime")
    committed = threading.Event()
    release = threading.Event()

    async def exercise() -> None:
        await journal.start()
        await journal.activate_run("run-cancel-resolve")
        original = journal._resolve_sync
        monkeypatch.setattr(
            journal,
            "_resolve_sync",
            _block_after_sync_commit(original, committed, release),
        )
        task = asyncio.create_task(journal.resolve("run-cancel-resolve", 0))
        await _wait_for_thread_event(committed)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        retried = await journal.resolve("run-cancel-resolve", 0)
        assert retried == {
            "state": "MISSING",
            "run_id": "run-cancel-resolve",
            "sequence": 0,
            "proven_not_applied": True,
            "run_sealed": True,
        }
        await journal.close()

    asyncio.run(exercise())


def test_resolve_route_cancellation_completes_release_and_preserves_retry_proof(
    tmp_path, monkeypatch
) -> None:
    app = _app(tmp_path)
    app.state.lease_coordinator.ttl_seconds = 0.02
    resolved = threading.Event()
    allow_release = threading.Event()

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            state = app.state
            async with state.input_lock:
                await state.receipt_journal.activate_run("run-cancel-resolve-route")
                grant = state.lease_coordinator.acquire("run-cancel-resolve-route")
            headers = {
                LEASE_ID_HEADER: grant.lease_id,
                LEASE_EPOCH_HEADER: grant.epoch,
                LEASE_FENCE_HEADER: str(grant.fence),
                LEASE_TOKEN_HEADER: grant.token,
            }
            original_resolve = state.receipt_journal.resolve

            async def delayed_resolve(run_id: str, sequence: int):
                result = await original_resolve(run_id, sequence)
                resolved.set()
                while not allow_release.is_set():
                    await asyncio.sleep(0.001)
                return result

            monkeypatch.setattr(state.receipt_journal, "resolve", delayed_resolve)
            payload = recovery_routes._ReceiptResolveRequest(
                run_id="run-cancel-resolve-route",
                sequence=0,
            )
            request = _route_request(app, "/v1/receipts/resolve", headers)
            task = asyncio.create_task(
                recovery_routes.resolve_receipt(payload, request, Response())
            )
            await _wait_for_thread_event(resolved)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            allow_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state.input_lock.locked() is False
            assert state.lease_coordinator.status()["state"] == "released"

            await asyncio.sleep(0.04)
            lease_status = await lease_routes.status(
                _route_request(app, "/v1/leases/status", {}),
                Response(),
            )
            assert lease_status["state"] == "released"
            await state.receipt_journal.seal_run(
                "run-cancel-resolve-route",
                "lease_expired",
            )
            retry = await recovery_routes.resolve_receipt(
                payload,
                _route_request(app, "/v1/receipts/resolve", headers),
                Response(),
            )
            assert retry == {
                "state": "MISSING",
                "run_id": "run-cancel-resolve-route",
                "sequence": 0,
                "proven_not_applied": True,
                "run_sealed": True,
            }
            with sqlite3.connect(state.receipt_journal.db_path) as connection:
                reason = connection.execute(
                    "SELECT seal_reason FROM runs WHERE run_id = ?",
                    ("run-cancel-resolve-route",),
                ).fetchone()[0]
            assert reason == "resolved_missing"

    asyncio.run(exercise())


def test_seal_run_preserves_first_terminal_reason(tmp_path) -> None:
    journal = ReceiptJournal(tmp_path / "runtime")

    async def exercise() -> None:
        await journal.start()
        await journal.activate_run("run-first-seal-wins")
        await journal.seal_run("run-first-seal-wins", "resolved_missing")
        await journal.seal_run("run-first-seal-wins", "lease_released")
        await journal.seal_run("run-first-seal-wins", "lease_expired")
        with sqlite3.connect(journal.db_path) as connection:
            row = connection.execute(
                "SELECT sealed, seal_reason FROM runs WHERE run_id = ?",
                ("run-first-seal-wins",),
            ).fetchone()
        assert row == (1, "resolved_missing")
        await journal.close()

    asyncio.run(exercise())


def test_resolve_returns_terminal_receipt_without_releasing_lease(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-resolve-terminal")
        first = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )
        resolved = client.post(
            "/v1/receipts/resolve",
            json={"run_id": "run-resolve-terminal", "sequence": 0},
            headers=lease,
        )
        second = client.post(
            "/v1/mouse/move",
            json={"x": 3, "y": 4},
            headers=_operation_headers(lease, 1),
        )

    assert first.status_code == 200
    assert resolved.status_code == 200
    assert resolved.json()["state"] == "COMPLETED"
    assert resolved.json()["result_available"] is False
    assert second.status_code == 200


def test_resolved_missing_lost_response_retry_authenticates_only_exact_last_release(
    tmp_path,
) -> None:
    app = _app(tmp_path)
    payload = {"run_id": "run-resolve-lost-response", "sequence": 0}
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, released_lease = _acquire(client, payload["run_id"])
        first = client.post("/v1/receipts/resolve", json=payload, headers=released_lease)
        retry = client.post("/v1/receipts/resolve", json=payload, headers=released_lease)
        wrong_sequence = client.post(
            "/v1/receipts/resolve",
            json={**payload, "sequence": 1},
            headers=released_lease,
        )
        wrong_run = client.post(
            "/v1/receipts/resolve",
            json={"run_id": "some-other-run", "sequence": 0},
            headers=released_lease,
        )
        _, next_lease = _acquire(client, "run-after-resolve-retry")
        stale_after_fence = client.post(
            "/v1/receipts/resolve",
            json=payload,
            headers=released_lease,
        )
        wrong_current_run = client.post(
            "/v1/receipts/resolve",
            json=payload,
            headers=next_lease,
        )

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert retry.json()["proven_not_applied"] is True
    assert wrong_sequence.json()["code"] == "receipt_access_denied"
    assert wrong_run.json()["code"] == "receipt_access_denied"
    assert stale_after_fence.json()["code"] == "lease_stale"
    assert wrong_current_run.json()["code"] == "receipt_access_denied"


def test_owner_recovers_only_existing_resolved_missing_proof_after_restart(tmp_path) -> None:
    first = _app(tmp_path)
    with TestClient(first, headers={"Authorization": "Bearer dev"}) as client:
        _, proof_lease = _acquire(client, "run-proof-after-restart")
        resolved = client.post(
            "/v1/receipts/resolve",
            json={"run_id": "run-proof-after-restart", "sequence": 0},
            headers=proof_lease,
        )
        assert resolved.status_code == 200
        _, unproved_lease = _acquire(client, "run-without-proof")
        released = client.post("/v1/leases/release", headers=unproved_lease)
        assert released.status_code == 200

    restarted = _app(tmp_path)
    with TestClient(restarted, headers={"Authorization": "Bearer dev"}) as client:
        exact = client.post(
            "/v1/receipts/resolve",
            json={"run_id": "run-proof-after-restart", "sequence": 0},
            headers=_owner_headers(),
        )
        wrong_sequence = client.post(
            "/v1/receipts/resolve",
            json={"run_id": "run-proof-after-restart", "sequence": 1},
            headers=_owner_headers(),
        )
        wrong_run = client.post(
            "/v1/receipts/resolve",
            json={"run_id": "missing-run", "sequence": 0},
            headers=_owner_headers(),
        )
        unproved = client.post(
            "/v1/receipts/resolve",
            json={"run_id": "run-without-proof", "sequence": 0},
            headers=_owner_headers(),
        )

    assert exact.status_code == 200
    assert exact.json() == {
        "state": "MISSING",
        "run_id": "run-proof-after-restart",
        "sequence": 0,
        "proven_not_applied": True,
        "run_sealed": True,
    }
    assert wrong_sequence.json()["code"] == "receipt_access_denied"
    assert wrong_run.json()["code"] == "receipt_access_denied"
    assert unproved.json()["code"] == "receipt_access_denied"
    with sqlite3.connect(restarted.state.receipt_journal.db_path) as connection:
        runs = connection.execute(
            "SELECT run_id, seal_reason FROM runs ORDER BY run_id"
        ).fetchall()
    assert runs == [
        ("run-proof-after-restart", "resolved_missing"),
        ("run-without-proof", "lease_released"),
    ]


def test_resolve_missing_seals_and_fences_a_delayed_original(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path)
    resolve_entered = threading.Event()
    allow_resolve = threading.Event()
    original_started = threading.Event()
    original_resolve = app.state.receipt_journal.resolve

    async def delayed_resolve(run_id: str, sequence: int):
        resolve_entered.set()
        while not allow_resolve.is_set():
            await asyncio.sleep(0.001)
        return await original_resolve(run_id, sequence)

    monkeypatch.setattr(app.state.receipt_journal, "resolve", delayed_resolve)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-resolve-missing")
        responses: dict[str, Any] = {}

        def request_resolve() -> None:
            responses["resolve"] = client.post(
                "/v1/receipts/resolve",
                json={"run_id": "run-resolve-missing", "sequence": 0},
                headers=lease,
            )

        def request_original() -> None:
            original_started.set()
            responses["original"] = client.post(
                "/v1/mouse/move",
                json={"x": 1, "y": 2},
                headers=_operation_headers(lease, 0),
            )

        resolve_thread = threading.Thread(target=request_resolve)
        resolve_thread.start()
        assert resolve_entered.wait(timeout=2)
        original_thread = threading.Thread(target=request_original)
        original_thread.start()
        assert original_started.wait(timeout=2)
        allow_resolve.set()
        resolve_thread.join(timeout=2)
        original_thread.join(timeout=2)
        assert not resolve_thread.is_alive()
        assert not original_thread.is_alive()

    assert responses["resolve"].status_code == 200
    assert responses["resolve"].json() == {
        "state": "MISSING",
        "run_id": "run-resolve-missing",
        "sequence": 0,
        "proven_not_applied": True,
        "run_sealed": True,
    }
    assert responses["original"].json()["code"] == "lease_released"


def test_contended_forced_readiness_failure_does_not_consume_sequence(
    tmp_path, monkeypatch
) -> None:
    app = _app(tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    original_move = app.state.backend.mouse_move

    async def blocking_move(x: int, y: int):
        if x == 10:
            first_entered.set()
            while not release_first.is_set():
                await asyncio.sleep(0.001)
        return await original_move(x, y)

    forced_calls = 0

    async def controlled_readiness(_backend, *, force: bool = False):
        nonlocal forced_calls
        if force:
            forced_calls += 1
            return False, ["injected forced readiness failure"]
        return True, []

    app.state.backend.mouse_move = blocking_move
    monkeypatch.setattr(app.state.readiness_cache, "backend_ready", controlled_readiness)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-contended-readiness")
        input_lock = app.state.input_lock
        contention_observed = threading.Event()

        class ObservedInputLock:
            def locked(self) -> bool:
                locked = input_lock.locked()
                if locked:
                    contention_observed.set()
                return locked

            async def __aenter__(self) -> ObservedInputLock:
                await input_lock.acquire()
                return self

            async def __aexit__(self, *_args: object) -> None:
                input_lock.release()

        app.state.input_lock = ObservedInputLock()
        responses: dict[str, Any] = {}

        def first_request() -> None:
            responses["first"] = client.post(
                "/v1/mouse/move",
                json={"x": 10, "y": 2},
                headers=_operation_headers(lease, 0),
            )

        def contended_request() -> None:
            responses["contended"] = client.post(
                "/v1/mouse/move",
                json={"x": 20, "y": 2},
                headers=_operation_headers(lease, 1),
            )

        first_thread = threading.Thread(target=first_request)
        first_thread.start()
        assert first_entered.wait(timeout=2)
        contended_thread = threading.Thread(target=contended_request)
        contended_thread.start()
        assert contention_observed.wait(timeout=2)
        release_first.set()
        first_thread.join(timeout=2)
        contended_thread.join(timeout=2)
        assert not first_thread.is_alive()
        assert not contended_thread.is_alive()
        retried = client.post(
            "/v1/mouse/move",
            json={"x": 20, "y": 2},
            headers=_operation_headers(lease, 1),
        )

    assert responses["first"].status_code == 200
    assert responses["contended"].json()["code"] == "desktop_not_ready"
    assert forced_calls == 1
    assert retried.status_code == 200


def test_leased_budget_preflight_rejection_does_not_consume_sequence(tmp_path) -> None:
    app = _app(tmp_path, max_actions=1)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-budget-preflight")
        rejected = client.post(
            "/v1/actions/run",
            headers=_operation_headers(lease, 0),
            json={
                "actions": [
                    {
                        "type": "hold_key",
                        "key": "shift",
                        "actions": [
                            {"type": "move", "x": 1, "y": 2},
                            {"type": "move", "x": 3, "y": 4},
                        ],
                    }
                ]
            },
        )
        retried = client.post(
            "/v1/actions/run",
            headers=_operation_headers(lease, 0),
            json={"actions": [{"type": "move", "x": 5, "y": 6}]},
        )

    assert rejected.json()["code"] == "budget_exceeded"
    assert retried.status_code == 200
    assert retried.json()["ok"] is True


def test_startup_recovers_in_progress_and_completed_receipt_survives_restart(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        grant, lease_headers = _acquire(client, "run-restart")
        completed = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease_headers, 0),
        )
        assert completed.status_code == 200
        asyncio.run(
            app.state.receipt_journal.begin(
                lease=MutationLease(
                    run_id="run-restart",
                    epoch=grant["daemon_epoch"],
                    fence=grant["fence"],
                ),
                sequence=1,
                operation_kind="test.interrupted",
                semantic_data={"safe": True},
            )
        )

    restarted = _app(tmp_path, browser_prewarm=True)
    prewarm_calls = 0

    async def prewarm():
        nonlocal prewarm_calls
        prewarm_calls += 1

    restarted.state.backend.prewarm_browser = prewarm
    with TestClient(restarted, headers={"Authorization": "Bearer dev"}) as client:
        recovery = client.get("/v1/recovery/status", headers=_owner_headers())
        first_status = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-restart", "sequence": 0},
            headers=_owner_headers(),
        )
        interrupted = client.post(
            "/v1/receipts/status",
            json={"run_id": "run-restart", "sequence": 1},
            headers=_owner_headers(),
        )

    assert recovery.json()["recovery_required"] is True
    assert first_status.json()["state"] == "COMPLETED"
    assert first_status.json()["result_available"] is False
    assert interrupted.json()["state"] == "INDETERMINATE"
    assert prewarm_calls == 0
    assert restarted.state.browser_prewarm.output == {"code": "recovery_required"}


def test_release_and_expiry_seal_zero_operation_runs_and_require_new_run(tmp_path) -> None:
    app = _app(tmp_path)
    app.state.lease_coordinator.ttl_seconds = 0.2
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-zero")
        busy = client.post("/v1/leases/acquire", json={"run_id": "run-zero"})
        released = client.post("/v1/leases/release", headers=lease)
        same = client.post("/v1/leases/acquire", json={"run_id": "run-zero"})
        new, new_lease = _acquire(client, "run-new")
        stale_after_release = client.post(
            "/v1/mouse/move",
            json={"x": 1, "y": 2},
            headers=_operation_headers(lease, 0),
        )
        new_mutation = client.post(
            "/v1/mouse/move",
            json={"x": 3, "y": 4},
            headers=_operation_headers(new_lease, 0),
        )
        time.sleep(0.25)
        assert client.get("/v1/leases/status").json()["state"] == "expired"
        expired_same = client.post("/v1/leases/acquire", json={"run_id": "run-new"})
        after_expiry, after_expiry_lease = _acquire(client, "run-after-expiry")
        stale_after_expiry = client.post(
            "/v1/mouse/move",
            json={"x": 5, "y": 6},
            headers=_operation_headers(new_lease, 1),
        )
        fresh_mutation = client.post(
            "/v1/mouse/move",
            json={"x": 7, "y": 8},
            headers=_operation_headers(after_expiry_lease, 0),
        )

    assert busy.json()["code"] == "session_busy"
    assert released.status_code == 200
    assert same.json()["code"] == "run_sealed"
    assert new["run_id"] == "run-new"
    assert stale_after_release.json()["code"] == "lease_stale"
    assert new_mutation.status_code == 200
    assert expired_same.json()["code"] == "run_sealed"
    assert after_expiry["run_id"] == "run-after-expiry"
    assert stale_after_expiry.json()["code"] == "lease_stale"
    assert fresh_mutation.status_code == 200


def test_restart_seals_zero_operation_run(tmp_path) -> None:
    first = _app(tmp_path)
    with TestClient(first, headers={"Authorization": "Bearer dev"}) as client:
        _acquire(client, "run-zero-restart")

    restarted = _app(tmp_path)
    with TestClient(restarted, headers={"Authorization": "Bearer dev"}) as client:
        same = client.post("/v1/leases/acquire", json={"run_id": "run-zero-restart"})
        different = client.post("/v1/leases/acquire", json={"run_id": "different-run"})

    assert same.json()["code"] == "run_sealed"
    assert different.status_code == 200


def test_artifact_screenshot_requires_receipt_but_inline_remains_read_only(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-screenshot")
        inline = client.post(
            "/v1/screenshots/full",
            json={"storage": "inline"},
            headers=lease,
        )
        missing = client.post(
            "/v1/screenshots/full",
            json={"storage": "artifact"},
            headers=lease,
        )
        artifact = client.post(
            "/v1/screenshots/full",
            json={"storage": "artifact"},
            headers=_operation_headers(lease, 0),
        )

    assert inline.status_code == 200
    assert missing.json()["code"] == "operation_sequence_required"
    assert artifact.status_code == 200


def test_journal_key_permissions_and_sensitive_content_absence(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-sensitive")
        response = client.put(
            "/v1/clipboard/text",
            json={"text": "never-store-this-clipboard"},
            headers=_operation_headers(lease, 0),
        )
        assert response.status_code == 200

    assert os.stat(app.state.receipt_journal.key_path).st_mode & 0o777 == 0o600
    database_files = list((tmp_path / "runtime").glob("trajectory-receipts.sqlite3*"))
    stored = b"".join(path.read_bytes() for path in database_files)
    assert b"never-store-this-clipboard" not in stored
    assert b"clipboard_set" not in stored


def test_dynamic_artifact_path_is_hmac_only_and_route_kind_is_stable(tmp_path) -> None:
    sentinel = "sentinel-customer-object-7d2c4e9b"
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        _, lease = _acquire(client, "run-dynamic-artifact")
        response = client.put(
            f"/v1/artifacts/private/{sentinel}.bin",
            content=b"non-sensitive-test-body",
            headers=_operation_headers(lease, 0),
        )
        assert response.status_code == 200
        database_files = list((tmp_path / "runtime").glob("trajectory-receipts.sqlite3*"))
        stored = b"".join(path.read_bytes() for path in database_files)
        with sqlite3.connect(app.state.receipt_journal.db_path) as connection:
            operation_kind = connection.execute(
                "SELECT operation_kind FROM operations WHERE sequence = 0"
            ).fetchone()[0]

    assert sentinel.encode() not in stored
    assert operation_kind == "/v1/artifacts/{path:path}"


def test_truncated_hmac_key_fails_closed_on_startup(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".trajectory-receipts.key").write_bytes(b"short")
    app = _app(tmp_path)

    with pytest.raises(RuntimeError, match="HMAC key is invalid"), TestClient(app):
        pass


def test_corrupt_sqlite_fails_closed_on_startup(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".trajectory-receipts.key").write_bytes(b"k" * 32)
    (runtime / "trajectory-receipts.sqlite3").write_bytes(b"not-a-sqlite-database")
    app = _app(tmp_path)

    with pytest.raises(sqlite3.DatabaseError), TestClient(app):
        pass


def test_journal_worker_does_not_block_event_loop(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}):
        original = app.state.receipt_journal._receipt_status_sync

        def slow_status(run_id: str, sequence: int):
            time.sleep(0.05)
            return original(run_id, sequence)

        monkeypatch.setattr(app.state.receipt_journal, "_receipt_status_sync", slow_status)

        async def exercise() -> int:
            task = asyncio.create_task(
                app.state.receipt_journal.receipt_status("missing", 0)
            )
            ticks = 0
            while not task.done():
                ticks += 1
                await asyncio.sleep(0.005)
            return ticks

        ticks = asyncio.run(exercise())

    assert ticks >= 3
