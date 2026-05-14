from __future__ import annotations


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


def test_windows_wait_for_filters_class_name(test_client, app) -> None:
    from modal_computer_use.models import X11Window

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
