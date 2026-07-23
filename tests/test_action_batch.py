from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime

import pytest
from PIL import Image

from modal_computer_use.daemon.desktop.screenshots import (
    CapturedRawScreenshot,
    CapturedScreenshot,
)
from modal_computer_use.daemon.desktop.xdamage import XDamageWaitResult
from modal_computer_use.daemon.routes import actions as action_routes
from modal_computer_use.models import ActionResult, CoordinateSpace, Point, Region, sha256_bytes


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


def test_action_batch_observe_change_waits_when_click_ack_precedes_paint(
    test_client,
    app,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    click_count = 0
    capture_count = 0
    click_acknowledged = False
    events: list[str] = []

    async def click_then_defer_paint(*_args, **_kwargs):
        nonlocal click_acknowledged, click_count
        click_count += 1
        click_acknowledged = True
        events.append("click_acknowledged")
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        nonlocal capture_count
        assert click_acknowledged is True
        capture_count += 1
        if capture_count == 1:
            events.append("unchanged_capture")
            return before
        events.append("changed_capture")
        return after

    async def screenshot_bytes(*_args, **_kwargs):
        raise AssertionError("changed confirmation should be the returned frame")

    app.state.backend.mouse_click = click_then_defer_paint
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "click", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": "poll",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert click_count == 1
    assert change_result["detected"] is True
    assert change_result["attempts"] == 2
    assert change_result["timeout_reached"] is False
    assert change_result["source_sha256"] == after.sha256
    assert events == [
        "click_acknowledged",
        "unchanged_capture",
        "changed_capture",
    ]
    assert change_result["confirmed_frame_reused"] is True
    assert change_result["final_frame_source"] == "source_confirmation"
    assert change_result["final_desktop_capture_performed"] is False
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (0, 0, 0)


def test_action_batch_observe_change_reuses_post_action_confirmation_not_baseline(
    test_client,
    app,
) -> None:
    baseline = _raw_screenshot_bytes("white")
    confirming = _raw_screenshot_bytes("red")
    raw_captures = iter([baseline, confirming])
    raw_capture_count = 0

    async def screenshot_raw_pixels(*_args, **_kwargs):
        nonlocal raw_capture_count
        raw_capture_count += 1
        return next(raw_captures)

    async def screenshot_bytes(*_args, **_kwargs):
        raise AssertionError("confirming frame must be encoded without another desktop capture")

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": "poll",
        },
    )

    change_result = _change_result(response)
    change_timing = json.loads(response.headers["x-computer-use-change-timing-ms"])
    assert response.status_code == 200
    assert raw_capture_count == 2
    assert change_result["baseline_source_sha256"] == baseline.sha256
    assert change_result["source_sha256"] == confirming.sha256
    assert change_result["confirmed_frame_reused"] is True
    assert change_result["final_frame_source"] == "source_confirmation"
    assert change_result["final_desktop_capture_performed"] is False
    assert change_timing["confirmed_frame_encode_ms"] >= 0.0
    assert change_timing["fresh_final_capture_ms"] == 0.0
    assert change_timing["screenshot_ms"] >= 0.0
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_action_batch_observe_change_timeout_does_not_retry_click(
    test_client,
    app,
) -> None:
    unchanged = _raw_screenshot_bytes("white")
    click_count = 0

    async def click_without_paint(*_args, **_kwargs):
        nonlocal click_count
        click_count += 1
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return unchanged

    async def screenshot_bytes(*_args, **_kwargs):
        return _encoded_screenshot("white")

    app.state.backend.mouse_click = click_without_paint
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "click", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": unchanged.sha256,
            "change_timeout_ms": 0,
            "poll_interval_ms": 1,
            "change_signal": "poll",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert click_count == 1
    assert change_result["detected"] is False
    assert change_result["attempts"] == 1
    assert change_result["timeout_reached"] is True
    assert change_result["confirmed_frame_reused"] is False
    assert change_result["final_frame_source"] == "fresh_capture"
    assert change_result["final_desktop_capture_performed"] is True


