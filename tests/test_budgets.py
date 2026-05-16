from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings


def test_status_includes_budget_snapshot(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_actions=10,
            max_screenshots=3,
            max_artifact_bytes=100_000,
            max_recording_seconds=60,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        client.post("/v1/actions/run", json={"actions": [{"type": "move", "x": 1, "y": 2}]})
        status = client.get("/v1/computer/status").json()

    assert status["budgets"]["actions"] == 1
    assert status["budgets"]["max_actions"] == 10
    assert status["budgets"]["screenshots"] == 0
    assert status["budgets"]["max_screenshots"] == 3
    assert status["budgets"]["artifact_bytes"] == 0
    assert status["budgets"]["max_artifact_bytes"] == 100_000
    assert status["budgets"]["max_recording_seconds"] == 60


def test_status_budget_snapshot_tolerates_unsafe_artifact_symlink(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, artifacts_dir / "link")

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.get("/v1/computer/status")

    assert response.status_code == 200
    assert response.json()["budgets"]["artifact_bytes"] == 0


def test_recording_stop_counts_recording_bytes_against_artifact_budget(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        started = client.post("/v1/recordings", json={}).json()
        response = client.post(f"/v1/recordings/{started['id']}/stop")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert response.json()["details"]["budgets"]["artifact_bytes"] > 1
    assert not list((artifacts_dir / "recordings").glob("*.mp4"))
    assert not (artifacts_dir / "manifest.ndjson").exists()


def test_recording_start_rejects_exhausted_duration_budget_without_state(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_recording_seconds=0,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/recordings", json={})

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert app.state.recordings.list() == []


def test_direct_artifact_write_enforces_artifact_byte_budget(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.put("/v1/artifacts/big.bin", content=b"xx")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert response.json()["details"]["budgets"]["artifact_bytes"] == 2


def test_failed_direct_screenshot_counts_as_attempted_screenshot(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    async def screenshot(*args, **kwargs):
        raise RuntimeError("capture failed")

    app.state.backend.screenshot = screenshot

    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        response = client.post("/v1/screenshots/full", json={})

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert app.state.screenshot_count == 1


def test_clipboard_mutations_reserve_action_budget_before_mutating(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_actions=0,
        )
    )
    app.state.backend.clipboard = "existing"

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        put = client.put("/v1/clipboard/text", json={"text": "secret"})
        delete = client.delete("/v1/clipboard/text")

    assert put.status_code == 429
    assert put.json()["code"] == "budget_exceeded"
    assert delete.status_code == 429
    assert delete.json()["code"] == "budget_exceeded"
    assert app.state.backend.clipboard == "existing"
    assert app.state.action_count == 0


def test_over_budget_direct_action_rejects_without_waiting_for_input_lock(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_actions=0,
        )
    )
    app.state.supervisor.running = True

    async def call_with_lock_held() -> int:
        body = json.dumps({"x": 1, "y": 2}).encode()
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/mouse/move",
            "raw_path": b"/v1/mouse/move",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer dev"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        await app.state.input_lock.acquire()
        try:
            await asyncio.wait_for(app(scope, receive, send), timeout=0.1)
        finally:
            app.state.input_lock.release()
        return next(message for message in sent if message["type"] == "http.response.start")[
            "status"
        ]

    assert asyncio.run(call_with_lock_held()) == 429
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0
    assert app.state.action_count == 0


def test_clipboard_mutation_does_not_consume_action_budget_while_waiting_for_lock(
    tmp_path,
) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    app.state.supervisor.running = True

    async def call_with_lock_held() -> int:
        body = json.dumps({"text": "queued-secret"}).encode()
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": "/v1/clipboard/text",
            "raw_path": b"/v1/clipboard/text",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer dev"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        await app.state.input_lock.acquire()
        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.sleep(0.05)
            assert app.state.action_count == 0
            assert app.state.backend.clipboard == ""
        finally:
            app.state.input_lock.release()
        await asyncio.wait_for(task, timeout=1)
        return next(message for message in sent if message["type"] == "http.response.start")[
            "status"
        ]

    assert asyncio.run(call_with_lock_held()) == 200
    assert app.state.action_count == 1
    assert app.state.backend.clipboard == "queued-secret"


def test_clipboard_mutations_respect_input_rate_limit(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            input_rate_limit_per_sec=1,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post("/v1/mouse/move", json={"x": 1, "y": 1})
        second = client.put("/v1/clipboard/text", json={"text": "secret"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limited"
    assert app.state.backend.clipboard == ""
    assert app.state.action_count == 1


def test_empty_artifact_write_without_content_length_enforces_idle_budget(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    app.state.last_activity_at = time.monotonic() - 2
    messages = [
        {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }
    ]
    sent: list[dict] = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": "/v1/artifacts/empty.bin",
        "raw_path": b"/v1/artifacts/empty.bin",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer dev")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    asyncio.run(app(scope, receive, send))

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 429
    assert not (artifacts_dir / "empty.bin").exists()


def test_artifact_delete_enforces_idle_budget(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    app.state.artifacts.write_bytes("delete-me.txt", b"ok")
    app.state.last_activity_at = time.monotonic() - 2

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.delete("/v1/artifacts/delete-me.txt")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert (artifacts_dir / "delete-me.txt").exists()


def test_artifact_sync_enforces_idle_budget_before_sync(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    called = False

    def sync_spy():
        nonlocal called
        called = True
        return {"ok": True, "persistent": False}

    app.state.artifacts.sync = sync_spy
    app.state.last_activity_at = time.monotonic() - 2

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/artifacts/sync")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert called is False


def test_recording_stop_enforces_idle_budget_before_stop(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        started = client.post("/v1/recordings", json={}).json()
        app.state.last_activity_at = time.monotonic() - 2
        response = client.post(f"/v1/recordings/{started['id']}/stop")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert app.state.recordings.get(started["id"]).status == "recording"


def test_recording_delete_enforces_idle_budget_before_delete(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        started = client.post("/v1/recordings", json={}).json()
        app.state.last_activity_at = time.monotonic() - 2
        response = client.delete(f"/v1/recordings/{started['id']}")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert app.state.recordings.get(started["id"]).status == "recording"


def test_screenshot_artifact_write_is_rejected_before_file_persists(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/screenshots/full", json={"storage": "artifact"})

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert not list((artifacts_dir / "screenshots").glob("*.png"))
    assert not (artifacts_dir / "manifest.ndjson").exists()


def test_action_screenshot_artifact_budget_failure_uses_budget_code(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "screenshot", "options": {"storage": "artifact"}}]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][0]["error_code"] == "budget_exceeded"
    assert body["results"][0]["output"]["code"] == "budget_exceeded"
    assert not list((artifacts_dir / "screenshots").glob("*.png"))
    assert not (artifacts_dir / "manifest.ndjson").exists()


def test_nested_hold_screenshot_artifact_budget_failure_releases_key(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "hold_key",
                        "key": "shift",
                        "actions": [
                            {
                                "type": "screenshot",
                                "options": {"storage": "artifact"},
                            }
                        ],
                    }
                ]
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][0]["type"] == "hold_key"
    assert body["results"][0]["error_code"] == "budget_exceeded"
    assert body["results"][0]["output"]["code"] == "budget_exceeded"
    assert app.state.backend.held_keys == set()
    assert app.state.screenshot_count == 1
    assert not list((artifacts_dir / "screenshots").glob("*.png"))
    assert not (artifacts_dir / "manifest.ndjson").exists()


def test_direct_keyboard_hold_rejects_nested_screenshot_without_budget_side_effects(
    tmp_path,
) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/keyboard/hold",
            json={
                "key": "shift",
                "actions": [
                    {
                        "type": "screenshot",
                        "options": {"storage": "artifact"},
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert app.state.backend.held_keys == set()
    assert app.state.screenshot_count == 0
    assert not list((artifacts_dir / "screenshots").glob("*.png"))
    assert not (artifacts_dir / "manifest.ndjson").exists()


def test_screenshot_after_artifact_budget_failure_uses_budget_code(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
            post_action_delay_ms=0,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 1, "y": 2}],
                "screenshot_after": True,
                "screenshot_options": {"storage": "artifact"},
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][-1]["type"] == "screenshot_after"
    assert body["results"][-1]["error_code"] == "budget_exceeded"
    assert body["results"][-1]["output"]["code"] == "budget_exceeded"
    assert not list((artifacts_dir / "screenshots").glob("*.png"))
