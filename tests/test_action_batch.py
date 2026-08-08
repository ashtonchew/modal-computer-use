from __future__ import annotations

import base64
import inspect
import json
import threading
from datetime import UTC, datetime
from io import BytesIO
from typing import get_type_hints

import pytest
from PIL import Image

import modal_computer_use.daemon.actions as daemon_actions
from modal_computer_use.daemon.desktop.screenshots import (
    CapturedRawScreenshot,
    CapturedScreenshot,
    encode_image,
)
from modal_computer_use.daemon.desktop.xtest import (
    X11InputInjectionError,
    X11InputStateConflictError,
    X11InputUnavailableError,
)
from modal_computer_use.daemon.routes import actions as action_routes
from modal_computer_use.models import ActionResult, CoordinateSpace, Point, sha256_bytes


def test_daemon_action_exports_have_complete_resolvable_annotations() -> None:
    for name in daemon_actions.__all__:
        value = getattr(daemon_actions, name)
        if not inspect.isfunction(value):
            continue
        signature = inspect.signature(value)
        assert signature.return_annotation is not inspect.Signature.empty, name
        missing = [
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.annotation is inspect.Signature.empty
        ]
        assert not missing, f"{name} has unannotated parameters: {missing}"
        get_type_hints(value)


def test_action_batch_validates_the_whole_array_before_mutation(test_client, app) -> None:
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
    assert app.state.backend.cursor == Point(x=0, y=0)


def test_action_batch_attributes_keyboard_input_backend(test_client) -> None:
    result = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "type", "text": "safe", "method": "keystrokes"}]},
    ).json()

    assert result["ok"] is True
    assert result["results"][0]["output"]["input_backend"] == "mock"


def test_action_batch_executes_actions_in_request_order(test_client, app) -> None:
    calls: list[tuple[int, int]] = []
    original_move = app.state.backend.mouse_move

    async def record_move(x: int, y: int) -> Point:
        calls.append((x, y))
        return await original_move(x, y)

    app.state.backend.mouse_move = record_move

    result = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {"type": "move", "x": 1, "y": 2},
                {"type": "move", "x": 3, "y": 4},
            ]
        },
    ).json()

    assert result["ok"] is True
    assert calls == [(1, 2), (3, 4)]


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


def test_action_batch_observe_change_auto_without_display_reports_poll_fallback(
    test_client,
    app,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": "auto",
        },
    )

    change_result = json.loads(
        base64.b64decode(response.headers["x-computer-use-change-result"]).decode("utf-8")
    )
    assert response.status_code == 200
    assert change_result["change_signal_requested"] == "auto"
    assert change_result["change_signal_active"] == "poll"
    assert change_result["change_signal_available"] is False
    assert change_result["change_signal_detected"] is None
    assert change_result["change_signal_reason"] == "backend has no X11 display"


def test_action_batch_observe_change_route_is_marked_experimental(test_client) -> None:
    operation = test_client.app.openapi()["paths"][
        "/v1/actions/run/observe-change/raw-screenshot"
    ]["post"]

    assert operation["summary"].startswith("Experimental:")
    assert "does not establish application readiness" in operation["description"]


def test_action_batch_observe_change_explicit_xdamage_preserves_unavailable_result(
    test_client,
    app,
    monkeypatch,
) -> None:
    class FakeXDamageWatcher:
        failure = "XDamage extension unavailable"

        def __init__(self, *, display: str) -> None:
            self.display = display
            self.closed = False

        def arm(self) -> None:
            raise RuntimeError(self.failure)

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=False,
                detected=False,
                wait_ms=1.25,
                reason=self.failure,
            )

        def close(self) -> None:
            self.closed = True

    app.state.backend.display = ":99"
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "change_timeout_ms": 25,
            "change_signal": "xdamage",
        },
    )

    change_result = json.loads(
        base64.b64decode(response.headers["x-computer-use-change-result"]).decode("utf-8")
    )
    assert response.status_code == 200
    assert change_result["attempts"] == 1
    assert change_result["change_signal_requested"] == "xdamage"
    assert change_result["change_signal_active"] == "xdamage"
    assert change_result["change_signal_available"] is False
    assert change_result["change_signal_detected"] is False
    assert change_result["change_signal_wait_ms"] == 1.25
    assert change_result["change_signal_reason"] == "XDamage extension unavailable"


