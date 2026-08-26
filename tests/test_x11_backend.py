from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from PIL import Image

from modal_computer_use.artifacts import ArtifactStore
from modal_computer_use.daemon.desktop import browser as browser_module
from modal_computer_use.daemon.desktop import process_runner as process_runner_module
from modal_computer_use.daemon.desktop import screenshots as screenshots_module
from modal_computer_use.daemon.desktop import x11 as x11_module
from modal_computer_use.daemon.desktop.clipboard import X11ClipboardController
from modal_computer_use.daemon.desktop.process_runner import AsyncioProcessRunner
from modal_computer_use.daemon.desktop.x11 import (
    MockDesktopBackend,
    X11DesktopBackend,
    choose_backend,
)
from modal_computer_use.daemon.desktop.xtest import (
    ButtonEvent,
    MotionEvent,
    X11InputInjectionError,
    X11InputReleaseError,
    XTestUnavailableError,
)
from modal_computer_use.models import ActionResult, Point, Region, ScreenshotOptions


class RecordingX11Backend(X11DesktopBackend):
    def __init__(self) -> None:
        super().__init__(
            width=100,
            height=100,
            browser_profile_dir="/tmp/mcu-browser-test",
        )
        self.commands: list[tuple[str, ...]] = []
        self.capture_output_options: list[bool] = []
        self.spawned: list[tuple[str, ...]] = []
        self.clipboard_owner_texts: list[str] = []

    async def _run(
        self,
        *args: str,
        timeout: float = 10.0,
        input_text: str | None = None,
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        self.capture_output_options.append(capture_output)
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

    async def _spawn_clipboard_owner(self, text: str):
        self.clipboard = text
        self.clipboard_owner_texts.append(text)

        class Process:
            alive = True

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.alive = False

            def wait(self, timeout=None):
                del timeout
                self.alive = False
                return 0

            def kill(self):
                self.alive = False

        return Process()


def test_browser_gpu_mode_remains_exported() -> None:
    assert "BrowserGpuMode" in browser_module.__all__


class FakeXTestPointer:
    def __init__(self, *, available: bool = True, fail: bool = False) -> None:
        self._available = available
        self._fail = fail
        self.calls: list[tuple[str, object]] = []

    def available(self) -> bool:
        return self._available

    def emit(
        self,
        events: object,
        *,
        preserve_pressed_keycodes: object = (),
    ) -> None:
        self._record(
            "emit",
            {
                "events": tuple(events),
                "preserve_pressed_keycodes": tuple(preserve_pressed_keycodes),
            },
        )

    def _record(self, name: str, payload: object) -> None:
        if self._fail:
            raise XTestUnavailableError("boom")
        self.calls.append((name, payload))


def test_x11_mouse_uses_xtest_pointer_backend_when_available() -> None:
    backend = RecordingX11Backend()
    xtest = FakeXTestPointer()
    backend._mouse._xtest = xtest

    anyio.run(backend.mouse_move, 1, 2)
    anyio.run(backend.mouse_click, 3, 4)
    anyio.run(backend.mouse_scroll, "down", 2, 5, 6)
    anyio.run(backend.mouse_down, "right", 7, 8)
    anyio.run(backend.mouse_up, "right", 9, 10)

    assert backend.commands == []
    assert backend.input_backend == "xtest"
    assert xtest.calls == [
        (
            "emit",
            {
                "events": (MotionEvent(1, 2),),
                "preserve_pressed_keycodes": (),
            },
        ),
        (
            "emit",
            {
                "events": (
                    MotionEvent(3, 4),
                    ButtonEvent(1, True),
                    ButtonEvent(1, False),
                ),
                "preserve_pressed_keycodes": (),
            },
        ),
        (
            "emit",
            {
                "events": (
                    MotionEvent(5, 6),
                    ButtonEvent(5, True),
                    ButtonEvent(5, False),
                    ButtonEvent(5, True),
                    ButtonEvent(5, False),
                ),
                "preserve_pressed_keycodes": (),
            },
        ),
        (
            "emit",
            {
                "events": (MotionEvent(7, 8), ButtonEvent(3, True)),
                "preserve_pressed_keycodes": (),
            },
        ),
        (
            "emit",
            {
                "events": (MotionEvent(9, 10), ButtonEvent(3, False)),
                "preserve_pressed_keycodes": (),
            },
        ),
    ]


def test_x11_mouse_falls_back_to_xdotool_when_auto_xtest_fails() -> None:
    backend = RecordingX11Backend()
    backend._mouse._xtest = FakeXTestPointer(fail=True)

    point = anyio.run(backend.mouse_click, 4, 5)

    assert point == Point(x=4, y=5)
    assert backend.input_backend == "xdotool"
    assert backend.commands == [
        ("xdotool", "mousemove", "4", "5", "click", "--delay", "0", "--repeat", "1", "1")
    ]


def test_mouse_release_attempt_backend_records_forced_native_failure() -> None:
    backend = RecordingX11Backend()
    backend._mouse._configured_backend = "xtest"
    backend._mouse._xtest = FakeXTestPointer(fail=True)

    with pytest.raises(XTestUnavailableError) as raised:
        anyio.run(backend._mouse.up, "left")

    assert raised.value.input_backend == "xtest"
    assert backend._mouse.release_attempt_backend == "xtest"


def test_mouse_release_attempt_backend_records_xdotool_before_command_failure() -> None:
    backend = RecordingX11Backend()
    backend._mouse._xtest = FakeXTestPointer(fail=True)

    async def fail_run(*_args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("xdotool release failed")

    backend._run = fail_run  # type: ignore[method-assign]

    with pytest.raises(X11InputReleaseError) as raised:
        anyio.run(backend._mouse.up, "left")

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "xdotool release failed"
    assert raised.value.input_backend == "xdotool"
    assert backend._mouse.release_attempt_backend == "xdotool"


def test_release_all_reports_possible_native_button_after_failed_down_compensation() -> None:
    original_error = X11InputInjectionError("partial button down")

    class PartialDownSession:
        failure = None
        release_succeeds = False

        def available(self) -> bool:
            return True

        def emit(self, events: object, **_kwargs: object) -> None:
            button_event = next(
                event for event in events if isinstance(event, ButtonEvent)
            )
            if button_event.pressed:
                raise original_error
            if not self.release_succeeds:
                raise X11InputInjectionError("button up failed")

    backend = RecordingX11Backend()
    session = PartialDownSession()
    backend._mouse._configured_backend = "xtest"
    backend._mouse._xtest = session

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(backend.mouse_down, "left")

    assert raised.value is original_error
    assert backend.held_buttons == {"left"}

    incomplete = anyio.run(backend.release_all)

    assert incomplete.ok is False
    assert incomplete.output["remaining"] == {"keys": [], "buttons": ["left"]}
    assert incomplete.output["failures"] == [
        {
            "kind": "button",
            "value": "left",
            "input_backend": "xtest",
            "code": "button_release_failed",
        }
    ]

    session.release_succeeds = True
    completed = anyio.run(backend.release_all)

    assert completed.ok is True
    assert completed.output == {"keys": [], "buttons": ["left"]}
    assert backend.held_buttons == set()


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
        ("xdotool", "mousemove", "4", "5", "click", "--delay", "0", "--repeat", "1", "1")
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


@pytest.mark.parametrize("operation", ["click", "drag"])
def test_modified_pointer_operations_preserve_preheld_modifiers(operation: str) -> None:
    backend = RecordingX11Backend()
    anyio.run(backend.key_down, "shift")
    backend.commands.clear()

    async def perform() -> None:
        if operation == "click":
            await backend.mouse_click(4, 5, modifiers=["shift"])
        else:
            await backend.mouse_drag(
                start=Point(x=1, y=2),
                end=Point(x=4, y=5),
                duration_ms=0,
                modifiers=["shift"],
            )

    anyio.run(perform)

    assert ("xdotool", "keyup", "shift") not in backend.commands
    assert backend.held_keys == {"shift"}


def test_managed_browser_profile_is_prepared_by_desktop_child(tmp_path, monkeypatch) -> None:
    profile = tmp_path / "browser-profile"
    environment = {
        "COMPUTER_USE_DESKTOP_USER": "computer-desktop",
        "DISPLAY": ":99",
    }
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        browser_module,
        "desktop_process_environment",
        lambda *, display: environment,
    )
    monkeypatch.setattr(
        browser_module,
        "desktop_process_command",
        lambda *args, environ: ("setpriv", "--", *args),
    )

    def run(command, **kwargs):
        commands.append(tuple(command))
        assert kwargs["env"] is environment
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_module.subprocess, "run", run)

    resolved = browser_module.ensure_browser_profile(str(profile))

    assert resolved == str(profile)
    assert commands == [
        (
            "setpriv",
            "--",
            "install",
            "-d",
            "-m",
            "0700",
            "--",
            str(profile),
        )
    ]
    assert not profile.exists()


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
    assert released.output == {
        "keys": ["shift"],
        "buttons": ["left"],
    }


