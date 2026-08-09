from __future__ import annotations

import asyncio
import errno
import json

import pytest
from fastapi.testclient import TestClient

import modal_computer_use.daemon.readiness as readiness
import modal_computer_use.daemon.routes.commands as command_routes
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.routes.validation import command_argument_byte_limit
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ActionResult, Point


def test_direct_mouse_routes_reject_out_of_bounds_coordinates(test_client) -> None:
    response = test_client.post("/v1/mouse/move", json={"x": 1024, "y": 1})

    assert response.status_code == 422
    assert response.json()["code"] == "coordinate_out_of_bounds"


def test_direct_mouse_routes_reject_partial_coordinate_pairs(test_client) -> None:
    response = test_client.post("/v1/mouse/click", json={"x": 10})

    assert response.status_code == 422


def test_direct_mouse_click_rejects_unsupported_modifiers_before_execution(
    test_client,
    app,
) -> None:
    response = test_client.post(
        "/v1/mouse/click",
        json={"x": 10, "y": 20, "modifiers": ["definitely-not-a-key"]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_key"
    assert app.state.backend.cursor == Point(x=0, y=0)
    assert app.state.action_count == 0


def test_direct_mouse_drag_rejects_unsupported_modifiers_before_execution(test_client, app) -> None:
    response = test_client.post(
        "/v1/mouse/drag",
        json={
            "start_x": 1,
            "start_y": 2,
            "end_x": 3,
            "end_y": 4,
            "modifiers": ["definitely-not-a-key"],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_key"
    assert app.state.backend.cursor == Point(x=0, y=0)
    assert app.state.action_count == 0


def test_direct_input_collection_limits_reject_before_execution(tmp_path) -> None:
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
        drag = client.post(
            "/v1/mouse/drag",
            json={"path": [{"x": 1, "y": 1}, {"x": 2, "y": 2}, {"x": 3, "y": 3}]},
        )
        hotkey = client.post(
            "/v1/keyboard/hotkey",
            json={"keys": ["ctrl", "shift", "t"]},
        )

    assert drag.status_code == 422
    assert drag.json()["code"] == "too_many_drag_points"
    assert drag.json()["details"] == {"field": "path", "count": 3, "maximum": 2}
    assert hotkey.status_code == 422
    assert hotkey.json()["code"] == "too_many_keys"
    assert app.state.action_count == 0


def test_command_limits_are_structured_and_do_not_echo_arguments(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_command_arguments=2,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/commands/run",
            json={"command": ["printf", "secret-one", "secret-two"]},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "too_many_command_arguments"
    assert response.json()["details"] == {
        "field": "command",
        "count": 3,
        "maximum": 2,
    }
    assert "secret" not in response.text
    assert app.state.action_count == 0


def test_command_e2big_is_mapped_to_sanitized_422(test_client, app) -> None:
    async def fail_command(_command, timeout=30.0):
        del timeout
        raise OSError(errno.E2BIG, "argument list includes secret-value")

    app.state.backend.run_command = fail_command

    response = test_client.post("/v1/commands/run", json={"command": ["true"]})

    assert response.status_code == 422
    assert response.json()["code"] == "command_too_large"
    assert response.json()["details"] == {"errno": "E2BIG"}
    assert "secret-value" not in response.text


def test_command_argument_byte_limit_rejects_encoded_bytes_without_echo(test_client) -> None:
    oversized = "s" * (command_argument_byte_limit() + 1)

    response = test_client.post(
        "/v1/apps/launch",
        json={"command": "true", "args": [oversized]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "command_argument_too_large"
    assert response.json()["details"] == {
        "field": "command[1]",
        "encoded_bytes": len(oversized),
        "maximum_bytes": command_argument_byte_limit(),
    }
    assert oversized not in response.text


def test_zero_command_argument_limit_is_explicitly_unlimited(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_command_arguments=0,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/apps/launch",
            json={"command": "true", "args": ["one", "two", "three"]},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_action_batch_rejects_partial_mouse_button_coordinate_pairs(test_client) -> None:
    down = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "mouse_down", "x": 10}]},
    )
    up = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "mouse_up", "y": 10}]},
    )

    assert down.status_code == 422
    assert up.status_code == 422


