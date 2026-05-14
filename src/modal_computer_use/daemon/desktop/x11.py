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
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from modal_computer_use.actions import normalize_key, normalize_key_combo
from modal_computer_use.artifacts import ArtifactStore
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

    async def run_command(self, command: Sequence[str], timeout: float = 30.0) -> ActionResult:
        return ActionResult(
            ok=True,
            message="command not executed by mock backend",
            output={"command": list(command)},
        )


class MockDesktopBackend(DesktopBackend):
    def __init__(self, width: int = 1440, height: int = 900) -> None:
        self.width = width
        self.height = height
        self.cursor = Point(x=0, y=0)
        self.clipboard = ""
        self.held_keys: set[str] = set()
        self.held_buttons: set[str] = set()
        self.recordings: dict[str, Recording] = {}

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
        source = region or Region(x=0, y=0, width=self.width, height=self.height)
        image_width = max(1, round(source.width * options.scale))
        image_height = max(1, round(source.height * options.scale))
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
        data = _encode_image(img, options.format, options.quality)
        coordinate_space = CoordinateSpace.from_dimensions(
            desktop_width=self.width,
            desktop_height=self.height,
            image_width=image_width,
            image_height=image_height,
            source_region=region,
        )
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
            width=image_width,
            height=image_height,
            size_bytes=len(data),
            data_base64=data_base64,
            artifact_uri=artifact_uri,
            sha256=sha256_bytes(data),
            captured_at=datetime.now(UTC),
            coordinate_space=coordinate_space,
            cursor_visible=options.show_cursor,
            cursor_position=self.cursor,
        )

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
        width: int = 1440,
        height: int = 900,
        display: str = ":99",
        browser: str | None = None,
    ) -> None:
        super().__init__(width=width, height=height)
        self.display = display
        self.browser = browser

    async def ready(self) -> tuple[bool, list[str]]:
        missing = [
            tool
            for tool in ("xdotool", "wmctrl", "maim", "xclip", "xsel", "xdpyinfo", "ffmpeg")
            if shutil.which(tool) is None
        ]
        if missing:
            return False, [f"missing required tools: {', '.join(missing)}"]
        result = await self._run("xdpyinfo", timeout=2, check=False)
        if result.returncode != 0:
            return False, ["xdpyinfo could not reach display"]
        wm = await self._run("wmctrl", "-m", timeout=2, check=False)
        if wm.returncode != 0:
            return False, ["window manager is not responding"]
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
        argv = (command, *args)
        try:
            process = await self._spawn(*argv)
        except OSError as exc:
            return ActionResult(
                ok=False,
                message="failed to launch application",
                output={"command": command, "returncode": None, "error": str(exc)},
            )
        await asyncio.sleep(0.2)
        returncode = process.poll()
        return ActionResult(
            ok=returncode in (None, 0),
            message=None if returncode in (None, 0) else "application exited immediately",
            output={
                "command": command,
                "args": list(args),
                "pid": process.pid,
                "returncode": returncode,
            },
        )

    async def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        before = len(await self.windows()) if wait_for_window else None
        command = self.browser or "xdg-open"
        result = await self.launch(command, [url])
        if not result.ok or not wait_for_window:
            return result
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            windows = await self.windows()
            if before is None or len(windows) > before:
                result.output["windows"] = len(windows)
                return result
            await asyncio.sleep(0.2)
        result.output["windows"] = before
        result.output["wait_for_window_timed_out"] = True
        return result

    async def windows(self) -> list[X11Window]:
        result = await self._run("wmctrl", "-lpGx", timeout=2, check=False)
        if result.returncode != 0:
            return await super().windows()
        active = await self._run("xdotool", "getactivewindow", timeout=2, check=False)
        active_id = _normalize_window_id(active.stdout)
        windows: list[X11Window] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            window_id, _desktop, pid, x, y, width, height, class_name, title = parts
            try:
                windows.append(
                    X11Window(
                        id=window_id,
                        title=title,
                        class_name=class_name,
                        pid=int(pid) if pid != "0" else None,
                        x=int(x),
                        y=int(y),
                        width=int(width),
                        height=int(height),
                        is_active=_normalize_window_id(window_id) == active_id,
                    )
                )
            except ValueError:
                continue
        return windows

    async def active_window(self) -> X11Window | None:
        windows = await self.windows()
        for window in windows:
            if window.is_active:
                return window
        return windows[0] if windows else None

    async def activate_window(self, window_id: str) -> ActionResult:
        result = await self._run("wmctrl", "-ia", window_id, timeout=5, check=False)
        return ActionResult(
            ok=result.returncode == 0,
            message=None if result.returncode == 0 else "failed to activate window",
            output={"window_id": window_id},
        )

    async def close_window(self, window_id: str) -> ActionResult:
        result = await self._run("wmctrl", "-ic", window_id, timeout=5, check=False)
        return ActionResult(
            ok=result.returncode == 0,
            message=None if result.returncode == 0 else "failed to close window",
            output={"window_id": window_id},
        )

    async def mouse_move(self, x: int, y: int) -> Point:
        await self._run("xdotool", "mousemove", str(x), str(y))
        return await super().mouse_move(x, y)

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
            await self.mouse_move(x, y)
        button_number = {"left": "1", "middle": "2", "right": "3"}[button]
        modifier_keys = [normalize_key(modifier) for modifier in modifiers]
        for modifier in modifier_keys:
            await self.key_down(modifier)
        try:
            await self._run("xdotool", "click", "--repeat", str(count), button_number)
        finally:
            for modifier in reversed(modifier_keys):
                with contextlib.suppress(Exception):
                    await self.key_up(modifier)
        return await super().mouse_click(x, y, button=button, count=count, modifiers=modifiers)

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
        points = list(path or [])
        moved_to_path_start = False
        if points and start is None:
            start = points[0]
            await self.mouse_move(start.x, start.y)
            points = points[1:]
            moved_to_path_start = True
        if not points:
            if start is not None:
                if not moved_to_path_start:
                    await self.mouse_move(start.x, start.y)
            elif end is None:
                start = await self.mouse_position()
            if end is not None:
                points = [end]

        interval_ms = 0
        if points:
            interval_ms = duration_ms // max(len(points), 1)
        modifier_keys = [normalize_key(modifier) for modifier in modifiers]
        for modifier in modifier_keys:
            await self.key_down(modifier)
        try:
            await self.mouse_down(button)
            for point in points:
                await self._run(
                    "xdotool",
                    "mousemove",
                    str(point.x),
                    str(point.y),
                )
                if interval_ms > 0:
                    await asyncio.sleep(interval_ms / 1000)
        finally:
            with contextlib.suppress(Exception):
                await self.mouse_up(button)
            for modifier in reversed(modifier_keys):
                with contextlib.suppress(Exception):
                    await self.key_up(modifier)
        return await super().mouse_drag(
            start=start,
            end=end,
            path=path,
            button=button,
            duration_ms=duration_ms,
            modifiers=modifiers,
        )

    async def mouse_scroll(
        self, direction: str, amount: int = 1, x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if x is not None and y is not None:
            await self.mouse_move(x, y)
        button_number = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
        await self._run("xdotool", "click", "--repeat", str(amount), button_number)
        return await super().mouse_scroll(direction, amount=amount, x=x, y=y)

    async def mouse_down(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if x is not None and y is not None:
            await self.mouse_move(x, y)
        button_number = {"left": "1", "middle": "2", "right": "3"}[button]
        await self._run("xdotool", "mousedown", button_number)
        return await super().mouse_down(button, x=x, y=y)

    async def mouse_up(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if x is not None and y is not None:
            await self.mouse_move(x, y)
        button_number = {"left": "1", "middle": "2", "right": "3"}[button]
        await self._run("xdotool", "mouseup", button_number)
        return await super().mouse_up(button, x=x, y=y)

    async def mouse_position(self) -> Point:
        result = await self._run("xdotool", "getmouselocation", "--shell")
        values: dict[str, int] = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key in {"X", "Y"} and value.isdigit():
                values[key] = int(value)
        if "X" not in values or "Y" not in values:
            return await super().mouse_position()
        return await super().mouse_move(values["X"], values["Y"])

    async def keyboard_type(
        self, text: str, delay_ms: int = 10, method: str = "auto"
    ) -> ActionResult:
        if method in ("auto", "clipboard") and (len(text) > 80 or not text.isascii()):
            await self.clipboard_set(text)
            await self.keyboard_hotkey(["ctrl", "v"])
        else:
            await self._run("xdotool", "type", "--delay", str(delay_ms), text)
        return await super().keyboard_type(text, delay_ms=delay_ms, method=method)

    async def keyboard_press(
        self, key: str, modifiers: Sequence[str] = (), duration_ms: int = 0
    ) -> ActionResult:
        normalized_key = normalize_key(key)
        modifier_keys = [normalize_key(modifier) for modifier in modifiers]
        for modifier in modifier_keys:
            await self.key_down(modifier)
        try:
            if duration_ms > 0:
                await self.key_down(normalized_key)
                await asyncio.sleep(duration_ms / 1000)
                await self.key_up(normalized_key)
            else:
                await self._run("xdotool", "key", normalized_key)
        finally:
            for modifier in reversed(modifier_keys):
                with contextlib.suppress(Exception):
                    await self.key_up(modifier)
        return await super().keyboard_press(
            key,
            modifiers=modifiers,
            duration_ms=duration_ms,
        )

    async def keyboard_hotkey(self, keys: Sequence[str], duration_ms: int = 0) -> ActionResult:
        combo = "+".join(normalize_key_combo(keys))
        await self._run("xdotool", "key", combo)
        return await super().keyboard_hotkey(keys, duration_ms=duration_ms)

    async def key_down(self, key: str) -> None:
        await self._run("xdotool", "keydown", normalize_key(key))
        await super().key_down(key)

    async def key_up(self, key: str) -> None:
        await self._run("xdotool", "keyup", normalize_key(key))
        await super().key_up(key)

    async def clipboard_get(self) -> str:
        result = await self._run("xclip", "-selection", "clipboard", "-o", check=False)
        self.clipboard = result.stdout if result.returncode == 0 else ""
        return self.clipboard

    async def clipboard_set(self, text: str) -> ActionResult:
        await self._run("xclip", "-selection", "clipboard", input_text=text)
        return await super().clipboard_set(text)

    async def clipboard_clear(self) -> ActionResult:
        await self._run("xclip", "-selection", "clipboard", input_text="")
        return await super().clipboard_clear()

    async def release_all(self) -> ActionResult:
        released = {"keys": sorted(self.held_keys), "buttons": sorted(self.held_buttons)}
        for key in reversed(sorted(self.held_keys)):
            with contextlib.suppress(Exception):
                await self._run("xdotool", "keyup", key)
        for button in reversed(sorted(self.held_buttons)):
            button_number = {"left": "1", "middle": "2", "right": "3"}[button]
            with contextlib.suppress(Exception):
                await self._run("xdotool", "mouseup", button_number)
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
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        command = ["maim"]
        if not options.show_cursor:
            command.append("-u")
        if region:
            command.extend(["-g", f"{region.width}x{region.height}+{region.x}+{region.y}"])
        command.append(str(temp_path))
        await self._run(*command)
        try:
            image = Image.open(temp_path)
            if options.scale != 1.0:
                image = image.resize(
                    (round(image.width * options.scale), round(image.height * options.scale))
                )
            data = _encode_image(image.convert("RGB"), options.format, options.quality)
        finally:
            temp_path.unlink(missing_ok=True)
        coordinate_space = CoordinateSpace.from_dimensions(
            desktop_width=self.width,
            desktop_height=self.height,
            image_width=round((region.width if region else self.width) * options.scale),
            image_height=round((region.height if region else self.height) * options.scale),
            source_region=region,
        )
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
            width=coordinate_space.image_width,
            height=coordinate_space.image_height,
            size_bytes=len(data),
            data_base64=data_base64,
            artifact_uri=artifact_uri,
            sha256=sha256_bytes(data),
            captured_at=datetime.now(UTC),
            coordinate_space=coordinate_space,
            cursor_visible=options.show_cursor,
            cursor_position=await self.mouse_position(),
        )

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


def _encode_image(image: Image.Image, image_format: str, quality: int) -> bytes:
    output = BytesIO()
    fmt = "JPEG" if image_format == "jpeg" else image_format.upper()
    image.save(output, format=fmt, quality=quality)
    return output.getvalue()


def choose_backend(
    kind: str, *, width: int, height: int, display: str, browser: str | None = None
) -> DesktopBackend:
    if kind == "mock":
        return MockDesktopBackend(width=width, height=height)
    if kind == "x11":
        return X11DesktopBackend(width=width, height=height, display=display, browser=browser)
    if os.name != "posix":
        return MockDesktopBackend(width=width, height=height)
    return X11DesktopBackend(width=width, height=height, display=display, browser=browser)


def _normalize_window_id(value: str) -> str:
    raw = value.strip().lower()
    if not raw:
        return ""
    try:
        return f"0x{int(raw, 0):08x}"
    except ValueError:
        return raw
