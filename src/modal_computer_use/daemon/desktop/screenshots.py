from __future__ import annotations

import base64
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from time import perf_counter

from PIL import Image

from modal_computer_use.artifacts import ArtifactStore
from modal_computer_use.models import (
    CoordinateSpace,
    Point,
    Region,
    Screenshot,
    ScreenshotOptions,
    sha256_bytes,
)

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]


@dataclass(frozen=True)
class CapturedScreenshot:
    format: str
    width: int
    height: int
    data: bytes
    sha256: str
    captured_at: datetime
    coordinate_space: CoordinateSpace
    cursor_visible: bool
    cursor_position: Point | None = None
    timings_ms: Mapping[str, float] = field(default_factory=dict)


class X11ScreenshotController:
    def __init__(
        self,
        *,
        run: RunCommand,
        width: int,
        height: int,
        display: str,
        cursor_position: Callable[[], Awaitable[Point]],
    ) -> None:
        self._run = run
        self.width = width
        self.height = height
        self.display = display
        self._cursor_position = cursor_position

    async def capture(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        artifact_store: ArtifactStore | None = None,
        call_id: str | None = None,
        retention_class: str = "ephemeral",
    ) -> Screenshot:
        captured = await self.capture_bytes(
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

    async def capture_bytes(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        include_cursor_position: bool = False,
        prefer_native_png: bool = False,
    ) -> CapturedScreenshot:
        started_total = perf_counter()
        timings_ms: dict[str, float] = {}
        source = region or Region(x=0, y=0, width=self.width, height=self.height)
        mss_capture = None
        if prefer_native_png and _can_use_mss_fast_path(options):
            started = perf_counter()
            mss_capture = _capture_mss_png(source, display=self.display)
            if mss_capture is not None:
                timings_ms["capture_ms"] = _elapsed_ms(started)
                data = mss_capture
                image_width = source.width
                image_height = source.height

        if mss_capture is None:
            data, image_width, image_height = await self._capture_via_file(
                options,
                region=region,
                prefer_native_png=prefer_native_png,
                timings_ms=timings_ms,
            )

        coordinate_space = CoordinateSpace.from_dimensions(
            desktop_width=self.width,
            desktop_height=self.height,
            image_width=image_width,
            image_height=image_height,
            source_region=region,
        )
        if include_cursor_position:
            started = perf_counter()
            cursor_position = await self._cursor_position()
            timings_ms["cursor_position_ms"] = _elapsed_ms(started)
        else:
            cursor_position = None
        timings_ms["total_ms"] = _elapsed_ms(started_total)
        return CapturedScreenshot(
            format=options.format,
            width=coordinate_space.image_width,
            height=coordinate_space.image_height,
            data=data,
            sha256=sha256_bytes(data),
            captured_at=datetime.now(UTC),
            coordinate_space=coordinate_space,
            cursor_visible=options.show_cursor,
            cursor_position=cursor_position,
            timings_ms=timings_ms,
        )

    async def _capture_via_file(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None,
        prefer_native_png: bool,
        timings_ms: dict[str, float],
    ) -> tuple[bytes, int, int]:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        started = perf_counter()
        await self._capture_native_png(
            temp_path,
            options=options,
            region=region,
            prefer_scrot=prefer_native_png,
        )
        timings_ms["capture_ms"] = _elapsed_ms(started)
        try:
            if prefer_native_png and _can_preserve_native_png(options):
                started = perf_counter()
                data = temp_path.read_bytes()
                timings_ms["read_ms"] = _elapsed_ms(started)
                source = region or Region(x=0, y=0, width=self.width, height=self.height)
                image_width = source.width
                image_height = source.height
            else:
                started = perf_counter()
                image = Image.open(temp_path)
                image.load()
                timings_ms["decode_ms"] = _elapsed_ms(started)
                image_width = scaled_dimension(image.width, options.scale)
                image_height = scaled_dimension(image.height, options.scale)
                started = perf_counter()
                native_png = temp_path.read_bytes() if _can_preserve_native_png(options) else None
                if native_png is not None:
                    timings_ms["read_ms"] = _elapsed_ms(started)
                if options.scale != 1.0:
                    started = perf_counter()
                    image = image.resize((image_width, image_height))
                    timings_ms["resize_ms"] = _elapsed_ms(started)
                started = perf_counter()
                encoded = encode_image(image.convert("RGB"), options.format, options.quality)
                timings_ms["encode_ms"] = _elapsed_ms(started)
                data = _smallest_png(native_png, encoded) if native_png is not None else encoded
        finally:
            temp_path.unlink(missing_ok=True)
        return data, image_width, image_height

    async def _capture_native_png(
        self,
        path: Path,
        *,
        options: ScreenshotOptions,
        region: Region | None,
        prefer_scrot: bool,
    ) -> None:
        primary = _capture_command(
            path,
            options=options,
            region=region,
            prefer_scrot=prefer_scrot,
        )
        fallback = _capture_command(path, options=options, region=region, prefer_scrot=False)
        try:
            await self._run(*primary)
            if path.exists() and path.stat().st_size > 0:
                return
        except Exception:
            if primary == fallback:
                raise
        await self._run(*fallback)
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError("screenshot capture produced an empty file")


def encode_image(image: Image.Image, image_format: str, quality: int) -> bytes:
    output = BytesIO()
    fmt = "JPEG" if image_format == "jpeg" else image_format.upper()
    image.save(output, format=fmt, quality=quality)
    return output.getvalue()


def _smallest_png(native_png: bytes, encoded_png: bytes) -> bytes:
    return native_png if len(native_png) < len(encoded_png) else encoded_png


def _can_preserve_native_png(options: ScreenshotOptions) -> bool:
    return options.format == "png" and options.scale == 1.0


def _capture_mss_png(source: Region, *, display: str) -> bytes | None:
    try:
        import mss
        from mss import tools
    except ImportError:
        return None
    monitor = {
        "left": source.x,
        "top": source.y,
        "width": source.width,
        "height": source.height,
    }
    try:
        with mss.MSS(display=display) as screenshotter:
            shot = screenshotter.grab(monitor)
            data = tools.to_png(shot.rgb, shot.size, level=1)
            return data if isinstance(data, bytes) else None
    except Exception:
        return None


def _capture_command(
    path: Path,
    *,
    options: ScreenshotOptions,
    region: Region | None,
    prefer_scrot: bool,
) -> list[str]:
    if prefer_scrot and _can_use_scrot_fast_path(options):
        command = ["scrot", "-z", "-o"]
        if region:
            command.extend(["-a", f"{region.x},{region.y},{region.width},{region.height}"])
        command.append(str(path))
        return command
    command = ["maim"]
    if not options.show_cursor:
        command.append("-u")
    if region:
        command.extend(["-g", f"{region.width}x{region.height}+{region.x}+{region.y}"])
    command.append(str(path))
    return command


def _can_use_scrot_fast_path(options: ScreenshotOptions) -> bool:
    return not options.show_cursor and _can_preserve_native_png(options)


def _can_use_mss_fast_path(options: ScreenshotOptions) -> bool:
    return not options.show_cursor and _can_preserve_native_png(options)


def scaled_dimension(value: int, scale: float) -> int:
    return max(1, round(value * scale))


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000