def test_action_batch_observe_change_cursor_output_uses_one_fresh_capture(
    test_client,
    app,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    final_capture_count = 0

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    async def screenshot_bytes(*_args, **_kwargs):
        nonlocal final_capture_count
        final_capture_count += 1
        return _encoded_screenshot("red")

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": True},
            "previous_source_sha256": before.sha256,
            "change_signal": "poll",
        },
    )

    change_result = _change_result(response)
    change_timing = json.loads(response.headers["x-computer-use-change-timing-ms"])
    assert response.status_code == 200
    assert final_capture_count == 1
    assert change_result["detected"] is True
    assert change_result["confirmed_frame_reused"] is False
    assert change_result["final_frame_source"] == "fresh_capture"
    assert change_result["final_desktop_capture_performed"] is True
    assert change_timing["confirmed_frame_encode_ms"] == 0.0
    assert change_timing["fresh_final_capture_ms"] >= 0.0
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_action_batch_observe_change_raw_unavailable_uses_one_fresh_capture(
    test_client,
    app,
) -> None:
    before = _encoded_screenshot("white")
    confirming = _encoded_screenshot("black")
    final = _encoded_screenshot("red")
    encoded_captures = iter([confirming, final])
    encoded_capture_count = 0

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return None

    async def screenshot_bytes(*_args, **_kwargs):
        nonlocal encoded_capture_count
        encoded_capture_count += 1
        return next(encoded_captures)

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "previous_source_sha256": before.sha256,
            "change_signal": "poll",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert encoded_capture_count == 2
    assert change_result["source_sha256"] == confirming.sha256
    assert change_result["confirmed_frame_reused"] is False
    assert change_result["final_frame_source"] == "fresh_capture"
    assert change_result["final_desktop_capture_performed"] is True
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_action_batch_observe_change_regional_confirmation_uses_full_fresh_capture(
    test_client,
    app,
) -> None:
    region = {"x": 0, "y": 0, "width": 4, "height": 4}
    region_rgb = Image.new("RGB", (4, 4), "black").tobytes()
    before_sha256 = sha256_bytes(Image.new("RGB", (4, 4), "white").tobytes())
    confirming = CapturedRawScreenshot(
        width=4,
        height=4,
        rgb=region_rgb,
        sha256=sha256_bytes(region_rgb),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=8,
            desktop_height=8,
            source_region=Region.model_validate(region),
        ),
        cursor_visible=False,
    )
    final_capture_count = 0

    async def screenshot_raw_pixels(*_args, **kwargs):
        assert kwargs["region"].model_dump(mode="json") == region
        return confirming

    async def screenshot_bytes(*_args, **_kwargs):
        nonlocal final_capture_count
        final_capture_count += 1
        return _encoded_screenshot("red")

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "move", "x": 2, "y": 2}],
            "previous_source_sha256": before_sha256,
            "change_detection": "region",
            "change_detection_region": region,
            "change_signal": "poll",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert final_capture_count == 1
    assert change_result["detected"] is True
    assert change_result["confirmed_frame_reused"] is False
    assert change_result["final_frame_source"] == "fresh_capture"
    assert change_result["final_desktop_capture_performed"] is True


def test_action_batch_observe_change_encoding_error_closes_watcher_without_replay(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    mutation_count = 0
    fresh_capture_count = 0
    watcher_close_count = 0

    class DetectedWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            assert display == ":99"

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int) -> XDamageWaitResult:
            return XDamageWaitResult(available=True, detected=True, wait_ms=0.0)

        def close(self) -> None:
            nonlocal watcher_close_count
            watcher_close_count += 1

    async def mouse_move_once(*_args, **_kwargs):
        nonlocal mutation_count
        mutation_count += 1
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    async def screenshot_bytes(*_args, **_kwargs):
        nonlocal fresh_capture_count
        fresh_capture_count += 1
        return _encoded_screenshot("red")

    def fail_encoding(*_args, **_kwargs):
        raise RuntimeError("confirmed frame encoding failed")

    app.state.backend.display = ":99"
    app.state.backend.mouse_move = mouse_move_once
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes
    monkeypatch.setattr(action_routes, "XDamageWatcher", DetectedWatcher)
    monkeypatch.setattr(action_routes, "try_encode_captured_raw", fail_encoding, raising=False)

    with pytest.raises(RuntimeError, match="confirmed frame encoding failed"):
        test_client.post(
            "/v1/actions/run/observe-change/raw-screenshot",
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "previous_source_sha256": before.sha256,
                "change_signal": "xdamage",
            },
        )

    assert mutation_count == 1
    assert fresh_capture_count == 0
    assert watcher_close_count == 1


