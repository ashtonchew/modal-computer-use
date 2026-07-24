from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from PIL import Image

from modal_computer_use.artifacts import ArtifactStore
from modal_computer_use.daemon.desktop import screenshots as screenshots_module
from modal_computer_use.daemon.desktop import x11 as x11_module
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
from modal_computer_use.models import Point, Region, ScreenshotOptions


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


def test_mss_capture_session_falls_back_when_xshm_open_fails(monkeypatch) -> None:
    backends = []

    class FakeMSS:
        def __init__(self, **kwargs):
            backend = kwargs.get("backend")
            backends.append(backend)
            if backend == "xshmgetimage":
                raise RuntimeError("xshm unavailable")

        def grab(self, _monitor):
            return _fake_mss_capture((10, 10)).shot

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=FakeMSS))

    session = screenshots_module._MSSCaptureSession(display=":99")

    assert session.grab(Region(x=0, y=0, width=10, height=10)) is not None
    assert backends == ["xshmgetimage", None]


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


def test_native_only_readiness_does_not_require_xdotool(monkeypatch) -> None:
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

    assert ready is True
    assert errors == []
    assert backend.input_backend == "xtest"
    assert backend.available_input_backends == ("xtest",)
