from __future__ import annotations


def test_action_batch_stop_on_error(test_client) -> None:
    response = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {"type": "move", "x": 1, "y": 2},
                {"type": "drag"},
                {"type": "move", "x": 3, "y": 4},
            ],
        },
    )
    assert response.status_code == 422


def test_action_batch_stop_on_runtime_error(test_client, app) -> None:
    original = app.state.backend.mouse_move

    async def fail_once(x: int, y: int):
        if x == 9:
            raise RuntimeError("boom")
        return await original(x, y)

    app.state.backend.mouse_move = fail_once
    result = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {"type": "move", "x": 9, "y": 9},
                {"type": "move", "x": 3, "y": 4},
            ],
        },
    ).json()
    assert result["ok"] is False
    assert len(result["results"]) == 1
    assert result["results"][0]["ok"] is False


def test_action_batch_stop_on_runtime_error_skips_screenshot_after(
    test_client, app
) -> None:
    async def fail_move(x: int, y: int):
        raise RuntimeError("boom")

    app.state.backend.mouse_move = fail_move
    result = test_client.post(
        "/v1/actions/run",
        json={
            "screenshot_after": True,
            "actions": [{"type": "move", "x": 9, "y": 9}],
        },
    ).json()

    assert result["ok"] is False
    assert [item["type"] for item in result["results"]] == ["move"]
    assert result["screenshot"] is None


def test_action_batch_continue_runtime_error(test_client, app) -> None:
    original = app.state.backend.mouse_move

    async def fail_once(x: int, y: int):
        if x == 9:
            raise RuntimeError("boom")
        return await original(x, y)

    app.state.backend.mouse_move = fail_once
    result = test_client.post(
        "/v1/actions/run",
        json={
            "continue_on_error": True,
            "actions": [
                {"type": "move", "x": 9, "y": 9},
                {"type": "move", "x": 3, "y": 4},
            ],
        },
    ).json()
    assert result["ok"] is False
    assert result["results"][0]["ok"] is False
    assert result["results"][1]["ok"] is True


def test_action_batch_idempotency(test_client) -> None:
    payload = {"actions": [{"type": "move", "x": 10, "y": 20}]}
    first = test_client.post(
        "/v1/actions/run", json=payload, headers={"Idempotency-Key": "abc"}
    ).json()
    second = test_client.post(
        "/v1/actions/run", json=payload, headers={"Idempotency-Key": "abc"}
    ).json()
    assert first["call_id"] == second["call_id"]


def test_action_batch_includes_safe_daemon_timing(test_client) -> None:
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "move", "x": 10, "y": 20}]},
    ).json()

    assert result["ok"] is True
    assert set(result["timing"]) == {"daemon_ms"}
    assert result["timing"]["daemon_ms"] >= 0
    serialized = str(result)
    assert "xdotool" not in serialized
    assert "stdout" not in serialized.lower()
    assert "stderr" not in serialized.lower()


def test_type_action_failure_redacts_typed_text(test_client, app) -> None:
    sentinel = "_".join(["SENTINEL", "TYPED", "PAYLOAD", "NO", "LEAK"])

    async def fail_type(text: str, delay_ms: int = 10, method: str = "auto"):
        raise RuntimeError(f"typing failed for {text}")

    app.state.backend.keyboard_type = fail_type
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "type", "text": sentinel}]},
    ).json()

    serialized = str(result)
    assert result["ok"] is False
    assert result["results"][0]["error"] == "typing failed for [redacted typed text]"
    assert sentinel not in serialized


def test_hold_key_nested_type_failure_redacts_typed_text(test_client, app) -> None:
    sentinel = "_".join(["NESTED", "TYPED", "PAYLOAD", "NO", "LEAK"])

    async def fail_type(text: str, delay_ms: int = 10, method: str = "auto"):
        raise RuntimeError(f"typing failed for {text}")

    app.state.backend.keyboard_type = fail_type
    result = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "type", "text": sentinel}],
                }
            ],
        },
    ).json()

    serialized = str(result)
    assert result["ok"] is False
    assert result["results"][0]["error"] == "typing failed for [redacted typed text]"
    assert sentinel not in serialized
    assert app.state.backend.held_keys == set()