def test_action_batch_unknown_action_type_uses_action_validation_error(test_client) -> None:
    response = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "future_action"}]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"


def test_action_validate_extra_action_field_uses_action_validation_error(test_client) -> None:
    response = test_client.post(
        "/v1/actions/validate",
        json={"actions": [{"type": "move", "x": 1, "y": 2, "unexpected": "value"}]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"


def test_action_batch_mouse_up_releases_button(test_client, app) -> None:
    response = test_client.post(
        "/v1/actions/run",
        json={
            "actions": [
                {"type": "mouse_down", "button": "left"},
                {"type": "mouse_up", "button": "left"},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert app.state.backend.held_buttons == set()


def test_region_screenshot_rejects_out_of_bounds_region(test_client) -> None:
    response = test_client.post(
        "/v1/screenshots/region",
        json={"region": {"x": 1000, "y": 0, "width": 80, "height": 100}},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "region_out_of_bounds"


def test_action_validate_uses_desktop_geometry(test_client) -> None:
    response = test_client.post(
        "/v1/actions/validate",
        json={"actions": [{"type": "move", "x": 1024, "y": 0}]},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "x coordinate 1024" in response.json()["errors"][0]


def test_action_validate_rejects_unready_desktop_before_backend_geometry(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    async def not_ready():
        return False, ["display missing"]

    app.state.backend.ready = not_ready

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/validate",
            json={"actions": [{"type": "move", "x": 1, "y": 2}]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "desktop_not_ready"


def test_zoom_screenshot_rejects_out_of_bounds_region(test_client) -> None:
    response = test_client.post(
        "/v1/screenshots/zoom",
        json={"region": {"x": 0, "y": 760, "width": 100, "height": 40}},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "region_out_of_bounds"


def test_zoom_screenshot_show_cursor_skips_cursor_outside_region(test_client, app) -> None:
    app.state.backend.cursor = Point(x=10, y=10)

    response = test_client.post(
        "/v1/screenshots/zoom",
        json={
            "region": {"x": 100, "y": 100, "width": 40, "height": 30},
            "scale": 2,
            "show_cursor": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["width"] == 80
    assert body["height"] == 60
    assert body["cursor_visible"] is True
    assert body["cursor_position"] == {"x": 10, "y": 10}


def test_scaled_screenshot_routes_enforce_output_pixel_budget(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            screenshot_max_pixels=1440 * 900,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        full = client.post("/v1/screenshots/full", json={"scale": 2})
        region = client.post(
            "/v1/screenshots/region",
            json={"region": {"x": 0, "y": 0, "width": 720, "height": 450}, "scale": 3},
        )

    assert full.status_code == 413
    assert full.json()["code"] == "screenshot_too_large"
    assert region.status_code == 413
    assert region.json()["code"] == "screenshot_too_large"


def test_action_screenshot_enforces_output_pixel_budget(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            screenshot_max_pixels=1_000,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        screenshot = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "screenshot"}]},
        )
        zoom = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "zoom",
                        "region": {"x": 0, "y": 0, "width": 100, "height": 100},
                        "scale": 2,
                    }
                ]
            },
        )

    assert screenshot.status_code == 422
    assert screenshot.json()["code"] == "action_validation_failed"
    assert "screenshot output" in screenshot.text
    assert zoom.status_code == 422
    assert zoom.json()["code"] == "action_validation_failed"
    assert "screenshot output" in zoom.text


def test_action_zoom_validates_effective_scale_before_execution(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            screenshot_max_pixels=10_000,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "zoom",
                        "region": {"x": 0, "y": 0, "width": 100, "height": 100},
                        "scale": 2,
                        "options": {"scale": 1},
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert "screenshot output" in response.text
    assert app.state.screenshot_count == 0


def test_screenshot_after_enforces_output_pixel_budget(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            screenshot_max_pixels=1_000,
            post_action_delay_ms=0,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 1, "y": 2}],
                "screenshot_after": True,
            },
        )
        position = client.get("/v1/mouse/position")

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert "screenshot_after screenshot output" in response.text
    assert position.json() == {"x": 0, "y": 0}
    assert app.state.screenshot_count == 0


def test_action_validate_matches_run_timeout_and_screenshot_preflight(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_action_timeout_ms=1_000,
            screenshot_max_pixels=1_000,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        timeout = client.post(
            "/v1/actions/validate",
            json={"actions": [{"type": "wait", "duration_ms": 0, "timeout_ms": 2_000}]},
        )
        screenshot = client.post(
            "/v1/actions/validate",
            json={"actions": [{"type": "screenshot"}]},
        )

    assert timeout.json()["ok"] is False
    assert "timeout_ms 2000 exceeds" in timeout.text
    assert screenshot.json()["ok"] is False
    assert "screenshot output" in screenshot.text


def test_keyboard_hold_rejects_nested_actions_before_execution(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_action_timeout_ms=1_000,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/keyboard/hold",
            json={
                "key": "shift",
                "actions": [{"type": "wait", "duration_ms": 0, "timeout_ms": 2_000}],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert app.state.backend.held_keys == set()


def test_keyboard_hold_rejects_nested_screenshot_payload_before_execution(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            screenshot_max_pixels=1_000,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/keyboard/hold",
            json={
                "key": "shift",
                "actions": [
                    {
                        "type": "zoom",
                        "region": {"x": 0, "y": 0, "width": 100, "height": 100},
                        "scale": 2,
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert app.state.backend.held_keys == set()
    assert app.state.screenshot_count == 0


def test_keyboard_hold_nested_actions_reject_before_key_down(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_actions=1,
        )
    )
    key_down_calls: list[str] = []
    original_key_down = app.state.backend.key_down

    async def key_down(key: str) -> None:
        key_down_calls.append(key)
        await original_key_down(key)

    app.state.backend.key_down = key_down

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/keyboard/hold",
            json={
                "key": "shift",
                "actions": [{"type": "move", "x": 1, "y": 2}],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert key_down_calls == []
    assert app.state.backend.held_keys == set()
    assert app.state.action_count == 0


def test_validation_error_response_does_not_echo_typed_text(tmp_path) -> None:
    sentinel = "SECRET_TYPED_TEXT"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "type", "text": f"{sentinel}\t"}]},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert sentinel not in response.text
    assert "input" not in response.text


def test_nested_hold_validation_error_does_not_echo_typed_text(tmp_path) -> None:
    sentinel = "NESTED_TYPED_SECRET"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        action_response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "hold_key",
                        "key": "shift",
                        "actions": [{"type": "type", "text": f"{sentinel}\t"}],
                    }
                ]
            },
        )
        keyboard_response = client.post(
            "/v1/keyboard/hold",
            json={
                "key": "shift",
                "actions": [{"type": "type", "text": f"{sentinel}\t"}],
            },
        )

    assert action_response.status_code == 422
    assert keyboard_response.status_code == 422
    assert sentinel not in action_response.text
    assert sentinel not in keyboard_response.text


def test_action_batch_rejects_unsupported_key_before_execution(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {"type": "move", "x": 10, "y": 20},
                    {"type": "keypress", "key": "definitely-not-a-key"},
                ]
            },
        )
        position = client.get("/v1/mouse/position")

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert "not a supported key" in response.text
    assert position.json() == {"x": 0, "y": 0}


def test_action_batch_rejects_nested_hold_unsupported_key_before_execution(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
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
                        "actions": [{"type": "keypress", "key": "definitely-not-a-key"}],
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert "actions[0].actions[0].key is not a supported key" in response.text
    assert app.state.backend.held_keys == set()


def test_direct_mouse_routes_reject_unsupported_modifiers_before_execution(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        click = client.post(
            "/v1/mouse/click",
            json={"x": 10, "y": 20, "modifiers": ["definitely-not-a-key"]},
        )
        drag = client.post(
            "/v1/mouse/drag",
            json={
                "path": [{"x": 10, "y": 20}, {"x": 30, "y": 40}],
                "modifiers": ["definitely-not-a-key"],
            },
        )

    assert click.status_code == 422
    assert click.json()["code"] == "unsupported_key"
    assert drag.status_code == 422
    assert drag.json()["code"] == "unsupported_key"
    assert app.state.action_count == 0
    assert app.state.backend.cursor == Point(x=0, y=0)


def test_action_batch_rejects_nested_hold_screenshot_budget_before_execution(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            screenshot_max_pixels=1_000,
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
                                "type": "zoom",
                                "region": {"x": 0, "y": 0, "width": 100, "height": 100},
                                "scale": 2,
                            }
                        ],
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert "actions[0].actions[0] screenshot output" in response.text
    assert app.state.backend.held_keys == set()
    assert app.state.screenshot_count == 0


def test_input_routes_reject_unready_desktop_before_budget_or_action(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    async def not_ready():
        return False, ["display missing"]

    app.state.backend.ready = not_ready

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        mouse = client.post("/v1/mouse/move", json={"x": 1, "y": 2})
        action = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 3, "y": 4}]},
        )

    assert mouse.status_code == 503
    assert mouse.json()["code"] == "desktop_not_ready"
    assert action.status_code == 503
    assert action.json()["code"] == "desktop_not_ready"
    assert app.state.backend.cursor == Point(x=0, y=0)
    assert app.state.action_count == 0