def test_x11_keyboard_type_restores_clipboard_after_clipboard_paste() -> None:
    backend = RecordingX11Backend()
    backend.clipboard = "previous clipboard"

    result = anyio.run(backend.keyboard_type, "x" * 81)

    assert result.ok is True
    assert backend.clipboard_owner_texts
    assert ("xdotool", "key", "ctrl+v") in backend.commands
    assert backend.clipboard == "previous clipboard"


def test_x11_clipboard_set_propagates_process_failure() -> None:
    async def fail(*_args: str, **_kwargs):
        raise FileNotFoundError("xclip")

    async def get_state() -> str:
        return ""

    async def set_state(text: str) -> ActionResult:
        return ActionResult(ok=True, output={"length": len(text)})

    controller = X11ClipboardController(
        run=fail,
        spawn_owner=fail,
        get_state=get_state,
        set_state=set_state,
        clear_state=lambda: set_state(""),
    )

    with pytest.raises(FileNotFoundError, match="xclip"):
        anyio.run(controller.set, "x" * 81)


@pytest.mark.skipif(
    os.name != "posix" or shutil.which("Xvfb") is None or shutil.which("xclip") is None,
    reason="requires Xvfb and xclip",
)
def test_x11_clipboard_daemon_child_preserves_long_text_and_restores_state() -> None:
    xvfb_path = shutil.which("Xvfb")
    assert xvfb_path is not None
    xvfb = subprocess.Popen(  # noqa: S603 - trusted executable resolved by shutil.which.
        [xvfb_path, "-displayfd", "1", "-screen", "0", "1024x768x24"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert xvfb.stdout is not None
    display_number = xvfb.stdout.readline().strip()
    assert display_number.isdigit()
    display = f":{display_number}"
    backend = X11DesktopBackend(display=display, process_runner=AsyncioProcessRunner())
    previous = "previous clipboard"
    long_text = "0123456789" * 10

    async def exercise() -> None:
        await backend.clipboard_set(previous)
        assert await backend.clipboard_get() == previous
        await backend.clipboard_set(long_text)
        assert await backend.clipboard_get() == long_text
        await backend.clipboard_set(previous)
        assert await backend.clipboard_get() == previous

    try:
        anyio.run(exercise)
    finally:
        backend.close()
        xvfb.terminate()
        xvfb.wait(timeout=5)


def test_x11_cursor_position_reads_xdotool_shell_output() -> None:
    backend = RecordingX11Backend()

    point = anyio.run(backend.mouse_position)

    assert point == Point(x=12, y=34)
    assert backend.cursor == Point(x=12, y=34)


def test_x11_screenshot_auto_storage_spills_large_images_to_artifact(tmp_path, monkeypatch) -> None:
    backend = RecordingX11Backend()
    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", lambda *_args: None)

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
    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", lambda *_args: None)

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


def test_x11_screenshot_removes_temp_file_when_native_capture_fails(monkeypatch) -> None:
    backend = RecordingX11Backend()
    temp_paths: list[Path] = []
    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", lambda *_args: None)

    async def fail_capture(path: Path, **_kwargs) -> str:
        temp_paths.append(path)
        raise RuntimeError("capture failed")

    monkeypatch.setattr(backend._screenshots, "_capture_native_png", fail_capture)

    with pytest.raises(RuntimeError, match="capture failed"):
        anyio.run(backend.screenshot, ScreenshotOptions())

    assert temp_paths
    assert all(not path.exists() for path in temp_paths)


def test_x11_screenshot_uses_reencoded_png_when_smaller(monkeypatch) -> None:
    backend = RecordingX11Backend()
    native_png = b"native-png" * 10
    encoded_png = b"small-png"
    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", lambda *_args: None)

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
    backend._screenshots._mss.grab = lambda _source: None

    async def capture() -> None:
        await backend.screenshot(ScreenshotOptions(show_cursor=False))
        await backend.screenshot(ScreenshotOptions(show_cursor=True))

    anyio.run(capture)

    maim_commands = [command for command in backend.commands if command and command[0] == "maim"]
    assert maim_commands[0][1] == "-u"
    assert "-u" not in maim_commands[1]


def test_x11_file_capture_prepares_output_for_desktop_child(monkeypatch) -> None:
    backend = RecordingX11Backend()
    prepared: list[int] = []

    async def write_png(*args: str, **_kwargs):
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png
    backend._screenshots._mss.grab = lambda _source: None
    monkeypatch.setattr(
        screenshots_module,
        "prepare_desktop_output_file",
        prepared.append,
    )

    anyio.run(
        backend.screenshot_bytes,
        ScreenshotOptions(format="png", show_cursor=True),
    )

    assert len(prepared) == 1


def test_x11_screenshot_bytes_skips_cursor_position_by_default(tmp_path) -> None:
    backend = RecordingX11Backend()

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        if args[:2] == ("xdotool", "getmouselocation"):
            raise AssertionError("raw screenshot path should not query cursor position by default")
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png
    backend._screenshots._mss.grab = lambda _source: None

    shot = anyio.run(backend.screenshot_bytes, ScreenshotOptions(format="png"))

    assert shot.width == 10
    assert shot.height == 10
    assert shot.cursor_position is None


def test_x11_screenshot_bytes_native_png_fast_path_skips_pillow(monkeypatch) -> None:
    backend = RecordingX11Backend()
    native_png = _png_bytes("RGB", (10, 10), "white")
    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", lambda *_args: None)

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
    assert shot.capture_backend == "scrot"
    assert backend.commands == [("scrot", "-z", "-o", backend.commands[0][-1])]


def test_x11_screenshot_bytes_scrot_fast_path_supports_regions(monkeypatch) -> None:
    backend = RecordingX11Backend()
    native_png = _png_bytes("RGB", (10, 10), "white")
    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", lambda *_args: None)

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        with open(args[-1], "wb") as handle:
            handle.write(native_png)
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png

    async def capture():
        return await backend.screenshot_bytes(
            ScreenshotOptions(format="png", scale=1.0),
            region=Region(x=3, y=4, width=10, height=11),
            prefer_native_png=True,
        )

    shot = anyio.run(capture)

    assert shot.width == 10
    assert shot.height == 11
    assert backend.commands == [("scrot", "-z", "-o", "-a", "3,4,10,11", backend.commands[0][-1])]


def test_x11_screenshot_bytes_falls_back_to_maim_when_scrot_fails(monkeypatch) -> None:
    backend = RecordingX11Backend()
    native_png = _png_bytes("RGB", (10, 10), "white")
    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", lambda *_args: None)

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        if args[0] == "scrot":
            raise RuntimeError("scrot unavailable")
        with open(args[-1], "wb") as handle:
            handle.write(native_png)
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png

    async def capture():
        return await backend.screenshot_bytes(
            ScreenshotOptions(format="png", scale=1.0),
            prefer_native_png=True,
        )

    shot = anyio.run(capture)

    assert shot.data == native_png
    assert shot.capture_backend == "maim"
    assert backend.commands == [
        ("scrot", "-z", "-o", backend.commands[0][-1]),
        ("maim", "-u", backend.commands[1][-1]),
    ]


def test_x11_screenshot_bytes_prefers_mss_for_raw_native_png(monkeypatch) -> None:
    backend = RecordingX11Backend()
    native_png = _png_bytes("RGB", (10, 10), "white")
    sources: list[Region] = []

    def capture_mss(_session, source: Region) -> object:
        sources.append(source)
        return _fake_mss_capture((source.width, source.height))

    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", capture_mss)
    monkeypatch.setattr(screenshots_module, "_encode_mss_png", lambda _capture: native_png)

    async def capture():
        return await backend.screenshot_bytes(
            ScreenshotOptions(format="png", scale=1.0),
            prefer_native_png=True,
        )

    shot = anyio.run(capture)

    assert shot.data == native_png
    assert shot.width == backend.width
    assert shot.height == backend.height
    assert shot.capture_backend == "mss"
    assert sources == [Region(x=0, y=0, width=backend.width, height=backend.height)]
    assert backend.commands == []


def test_x11_screenshot_bytes_uses_mss_for_raw_jpeg_without_file_capture(monkeypatch) -> None:
    backend = RecordingX11Backend()
    sources: list[Region] = []

    def capture_mss(_session, source: Region) -> object:
        sources.append(source)
        return _fake_mss_capture((10, 10), color=(255, 0, 0))

    monkeypatch.setattr(screenshots_module._MSSCaptureSession, "grab", capture_mss)

    async def fail_file_capture(*_args, **_kwargs):
        raise AssertionError("file screenshot fallback should not run")

    backend._run = fail_file_capture

    async def capture():
        return await backend.screenshot_bytes(
            ScreenshotOptions(format="jpeg", quality=80),
            prefer_native_png=True,
        )

    shot = anyio.run(capture)

    assert shot.format == "jpeg"
    assert shot.width == 10
    assert shot.height == 10
    assert shot.capture_backend == "mss"
    assert shot.data.startswith(b"\xff\xd8")
    assert sources == [Region(x=0, y=0, width=backend.width, height=backend.height)]
    assert backend.commands == []


def test_x11_screenshot_bytes_uses_mss_for_scaled_webp(monkeypatch) -> None:
    backend = RecordingX11Backend()

    monkeypatch.setattr(
        screenshots_module._MSSCaptureSession,
        "grab",
        lambda _session, _source: _fake_mss_capture((10, 10), color=(0, 255, 0)),
    )

    async def capture():
        return await backend.screenshot_bytes(
            ScreenshotOptions(format="webp", quality=80, scale=0.5),
            prefer_native_png=True,
        )

    shot = anyio.run(capture)

    assert shot.format == "webp"
    assert shot.width == 5
    assert shot.height == 5
    assert shot.capture_backend == "mss"
    assert shot.data.startswith(b"RIFF")
    assert backend.commands == []


def test_x11_screenshot_show_cursor_uses_file_capture_when_mss_available(monkeypatch) -> None:
    backend = RecordingX11Backend()
    monkeypatch.setattr(
        screenshots_module._MSSCaptureSession,
        "grab",
        lambda _session, _source: _fake_mss_capture((10, 10)),
    )

    async def write_png(*args: str, **_kwargs):
        backend.commands.append(args)
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = write_png

    async def capture():
        return await backend.screenshot_bytes(
            ScreenshotOptions(format="png", show_cursor=True),
            prefer_native_png=True,
        )

    shot = anyio.run(capture)

    assert shot.capture_backend == "maim"
    assert backend.commands == [("maim", backend.commands[0][-1])]


def test_x11_screenshot_readiness_owns_cursor_capture_probe(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []

    async def run(*args: str, **_kwargs):
        commands.append(args)
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    async def cursor_position() -> Point:
        return Point(x=0, y=0)

    monkeypatch.setattr(
        screenshots_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "maim" else None,
    )
    controller = screenshots_module.X11ScreenshotController(
        run=run,
        width=10,
        height=10,
        display=":99",
        cursor_position=cursor_position,
    )
    monkeypatch.setattr(
        controller._mss,
        "grab",
        lambda _source: _fake_mss_capture((10, 10)),
    )

    ready, error = anyio.run(controller.probe)

    assert ready is True
    assert error is None
    assert commands == [("maim", commands[0][-1])]
    controller.close()


def test_x11_screenshot_readiness_requires_maim_but_not_scrot(monkeypatch) -> None:
    async def run(*_args: str, **_kwargs):
        raise AssertionError("capture should not run without maim")

    async def cursor_position() -> Point:
        return Point(x=0, y=0)

    monkeypatch.setattr(screenshots_module.shutil, "which", lambda _name: None)
    controller = screenshots_module.X11ScreenshotController(
        run=run,
        width=10,
        height=10,
        display=":99",
        cursor_position=cursor_position,
    )

    ready, error = anyio.run(controller.probe)

    assert ready is False
    assert error == "missing required tools: maim"


def test_mss_capture_session_reuses_screenshotter(monkeypatch) -> None:
    instances = []

    class FakeMSS:
        def __init__(self, **_kwargs):
            self.grabs = 0
            instances.append(self)

        def grab(self, _monitor):
            self.grabs += 1
            return _fake_mss_capture((10, 10)).shot

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=FakeMSS))

    session = screenshots_module._MSSCaptureSession(display=":99")

    assert session.grab(Region(x=0, y=0, width=10, height=10)) is not None
    assert session.grab(Region(x=0, y=0, width=10, height=10)) is not None
    assert len(instances) == 1
    assert instances[0].grabs == 2


def test_mss_capture_session_prefers_xshm_and_reopens_once(monkeypatch) -> None:
    kwargs_seen: list[dict[str, object]] = []
    instances = []

    class FakeMSS:
        def __init__(self, **kwargs):
            kwargs_seen.append(kwargs)
            self.closed = False
            self.instance = len(instances)
            instances.append(self)

        def grab(self, _monitor):
            if self.instance == 0:
                raise RuntimeError("stale display connection")
            return _fake_mss_capture((10, 10)).shot

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=FakeMSS))

    session = screenshots_module._MSSCaptureSession(display=":99")

    assert session.grab(Region(x=0, y=0, width=10, height=10)) is not None
    assert kwargs_seen == [
        {"display": ":99", "backend": "xshmgetimage"},
        {"display": ":99", "backend": "xshmgetimage"},
    ]
    assert instances[0].closed is True
    assert instances[1].closed is False


