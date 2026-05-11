from __future__ import annotations

import asyncio
import base64
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
    async def release_all(self) -> ActionResult:
        raise NotImplementedError

    async def launch(self, command: str, args: Sequence[str] = ()) -> ActionResult:
        return ActionResult(
            ok=True, message=f"launch requested: {command}", output={"args": list(args)}
        )

    async def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        return ActionResult(
            ok=True, message="url open requested", output={"url": url, "wait": wait_for_window}
        )

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
            rel = CoordinateSpace.from_dimensions(
                desktop_width=self.width,
                desktop_height=self.height,
                image_width=image_width,
                image_height=image_height,
                source_region=source,
            ).to_image(self.cursor)
            draw.line([(rel.x - 6, rel.y), (rel.x + 6, rel.y)], fill=(220, 38, 38), width=2)
            draw.line([(rel.x, rel.y - 6), (rel.x, rel.y + 6)], fill=(220, 38, 38), width=2)
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

    async def release_all(self) -> ActionResult:
        released = {"keys": sorted(self.held_keys), "buttons": sorted(self.held_buttons)}
        self.held_keys.clear()
        self.held_buttons.clear()
        return ActionResult(ok=True, output=released)


class X11DesktopBackend(MockDesktopBackend):
    def __init__(self, width: int = 1440, height: int = 900, display: str = ":99") -> None:
        super().__init__(width=width, height=height)
        self.display = display

    async def ready(self) -> tuple[bool, list[str]]:
        missing = [
            tool
            for tool in ("xdotool", "wmctrl", "maim", "xclip", "xsel", "xdpyinfo")
            if shutil.which(tool) is None
        ]
        if missing:
            return False, [f"missing required tools: {', '.join(missing)}"]
        result = await self._run("xdpyinfo", timeout=2, check=False)
        if result.returncode != 0:
            return False, ["xdpyinfo could not reach display"]
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
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input_text.encode() if input_text is not None else None),
            timeout=timeout,
        )
        completed = subprocess.CompletedProcess(
            args,
            process.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
        if check and completed.returncode != 0:
            raise RuntimeError(f"{args[0]} failed: {completed.stderr}")
        return completed

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
        await self._run("xdotool", "click", "--repeat", str(count), button_number)
        return await super().mouse_click(x, y, button=button, count=count, modifiers=modifiers)

    async def keyboard_type(
        self, text: str, delay_ms: int = 10, method: str = "auto"
    ) -> ActionResult:
        if method in ("auto", "clipboard") and (len(text) > 80 or not text.isascii()):
            await self.clipboard_set(text)
            await self.keyboard_hotkey(["ctrl", "v"])
        else:
            await self._run("xdotool", "type", "--delay", str(delay_ms), text)
        return await super().keyboard_type(text, delay_ms=delay_ms, method=method)

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
        if options.storage == "artifact":
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


def choose_backend(kind: str, *, width: int, height: int, display: str) -> DesktopBackend:
    if kind == "mock":
        return MockDesktopBackend(width=width, height=height)
    if kind == "x11":
        return X11DesktopBackend(width=width, height=height, display=display)
    if os.name != "posix" or shutil.which("xdotool") is None:
        return MockDesktopBackend(width=width, height=height)
    return X11DesktopBackend(width=width, height=height, display=display)