def test_action_batch_observe_change_falls_back_when_xdamage_setup_is_unavailable(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    click_count = 0

    class UnavailableWatcher:
        failure = "secret setup detail"

        def __init__(self, *, display: str) -> None:
            assert display == ":99"

        def arm(self) -> None:
            raise RuntimeError(self.failure)

        def close(self) -> None:
            pass

    async def click_once(*_args, **_kwargs):
        nonlocal click_count
        click_count += 1
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    app.state.backend.display = ":99"
    app.state.backend.mouse_click = click_once
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", UnavailableWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "click", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": "auto",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert click_count == 1
    assert change_result["detected"] is True
    assert change_result["change_signal_active"] == "poll"
    assert change_result["change_signal_reason"] == "unavailable"
    assert change_result["source_confirmation_active"] is True
    assert change_result["source_confirmation_attempts"] == 1
    assert change_result["source_confirmation_fallback_used"] is True
    assert change_result["source_confirmation_fallback_reason"] == "setup_unavailable"
    assert "secret" not in str(change_result)


def test_action_batch_observe_change_falls_back_when_xdamage_wait_is_unavailable(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    click_count = 0

    class WaitUnavailableWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            assert display == ":99"

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int) -> XDamageWaitResult:
            assert 0 <= timeout_ms <= 25
            return XDamageWaitResult(
                available=False,
                detected=False,
                wait_ms=0.1,
                reason="secret wait detail",
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def click_once(*_args, **_kwargs):
        nonlocal click_count
        click_count += 1
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    app.state.backend.display = ":99"
    app.state.backend.mouse_click = click_once
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", WaitUnavailableWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "click", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": "auto",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert click_count == 1
    assert change_result["detected"] is True
    assert change_result["change_signal_active"] == "xdamage"
    assert change_result["change_signal_available"] is False
    assert change_result["change_signal_reason"] == "unavailable"
    assert change_result["source_confirmation_attempts"] == 1
    assert change_result["source_confirmation_fallback_used"] is True
    assert change_result["source_confirmation_fallback_reason"] == "wait_unavailable"
    assert "secret" not in str(change_result)


def test_action_batch_observe_change_source_confirms_after_irrelevant_xdamage(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    captures = iter([before, after])
    click_count = 0

    class DetectedWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            assert display == ":99"

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int) -> XDamageWaitResult:
            assert 0 <= timeout_ms <= 25
            return XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def click_once(*_args, **_kwargs):
        nonlocal click_count
        click_count += 1
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    app.state.backend.display = ":99"
    app.state.backend.mouse_click = click_once
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", DetectedWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "click", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": "auto",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert click_count == 1
    assert change_result["detected"] is True
    assert change_result["change_signal_detected"] is True
    assert change_result["source_confirmation_attempts"] == 2
    assert change_result["attempts"] == 2
    assert change_result["source_confirmation_fallback_used"] is True
    assert change_result["source_confirmation_fallback_reason"] == "source_unchanged"


def test_action_batch_observe_change_xdamage_source_confirmation_needs_no_fallback(
    test_client,
    app,
    monkeypatch,
) -> None:
    before = _raw_screenshot_bytes("white")
    after = _raw_screenshot_bytes("black")
    click_count = 0

    class DetectedWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            assert display == ":99"

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int) -> XDamageWaitResult:
            assert 0 <= timeout_ms <= 25
            return XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    async def click_once(*_args, **_kwargs):
        nonlocal click_count
        click_count += 1
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return after

    app.state.backend.display = ":99"
    app.state.backend.mouse_click = click_once
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", DetectedWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "click", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": before.sha256,
            "change_timeout_ms": 25,
            "poll_interval_ms": 1,
            "change_signal": "auto",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert click_count == 1
    assert change_result["detected"] is True
    assert change_result["source_confirmation_attempts"] == 1
    assert change_result["source_confirmation_fallback_used"] is False
    assert change_result["source_confirmation_fallback_reason"] is None


def test_action_batch_observe_change_xdamage_signal_timeout_is_bounded(
    test_client,
    app,
    monkeypatch,
) -> None:
    unchanged = _raw_screenshot_bytes("white")
    click_count = 0

    class TimeoutWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            assert display == ":99"

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int) -> XDamageWaitResult:
            assert timeout_ms == 0
            return XDamageWaitResult(
                available=True,
                detected=False,
                wait_ms=0.0,
                reason="secret timeout detail",
            )

        def close(self) -> None:
            pass

    async def click_once(*_args, **_kwargs):
        nonlocal click_count
        click_count += 1
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        raise AssertionError("no source-confirmation budget remains")

    app.state.backend.display = ":99"
    app.state.backend.mouse_click = click_once
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", TimeoutWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "click", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": unchanged.sha256,
            "change_timeout_ms": 0,
            "poll_interval_ms": 1,
            "change_signal": "auto",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert click_count == 1
    assert change_result["detected"] is False
    assert change_result["change_signal_reason"] == "timeout"
    assert change_result["source_confirmation_attempts"] == 0
    assert change_result["source_confirmation_fallback_used"] is True
    assert change_result["source_confirmation_fallback_reason"] == "signal_timeout"
    assert "secret" not in str(change_result)