def test_action_batch_observe_change_xdamage_does_not_report_unchanged_pixels(
    test_client,
    app,
    monkeypatch,
) -> None:
    unchanged = _raw_screenshot_bytes("white")

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display

        def arm(self) -> None:
            pass

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                reason=None,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return unchanged

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": unchanged.sha256,
            "change_timeout_ms": 1,
            "change_signal": "xdamage",
        },
    )

    change_result = json.loads(
        base64.b64decode(response.headers["x-computer-use-change-result"]).decode("utf-8")
    )
    assert response.status_code == 200
    assert change_result["change_signal_detected"] is True
    assert change_result["detected"] is False
    assert change_result["timeout_reached"] is True
    assert change_result["source_sha256"] == unchanged.sha256


def test_action_batch_observe_change_capture_error_is_limited_to_one_request(
    test_client,
    app,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    capture_attempts = 0

    async def screenshot_raw_pixels(*_args, **_kwargs):
        nonlocal capture_attempts
        capture_attempts += 1
        if capture_attempts == 1:
            raise RuntimeError("transient Xlib capture race")
        return after

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    payload = {
        "actions": [{"type": "move", "x": 10, "y": 20}],
        "previous_source_sha256": before.sha256,
        "change_timeout_ms": 25,
        "poll_interval_ms": 1,
        "change_signal": "poll",
    }

    with pytest.raises(RuntimeError, match="transient Xlib capture race"):
        test_client.post(
            "/v1/actions/run/observe-change/raw-screenshot",
            json=payload,
        )
    succeeded = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json=payload,
    )

    assert succeeded.status_code == 200
    assert _change_result(succeeded)["detected"] is True
    assert capture_attempts == 2


@pytest.mark.parametrize(
    ("screenshot_options", "expected_format", "expected_size"),
    [
        ({"format": "png", "show_cursor": False}, "PNG", (8, 8)),
        (
            {"format": "jpeg", "quality": 80, "scale": 0.5, "show_cursor": False},
            "JPEG",
            (4, 4),
        ),
    ],
)
def test_action_batch_observe_change_returns_the_verified_changed_frame(
    test_client,
    app,
    monkeypatch,
    screenshot_options,
    expected_format: str,
    expected_size: tuple[int, int],
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display

        def arm(self) -> None:
            pass

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": before.sha256,
            "screenshot_options": screenshot_options,
            "change_timeout_ms": 25,
            "change_signal": "xdamage",
        },
    )

    change_result = _change_result(response)
    with Image.open(BytesIO(response.content)) as returned_image:
        returned_rgb = returned_image.convert("RGB")
        assert returned_image.format == expected_format
        assert returned_rgb.size == expected_size
        assert max(returned_rgb.getpixel((0, 0))) <= 2
    assert response.status_code == 200
    assert change_result["detected"] is True
    assert change_result["timeout_reached"] is False
    assert change_result["attempts"] == 1
    assert change_result["source_sha256"] == after.sha256


def test_action_batch_observe_change_xdamage_waits_again_after_unchanged_event(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    captures = iter([before, after])
    waits = iter([0.1, 0.2])

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display

        def arm(self) -> None:
            pass

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=next(waits),
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 25,
            "change_signal": "xdamage",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert change_result["detected"] is True
    assert change_result["timeout_reached"] is False
    assert change_result["attempts"] == 2
    assert change_result["change_signal_detected"] is True
    assert change_result["change_signal_wait_ms"] == pytest.approx(0.3)
    assert change_result["source_sha256"] == after.sha256


def test_action_batch_observe_change_xdamage_uses_real_deadline_after_false_event(
    test_client,
    app,
    monkeypatch,
) -> None:
    unchanged = _raw_screenshot_bytes("white")

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display
            self.calls = 0

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            self.calls += 1
            if self.calls == 1:
                return action_routes.XDamageWaitResult(
                    available=True,
                    detected=True,
                    wait_ms=0.1,
                    version="1.1",
                )
            import time

            time.sleep(timeout_ms / 1000)
            return action_routes.XDamageWaitResult(
                available=True,
                detected=False,
                wait_ms=float(timeout_ms),
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return unchanged

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": unchanged.sha256,
            "change_timeout_ms": 5,
            "change_signal": "xdamage",
        },
    )

    change_result = _change_result(response)
    action_result = json.loads(
        base64.b64decode(response.headers["x-computer-use-action-result"]).decode("utf-8")
    )
    assert response.status_code == 200
    assert action_result["ok"] is True
    assert change_result["detected"] is False
    assert change_result["timeout_reached"] is True
    assert change_result["attempts"] == 2
    assert change_result["change_signal_detected"] is True
    assert change_result["source_sha256"] == unchanged.sha256