def test_mss_capture_session_returns_none_after_one_reopen(monkeypatch) -> None:
    instances = []

    class FakeMSS:
        def __init__(self, **_kwargs):
            self.closed = False
            instances.append(self)

        def grab(self, _monitor):
            raise RuntimeError("capture unavailable")

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=FakeMSS))

    session = screenshots_module._MSSCaptureSession(display=":99")

    assert session.grab(Region(x=0, y=0, width=10, height=10)) is None
    assert len(instances) == 2
    assert all(instance.closed for instance in instances)


def test_x11_screenshot_tiny_positive_scale_returns_minimum_dimensions() -> None:
    backend = RecordingX11Backend()
    backend._screenshots._mss.grab = lambda _source: None

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


def _fake_mss_capture(
    size: tuple[int, int],
    *,
    color: tuple[int, int, int] = (255, 255, 255),
) -> screenshots_module._MSSCapture:
    width, height = size
    red, green, blue = color

    class Shot:
        rgb = bytes((red, green, blue)) * width * height
        bgra = bytes((blue, green, red, 255)) * width * height

    return screenshots_module._MSSCapture(shot=Shot(), width=width, height=height)


def test_x11_run_kills_subprocess_on_timeout(monkeypatch) -> None:
    backend = X11DesktopBackend(width=100, height=100)
    state = {"killed": False, "drained": False}

    class HangingProcess:
        returncode = None

        async def communicate(self, _input=None):
            if state["killed"]:
                state["drained"] = True
                return b"", b""
            await anyio.sleep_forever()

        def kill(self):
            state["killed"] = True
            self.returncode = -9

    async def create_process(*_args, **_kwargs):
        return HangingProcess()

    async def run_command():
        return await backend._run("xdotool", "mousemove", "1", "2", timeout=0.01)

    monkeypatch.setattr(process_runner_module.asyncio, "create_subprocess_exec", create_process)

    try:
        with pytest.raises(TimeoutError):
            anyio.run(run_command)
        assert state == {"killed": True, "drained": True}
    finally:
        backend.close()


