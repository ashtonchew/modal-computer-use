from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.tracing import load_trace


def _app(tmp_path, **overrides):
    return create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            local_token="dev",
            **overrides,
        )
    )


def test_idempotency_replay_does_not_reexecute_or_increment_budgets(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-move"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )
        second = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-move"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert app.state.action_count == 1
    assert app.state.screenshot_count == 0


def test_idempotency_replay_preserves_failed_action_without_reexecution(tmp_path) -> None:
    app = _app(tmp_path)
    calls = 0

    async def fail_once(x: int, y: int):
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic move failure")

    app.state.backend.mouse_move = fail_once
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-fail"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )
        second = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-fail"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["ok"] is False
    assert second.json() == first.json()
    assert calls == 1
    assert app.state.action_count == 0


def test_idempotency_replay_does_not_duplicate_screenshot_after_or_trace(tmp_path) -> None:
    app = _app(tmp_path, trace_actions=True)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-shot"},
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
                "screenshot_options": {"storage": "artifact"},
            },
        )
        second = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-shot"},
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
                "screenshot_options": {"storage": "artifact"},
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert app.state.action_count == 1
    assert app.state.screenshot_count == 1
    assert len(load_trace(tmp_path / "traces" / "actions.ndjson")) == 2


def test_idempotency_key_conflict_rejects_different_payload(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-conflict"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )
        second = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-conflict"},
            json={"actions": [{"type": "move", "x": 11, "y": 20}]},
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "idempotency_key_conflict"
    assert app.state.action_count == 1


def test_action_timeout_releases_input_and_stops_batch(tmp_path) -> None:
    app = _app(tmp_path, default_action_timeout_ms=10)

    async def slow_mouse_down(button: str = "left", x: int | None = None, y: int | None = None):
        app.state.backend.held_buttons.add(button)
        await asyncio.sleep(1)

    app.state.backend.mouse_down = slow_mouse_down
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {"type": "mouse_down", "button": "left"},
                    {"type": "move", "x": 10, "y": 20},
                ]
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert len(body["results"]) == 1
    assert body["results"][0]["error_code"] == "timeout"
    assert body["results"][0]["output"] == {"code": "timeout", "timeout_ms": 10}
    assert app.state.backend.held_buttons == set()
    assert app.state.action_count == 0


def test_action_timeout_continue_on_error_executes_next_action(tmp_path) -> None:
    app = _app(tmp_path, default_action_timeout_ms=10)

    async def slow_mouse_down(button: str = "left", x: int | None = None, y: int | None = None):
        app.state.backend.held_buttons.add(button)
        await asyncio.sleep(1)

    app.state.backend.mouse_down = slow_mouse_down
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "continue_on_error": True,
                "actions": [
                    {"type": "mouse_down", "button": "left"},
                    {"type": "move", "x": 10, "y": 20},
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert [item["ok"] for item in body["results"]] == [False, True]
    assert app.state.backend.cursor.x == 10
    assert app.state.backend.cursor.y == 20
    assert app.state.backend.held_buttons == set()
    assert app.state.action_count == 1


def test_action_timeout_rejects_values_above_configured_max(tmp_path) -> None:
    app = _app(tmp_path, max_action_timeout_ms=25)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 1, "y": 2, "timeout_ms": 26}]},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert "timeout_ms 26 exceeds configured maximum 25" in response.json()["details"]["errors"][0]
