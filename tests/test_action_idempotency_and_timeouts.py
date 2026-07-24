from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ActionResult
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


def test_concurrent_idempotency_replay_does_not_reexecute(tmp_path) -> None:
    app = _app(tmp_path)
    calls = 0

    async def slow_move(x: int, y: int):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return app.state.backend.cursor.model_copy(update={"x": x, "y": y})

    app.state.backend.mouse_move = slow_move
    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        futures = [
            executor.submit(
                client.post,
                "/v1/actions/run",
                headers={"Idempotency-Key": "idem-concurrent"},
                json={"actions": [{"type": "move", "x": 10, "y": 20}]},
            )
            for _ in range(2)
        ]
        responses = [future.result() for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert calls == 1
    assert app.state.action_count == 1


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
    assert app.state.action_count == 1


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


def test_idempotency_replay_returns_cached_result_when_desktop_becomes_unready(tmp_path) -> None:
    app = _app(tmp_path)

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-ready"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

        async def not_ready():
            return False, ["desktop stopped after original request"]

        app.state.backend.ready = not_ready
        second = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-ready"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert app.state.action_count == 1


def test_idempotency_replay_returns_cached_result_when_geometry_changes(tmp_path) -> None:
    app = _app(tmp_path)

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-geometry"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

        app.state.backend.width = 5
        second = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-geometry"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert app.state.action_count == 1


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


def test_idempotency_cache_zero_entries_does_not_store_results(tmp_path) -> None:
    app = _app(tmp_path, idempotency_cache_max_entries=0)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-disabled"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )
        second = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "idem-disabled"},
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["call_id"] != second.json()["call_id"]
    assert app.state.action_count == 2
    assert len(app.state.idempotency_cache) == 0


def test_body_idempotency_key_replays_without_reexecution(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/actions/run",
            json={
                "idempotency_key": "body-idem",
                "actions": [{"type": "move", "x": 10, "y": 20}],
            },
        )
        second = client.post(
            "/v1/actions/run",
            json={
                "idempotency_key": "body-idem",
                "actions": [{"type": "move", "x": 10, "y": 20}],
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert app.state.action_count == 1


def test_header_and_body_idempotency_key_mismatch_is_rejected(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            headers={"Idempotency-Key": "header-idem"},
            json={
                "idempotency_key": "body-idem",
                "actions": [{"type": "move", "x": 10, "y": 20}],
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_key_conflict"
    assert app.state.action_count == 0


def test_post_action_delay_runs_before_screenshot_after(tmp_path) -> None:
    app = _app(tmp_path, post_action_delay_ms=25)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["timing"]["daemon_ms"] >= 20


def test_post_action_delay_defaults_to_zero(tmp_path) -> None:
    app = _app(tmp_path)

    async def unexpected_sleep(_duration: float) -> None:
        raise AssertionError("post-action delay should be opt-in")

    app.state.sleep = unexpected_sleep
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
            },
        )

    assert response.status_code == 200


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
    assert body["results"][0]["output"] == {
        "code": "timeout",
        "timeout_ms": 10,
        "scope": "action",
    }
    assert app.state.backend.held_buttons == set()
    assert app.state.action_count == 1


def test_release_all_timeout_runs_secondary_cleanup_with_diagnostics(tmp_path) -> None:
    app = _app(tmp_path, default_action_timeout_ms=10)
    app.state.backend.held_keys.update(("a", "b"))
    calls = 0
    prefix_cancelled = False

    async def release_all() -> ActionResult:
        nonlocal calls, prefix_cancelled
        calls += 1
        if calls == 1:
            app.state.backend.held_keys.remove("a")
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                prefix_cancelled = True
                raise
        await asyncio.sleep(0.02)
        return ActionResult(
            ok=False,
            message="failed to release all held input",
            output={
                "code": "release_all_incomplete",
                "keys": [],
                "buttons": [],
                "remaining": {"keys": ["b"], "buttons": []},
                "failures": [
                    {
                        "kind": "key",
                        "value": "b",
                        "input_backend": "xtest",
                        "code": "key_release_failed",
                    }
                ],
            },
        )

    app.state.backend.release_all = release_all
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "release_all"}]},
        )

    item = response.json()["results"][0]
    assert calls == 2
    assert prefix_cancelled is True
    assert app.state.backend.held_keys == {"b"}
    assert item["error_code"] == "timeout"
    assert item["output"] == {
        "code": "timeout",
        "timeout_ms": 10,
        "scope": "action",
        "cleanup": {
            "code": "release_all_incomplete",
            "keys": [],
            "buttons": [],
            "remaining": {"keys": ["b"], "buttons": []},
            "failures": [
                {
                    "kind": "key",
                    "value": "b",
                    "input_backend": "xtest",
                    "code": "key_release_failed",
                }
            ],
        },
    }


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
    assert app.state.action_count == 2


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


def test_nested_hold_timeout_rejects_values_above_configured_max(tmp_path) -> None:
    app = _app(tmp_path, max_action_timeout_ms=25)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "hold_key",
                        "key": "shift",
                        "actions": [{"type": "wait", "duration_ms": 1, "timeout_ms": 26}],
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert (
        "actions[0].actions[0] timeout_ms 26 exceeds configured maximum 25"
        in response.json()["details"]["errors"]
    )


def test_batch_duration_timeout_stops_later_actions_even_with_continue_on_error(
    tmp_path,
) -> None:
    app = _app(
        tmp_path,
        default_action_timeout_ms=1_000,
        max_batch_duration_ms=20,
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "continue_on_error": True,
                "actions": [
                    {"type": "wait", "duration_ms": 100, "timeout_ms": 1_000},
                    {"type": "move", "x": 10, "y": 20},
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert len(body["results"]) == 1
    assert body["results"][0]["type"] == "wait"
    assert body["results"][0]["error_code"] == "timeout"
    assert body["results"][0]["output"] == {
        "code": "timeout",
        "timeout_ms": 20,
        "scope": "batch",
    }
    assert app.state.action_count == 1
    assert app.state.screenshot_count == 0
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0


def test_screenshot_after_timeout_returns_failed_result_and_counts_attempt(
    tmp_path,
) -> None:
    app = _app(tmp_path, default_action_timeout_ms=10, trace_actions=True)
    calls = 0

    async def slow_screenshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)

    app.state.backend.screenshot = slow_screenshot
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["screenshot"] is None
    assert [item["type"] for item in body["results"]] == ["move", "screenshot_after"]
    assert body["results"][1]["error_code"] == "timeout"
    assert body["results"][1]["output"] == {
        "code": "timeout",
        "timeout_ms": 10,
        "scope": "action",
    }
    assert calls == 1
    assert app.state.action_count == 1
    assert app.state.screenshot_count == 1
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 2
    assert entries[1].normalized_action == {"type": "screenshot_after"}
    assert entries[1].error is not None
    assert entries[1].error["code"] == "timeout"


def test_screenshot_after_timeout_releases_held_inputs(tmp_path) -> None:
    app = _app(tmp_path, default_action_timeout_ms=10)

    async def slow_screenshot(*args, **kwargs):
        await asyncio.sleep(1)

    app.state.backend.screenshot = slow_screenshot
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "mouse_down", "button": "left"}],
                "screenshot_after": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert app.state.backend.held_buttons == set()
