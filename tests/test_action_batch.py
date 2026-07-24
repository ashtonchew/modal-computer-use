from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from PIL import Image

from modal_computer_use.daemon.desktop.screenshots import CapturedRawScreenshot
from modal_computer_use.daemon.desktop.xtest import (
    X11InputInjectionError,
    X11InputStateConflictError,
    X11InputUnavailableError,
)
from modal_computer_use.models import ActionResult, CoordinateSpace, sha256_bytes


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


def test_action_batch_attributes_keyboard_input_backend(test_client) -> None:
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "type", "text": "safe", "method": "keystrokes"}]},
    ).json()

    assert result["ok"] is True
    assert result["results"][0]["output"]["input_backend"] == "mock"


@pytest.mark.parametrize(
    ("exception", "code", "retry_safe", "emission_state", "message"),
    [
        (
            X11InputUnavailableError,
            "input_backend_unavailable",
            True,
            "not_started",
            "native input backend is unavailable before input emission",
        ),
        (
            X11InputInjectionError,
            "input_may_be_partial",
            False,
            "possibly_partial",
            "input may have been partially applied",
        ),
    ],
)
def test_action_batch_preserves_native_input_retry_contract(
    test_client,
    app,
    exception,
    code: str,
    retry_safe: bool,
    emission_state: str,
    message: str,
) -> None:
    sentinel = "SENTINEL_TYPED_TEXT_MUST_NOT_LEAK"

    async def fail_type(text: str, delay_ms: int = 10, method: str = "auto"):
        del text, delay_ms, method
        raise exception(sentinel)

    app.state.backend.keyboard_type = fail_type
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "type", "text": sentinel}]},
    ).json()

    item = result["results"][0]
    assert item["error_code"] == code
    assert item["error"] == message
    assert item["output"] == {
        "code": code,
        "input_backend": "xtest",
        "retry_safe": retry_safe,
        "emission_state": emission_state,
    }
    assert sentinel not in str(result)


def test_action_batch_preserves_input_state_conflict(test_client, app) -> None:
    async def fail_press(key: str, modifiers=(), duration_ms: int = 0):
        del key, modifiers, duration_ms
        raise X11InputStateConflictError("private state details")

    app.state.backend.keyboard_press = fail_press
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "keypress", "key": "a"}]},
    ).json()

    item = result["results"][0]
    assert item["error_code"] == "input_state_conflict"
    assert item["error"] == "input target is already held"
    assert item["output"] == {
        "code": "input_state_conflict",
        "retry_safe": True,
        "emission_state": "not_started",
    }


def test_action_batch_attaches_incomplete_cleanup_without_replacing_primary_error(
    test_client,
    app,
) -> None:
    async def fail_move(x: int, y: int):
        del x, y
        raise RuntimeError("primary action failed")

    async def incomplete_release() -> ActionResult:
        return ActionResult(
            ok=False,
            message="failed to release all held input",
            output={
                "code": "release_all_incomplete",
                "keys": [],
                "buttons": [],
                "remaining": {"keys": ["shift"], "buttons": []},
                "failures": [
                    {
                        "kind": "key",
                        "value": "shift",
                        "input_backend": "xtest",
                        "code": "key_release_failed",
                    }
                ],
            },
        )

    app.state.backend.mouse_move = fail_move
    app.state.backend.release_all = incomplete_release
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "move", "x": 1, "y": 1}]},
    ).json()

    item = result["results"][0]
    assert item["error_code"] == "action_failed"
    assert item["error"] == "primary action failed"
    assert item["output"] == {
        "cleanup": {
            "code": "release_all_incomplete",
            "keys": [],
            "buttons": [],
            "remaining": {"keys": ["shift"], "buttons": []},
            "failures": [
                {
                    "kind": "key",
                    "value": "shift",
                    "input_backend": "xtest",
                    "code": "key_release_failed",
                }
            ],
        }
    }