def test_auto_backend_fails_closed_to_x11_on_posix_without_xdotool(monkeypatch) -> None:
    monkeypatch.setattr(x11_module.os, "name", "posix")
    monkeypatch.setattr(x11_module.shutil, "which", lambda _tool: None)

    backend = choose_backend("auto", width=100, height=100, display=":99")

    assert isinstance(backend, X11DesktopBackend)


def test_choose_backend_rejects_unknown_backend_kind() -> None:
    with pytest.raises(
        ValueError,
        match="unsupported desktop backend 'wayland'; expected one of: auto, mock, x11",
    ):
        choose_backend("wayland", width=100, height=100, display=":99")


def test_x11_backend_rejects_unknown_input_backend() -> None:
    with pytest.raises(
        ValueError,
        match="unsupported input backend 'native'; expected one of: auto, xtest, xdotool",
    ):
        X11DesktopBackend(input_backend="native")


@pytest.mark.parametrize(
    "subprocess_backend",
    ["asyncio", "threaded", "isolated-asyncio"],
)
def test_choose_backend_wires_selected_subprocess_backend(subprocess_backend: str) -> None:
    backend = choose_backend(
        "x11",
        width=100,
        height=100,
        display=":99",
        subprocess_backend=subprocess_backend,
    )
    try:
        assert backend.subprocess_backend == subprocess_backend
    finally:
        backend.close()