def test_release_all_rejects_unready_desktop_before_backend_action(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    called = False

    async def not_ready():
        return False, ["display missing"]

    async def release_all():
        nonlocal called
        called = True
        return await type(app.state.backend).release_all(app.state.backend)

    app.state.backend.ready = not_ready
    app.state.backend.release_all = release_all

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/input/release-all")

    assert response.status_code == 503
    assert response.json()["code"] == "desktop_not_ready"
    assert called is False


def test_direct_mouse_position_rejects_unready_desktop_like_action_cursor_position(
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

    async def not_ready():
        return False, ["display missing"]

    app.state.backend.ready = not_ready

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        direct = client.get("/v1/mouse/position")
        batch = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "cursor_position"}]},
        )

    assert direct.status_code == 503
    assert direct.json()["code"] == "desktop_not_ready"
    assert batch.status_code == 503
    assert batch.json()["code"] == "desktop_not_ready"
    assert app.state.action_count == 0


def test_readyz_and_actions_reject_after_mock_lifecycle_stop(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        stopped = client.post("/v1/computer/stop")
        status = client.get("/v1/computer/status")
        ready = client.get("/readyz")
        action = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 3, "y": 4}]},
        )

    assert stopped.status_code == 200
    assert status.json()["ready"] is False
    assert ready.status_code == 503
    assert ready.json()["ready"] is False
    assert action.status_code == 503
    assert action.json()["code"] == "desktop_not_ready"
    assert app.state.backend.cursor == Point(x=0, y=0)
    assert app.state.action_count == 0