def test_action_batch_reports_explicit_release_all_failure_without_nested_cleanup(
    test_client,
    app,
) -> None:
    calls = 0

    async def incomplete_release() -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(
            ok=False,
            message="failed to release all held input",
            output={
                "code": "release_all_incomplete",
                "keys": [],
                "buttons": [],
                "remaining": {"keys": ["shift"], "buttons": []},
                "failures": [],
            },
        )

    app.state.backend.release_all = incomplete_release
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "release_all"}]},
    ).json()

    item = result["results"][0]
    assert calls == 1
    assert item["error_code"] == "release_all_incomplete"
    assert item["output"] == {
        "code": "release_all_incomplete",
        "keys": [],
        "buttons": [],
        "remaining": {"keys": ["shift"], "buttons": []},
        "failures": [],
    }


def test_action_batch_unexpected_release_all_error_runs_secondary_cleanup(
    test_client,
    app,
) -> None:
    calls = 0

    async def release_all() -> ActionResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("release crashed")
        return ActionResult(
            ok=False,
            message="failed to release all held input",
            output={
                "code": "release_all_incomplete",
                "keys": [],
                "buttons": [],
                "remaining": {"keys": ["shift"], "buttons": []},
                "failures": [],
            },
        )

    app.state.backend.release_all = release_all
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "release_all"}]},
    ).json()

    item = result["results"][0]
    assert calls == 2
    assert item["error_code"] == "action_failed"
    assert item["error"] == "release crashed"
    assert item["output"] == {
        "cleanup": {
            "code": "release_all_incomplete",
            "keys": [],
            "buttons": [],
            "remaining": {"keys": ["shift"], "buttons": []},
            "failures": [],
        }
    }


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


def test_action_batch_stop_on_runtime_error_skips_screenshot_after(test_client, app) -> None:
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


def test_action_batch_treats_backend_action_result_failure_as_failed_item(
    test_client,
    app,
) -> None:
    async def fail_scroll(direction, amount, x=None, y=None):
        return ActionResult(ok=False, message="scroll failed", output={"code": "scroll_failed"})

    app.state.backend.mouse_scroll = fail_scroll

    result = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {"type": "scroll", "direction": "down", "amount": 1},
                {"type": "move", "x": 3, "y": 4},
            ],
        },
    ).json()

    assert result["ok"] is False
    assert [item["ok"] for item in result["results"]] == [False]
    assert result["results"][0]["error"] == "scroll failed"
    assert result["results"][0]["output"] == {"code": "scroll_failed"}
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0


def test_action_batch_continue_after_backend_action_result_failure(test_client, app) -> None:
    async def fail_scroll(direction, amount, x=None, y=None):
        return ActionResult(ok=False, message="scroll failed", output={"code": "scroll_failed"})

    app.state.backend.mouse_scroll = fail_scroll

    result = test_client.post(
        "/v1/actions/run",
        json={
            "continue_on_error": True,
            "actions": [
                {"type": "scroll", "direction": "down", "amount": 1},
                {"type": "move", "x": 3, "y": 4},
            ],
        },
    ).json()

    assert result["ok"] is False
    assert [item["ok"] for item in result["results"]] == [False, True]
    assert app.state.backend.cursor.x == 3
    assert app.state.backend.cursor.y == 4


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


