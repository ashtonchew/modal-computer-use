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