def test_action_rechecks_readiness_after_waiting_for_input_lock(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    app.state.supervisor.running = True

    async def call_with_readiness_flipped_while_queued() -> int:
        precheck_complete = asyncio.Event()
        readiness_calls = 0

        async def ready_then_not_ready():
            nonlocal readiness_calls
            readiness_calls += 1
            if readiness_calls == 1:
                precheck_complete.set()
                return True, []
            return False, ["display missing"]

        app.state.backend.ready = ready_then_not_ready
        body = json.dumps({"actions": [{"type": "move", "x": 7, "y": 8}]}).encode()
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
            "path": "/v1/actions/run",
            "raw_path": b"/v1/actions/run",
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
        await asyncio.wait_for(precheck_complete.wait(), timeout=1)
        app.state.input_lock.release()
        await asyncio.wait_for(task, timeout=1)
        return next(message for message in sent if message["type"] == "http.response.start")[
            "status"
        ]

    assert asyncio.run(call_with_readiness_flipped_while_queued()) == 503
    assert app.state.backend.cursor == Point(x=0, y=0)
    assert app.state.action_count == 0


def test_screenshot_hot_path_reuses_successful_readiness_probe(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    readiness_calls = 0

    async def counted_ready():
        nonlocal readiness_calls
        readiness_calls += 1
        return True, []

    app.state.backend.ready = counted_ready
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post("/v1/screenshots/full", json={"format": "png"})
        second = client.post("/v1/screenshots/full", json={"format": "png"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert readiness_calls == 1


def test_display_restart_invalidates_cached_readiness(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            readiness_cache_ttl_ms=60_000,
        )
    )
    readiness_calls = 0

    async def counted_ready():
        nonlocal readiness_calls
        readiness_calls += 1
        return True, []

    app.state.backend.ready = counted_ready
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        assert client.post("/v1/screenshots/full", json={"format": "png"}).status_code == 200
        assert client.post("/v1/computer/restart").status_code == 200
        assert client.post("/v1/screenshots/full", json={"format": "png"}).status_code == 200

    assert readiness_calls == 2


def test_readiness_invalidation_during_probe_does_not_publish_stale_snapshot() -> None:
    class Backend:
        calls = 0

        async def ready(self):
            self.calls += 1
            if self.calls == 1:
                started.set()
                await release.wait()
            return True, []

    async def scenario() -> None:
        cache = readiness.ReadinessCache(60_000)
        backend = Backend()
        first = asyncio.create_task(cache.backend_ready(backend))
        await started.wait()
        cache.invalidate()
        release.set()
        assert await first == (True, [])
        assert await cache.backend_ready(backend) == (True, [])
        assert backend.calls == 2

    started = asyncio.Event()
    release = asyncio.Event()
    asyncio.run(scenario())


def test_backend_generation_invalidates_cached_readiness() -> None:
    class Backend:
        readiness_generation = 0
        calls = 0

        async def ready(self):
            self.calls += 1
            return self.readiness_generation == 0, (
                [] if self.readiness_generation == 0 else ["capture source quarantined"]
            )

    async def scenario() -> None:
        cache = readiness.ReadinessCache(60_000)
        backend = Backend()
        assert await cache.backend_ready(backend) == (True, [])
        backend.readiness_generation += 1
        assert await cache.backend_ready(backend) == (
            False,
            ["capture source quarantined"],
        )
        assert backend.calls == 2

    asyncio.run(scenario())


def test_screenshot_success_refreshes_readiness_cache(tmp_path, monkeypatch) -> None:
    now = 0.0

    class Clock:
        def monotonic(self) -> float:
            return now

    monkeypatch.setattr(readiness, "time", Clock())
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            readiness_cache_ttl_ms=100,
        )
    )
    readiness_calls = 0

    async def counted_ready():
        nonlocal readiness_calls
        readiness_calls += 1
        return True, []

    app.state.backend.ready = counted_ready
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post("/v1/screenshots/full", json={"format": "png"})
        now += 0.06
        second = client.post("/v1/screenshots/full", json={"format": "png"})
        now += 0.06
        third = client.post("/v1/screenshots/full", json={"format": "png"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert readiness_calls == 1


def test_action_hot_path_reuses_successful_readiness_probe(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    readiness_calls = 0

    async def counted_ready():
        nonlocal readiness_calls
        readiness_calls += 1
        return True, []

    app.state.backend.ready = counted_ready
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post("/v1/actions/run", json={"actions": [{"type": "move", "x": 1, "y": 2}]})
        second = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "click", "x": 3, "y": 4}]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert readiness_calls == 1


def test_action_success_refreshes_readiness_cache(tmp_path, monkeypatch) -> None:
    now = 0.0

    class Clock:
        def monotonic(self) -> float:
            return now

    monkeypatch.setattr(readiness, "time", Clock())
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            readiness_cache_ttl_ms=100,
        )
    )
    readiness_calls = 0

    async def counted_ready():
        nonlocal readiness_calls
        readiness_calls += 1
        return True, []

    app.state.backend.ready = counted_ready
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post("/v1/actions/run", json={"actions": [{"type": "move", "x": 1, "y": 2}]})
        now += 0.06
        second = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "click", "x": 3, "y": 4}]},
        )
        now += 0.06
        third = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 5, "y": 6}]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert readiness_calls == 1


