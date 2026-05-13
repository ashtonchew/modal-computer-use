from __future__ import annotations

import subprocess

import anyio

from modal_computer_use.daemon.desktop.x11 import X11DesktopBackend
from modal_computer_use.models import Point


class RecordingX11Backend(X11DesktopBackend):
    def __init__(self) -> None:
        super().__init__(width=100, height=100)
        self.commands: list[tuple[str, ...]] = []
        self.spawned: list[tuple[str, ...]] = []

    async def _run(
        self,
        *args: str,
        timeout: float = 10.0,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        stdout = (
            "X=12\nY=34\nSCREEN=0\nWINDOW=1\n"
            if args[:2] == ("xdotool", "getmouselocation")
            else ""
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    async def _spawn(self, *args: str):
        self.spawned.append(args)

        class Process:
            pid = 1234

            def poll(self):
                return None

        return Process()


def test_x11_mouse_drag_uses_xdotool_down_move_up() -> None:
    backend = RecordingX11Backend()

    async def drag() -> Point:
        return await backend.mouse_drag(
            path=[Point(x=1, y=2), Point(x=8, y=9)],
            button="right",
            duration_ms=0,
        )

    point = anyio.run(drag)

    assert point == Point(x=8, y=9)
    assert ("xdotool", "mousedown", "3") in backend.commands
    assert ("xdotool", "mousemove", "1", "2") in backend.commands
    assert ("xdotool", "mousemove", "8", "9") in backend.commands
    assert ("xdotool", "mouseup", "3") in backend.commands


def test_x11_scroll_and_button_hold_are_real_xdotool_commands() -> None:
    backend = RecordingX11Backend()

    anyio.run(backend.mouse_scroll, "down", 3, 4, 5)
    anyio.run(backend.mouse_down, "right")
    anyio.run(backend.mouse_up, "right")

    assert ("xdotool", "mousemove", "4", "5") in backend.commands
    assert ("xdotool", "click", "--repeat", "3", "5") in backend.commands
    assert ("xdotool", "mousedown", "3") in backend.commands
    assert ("xdotool", "mouseup", "3") in backend.commands


def test_x11_mouse_click_applies_and_releases_modifiers() -> None:
    backend = RecordingX11Backend()

    async def click() -> None:
        await backend.mouse_click(4, 5, modifiers=["shift", "ctrl"])

    anyio.run(click)

    assert backend.commands == [
        ("xdotool", "mousemove", "4", "5"),
        ("xdotool", "keydown", "shift"),
        ("xdotool", "keydown", "ctrl"),
        ("xdotool", "click", "--repeat", "1", "1"),
        ("xdotool", "keyup", "ctrl"),
        ("xdotool", "keyup", "shift"),
    ]
    assert backend.held_keys == set()


def test_x11_launch_and_open_url_spawn_desktop_process() -> None:
    backend = RecordingX11Backend()
    backend.browser = "chromium"

    launched = anyio.run(backend.launch, "firefox", ["--new-window"])
    opened = anyio.run(backend.open_url, "https://example.com", False)

    assert launched.ok is True
    assert opened.ok is True
    assert backend.spawned == [
        ("firefox", "--new-window"),
        ("chromium", "https://example.com"),
    ]


def test_x11_keyboard_press_hotkey_hold_and_release_all() -> None:
    backend = RecordingX11Backend()

    anyio.run(backend.keyboard_press, "a", ["ctrl"], 0)
    anyio.run(backend.keyboard_hotkey, ["ctrl", "shift", "t"])
    anyio.run(backend.key_down, "shift")
    anyio.run(backend.mouse_down, "left")
    released = anyio.run(backend.release_all)

    assert ("xdotool", "keydown", "ctrl") in backend.commands
    assert ("xdotool", "key", "a") in backend.commands
    assert ("xdotool", "keyup", "ctrl") in backend.commands
    assert ("xdotool", "key", "ctrl+shift+t") in backend.commands
    assert ("xdotool", "keyup", "shift") in backend.commands
    assert ("xdotool", "mouseup", "1") in backend.commands
    assert released.output == {"keys": ["shift"], "buttons": ["left"]}


def test_x11_cursor_position_reads_xdotool_shell_output() -> None:
    backend = RecordingX11Backend()

    point = anyio.run(backend.mouse_position)

    assert point == Point(x=12, y=34)
    assert backend.cursor == Point(x=12, y=34)