def test_x11_backend_defaults_to_isolated_asyncio_subprocesses() -> None:
    backend = choose_backend("x11", width=100, height=100, display=":99")
    try:
        assert backend.subprocess_backend == "isolated-asyncio"
    finally:
        backend.close()


def test_x11_backend_close_releases_every_resource_and_preserves_first_error(
    monkeypatch,
) -> None:
    backend = X11DesktopBackend(input_backend="auto")
    events: list[str] = []

    def close(name: str, *, fail: bool = False):
        def operation() -> None:
            events.append(name)
            if fail:
                raise RuntimeError(name)

        return operation

    monkeypatch.setattr(backend._apps, "close", close("apps"))
    monkeypatch.setattr(backend._clipboard, "close", close("clipboard", fail=True))
    monkeypatch.setattr(backend._screenshots, "close", close("screenshots"))
    monkeypatch.setattr(backend._windows, "close", close("windows", fail=True))
    monkeypatch.setattr(backend._input, "close", close("input"))
    monkeypatch.setattr(backend._process_runner, "close", close("runner"))

    with pytest.raises(RuntimeError, match="clipboard"):
        backend.close()

    assert events == ["apps", "clipboard", "screenshots", "windows", "input", "runner"]


def test_x11_backend_invalidates_display_generation_in_place_and_preserves_first_error(
    monkeypatch,
) -> None:
    backend = X11DesktopBackend(input_backend="auto")
    backend.held_keys.add("shift")
    backend.held_buttons.add("left")
    events: list[str] = []

    async def release_all() -> ActionResult:
        events.append("release")
        return ActionResult(ok=False, message="release failed")

    async def invalidate_clipboard() -> None:
        events.append("clipboard")

    async def invalidate_apps() -> None:
        events.append("apps")

    def reset_screenshots() -> None:
        events.append("screenshots")

    def invalidate_windows() -> None:
        events.append("windows")

    def clear_keyboard() -> None:
        events.append("keyboard")

    def clear_mouse() -> None:
        events.append("mouse")

    def invalidate_input() -> None:
        events.append("input")

    monkeypatch.setattr(backend, "release_all", release_all)
    monkeypatch.setattr(backend._apps, "invalidate_display_generation", invalidate_apps)
    monkeypatch.setattr(backend._clipboard, "invalidate_display_generation", invalidate_clipboard)
    monkeypatch.setattr(backend._screenshots, "reset_capture_session", reset_screenshots)
    monkeypatch.setattr(backend._windows, "invalidate_display_generation", invalidate_windows)
    monkeypatch.setattr(backend._keyboard, "invalidate_display_generation", clear_keyboard)
    monkeypatch.setattr(backend._mouse, "invalidate_display_generation", clear_mouse)
    monkeypatch.setattr(backend._input, "invalidate_display_generation", invalidate_input)

    with pytest.raises(RuntimeError, match="release failed"):
        anyio.run(backend.invalidate_display_generation)

    assert events == [
        "release",
        "apps",
        "clipboard",
        "screenshots",
        "windows",
        "keyboard",
        "mouse",
        "input",
    ]
    assert backend.held_keys == set()
    assert backend.held_buttons == set()