def test_readyz_reuses_successful_readiness_probe_within_cache_ttl(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    readiness_calls = 0

    async def counted_ready():
        nonlocal readiness_calls
        readiness_calls += 1
        return True, []

    app.state.backend.ready = counted_ready
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.get("/readyz")
        second = client.get("/readyz")

    assert first.status_code == 200
    assert second.status_code == 200
    assert readiness_calls == 1


def test_missing_artifact_errors_do_not_echo_user_path(test_client) -> None:
    response = test_client.get("/v1/artifacts/private/customer-secret.txt")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert "customer-secret" not in response.text
    assert "private/" not in response.text


def test_screenshot_and_clipboard_routes_reject_unready_desktop(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    async def not_ready():
        return False, ["display missing"]

    app.state.backend.ready = not_ready

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        screenshot = client.post("/v1/screenshots/full", json={})
        clipboard = client.put("/v1/clipboard/text", json={"text": "secret"})

    assert screenshot.status_code == 503
    assert screenshot.json()["code"] == "desktop_not_ready"
    assert clipboard.status_code == 503
    assert clipboard.json()["code"] == "desktop_not_ready"
    assert app.state.screenshot_count == 0
    assert app.state.backend.clipboard == ""


def test_backend_read_routes_reject_unready_desktop_before_backend_reads(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    backend_calls: list[str] = []

    async def not_ready():
        return False, ["display missing"]

    async def display_info():
        backend_calls.append("display_info")
        return await type(app.state.backend).display_info(app.state.backend)

    async def windows():
        backend_calls.append("windows")
        return await type(app.state.backend).windows(app.state.backend)

    async def active_window():
        backend_calls.append("active_window")
        return await type(app.state.backend).active_window(app.state.backend)

    app.state.backend.ready = not_ready
    app.state.backend.display_info = display_info
    app.state.backend.windows = windows
    app.state.backend.active_window = active_window

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        responses = [
            client.get("/v1/display/info"),
            client.get("/v1/windows"),
            client.get("/v1/windows/active"),
            client.post("/v1/windows/wait-for", json={"title_regex": "missing", "timeout": 0.01}),
            client.get("/v1/browser/status"),
        ]

    assert [response.status_code for response in responses] == [503, 503, 503, 503, 503]
    assert {response.json()["code"] for response in responses} == {"desktop_not_ready"}
    assert backend_calls == []


def test_recording_start_rejects_unready_desktop_before_state(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    async def not_ready():
        return False, ["display missing"]

    app.state.backend.ready = not_ready

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/recordings", json={})

    assert response.status_code == 503
    assert response.json()["code"] == "desktop_not_ready"
    assert app.state.recordings.list() == []


def test_mouse_position_rejects_unready_desktop(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    async def not_ready():
        return False, ["display missing"]

    app.state.backend.ready = not_ready

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.get("/v1/mouse/position")

    assert response.status_code == 503
    assert response.json()["code"] == "desktop_not_ready"


def test_browser_open_url_rejects_non_http_urls(test_client) -> None:
    response = test_client.post("/v1/browser/open-url", json={"url": "file:///etc/passwd"})

    assert response.status_code == 422


def test_browser_open_url_rejects_url_credentials(test_client) -> None:
    response = test_client.post(
        "/v1/browser/open-url",
        json={"url": "https://user:secret@example.com/"},
    )

    assert response.status_code == 422


def test_browser_render_metrics_rejects_non_http_urls(test_client) -> None:
    response = test_client.post("/v1/browser/render-metrics", json={"url": "file:///etc/passwd"})

    assert response.status_code == 422


def test_browser_render_metrics_mock_backend_reports_unavailable(test_client) -> None:
    response = test_client.post(
        "/v1/browser/render-metrics",
        json={"url": "https://example.com", "timeout_seconds": 1},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "unavailable" in response.json()["message"]


def test_browser_status_reports_default_profile_dir(test_client) -> None:
    response = test_client.get("/v1/browser/status")

    assert response.status_code == 200
    assert response.json()["profile_dir"].endswith("/browser-profile")
    assert response.json()["gpu_mode"] == "auto"
    assert response.json()["launch_args"] == []
    assert response.json()["open_url_on_start"] is None


def test_app_launch_rejects_shell_command_shape(test_client) -> None:
    response = test_client.post(
        "/v1/apps/launch",
        json={"command": "firefox --private-window", "args": []},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "command",
    [
        [""],
        ["echo", "bad\x00arg"],
    ],
)
def test_command_run_rejects_invalid_argv_before_backend(test_client, app, command) -> None:
    called = False

    async def run_command(_command, timeout=30.0):
        nonlocal called
        called = True
        raise AssertionError("backend should not run invalid command vectors")

    app.state.backend.run_command = run_command

    response = test_client.post("/v1/commands/run", json={"command": command})

    assert response.status_code == 422
    assert called is False


def test_command_run_elapsed_ms_covers_full_route_work(monkeypatch, test_client, app) -> None:
    async def run_command(_command, timeout=30.0):
        return ActionResult(
            ok=True,
            elapsed_ms=1.0,
            output={"returncode": 0, "stdout": "42"},
        )

    ticks = iter((10.0, 10.025))
    monkeypatch.setattr(command_routes, "perf_counter", lambda: next(ticks))
    app.state.backend.run_command = run_command

    response = test_client.post(
        "/v1/commands/run",
        json={"command": ["sh", "-c", "printf 42"], "timeout": 30},
    )

    assert response.status_code == 200
    assert response.json()["elapsed_ms"] == pytest.approx(25.0)
    assert response.json()["output"] == {"returncode": 0, "stdout": "42"}


def test_direct_keyboard_type_rejects_unknown_method(test_client) -> None:
    response = test_client.post(
        "/v1/keyboard/type",
        json={"text": "hello", "method": "bogus"},
    )

    assert response.status_code == 422


def test_direct_keyboard_type_rejects_control_characters(test_client) -> None:
    response = test_client.post(
        "/v1/keyboard/type",
        json={"text": "hello\t"},
    )

    assert response.status_code == 422


def test_direct_keyboard_hold_is_primitive_only(test_client, app) -> None:
    response = test_client.post(
        "/v1/keyboard/hold",
        json={"key": "shift", "duration_ms": 1},
    )

    assert response.status_code == 200
    assert app.state.backend.held_keys == set()


def test_direct_keyboard_hold_rejects_nested_actions(test_client, app) -> None:
    response = test_client.post(
        "/v1/keyboard/hold",
        json={
            "key": "shift",
            "actions": [
                {"type": "mouse_down", "button": "left"},
                {"type": "wait", "duration_ms": 100, "timeout_ms": 1},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert app.state.backend.held_buttons == set()
    assert app.state.backend.held_keys == set()