@pytest.mark.parametrize("deadline_path", ["xdamage_signal", "xdamage_timeout", "poll"])
def test_action_batch_observe_change_rejects_pixel_proof_completed_after_deadline(
    test_client,
    app,
    monkeypatch,
    deadline_path: str,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")

    class ControlledClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display

        def arm(self) -> None:
            pass

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=True,
                detected=deadline_path == "xdamage_signal",
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    clock = ControlledClock()
    original_capture = action_routes._capture_source_frame

    async def capture_after_deadline(*args, **kwargs):
        frame = await original_capture(*args, **kwargs)
        clock.now = 0.006
        return frame

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "perf_counter", clock)
    monkeypatch.setattr(action_routes, "_capture_source_frame", capture_after_deadline)
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 5,
            "change_signal": "poll" if deadline_path == "poll" else "xdamage",
        },
    )

    change_result = _change_result(response)
    with Image.open(BytesIO(response.content)) as returned_image:
        assert returned_image.convert("RGB").getpixel((0, 0)) == (0, 0, 0)
    assert response.status_code == 200
    assert change_result["source_sha256"] == after.sha256
    assert change_result["detected"] is False
    assert change_result["timeout_reached"] is True


def test_action_batch_observe_change_xdamage_captures_missing_baseline_before_action(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    captures = iter([before, after])

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display

        def arm(self) -> None:
            pass

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "change_timeout_ms": 25,
            "change_signal": "xdamage",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert change_result["detected"] is True
    assert change_result["baseline_source_sha256"] == before.sha256
    assert change_result["source_sha256"] == after.sha256


def test_action_batch_observe_change_region_returns_verified_full_frame(
    test_client,
    app,
    monkeypatch,
) -> None:
    after = _raw_screenshot_bytes("black")
    region = {"x": 0, "y": 0, "width": 4, "height": 4}
    before_region_sha256 = sha256_bytes(Image.new("RGB", (4, 4), "white").tobytes())
    after_region_sha256 = sha256_bytes(Image.new("RGB", (4, 4), "black").tobytes())

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display

        def arm(self) -> None:
            pass

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": before_region_sha256,
            "change_detection": "region",
            "change_detection_region": region,
            "change_timeout_ms": 25,
            "change_signal": "xdamage",
        },
    )

    change_result = _change_result(response)
    with Image.open(BytesIO(response.content)) as returned_image:
        assert returned_image.size == (8, 8)
        assert returned_image.convert("RGB").getpixel((7, 7)) == (0, 0, 0)
    assert response.status_code == 200
    assert change_result["detected"] is True
    assert change_result["source_sha256"] == after_region_sha256


def test_action_batch_observe_change_returns_cursor_visible_canonical_pixels(
    test_client,
    app,
) -> None:
    image = Image.new("RGB", (8, 8), "blue")
    data = encode_image(image, "png", 100)
    returned = CapturedScreenshot(
        format="png",
        width=8,
        height=8,
        data=data,
        sha256=sha256_bytes(data),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=8,
            desktop_height=8,
            image_width=8,
            image_height=8,
        ),
        cursor_visible=True,
        capture_backend="test-cursor",
    )

    async def screenshot_bytes(options, *_args, **_kwargs):
        assert options.format == "png"
        assert options.scale == 1.0
        assert options.show_cursor is True
        return returned

    app.state.backend.display = ":99"
    app.state.backend.screenshot_bytes = screenshot_bytes

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": "0" * 64,
            "screenshot_options": {
                "format": "jpeg",
                "quality": 80,
                "scale": 0.5,
                "show_cursor": True,
            },
            "change_timeout_ms": 25,
            "change_signal": "xdamage",
        },
    )

    assert response.status_code == 200
    with Image.open(BytesIO(response.content)) as returned_image:
        assert returned_image.format == "JPEG"
        assert returned_image.size == (4, 4)
        assert returned_image.convert("RGB").getpixel((0, 0))[2] >= 250
    assert response.headers["x-computer-use-capture-backend"] == "test-cursor"
    assert _change_result(response)["detected"] is True