def test_input_backend_metadata_separates_policy_support_availability_and_last_use(
    monkeypatch,
) -> None:
    mock = MockDesktopBackend()
    assert mock.configured_input_backend == "mock"
    assert mock.supported_input_backends == ("mock",)
    assert mock.available_input_backends == ("mock",)
    assert mock.input_backend == "mock"

    backend = X11DesktopBackend(input_backend="auto")
    assert backend.configured_input_backend == "auto"
    assert backend.supported_input_backends == ("xtest", "xdotool")
    assert backend.available_input_backends == ()
    assert backend.input_backend is None

    monkeypatch.setattr(backend._input, "available", lambda: True)
    monkeypatch.setattr(
        x11_module.shutil,
        "which",
        lambda name: "/usr/bin/xdotool" if name == "xdotool" else None,
    )
    probe_calls: list[tuple[str, ...]] = []

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        probe_calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run  # type: ignore[method-assign]
    anyio.run(backend._cache_available_input_backends)

    assert backend.available_input_backends == ("xtest", "xdotool")
    assert backend.input_backend is None
    assert probe_calls == [("xdotool", "getmouselocation", "--shell")]


def test_xdotool_availability_requires_a_live_display_probe(monkeypatch) -> None:
    backend = X11DesktopBackend(input_backend="auto")
    monkeypatch.setattr(backend._input, "available", lambda: False)
    monkeypatch.setattr(x11_module.shutil, "which", lambda _name: "/usr/bin/xdotool")

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "cannot open display")

    backend._run = run  # type: ignore[method-assign]

    anyio.run(backend._cache_available_input_backends)

    assert backend.available_input_backends == ()


