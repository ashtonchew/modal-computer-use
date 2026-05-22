from __future__ import annotations

import subprocess

import anyio
import pytest
from PIL import Image

from modal_computer_use.artifacts import ArtifactStore
from modal_computer_use.daemon.desktop import screenshots as screenshots_module
from modal_computer_use.daemon.desktop import x11 as x11_module
from modal_computer_use.daemon.desktop.x11 import X11DesktopBackend, choose_backend
from modal_computer_use.models import Point, ScreenshotOptions


class RecordingX11Backend(X11DesktopBackend):
    def __init__(self) -> None:
        super().__init__(
            width=100,
            height=100,
            browser_profile_dir="/tmp/mcu-browser-test",
        )
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
        if args == ("xclip", "-selection", "clipboard", "-o"):
            stdout = self.clipboard
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
    assert backend.commands[:4] == [
        ("xdotool", "mousemove", "1", "2"),
        ("xdotool", "mousedown", "3"),
        ("xdotool", "mousemove", "8", "9"),
        ("xdotool", "mouseup", "3"),
    ]


def test_x11_scroll_and_button_hold_are_real_xdotool_commands() -> None:
    backend = RecordingX11Backend()

    anyio.run(backend.mouse_scroll, "down", 3, 4, 5)
    anyio.run(backend.mouse_down, "right")
    anyio.run(backend.mouse_up, "right")

    assert (
        "xdotool",
        "mousemove",
        "4",
        "5",
        "click",
        "--repeat",
        "3",
        "5",
    ) in backend.commands
    assert ("xdotool", "mousedown", "3") in backend.commands
    assert ("xdotool", "mouseup", "3") in backend.commands


def test_x11_mouse_click_batches_move_and_click_without_modifiers() -> None:
    backend = RecordingX11Backend()

    point = anyio.run(backend.mouse_click, 4, 5)

    assert point == Point(x=4, y=5)
    assert backend.commands == [
        ("xdotool", "mousemove", "4", "5", "click", "--repeat", "1", "1")
    ]


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
        (
            "chromium",
            "--user-data-dir=/tmp/mcu-browser-test",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--enable-gpu",
            "https://example.com",
        ),
    ]
    assert opened.output["browser"] == "chromium"


def test_x11_browser_open_url_applies_explicit_vulkan_gpu_mode_and_args() -> None:
    backend = X11DesktopBackend(
        width=100,
        height=100,
        browser="chromium",
        browser_profile_dir="/tmp/mcu-browser-test",
        browser_launch_args=["--force-device-scale-factor=1"],
        browser_gpu_mode="chromium-vulkan",
    )
    spawned: list[tuple[str, ...]] = []

    async def spawn(*args: str):
        spawned.append(args)

        class Process:
            pid = 1234

            def poll(self):
                return None

        return Process()

    backend._spawn = spawn

    result = anyio.run(backend.open_url, "https://example.com", False)

    assert result.ok is True
    assert spawned == [
        (
            "chromium",
            "--user-data-dir=/tmp/mcu-browser-test",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--enable-gpu",
            "--use-angle=vulkan",
            "--enable-features=Vulkan",
            "--disable-vulkan-surface",
            "--force-device-scale-factor=1",
            "https://example.com",
        )
    ]
    assert result.output["gpu_mode"] == "chromium-vulkan"
    assert result.output["launch_args"] == ["--force-device-scale-factor=1"]


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


def test_x11_keyboard_type_restores_clipboard_after_clipboard_paste() -> None:
    backend = RecordingX11Backend()
    backend.clipboard = "previous clipboard"

    result = anyio.run(backend.keyboard_type, "x" * 81)

    assert result.ok is True
    assert ("xclip", "-selection", "clipboard") in backend.commands
    assert ("xdotool", "key", "ctrl+v") in backend.commands
    assert backend.clipboard == "previous clipboard"


def test_x11_cursor_position_reads_xdotool_shell_output() -> None:
    backend = RecordingX11Backend()

    point = anyio.run(backend.mouse_position)

    assert point == Point(x=12, y=34)
    assert backend.cursor == Point(x=12, y=34)