@pytest.mark.parametrize("cpu_helper", ["regional_hash", "raw_encode"])
def test_action_batch_observe_change_offloads_cpu_heavy_frame_work(
    test_client,
    app,
    monkeypatch,
    cpu_helper: str,
) -> None:
    after = _raw_screenshot_bytes("black")
    event_loop_threads: list[int] = []
    helper_threads: list[int] = []

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display

        def arm(self) -> None:
            pass

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        event_loop_threads.append(threading.get_ident())
        return after

    request: dict[str, object] = {
        "actions": [{"type": "move", "x": 10, "y": 20}],
        "change_timeout_ms": 25,
        "change_signal": "xdamage",
    }
    if cpu_helper == "regional_hash":
        original_helper = action_routes._raw_detection_sha256

        def record_helper_thread(*args, **kwargs):
            helper_threads.append(threading.get_ident())
            return original_helper(*args, **kwargs)

        monkeypatch.setattr(action_routes, "_raw_detection_sha256", record_helper_thread)
        request.update(
            {
                "previous_source_sha256": sha256_bytes(
                    Image.new("RGB", (4, 4), "white").tobytes()
                ),
                "change_detection": "region",
                "change_detection_region": {"x": 0, "y": 0, "width": 4, "height": 4},
            }
        )
    else:
        original_helper = action_routes._encode_verified_screenshot

        def record_helper_thread(*args, **kwargs):
            helper_threads.append(threading.get_ident())
            return original_helper(*args, **kwargs)

        monkeypatch.setattr(action_routes, "_encode_verified_screenshot", record_helper_thread)
        request["previous_source_sha256"] = _raw_screenshot_bytes("white").sha256

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json=request,
    )

    assert response.status_code == 200
    assert _change_result(response)["detected"] is True
    assert len(event_loop_threads) == 1
    assert len(helper_threads) == 1
    assert helper_threads[0] != event_loop_threads[0]


def test_action_batch_observe_change_deadline_ends_after_pixel_verification_before_encoding(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")

    class ControlledClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    class FakeXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display

        def arm(self) -> None:
            pass

        def wait(self, _timeout_ms: int):
            return action_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    clock = ControlledClock()
    original_encode = action_routes._encode_verified_screenshot

    def encode_after_deadline(*args, **kwargs):
        clock.now = 0.006
        return original_encode(*args, **kwargs)

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "perf_counter", clock)
    monkeypatch.setattr(action_routes, "XDamageWatcher", FakeXDamageWatcher)
    monkeypatch.setattr(action_routes, "_encode_verified_screenshot", encode_after_deadline)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 5,
            "change_signal": "xdamage",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert change_result["source_sha256"] == after.sha256
    assert change_result["detected"] is True
    assert change_result["timeout_reached"] is False


@pytest.mark.parametrize("change_signal", ["poll", "auto", "xdamage"])
def test_action_batch_observe_change_cursor_visible_uses_pixel_polling(
    test_client,
    app,
    monkeypatch,
    change_signal: str,
) -> None:
    watcher_instances: list[object] = []

    class ControlledClock:
        now = 0.0

        def __call__(self) -> float:
            current = self.now
            self.now += 0.001
            return current

    class UnexpectedXDamageWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            self.display = display
            watcher_instances.append(self)

        def arm(self) -> None:
            raise AssertionError("cursor-visible observation must not arm XDamage")

        def wait(self, _timeout_ms: int):
            raise AssertionError("cursor-visible observation must not wait for XDamage")

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    monkeypatch.setattr(action_routes, "perf_counter", ControlledClock())
    monkeypatch.setattr(action_routes, "XDamageWatcher", UnexpectedXDamageWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 20, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": True},
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": change_signal,
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert watcher_instances == []
    assert change_result["detected"] is True
    assert change_result["timeout_reached"] is False
    assert change_result["change_signal_requested"] == change_signal
    assert change_result["change_signal_active"] == "poll"
    assert change_result["change_signal_detected"] is None
    if change_signal == "poll":
        assert change_result["change_signal_available"] is None
        assert change_result["change_signal_reason"] is None
    else:
        assert change_result["change_signal_available"] is False
        assert change_result["change_signal_reason"] == (
            "cursor-visible screenshots require pixel polling"
        )


def test_action_batch_observe_change_uses_canonical_pixels_before_lossy_scaled_output(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = Image.new("RGB", (8, 8), "white")
    after = before.copy()
    after.putpixel((0, 0), (0, 0, 0))
    canonical_frames = iter(
        [_captured_test_screenshot(before), _captured_test_screenshot(after)]
    )
    requested_options: list[object] = []

    class ControlledClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = ControlledClock()

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return None

    async def screenshot_bytes(options, *_args, **_kwargs):
        requested_options.append(options)
        frame = next(canonical_frames)
        if len(requested_options) == 2:
            clock.now = 0.004
        return frame

    original_encode = action_routes._encode_verified_screenshot

    def encode_after_deadline(*args, **kwargs):
        clock.now = 0.006
        return original_encode(*args, **kwargs)

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes
    monkeypatch.setattr(action_routes, "perf_counter", clock)
    monkeypatch.setattr(action_routes, "_encode_verified_screenshot", encode_after_deadline)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "screenshot_options": {
                "format": "jpeg",
                "quality": 1,
                "scale": 0.125,
                "show_cursor": False,
            },
            "change_timeout_ms": 5,
            "poll_interval_ms": 1,
            "change_signal": "poll",
        },
    )

    change_result = _change_result(response)
    with Image.open(BytesIO(response.content)) as returned_image:
        assert returned_image.format == "JPEG"
        assert returned_image.size == (1, 1)
    assert response.status_code == 200
    assert change_result["detected"] is True
    assert change_result["timeout_reached"] is False
    assert change_result["baseline_source_sha256"] == sha256_bytes(before.tobytes())
    assert change_result["source_sha256"] == sha256_bytes(after.tobytes())
    assert len(requested_options) == 2
    assert all(option.format == "png" for option in requested_options)
    assert all(option.scale == 1.0 for option in requested_options)
    assert all(option.show_cursor is False for option in requested_options)