def test_action_batch_observe_change_xdamage_unchanged_timeout_does_not_replay_click(
    test_client,
    app,
    monkeypatch,
) -> None:
    unchanged = _raw_screenshot_bytes("white")
    click_count = 0
    capture_count = 0

    class DetectedWatcher:
        failure = None

        def __init__(self, *, display: str) -> None:
            assert display == ":99"

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int) -> XDamageWaitResult:
            assert timeout_ms == 0
            return XDamageWaitResult(available=True, detected=True, wait_ms=0.0)

        def close(self) -> None:
            pass

    async def click_once(*_args, **_kwargs):
        nonlocal click_count
        click_count += 1
        return Point(x=10, y=20)

    async def screenshot_raw_pixels(*_args, **_kwargs):
        nonlocal capture_count
        capture_count += 1
        return unchanged

    app.state.backend.display = ":99"
    app.state.backend.mouse_click = click_once
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(action_routes, "XDamageWatcher", DetectedWatcher)

    response = test_client.post(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [{"type": "click", "x": 10, "y": 20}],
            "screenshot_options": {"format": "png", "show_cursor": False},
            "previous_source_sha256": unchanged.sha256,
            "change_timeout_ms": 0,
            "poll_interval_ms": 1,
            "change_signal": "auto",
        },
    )

    change_result = _change_result(response)
    assert response.status_code == 200
    assert click_count == 1
    assert capture_count == 1
    assert change_result["detected"] is False
    assert change_result["timeout_reached"] is True
    assert change_result["source_confirmation_attempts"] == 1
    assert change_result["source_confirmation_fallback_used"] is True
    assert change_result["source_confirmation_fallback_reason"] == "source_unchanged"


def test_remaining_timeout_ms_preserves_positive_fraction(monkeypatch) -> None:
    monkeypatch.setattr(action_routes, "perf_counter", lambda: 10.0001)

    assert action_routes._remaining_timeout_ms(10.001) == 1
    assert action_routes._remaining_timeout_ms(10.0) == 0


def _change_result(response) -> dict:
    return json.loads(
        base64.b64decode(response.headers["x-computer-use-change-result"]).decode("utf-8")
    )


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


def _encoded_screenshot(color: str) -> CapturedScreenshot:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    data = output.getvalue()
    return CapturedScreenshot(
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
        cursor_visible=False,
        capture_backend="test-encoded",
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
