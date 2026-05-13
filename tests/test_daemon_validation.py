from __future__ import annotations

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings


def test_direct_mouse_routes_reject_out_of_bounds_coordinates(test_client) -> None:
    response = test_client.post("/v1/mouse/move", json={"x": 1440, "y": 1})

    assert response.status_code == 422
    assert response.json()["code"] == "coordinate_out_of_bounds"


def test_direct_mouse_routes_reject_partial_coordinate_pairs(test_client) -> None:
    response = test_client.post("/v1/mouse/click", json={"x": 10})

    assert response.status_code == 422


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
        json={"region": {"x": 1400, "y": 0, "width": 80, "height": 100}},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "region_out_of_bounds"


def test_action_validate_uses_desktop_geometry(test_client) -> None:
    response = test_client.post(
        "/v1/actions/validate",
        json={"actions": [{"type": "move", "x": 1440, "y": 0}]},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "x coordinate 1440" in response.json()["errors"][0]


def test_zoom_screenshot_rejects_out_of_bounds_region(test_client) -> None:
    response = test_client.post(
        "/v1/screenshots/zoom",
        json={"region": {"x": 0, "y": 880, "width": 100, "height": 40}},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "region_out_of_bounds"


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

    assert screenshot.status_code == 200
    assert screenshot.json()["ok"] is False
    assert screenshot.json()["results"][0]["error_code"] == "screenshot_too_large"
    assert zoom.status_code == 200
    assert zoom.json()["ok"] is False
    assert zoom.json()["results"][0]["error_code"] == "screenshot_too_large"


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

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["screenshot"] is None
    assert body["results"][-1]["type"] == "screenshot_after"
    assert body["results"][-1]["error_code"] == "screenshot_too_large"
    assert app.state.screenshot_count == 0


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
    assert response.json()["code"] == "validation_error"
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


def test_browser_open_url_rejects_non_http_urls(test_client) -> None:
    response = test_client.post("/v1/browser/open-url", json={"url": "file:///etc/passwd"})

    assert response.status_code == 422


def test_browser_open_url_rejects_url_credentials(test_client) -> None:
    response = test_client.post(
        "/v1/browser/open-url",
        json={"url": "https://user:secret@example.com/"},
    )

    assert response.status_code == 422


def test_app_launch_rejects_shell_command_shape(test_client) -> None:
    response = test_client.post(
        "/v1/apps/launch",
        json={"command": "firefox --private-window", "args": []},
    )

    assert response.status_code == 422


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


def test_direct_keyboard_hold_executes_nested_actions(test_client, app) -> None:
    response = test_client.post(
        "/v1/keyboard/hold",
        json={"key": "shift", "actions": [{"type": "move", "x": 7, "y": 8}]},
    )

    assert response.status_code == 200
    assert app.state.backend.cursor.x == 7
    assert app.state.backend.cursor.y == 8
    assert app.state.backend.held_keys == set()