def test_action_batch_observe_change_derives_response_from_verified_canonical_pixels(
    test_client,
    app,
) -> None:
    before = Image.new("RGB", (8, 8), "white")
    after = Image.new("RGB", (8, 8), "blue")
    canonical_after = _captured_test_screenshot(after)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return None

    async def screenshot_bytes(options, *_args, **_kwargs):
        assert options.format == "png"
        assert options.scale == 1.0
        return canonical_after

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": sha256_bytes(before.tobytes()),
            "screenshot_options": {"format": "png", "scale": 0.5, "show_cursor": False},
            "change_timeout_ms": 25,
            "change_signal": "poll",
        },
    )

    with Image.open(BytesIO(response.content)) as returned_image:
        assert returned_image.format == "PNG"
        assert returned_image.size == (4, 4)
        assert returned_image.convert("RGB").getpixel((0, 0)) == (0, 0, 255)
    assert response.status_code == 200
    assert response.headers["x-computer-use-capture-backend"] == "test-canonical"
    assert _change_result(response)["source_sha256"] == sha256_bytes(after.tobytes())


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


def _captured_test_screenshot(image: Image.Image) -> CapturedScreenshot:
    data = encode_image(image, "png", 90)
    return CapturedScreenshot(
        format="png",
        width=image.width,
        height=image.height,
        data=data,
        sha256=sha256_bytes(data),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=image.width,
            desktop_height=image.height,
            image_width=image.width,
            image_height=image.height,
        ),
        cursor_visible=False,
        capture_backend="test-canonical",
    )


def _change_result(response) -> dict[str, object]:
    return json.loads(
        base64.b64decode(response.headers["x-computer-use-change-result"]).decode("utf-8")
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


def test_nested_hold_depth_is_rejected_before_input(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from modal_computer_use.daemon.app import create_app
    from modal_computer_use.daemon.settings import DaemonSettings

    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_action_depth=2,
        )
    )
    payload = {
        "actions": [
            {
                "type": "hold_key",
                "key": "shift",
                "actions": [
                    {
                        "type": "hold_key",
                        "key": "ctrl",
                        "actions": [{"type": "move", "x": 10, "y": 20}],
                    }
                ],
            }
        ]
    }

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/actions/run", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert "max_action_depth 2" in response.json()["details"]["errors"][0]
    assert app.state.backend.cursor == Point(x=0, y=0)
    assert app.state.action_count == 0


def test_batch_collection_limits_apply_to_nested_actions(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from modal_computer_use.daemon.app import create_app
    from modal_computer_use.daemon.settings import DaemonSettings

    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_drag_points=2,
            max_key_collection_size=2,
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
                            {"type": "hotkey", "keys": ["ctrl", "shift", "t"]}
                        ],
                    }
                ]
            },
        )
        drag_response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "drag",
                        "path": [
                            {"x": 1, "y": 1},
                            {"x": 2, "y": 2},
                            {"x": 3, "y": 3},
                        ],
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert "maximum is 2" in response.json()["details"]["errors"][0]
    assert drag_response.status_code == 422
    assert "maximum is 2" in drag_response.json()["details"]["errors"][0]
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