def test_hold_key_executes_nested_actions_and_releases_key(test_client, app) -> None:
    result = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "move", "x": 7, "y": 8}],
                }
            ],
        },
    ).json()

    assert result["ok"] is True
    assert app.state.backend.cursor.x == 7
    assert app.state.backend.cursor.y == 8
    assert app.state.backend.held_keys == set()
    assert result["results"][0]["output"]["actions"][0]["type"] == "move"


def test_hold_key_releases_key_when_nested_action_fails(test_client, app) -> None:
    async def fail_move(x: int, y: int):
        raise RuntimeError("nested failure")

    app.state.backend.mouse_move = fail_move
    result = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "move", "x": 7, "y": 8}],
                }
            ],
        },
    ).json()

    assert result["ok"] is False
    assert result["results"][0]["error_code"] == "action_failed"
    assert app.state.backend.held_keys == set()


def test_hold_key_nested_failure_is_atomic_under_continue_on_error(test_client, app) -> None:
    original = app.state.backend.mouse_move

    async def fail_first_nested_move(x: int, y: int):
        if x == 7:
            raise RuntimeError("nested failure")
        return await original(x, y)

    app.state.backend.mouse_move = fail_first_nested_move
    result = test_client.post(
        "/v1/actions/run",
        json={
            "continue_on_error": True,
            "actions": [
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [
                        {"type": "move", "x": 7, "y": 8},
                        {"type": "move", "x": 9, "y": 10},
                    ],
                },
                {"type": "move", "x": 3, "y": 4},
            ],
        },
    ).json()

    assert result["ok"] is False
    assert [item["ok"] for item in result["results"]] == [False, True]
    assert app.state.backend.cursor.x == 3
    assert app.state.backend.cursor.y == 4
    assert app.state.backend.held_keys == set()


def test_hold_key_nested_action_validation_uses_desktop_bounds(test_client) -> None:
    response = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "move", "x": 2000, "y": 8}],
                }
            ],
        },
    )

    assert response.status_code == 422
    assert "actions[0].actions[0] x coordinate 2000" in response.json()["details"]["errors"][0]


def test_hold_key_nested_actions_count_against_action_budget(test_client, app) -> None:
    app.state.settings = type(app.state.settings)(
        backend="mock",
        artifacts_dir=app.state.settings.artifacts_dir,
        recordings_dir=app.state.settings.recordings_dir,
        local_token="dev",
        max_actions=1,
    )

    result = test_client.post(
        "/v1/actions/run",
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
            ],
        },
    ).json()

    assert result["ok"] is False
    assert result["results"][0]["error_code"] == "budget_exceeded"
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0
    assert app.state.action_count == 1


def test_hold_key_nested_budget_rejection_happens_before_key_down(test_client, app) -> None:
    app.state.settings = type(app.state.settings)(
        backend="mock",
        artifacts_dir=app.state.settings.artifacts_dir,
        recordings_dir=app.state.settings.recordings_dir,
        local_token="dev",
        max_actions=1,
    )
    key_down_calls: list[str] = []
    original_key_down = app.state.backend.key_down

    async def record_key_down(key: str) -> None:
        key_down_calls.append(key)
        await original_key_down(key)

    app.state.backend.key_down = record_key_down

    result = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "move", "x": 1, "y": 2}],
                }
            ],
        },
    ).json()

    assert result["ok"] is False
    assert result["results"][0]["error_code"] == "budget_exceeded"
    assert key_down_calls == []


def test_hold_key_nested_actions_count_against_batch_limit(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from modal_computer_use.daemon.app import create_app
    from modal_computer_use.daemon.settings import DaemonSettings

    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_batch_actions=1,
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
                            {"type": "move", "x": 1, "y": 2},
                            {"type": "move", "x": 3, "y": 4},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 413
    assert response.json()["code"] == "batch_too_large"
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0
    assert app.state.action_count == 0


def test_drag_action_passes_requested_button_to_backend(test_client, app) -> None:
    seen: dict[str, object] = {}
    original = app.state.backend.mouse_drag

    async def record_drag(*, button: str = "left", **kwargs):
        seen["button"] = button
        return await original(button=button, **kwargs)

    app.state.backend.mouse_drag = record_drag

    result = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {
                    "type": "drag",
                    "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
                    "button": "right",
                }
            ],
        },
    ).json()

    assert result["ok"] is True
    assert seen["button"] == "right"