def test_forced_xdotool_readiness_fails_when_live_probe_cannot_reach_display(
    monkeypatch,
) -> None:
    backend = X11DesktopBackend(input_backend="xdotool")
    monkeypatch.setattr(x11_module.shutil, "which", lambda _name: "/usr/bin/xdotool")

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "cannot open display")

    backend._run = run  # type: ignore[method-assign]

    ready, errors = anyio.run(backend.ready)

    assert ready is False
    assert errors == ["xdotool could not reach display"]
    assert backend.available_input_backends == ()


def test_native_readiness_requires_public_xdotool_compatibility_path(monkeypatch) -> None:
    backend = X11DesktopBackend(input_backend="xtest")
    monkeypatch.setattr(backend._input, "available", lambda: True)
    backend._windows._backend_name = "xlib-ewmh"

    async def probe_windows() -> tuple[bool, str | None]:
        return True, None

    backend._windows.probe_backend = probe_windows  # type: ignore[method-assign]
    monkeypatch.setattr(
        x11_module.shutil,
        "which",
        lambda name: None if name == "xdotool" else f"/usr/bin/{name}",
    )

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        if args[0] == "maim":
            Path(args[1]).write_bytes(b"png")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run  # type: ignore[method-assign]

    ready, errors = anyio.run(backend.ready)

    assert ready is False
    assert errors == ["missing required tools: xdotool"]
    assert backend.input_backend == "xtest"
    assert backend.available_input_backends == ("xtest",)


