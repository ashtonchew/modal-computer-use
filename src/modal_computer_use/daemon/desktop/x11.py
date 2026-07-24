from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from PIL import Image, ImageDraw

from modal_computer_use.actions import normalize_key, normalize_key_combo
from modal_computer_use.artifacts import ArtifactStore
from modal_computer_use.daemon.desktop.apps import X11AppController
from modal_computer_use.daemon.desktop.browser import X11BrowserController
from modal_computer_use.daemon.desktop.clipboard import X11ClipboardController
from modal_computer_use.daemon.desktop.display import StaticDisplayController
from modal_computer_use.daemon.desktop.keyboard import X11KeyboardController
from modal_computer_use.daemon.desktop.mouse import X11MouseController
from modal_computer_use.daemon.desktop.screenshots import (
    CapturedRawScreenshot,
    CapturedScreenshot,
    X11ScreenshotController,
    encode_image,
    scaled_dimension,
)
from modal_computer_use.daemon.desktop.windows import X11WindowController
from modal_computer_use.daemon.desktop.xtest import X11InputSession
from modal_computer_use.models import (
    ActionResult,
    CoordinateSpace,
    DisplayGeometry,
    DisplayInfo,
    Point,
    Recording,
    Region,
    Screenshot,
    ScreenshotOptions,
    X11Window,
    sha256_bytes,
)