def test_x11_screenshot_auto_storage_spills_large_images_to_artifact(tmp_path, monkeypatch) -> None:
    backend = RecordingX11Backend()

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        if args[:2] == ("xdotool", "getmouselocation"):
            return subprocess.CompletedProcess(args, 0, "X=0\nY=0\n", "")
        Image.new("RGB", (100, 100), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png
    monkeypatch.setattr(
        screenshots_module, "encode_image", lambda *_args, **_kwargs: b"x" * 1_000_001
    )

    async def capture():
        return await backend.screenshot(
            ScreenshotOptions(format="jpeg", storage="auto"),
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
        )

    screenshot = anyio.run(capture)

    assert screenshot.artifact_uri is not None
    assert screenshot.data_base64 is None


def test_x11_screenshot_uses_native_png_when_smaller(monkeypatch) -> None:
    backend = RecordingX11Backend()
    native_png = _png_bytes("P", (10, 10), 0)

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        if args[:2] == ("xdotool", "getmouselocation"):
            return subprocess.CompletedProcess(args, 0, "X=0\nY=0\n", "")
        with open(args[-1], "wb") as handle:
            handle.write(native_png)
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png
    monkeypatch.setattr(
        screenshots_module,
        "encode_image",
        lambda *_args, **_kwargs: native_png + (b"x" * 10),
    )

    screenshot = anyio.run(backend.screenshot, ScreenshotOptions(format="png", scale=1.0))

    assert screenshot.size_bytes == len(native_png)
    assert screenshot.as_bytes() == native_png
    assert screenshot.width == 10
    assert screenshot.height == 10


def test_x11_screenshot_uses_reencoded_png_when_smaller(monkeypatch) -> None:
    backend = RecordingX11Backend()
    native_png = b"native-png" * 10
    encoded_png = b"small-png"

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        if args[:2] == ("xdotool", "getmouselocation"):
            return subprocess.CompletedProcess(args, 0, "X=0\nY=0\n", "")
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png
    monkeypatch.setattr(screenshots_module.Path, "read_bytes", lambda _path: native_png)
    monkeypatch.setattr(screenshots_module, "encode_image", lambda *_args, **_kwargs: encoded_png)

    screenshot = anyio.run(backend.screenshot, ScreenshotOptions(format="png", scale=1.0))

    assert screenshot.size_bytes == len(encoded_png)
    assert screenshot.as_bytes() == encoded_png


def test_x11_screenshot_show_cursor_changes_maim_flags(tmp_path) -> None:
    backend = RecordingX11Backend()

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        if args[:2] == ("xdotool", "getmouselocation"):
            return subprocess.CompletedProcess(args, 0, "X=0\nY=0\n", "")
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png

    async def capture() -> None:
        await backend.screenshot(ScreenshotOptions(show_cursor=False))
        await backend.screenshot(ScreenshotOptions(show_cursor=True))

    anyio.run(capture)

    maim_commands = [command for command in backend.commands if command and command[0] == "maim"]
    assert maim_commands[0][1] == "-u"
    assert "-u" not in maim_commands[1]


def test_x11_screenshot_bytes_skips_cursor_position_by_default(tmp_path) -> None:
    backend = RecordingX11Backend()

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        if args[:2] == ("xdotool", "getmouselocation"):
            raise AssertionError("raw screenshot path should not query cursor position by default")
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png

    shot = anyio.run(backend.screenshot_bytes, ScreenshotOptions(format="png"))

    assert shot.width == 10
    assert shot.height == 10
    assert shot.cursor_position is None


def test_x11_screenshot_bytes_native_png_fast_path_skips_pillow(monkeypatch) -> None:
    backend = RecordingX11Backend()
    native_png = _png_bytes("RGB", (10, 10), "white")

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        with open(args[-1], "wb") as handle:
            handle.write(native_png)
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png
    monkeypatch.setattr(
        screenshots_module.Image,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Pillow not expected")),
    )

    async def capture():
        return await backend.screenshot_bytes(
            ScreenshotOptions(format="png", scale=1.0),
            prefer_native_png=True,
        )

    shot = anyio.run(capture)

    assert shot.data == native_png
    assert shot.width == backend.width
    assert shot.height == backend.height


def test_x11_screenshot_tiny_positive_scale_returns_minimum_dimensions() -> None:
    backend = RecordingX11Backend()

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        if args[:2] == ("xdotool", "getmouselocation"):
            return subprocess.CompletedProcess(args, 0, "X=0\nY=0\n", "")
        Image.new("RGB", (1, 1), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png

    screenshot = anyio.run(backend.screenshot, ScreenshotOptions(scale=0.01))

    assert screenshot.width == 1
    assert screenshot.height == 1
    assert screenshot.coordinate_space.image_width == 1
    assert screenshot.coordinate_space.image_height == 1


def _png_bytes(mode: str, size: tuple[int, int], color: int | str) -> bytes:
    from io import BytesIO

    output = BytesIO()
    Image.new(mode, size, color).save(output, format="PNG", optimize=True)
    return output.getvalue()


def test_x11_run_kills_subprocess_on_timeout(monkeypatch) -> None:
    backend = X11DesktopBackend(width=100, height=100)
    state = {"killed": False, "waited": False}

    class HangingProcess:
        returncode = None

        async def communicate(self, _input=None):
            await anyio.sleep_forever()

        def kill(self):
            state["killed"] = True
            self.returncode = -9

        async def wait(self):
            state["waited"] = True

    async def create_process(*_args, **_kwargs):
        return HangingProcess()

    async def run_command():
        return await backend._run("xdotool", "mousemove", "1", "2", timeout=0.01)

    monkeypatch.setattr(x11_module.asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(TimeoutError):
        anyio.run(run_command)

    assert state == {"killed": True, "waited": True}


def test_auto_backend_fails_closed_to_x11_on_posix_without_xdotool(monkeypatch) -> None:
    monkeypatch.setattr(x11_module.os, "name", "posix")
    monkeypatch.setattr(x11_module.shutil, "which", lambda _tool: None)

    backend = choose_backend("auto", width=100, height=100, display=":99")

    assert isinstance(backend, X11DesktopBackend)