def test_x11_readiness_delegates_screenshot_probe_and_does_not_require_xsel_or_scrot(
    monkeypatch,
) -> None:
    backend = X11DesktopBackend(input_backend="xtest")
    monkeypatch.setattr(backend._input, "available", lambda: True)
    backend._windows._backend_name = "xlib-ewmh"

    async def cache_input_backends() -> None:
        backend._available_input_backends = ("xtest", "xdotool")

    async def probe_windows() -> tuple[bool, str | None]:
        return True, None

    async def probe_screenshots() -> tuple[bool, str | None]:
        return True, None

    backend._cache_available_input_backends = cache_input_backends  # type: ignore[method-assign]
    backend._windows.probe_backend = probe_windows  # type: ignore[method-assign]
    backend._screenshots.probe = probe_screenshots  # type: ignore[attr-defined,method-assign]
    monkeypatch.setattr(
        x11_module.shutil,
        "which",
        lambda name: None if name in {"xsel", "scrot"} else f"/usr/bin/{name}",
    )

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run  # type: ignore[method-assign]

    ready, errors = anyio.run(backend.ready)

    assert ready is True
    assert errors == []


def test_x11_readiness_surfaces_screenshot_probe_failure(monkeypatch) -> None:
    backend = X11DesktopBackend(input_backend="xtest")
    monkeypatch.setattr(backend._input, "available", lambda: True)
    backend._windows._backend_name = "xlib-ewmh"

    async def cache_input_backends() -> None:
        backend._available_input_backends = ("xtest", "xdotool")

    async def probe_windows() -> tuple[bool, str | None]:
        return True, None

    async def probe_screenshots() -> tuple[bool, str | None]:
        return False, "screenshot capture failed"

    backend._cache_available_input_backends = cache_input_backends  # type: ignore[method-assign]
    backend._windows.probe_backend = probe_windows  # type: ignore[method-assign]
    backend._screenshots.probe = probe_screenshots  # type: ignore[attr-defined,method-assign]
    monkeypatch.setattr(x11_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run  # type: ignore[method-assign]

    ready, errors = anyio.run(backend.ready)

    assert ready is False
    assert errors == ["screenshot capture failed"]


def test_x11_window_backend_reports_controller_backend() -> None:
    backend = X11DesktopBackend()

    backend._windows._backend_name = "wmctrl"

    assert backend.window_backend == "wmctrl"
