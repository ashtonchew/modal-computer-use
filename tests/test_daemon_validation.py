from __future__ import annotations


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


def test_direct_keyboard_hold_executes_nested_actions(test_client, app) -> None:
    response = test_client.post(
        "/v1/keyboard/hold",
        json={"key": "shift", "actions": [{"type": "move", "x": 7, "y": 8}]},
    )

    assert response.status_code == 200
    assert app.state.backend.cursor.x == 7
    assert app.state.backend.cursor.y == 8
    assert app.state.backend.held_keys == set()
