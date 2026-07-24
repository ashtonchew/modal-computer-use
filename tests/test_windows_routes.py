from __future__ import annotations

import pytest

from modal_computer_use.models import X11Window


def test_windows_activate_and_close_call_backend(test_client, app) -> None:
    calls: list[tuple[str, str]] = []

    async def activate_window(window_id: str):
        calls.append(("activate", window_id))
        from modal_computer_use.models import ActionResult

        return ActionResult(ok=True, output={"window_id": window_id})

    async def close_window(window_id: str):
        calls.append(("close", window_id))
        from modal_computer_use.models import ActionResult

        return ActionResult(ok=True, output={"window_id": window_id})

    app.state.backend.activate_window = activate_window
    app.state.backend.close_window = close_window

    activate = test_client.post("/v1/windows/mock-root/activate")
    close = test_client.post("/v1/windows/mock-root/close")

    assert activate.status_code == 200
    assert close.status_code == 200
    assert calls == [("activate", "mock-root"), ("close", "mock-root")]


@pytest.mark.parametrize("backend_used", ["xlib-ewmh", "wmctrl"])
def test_window_list_reports_the_backend_used_without_changing_its_body(
    test_client,
    app,
    backend_used: str,
) -> None:
    app.state.backend.window_backend = "before-operation"

    async def windows() -> list[X11Window]:
        app.state.backend.window_backend = backend_used
        return [
            X11Window(
                id="0x0000002a",
                title="Terminal",
                x=1,
                y=2,
                width=100,
                height=80,
                is_active=True,
            )
        ]

    app.state.backend.windows = windows

    response = test_client.get("/v1/windows")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "0x0000002a",
            "title": "Terminal",
            "class_name": None,
            "pid": None,
            "x": 1,
            "y": 2,
            "width": 100,
            "height": 80,
            "workspace": None,
            "is_active": True,
        }
    ]
    assert response.headers["x-computer-use-window-backend"] == backend_used


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/v1/windows/active", None),
        ("POST", "/v1/windows/mock-root/activate", None),
        ("POST", "/v1/windows/mock-root/close", None),
        (
            "POST",
            "/v1/windows/wait-for",
            {"title_regex": "Mock Desktop", "timeout": 0.1},
        ),
    ],
)
def test_each_direct_window_route_reports_its_backend(
    test_client,
    app,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    app.state.backend.window_backend = "mock-window"

    response = test_client.request(method, path, json=payload)

    assert response.status_code == 200
    assert response.headers["x-computer-use-window-backend"] == "mock-window"


def test_windows_wait_for_filters_class_name(test_client, app) -> None:
    async def windows():
        return [
            X11Window(id="one", title="Terminal", x=0, y=0, width=100, height=100),
            X11Window(
                id="two",
                title="Terminal",
                x=0,
                y=0,
                width=100,
                height=100,
                class_name="Browser",
            ),
        ]

    app.state.backend.windows = windows

    response = test_client.post(
        "/v1/windows/wait-for",
        json={"title_regex": "Terminal", "class_name": "Browser", "timeout": 0.1},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "two"


def test_windows_wait_for_rejects_unknown_fields(test_client, app) -> None:
    app.state.supervisor.running = True

    response = test_client.post(
        "/v1/windows/wait-for",
        json={"title_regex": "Terminal", "timeout": 0.1, "unexpected": "value"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_windows_wait_for_rejects_invalid_selectors(test_client, app) -> None:
    app.state.supervisor.running = True

    empty = test_client.post("/v1/windows/wait-for", json={"timeout": 0.1})
    bad_regex = test_client.post(
        "/v1/windows/wait-for",
        json={"title_regex": "[", "timeout": 0.1},
    )
    bad_pid = test_client.post(
        "/v1/windows/wait-for",
        json={"pid": -1, "timeout": 0.1},
    )

    assert empty.status_code == 422
    assert bad_regex.status_code == 422
    assert bad_pid.status_code == 422


def test_windows_wait_for_rechecks_readiness_before_each_poll(test_client, app) -> None:
    app.state.supervisor.running = True
    ready_results = [True, False]

    async def ready():
        return ready_results.pop(0), [] if ready_results else ["display stopped"]

    windows_calls = 0

    async def windows():
        nonlocal windows_calls
        windows_calls += 1
        if windows_calls == 1:
            return []
        return [X11Window(id="late", title="Terminal", x=0, y=0, width=100, height=100)]

    app.state.backend.ready = ready
    app.state.backend.windows = windows

    response = test_client.post(
        "/v1/windows/wait-for",
        json={"title_regex": "Terminal", "timeout": 0.2},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "desktop_not_ready"
    assert windows_calls == 1