def test_action_batch_raw_screenshot_after_returns_image_bytes(test_client) -> None:
    response = test_client.post(
        "/v1/actions/run/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "screenshot_after": True,
            "screenshot_options": {"format": "png"},
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert response.headers["x-computer-use-width"] == "1024"
    action_result = json.loads(
        base64.b64decode(response.headers["x-computer-use-action-result"]).decode("utf-8")
    )
    assert action_result["ok"] is True
    assert "screenshot" not in action_result
    assert action_result["results"][0]["type"] == "move"


def test_action_batch_raw_screenshot_after_requires_screenshot_after(test_client) -> None:
    response = test_client.post(
        "/v1/actions/run/raw-screenshot",
        json={"actions": [{"type": "move", "x": 10, "y": 20}]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "missing_screenshot_after"


def test_action_batch_observe_change_raw_screenshot_returns_image_and_change_headers(
    test_client,
    app,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    captures = iter([after])

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": "poll",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    action_result = json.loads(
        base64.b64decode(response.headers["x-computer-use-action-result"]).decode("utf-8")
    )
    change_result = json.loads(
        base64.b64decode(response.headers["x-computer-use-change-result"]).decode("utf-8")
    )
    change_timing = json.loads(response.headers["x-computer-use-change-timing-ms"])
    assert action_result["ok"] is True
    assert "screenshot" not in action_result
    assert change_result["detected"] is True
    assert change_result["attempts"] == 1
    assert change_result["baseline_source_sha256"] == before.sha256
    assert change_result["source_sha256"] == after.sha256
    assert change_timing["baseline_capture_ms"] == 0.0
    assert change_timing["total_ms"] >= 0.0


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


def _raw_screenshot_bytes(color: str) -> CapturedRawScreenshot:
    image = Image.new("RGB", (8, 8), color)
    rgb = image.tobytes()
    return CapturedRawScreenshot(
        width=8,
        height=8,
        rgb=rgb,
        sha256=sha256_bytes(rgb),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=8,
            desktop_height=8,
            image_width=8,
            image_height=8,
        ),
        cursor_visible=False,
        capture_backend="test-raw",
        timings_ms={"total_ms": 0.0},
    )


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


def test_hold_key_nested_failure_reports_failed_nested_action(test_client, app) -> None:
    original = app.state.backend.mouse_move

    async def fail_second_nested_move(x: int, y: int):
        if x == 9:
            raise RuntimeError("nested failure")
        return await original(x, y)

    app.state.backend.mouse_move = fail_second_nested_move
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
                        {"type": "move", "x": 11, "y": 12},
                    ],
                },
                {"type": "move", "x": 3, "y": 4},
            ],
        },
    ).json()

    hold = result["results"][0]
    assert result["ok"] is False
    assert [item["ok"] for item in result["results"]] == [False, True]
    assert hold["type"] == "hold_key"
    assert hold["output"]["failed_nested_action"] == {
        "index": 1,
        "type": "move",
        "path": "actions[0].actions[1]",
    }
    assert [item["ok"] for item in hold["output"]["actions"]] == [True, False]
    assert hold["output"]["actions"][1]["error"] == "nested failure"
    assert app.state.backend.cursor.x == 3
    assert app.state.backend.cursor.y == 4
    assert app.state.backend.held_keys == set()


def test_hold_key_nested_action_result_failure_is_atomic(test_client, app) -> None:
    async def fail_type(text: str, delay_ms: int = 10, method: str = "auto"):
        return ActionResult(ok=False, message="nested type failed", output={"code": "type_failed"})

    app.state.backend.keyboard_type = fail_type

    result = test_client.post(
        "/v1/actions/run",
        json={
            "continue_on_error": True,
            "actions": [
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [
                        {"type": "type", "text": "nested-secret"},
                        {"type": "move", "x": 9, "y": 10},
                    ],
                },
                {"type": "move", "x": 3, "y": 4},
            ],
        },
    ).json()

    hold = result["results"][0]
    serialized = str(result)
    assert result["ok"] is False
    assert [item["ok"] for item in result["results"]] == [False, True]
    assert hold["error_code"] == "type_failed"
    assert hold["output"]["failed_nested_action"] == {
        "index": 0,
        "type": "type",
        "path": "actions[0].actions[0]",
    }
    assert [item["ok"] for item in hold["output"]["actions"]] == [False]
    assert "nested-secret" not in serialized
    assert app.state.backend.cursor.x == 3
    assert app.state.backend.cursor.y == 4
    assert app.state.backend.held_keys == set()


def test_direct_keyboard_hold_rejects_nested_actions_without_leaking_text(test_client, app) -> None:
    async def fail_type(text: str, delay_ms: int = 10, method: str = "auto"):
        return ActionResult(
            ok=False,
            message=f"nested type failed for {text}",
            output={"code": "type_failed"},
        )

    app.state.backend.keyboard_type = fail_type

    response = test_client.post(
        "/v1/keyboard/hold",
        json={
            "key": "shift",
            "actions": [
                {"type": "type", "text": "nested-secret"},
                {"type": "move", "x": 9, "y": 10},
            ],
        },
    )

    serialized = response.text
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "nested-secret" not in serialized
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0
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