class DesktopBackend(ABC):
    width: int
    height: int

    @property
    def input_backend(self) -> str:
        return "unknown"

    def close(self) -> None:
        """Release persistent backend resources."""
        return None

    @abstractmethod
    async def ready(self) -> tuple[bool, list[str]]:
        raise NotImplementedError

    @abstractmethod
    async def mouse_move(self, x: int, y: int) -> Point:
        raise NotImplementedError

    @abstractmethod
    async def mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: Sequence[str] = (),
    ) -> Point:
        raise NotImplementedError

    @abstractmethod
    async def mouse_drag(
        self,
        *,
        start: Point | None = None,
        end: Point | None = None,
        path: Sequence[Point] | None = None,
        button: str = "left",
        duration_ms: int = 500,
        modifiers: Sequence[str] = (),
    ) -> Point:
        raise NotImplementedError

    @abstractmethod
    async def mouse_scroll(
        self,
        direction: str,
        amount: int = 1,
        x: int | None = None,
        y: int | None = None,
    ) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def mouse_down(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def mouse_up(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def mouse_position(self) -> Point:
        raise NotImplementedError

    @abstractmethod
    async def keyboard_type(
        self, text: str, delay_ms: int = 10, method: str = "auto"
    ) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def keyboard_press(
        self,
        key: str,
        modifiers: Sequence[str] = (),
        duration_ms: int = 0,
    ) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def keyboard_hotkey(self, keys: Sequence[str], duration_ms: int = 0) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def key_down(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def key_up(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def clipboard_get(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def clipboard_set(self, text: str) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def clipboard_clear(self) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def screenshot(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        artifact_store: ArtifactStore | None = None,
        call_id: str | None = None,
        retention_class: str = "ephemeral",
    ) -> Screenshot:
        raise NotImplementedError

    async def screenshot_bytes(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        include_cursor_position: bool = False,
        prefer_native_png: bool = False,
    ) -> CapturedScreenshot:
        screenshot = await self.screenshot(options, region=region)
        return CapturedScreenshot(
            format=screenshot.format,
            width=screenshot.width,
            height=screenshot.height,
            data=screenshot.as_bytes(),
            sha256=screenshot.sha256 or sha256_bytes(screenshot.as_bytes()),
            captured_at=screenshot.captured_at,
            coordinate_space=screenshot.coordinate_space,
            cursor_visible=screenshot.cursor_visible,
            cursor_position=screenshot.cursor_position if include_cursor_position else None,
            timings_ms={},
        )

    async def screenshot_raw_pixels(
        self,
        *,
        region: Region | None = None,
    ) -> CapturedRawScreenshot | None:
        return None

    @abstractmethod
    async def display_info(self) -> DisplayInfo:
        raise NotImplementedError

    @abstractmethod
    async def windows(self) -> list[X11Window]:
        raise NotImplementedError

    @abstractmethod
    async def active_window(self) -> X11Window | None:
        raise NotImplementedError

    @abstractmethod
    async def activate_window(self, window_id: str) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def close_window(self, window_id: str) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def release_all(self) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def launch(self, command: str, args: Sequence[str] = ()) -> ActionResult:
        raise NotImplementedError

    @abstractmethod
    async def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        raise NotImplementedError

    async def browser_render_metrics(
        self,
        url: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        return {
            "ok": False,
            "message": "browser render metrics are unavailable for this backend",
            "url": url,
            "timeout_seconds": timeout_seconds,
        }

    async def prewarm_browser(self) -> ActionResult:
        return ActionResult(ok=True, message="browser prewarm not configured")

    async def run_command(self, command: Sequence[str], timeout: float = 30.0) -> ActionResult:
        return ActionResult(
            ok=True,
            message="command not executed by mock backend",
            output={"command": list(command)},
        )


class MockDesktopBackend(DesktopBackend):
    def __init__(self, width: int = 1024, height: int = 768) -> None:
        self.width = width
        self.height = height
        self.cursor = Point(x=0, y=0)
        self.clipboard = ""
        self.held_keys: set[str] = set()
        self.held_buttons: set[str] = set()
        self.recordings: dict[str, Recording] = {}

    @property
    def input_backend(self) -> str:
        return "mock"

    async def ready(self) -> tuple[bool, list[str]]:
        return True, []

    def _bound(self, x: int, y: int) -> Point:
        return Point(x=min(max(x, 0), self.width - 1), y=min(max(y, 0), self.height - 1))

    async def mouse_move(self, x: int, y: int) -> Point:
        self.cursor = self._bound(x, y)
        return self.cursor

    async def mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: Sequence[str] = (),
    ) -> Point:
        if x is not None and y is not None:
            self.cursor = self._bound(x, y)
        return self.cursor

    async def mouse_drag(
        self,
        *,
        start: Point | None = None,
        end: Point | None = None,
        path: Sequence[Point] | None = None,
        button: str = "left",
        duration_ms: int = 500,
        modifiers: Sequence[str] = (),
    ) -> Point:
        if path:
            self.cursor = self._bound(path[-1].x, path[-1].y)
        elif end:
            self.cursor = self._bound(end.x, end.y)
        elif start:
            self.cursor = self._bound(start.x, start.y)
        return self.cursor

    async def mouse_scroll(
        self, direction: str, amount: int = 1, x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if x is not None and y is not None:
            self.cursor = self._bound(x, y)
        return ActionResult(ok=True, output={"direction": direction, "amount": amount})

    async def mouse_down(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if x is not None and y is not None:
            self.cursor = self._bound(x, y)
        self.held_buttons.add(button)
        return ActionResult(ok=True)

    async def mouse_up(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if x is not None and y is not None:
            self.cursor = self._bound(x, y)
        self.held_buttons.discard(button)
        return ActionResult(ok=True)

    async def mouse_position(self) -> Point:
        return self.cursor

    async def keyboard_type(
        self, text: str, delay_ms: int = 10, method: str = "auto"
    ) -> ActionResult:
        return ActionResult(
            ok=True, output={"length": len(text), "method": method, "delay_ms": delay_ms}
        )

    async def keyboard_press(
        self, key: str, modifiers: Sequence[str] = (), duration_ms: int = 0
    ) -> ActionResult:
        return ActionResult(
            ok=True, output={"key": normalize_key(key), "modifiers": list(modifiers)}
        )

    async def keyboard_hotkey(self, keys: Sequence[str], duration_ms: int = 0) -> ActionResult:
        return ActionResult(ok=True, output={"keys": normalize_key_combo(keys)})

    async def key_down(self, key: str) -> None:
        self.held_keys.add(normalize_key(key))

    async def key_up(self, key: str) -> None:
        self.held_keys.discard(normalize_key(key))

    async def clipboard_get(self) -> str:
        return self.clipboard

    async def clipboard_set(self, text: str) -> ActionResult:
        self.clipboard = text
        return ActionResult(ok=True, output={"length": len(text)})

    async def clipboard_clear(self) -> ActionResult:
        self.clipboard = ""
        return ActionResult(ok=True)

    async def screenshot(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        artifact_store: ArtifactStore | None = None,
        call_id: str | None = None,
        retention_class: str = "ephemeral",
    ) -> Screenshot:
        captured = await self.screenshot_bytes(
            options,
            region=region,
            include_cursor_position=True,
        )
        data = captured.data
        artifact_uri = None
        data_base64 = base64.b64encode(data).decode("ascii")
        if options.storage == "artifact" or (options.storage == "auto" and len(data) > 1_000_000):
            if artifact_store is None:
                raise RuntimeError("artifact_store required for artifact screenshot storage")
            suffix = "jpg" if options.format == "jpeg" else options.format
            name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            info = artifact_store.write_bytes(
                f"screenshots/{name}_{call_id or 'shot'}.{suffix}",
                data,
                content_type=f"image/{options.format}",
                created_by_call_id=call_id,
                retention_class=retention_class,
            )
            artifact_uri = info.uri
            data_base64 = None
        return Screenshot(
            format=options.format,
            width=captured.width,
            height=captured.height,
            size_bytes=len(data),
            data_base64=data_base64,
            artifact_uri=artifact_uri,
            sha256=captured.sha256,
            captured_at=captured.captured_at,
            coordinate_space=captured.coordinate_space,
            cursor_visible=captured.cursor_visible,
            cursor_position=captured.cursor_position,
        )

    async def screenshot_bytes(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        include_cursor_position: bool = False,
        prefer_native_png: bool = False,
    ) -> CapturedScreenshot:
        source = region or Region(x=0, y=0, width=self.width, height=self.height)
        image_width = scaled_dimension(source.width, options.scale)
        image_height = scaled_dimension(source.height, options.scale)
        img = Image.new("RGB", (image_width, image_height), (245, 246, 248))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, image_width - 1, image_height - 1], outline=(68, 84, 106))
        if options.show_cursor:
            cursor_in_source = (
                source.x <= self.cursor.x < source.right
                and source.y <= self.cursor.y < source.bottom
            )
            if cursor_in_source:
                cursor_x = round((self.cursor.x - source.x) * image_width / source.width)
                cursor_y = round((self.cursor.y - source.y) * image_height / source.height)
                draw.line(
                    [(cursor_x - 6, cursor_y), (cursor_x + 6, cursor_y)],
                    fill=(220, 38, 38),
                    width=2,
                )
                draw.line(
                    [(cursor_x, cursor_y - 6), (cursor_x, cursor_y + 6)],
                    fill=(220, 38, 38),
                    width=2,
                )
        data = encode_image(img, options.format, options.quality)
        coordinate_space = CoordinateSpace.from_dimensions(
            desktop_width=self.width,
            desktop_height=self.height,
            image_width=image_width,
            image_height=image_height,
            source_region=region,
        )
        return CapturedScreenshot(
            format=options.format,
            width=image_width,
            height=image_height,
            data=data,
            sha256=sha256_bytes(data),
            captured_at=datetime.now(UTC),
            coordinate_space=coordinate_space,
            cursor_visible=options.show_cursor,
            cursor_position=self.cursor if include_cursor_position else None,
            timings_ms={},
        )

    async def screenshot_raw_pixels(
        self,
        *,
        region: Region | None = None,
    ) -> CapturedRawScreenshot | None:
        return None

    async def display_info(self) -> DisplayInfo:
        display = DisplayGeometry(id=":99.0", x=0, y=0, width=self.width, height=self.height)
        return DisplayInfo(primary_display=display, total_displays=1, displays=[display])

    async def windows(self) -> list[X11Window]:
        return [
            X11Window(
                id="mock-root",
                title="Mock Desktop",
                x=0,
                y=0,
                width=self.width,
                height=self.height,
                is_active=True,
            )
        ]

    async def active_window(self) -> X11Window | None:
        return (await self.windows())[0]

    async def activate_window(self, window_id: str) -> ActionResult:
        return ActionResult(ok=True, output={"window_id": window_id})

    async def close_window(self, window_id: str) -> ActionResult:
        return ActionResult(ok=True, output={"window_id": window_id})

    async def release_all(self) -> ActionResult:
        released = {"keys": sorted(self.held_keys), "buttons": sorted(self.held_buttons)}
        self.held_keys.clear()
        self.held_buttons.clear()
        return ActionResult(ok=True, output=released)

    async def launch(self, command: str, args: Sequence[str] = ()) -> ActionResult:
        return ActionResult(
            ok=True, message=f"launch requested: {command}", output={"args": list(args)}
        )

    async def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        return ActionResult(
            ok=True, message="url open requested", output={"url": url, "wait": wait_for_window}
        )


class X11DesktopBackend(MockDesktopBackend):
    def __init__(
        self,
        width: int = 1024,
        height: int = 768,
        display: str = ":99",
        browser: str | None = None,
        browser_profile_dir: str | None = None,
        browser_launch_args: Sequence[str] = (),
        browser_gpu_mode: str = "auto",
        input_backend: str = "auto",
    ) -> None:
        super().__init__(width=width, height=height)
        self.display = display
        self.browser = browser
        normalized_input_backend = _normalize_input_backend(input_backend)
        self._last_input_backend = "xdotool"
        self._input = X11InputSession(display=display)
        self._display = StaticDisplayController(width=width, height=height, display=display)
        self._apps = X11AppController(spawn=lambda *args: self._spawn(*args))
        self._windows = X11WindowController(
            run=lambda *args, **kwargs: self._run(*args, **kwargs),
            fallback_windows=super().windows,
            display=display,
        )
        self._browser = X11BrowserController(
            browser=browser,
            launch=self._apps.launch,
            windows=self._windows.list,
            profile_dir=browser_profile_dir,
            launch_args=browser_launch_args,
            gpu_mode=browser_gpu_mode,
        )
        self._clipboard = X11ClipboardController(
            run=lambda *args, **kwargs: self._run(*args, **kwargs),
            get_state=super().clipboard_get,
            set_state=super().clipboard_set,
            clear_state=super().clipboard_clear,
        )
        self._keyboard = X11KeyboardController(
            run=lambda *args, **kwargs: self._run(*args, **kwargs),
            type_state=super().keyboard_type,
            press_state=super().keyboard_press,
            hotkey_state=super().keyboard_hotkey,
            key_down_state=super().key_down,
            key_up_state=super().key_up,
            clipboard_get=self.clipboard_get,
            clipboard_set=self.clipboard_set,
            input_backend=normalized_input_backend,
            xtest=self._input,
        )
        self._mouse = X11MouseController(
            run=lambda *args, **kwargs: self._run(*args, **kwargs),
            move_state=super().mouse_move,
            position_state=super().mouse_position,
            click_state=super().mouse_click,
            drag_state=super().mouse_drag,
            scroll_state=super().mouse_scroll,
            button_down_state=super().mouse_down,
            button_up_state=super().mouse_up,
            key_down=self.key_down,
            key_up=self.key_up,
            input_backend=normalized_input_backend,
            xtest=self._input,
        )
        self._screenshots = X11ScreenshotController(
            run=lambda *args, **kwargs: self._run(*args, **kwargs),
            width=width,
            height=height,
            display=display,
            cursor_position=self.mouse_position,
        )

    @property
    def input_backend(self) -> str:
        return self._last_input_backend

    def close(self) -> None:
        self._windows.close()
        self._input.close()

    async def ready(self) -> tuple[bool, list[str]]:
        input_ready, input_error = self._mouse.probe_backend()
        if not input_ready:
            return False, [input_error or "input backend is not ready"]
        self._last_input_backend = self._mouse.backend_name
        windows_ready, windows_error = self._windows.probe_backend()
        if not windows_ready:
            return False, [windows_error or "window backend is not ready"]

        required_tools = {"maim", "xclip", "xsel", "xdpyinfo", "ffmpeg"}
        if self._mouse.backend_name != "xtest":
            required_tools.add("xdotool")
        if self._windows.backend_name != "xlib-ewmh":
            required_tools.update(("wmctrl", "xdotool"))
        missing = [
            tool
            for tool in sorted(required_tools)
            if shutil.which(tool) is None
        ]
        if missing:
            return False, [f"missing required tools: {', '.join(missing)}"]
        result = await self._run("xdpyinfo", timeout=2, check=False)
        if result.returncode != 0:
            return False, ["xdpyinfo could not reach display"]
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            shot = await self._run("maim", str(temp_path), timeout=3, check=False)
            if shot.returncode != 0 or not temp_path.exists() or temp_path.stat().st_size == 0:
                return False, ["screenshot capture failed"]
        finally:
            temp_path.unlink(missing_ok=True)
        return True, []

    async def _run(
        self,
        *args: str,
        timeout: float = 10.0,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        process = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input_text.encode() if input_text is not None else None),
                timeout=timeout,
            )
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            raise
        completed = subprocess.CompletedProcess(
            args,
            process.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
        if check and completed.returncode != 0:
            raise RuntimeError(f"{args[0]} failed: {completed.stderr}")
        return completed

    async def _spawn(self, *args: str) -> subprocess.Popen[str]:
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        return subprocess.Popen(  # noqa: S603 - daemon validates command shape before launch.
            args,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )

    async def launch(self, command: str, args: Sequence[str] = ()) -> ActionResult:
        return await self._apps.launch(command, args)

    async def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        self._browser.browser = self.browser
        return await self._browser.open_url(url, wait_for_window=wait_for_window)

    async def browser_render_metrics(
        self,
        url: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        return await self._browser.render_metrics(
            url,
            display=self.display,
            timeout_seconds=timeout_seconds,
        )

    async def prewarm_browser(self) -> ActionResult:
        return await self._browser.prewarm()

    async def windows(self) -> list[X11Window]:
        return await self._windows.list()

    async def active_window(self) -> X11Window | None:
        return await self._windows.active()

    async def activate_window(self, window_id: str) -> ActionResult:
        return await self._windows.activate(window_id)

    async def close_window(self, window_id: str) -> ActionResult:
        return await self._windows.close_window(window_id)

    async def display_info(self) -> DisplayInfo:
        return await self._display.info()

    async def mouse_move(self, x: int, y: int) -> Point:
        result = await self._mouse.move(x, y)
        self._last_input_backend = self._mouse.backend_name
        return result

    async def mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: Sequence[str] = (),
    ) -> Point:
        result = await self._mouse.click(
            x,
            y,
            button=button,
            count=count,
            modifiers=modifiers,
        )
        self._last_input_backend = self._mouse.backend_name
        return result

    async def mouse_drag(
        self,
        *,
        start: Point | None = None,
        end: Point | None = None,
        path: Sequence[Point] | None = None,
        button: str = "left",
        duration_ms: int = 500,
        modifiers: Sequence[str] = (),
    ) -> Point:
        result = await self._mouse.drag(
            start=start,
            end=end,
            path=path,
            button=button,
            duration_ms=duration_ms,
            modifiers=modifiers,
        )
        self._last_input_backend = self._mouse.backend_name
        return result

    async def mouse_scroll(
        self, direction: str, amount: int = 1, x: int | None = None, y: int | None = None
    ) -> ActionResult:
        result = await self._mouse.scroll(direction, amount=amount, x=x, y=y)
        self._last_input_backend = self._mouse.backend_name
        return result

    async def mouse_down(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        result = await self._mouse.down(button, x=x, y=y)
        self._last_input_backend = self._mouse.backend_name
        return result

    async def mouse_up(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        result = await self._mouse.up(button, x=x, y=y)
        self._last_input_backend = self._mouse.backend_name
        return result

    async def mouse_position(self) -> Point:
        result = await self._mouse.position()
        self._last_input_backend = self._mouse.backend_name
        return result

    async def keyboard_type(
        self, text: str, delay_ms: int = 10, method: str = "auto"
    ) -> ActionResult:
        result = await self._keyboard.type_text(text, delay_ms=delay_ms, method=method)
        self._last_input_backend = self._keyboard.backend_name
        return result

    async def keyboard_press(
        self, key: str, modifiers: Sequence[str] = (), duration_ms: int = 0
    ) -> ActionResult:
        result = await self._keyboard.press(
            key,
            modifiers=modifiers,
            duration_ms=duration_ms,
        )
        self._last_input_backend = self._keyboard.backend_name
        return result

    async def keyboard_hotkey(self, keys: Sequence[str], duration_ms: int = 0) -> ActionResult:
        result = await self._keyboard.hotkey(keys, duration_ms=duration_ms)
        self._last_input_backend = self._keyboard.backend_name
        return result

    async def key_down(self, key: str) -> None:
        await self._keyboard.down(key)
        self._last_input_backend = self._keyboard.backend_name

    async def key_up(self, key: str) -> None:
        await self._keyboard.up(key)
        self._last_input_backend = self._keyboard.backend_name

    async def clipboard_get(self) -> str:
        return await self._clipboard.get()

    async def clipboard_set(self, text: str) -> ActionResult:
        return await self._clipboard.set(text)

    async def clipboard_clear(self) -> ActionResult:
        return await self._clipboard.clear()

    async def release_all(self) -> ActionResult:
        released = {"keys": sorted(self.held_keys), "buttons": sorted(self.held_buttons)}
        for key in reversed(sorted(self.held_keys)):
            with contextlib.suppress(Exception):
                await self._keyboard.up(key)
        for button in reversed(sorted(self.held_buttons)):
            with contextlib.suppress(Exception):
                await self._mouse.up(button)
        self.held_keys.clear()
        self.held_buttons.clear()
        return ActionResult(ok=True, output=released)

    async def screenshot(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        artifact_store: ArtifactStore | None = None,
        call_id: str | None = None,
        retention_class: str = "ephemeral",
    ) -> Screenshot:
        return await self._screenshots.capture(
            options,
            region=region,
            artifact_store=artifact_store,
            call_id=call_id,
            retention_class=retention_class,
        )

    async def screenshot_bytes(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        include_cursor_position: bool = False,
        prefer_native_png: bool = False,
    ) -> CapturedScreenshot:
        return await self._screenshots.capture_bytes(
            options,
            region=region,
            include_cursor_position=include_cursor_position,
            prefer_native_png=prefer_native_png,
        )

    async def screenshot_raw_pixels(
        self,
        *,
        region: Region | None = None,
    ) -> CapturedRawScreenshot | None:
        return await self._screenshots.capture_raw_pixels(region=region)

    async def run_command(self, command: Sequence[str], timeout: float = 30.0) -> ActionResult:
        result = await self._run(*command, timeout=timeout, check=False)
        return ActionResult(
            ok=result.returncode == 0,
            message=None if result.returncode == 0 else result.stderr,
            output={
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )


def choose_backend(
    kind: str,
    *,
    width: int,
    height: int,
    display: str,
    browser: str | None = None,
    browser_profile_dir: str | None = None,
    browser_launch_args: Sequence[str] = (),
    browser_gpu_mode: str = "auto",
    input_backend: str = "auto",
) -> DesktopBackend:
    if kind == "mock":
        return MockDesktopBackend(width=width, height=height)
    if kind == "x11":
        return X11DesktopBackend(
            width=width,
            height=height,
            display=display,
            browser=browser,
            browser_profile_dir=browser_profile_dir,
            browser_launch_args=browser_launch_args,
            browser_gpu_mode=browser_gpu_mode,
            input_backend=input_backend,
        )
    if os.name != "posix":
        return MockDesktopBackend(width=width, height=height)
    return X11DesktopBackend(
        width=width,
        height=height,
        display=display,
        browser=browser,
        browser_profile_dir=browser_profile_dir,
        browser_launch_args=browser_launch_args,
        browser_gpu_mode=browser_gpu_mode,
        input_backend=input_backend,
    )


def _normalize_input_backend(value: str) -> Literal["auto", "xtest", "xdotool"]:
    if value in {"auto", "xtest", "xdotool"}:
        return cast("Literal['auto', 'xtest', 'xdotool']", value)
    return "auto"
