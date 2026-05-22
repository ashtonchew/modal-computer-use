from __future__ import annotations

import base64
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any

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
class _MSSCapture:
    shot: Any
    width: int
    height: int

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


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
    capture_backend: str | None = None
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
        self._mss = _MSSCaptureSession(display=display)

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
        data = None
        capture_backend = None
        if _can_use_mss_fast_path(options):
            started = perf_counter()
            mss_capture = self._mss.grab(source)
            if mss_capture is not None:
                timings_ms["capture_ms"] = _elapsed_ms(started)
                capture_backend = "mss"
                data, image_width, image_height = _encode_mss_capture(
                    mss_capture,
                    options,
                    timings_ms=timings_ms,
                    prefer_native_png=prefer_native_png,
                )

        if data is None:
            data, image_width, image_height, capture_backend = await self._capture_via_file(
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
            capture_backend=capture_backend,
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
    ) -> tuple[bytes, int, int, str]:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        started = perf_counter()
        capture_backend = await self._capture_native_png(
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
        return data, image_width, image_height, capture_backend

    async def _capture_native_png(
        self,
        path: Path,
        *,
        options: ScreenshotOptions,
        region: Region | None,
        prefer_scrot: bool,
    ) -> str:
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
                return primary[0]
        except Exception:
            if primary == fallback:
                raise
        await self._run(*fallback)
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError("screenshot capture produced an empty file")
        return fallback[0]


def encode_image(image: Image.Image, image_format: str, quality: int) -> bytes:
    output = BytesIO()
    fmt = "JPEG" if image_format == "jpeg" else image_format.upper()
    save_kwargs: dict[str, Any] = {"quality": quality}
    if fmt == "WEBP":
        save_kwargs["method"] = 0
    image.save(output, format=fmt, **save_kwargs)
    return output.getvalue()


def _smallest_png(native_png: bytes, encoded_png: bytes) -> bytes:
    return native_png if len(native_png) < len(encoded_png) else encoded_png


def _can_preserve_native_png(options: ScreenshotOptions) -> bool:
    return options.format == "png" and options.scale == 1.0


def _encode_mss_capture(
    capture: _MSSCapture,
    options: ScreenshotOptions,
    *,
    timings_ms: dict[str, float],
    prefer_native_png: bool,
) -> tuple[bytes, int, int]:
    if prefer_native_png and _can_preserve_native_png(options):
        started = perf_counter()
        data = _encode_mss_png(capture)
        timings_ms["encode_ms"] = _elapsed_ms(started)
        return data, capture.width, capture.height

    started = perf_counter()
    image = _mss_capture_to_image(capture)
    timings_ms["pixel_convert_ms"] = _elapsed_ms(started)

    image_width = scaled_dimension(capture.width, options.scale)
    image_height = scaled_dimension(capture.height, options.scale)
    if options.scale != 1.0:
        started = perf_counter()
        image = image.resize((image_width, image_height))
        timings_ms["resize_ms"] = _elapsed_ms(started)

    started = perf_counter()
    data = encode_image(image, options.format, options.quality)
    timings_ms["encode_ms"] = _elapsed_ms(started)
    return data, image_width, image_height


def _encode_mss_png(capture: _MSSCapture) -> bytes:
    from mss import tools

    data = tools.to_png(capture.shot.rgb, capture.size, level=1)
    if not isinstance(data, bytes):
        raise RuntimeError("mss png encoder returned non-bytes")
    return data


def _mss_capture_to_image(capture: _MSSCapture) -> Image.Image:
    return Image.frombytes("RGB", capture.size, capture.shot.bgra, "raw", "BGRX")


class _MSSCaptureSession:
    def __init__(self, *, display: str) -> None:
        self._display = display
        self._screenshotter: Any | None = None
        self._prefer_xshm = True

    def grab(self, source: Region) -> _MSSCapture | None:
        monitor = {
            "left": source.x,
            "top": source.y,
            "width": source.width,
            "height": source.height,
        }
        for attempt in range(2):
            try:
                screenshotter = self._screenshotter or self._open(prefer_xshm=self._prefer_xshm)
                self._screenshotter = screenshotter
                shot = screenshotter.grab(monitor)
                return _MSSCapture(shot=shot, width=source.width, height=source.height)
            except Exception:
                self._reset()
                if attempt == 0 and self._prefer_xshm:
                    self._prefer_xshm = False
                    continue
                return None
        return None

    def _open(self, *, prefer_xshm: bool) -> Any:
        import mss

        if prefer_xshm:
            try:
                return mss.MSS(display=self._display, backend="xshmgetimage")
            except TypeError:
                return mss.MSS(display=self._display)
            except Exception:
                return mss.MSS(display=self._display)
        return mss.MSS(display=self._display)

    def _reset(self) -> None:
        if self._screenshotter is not None:
            with suppress(Exception):
                self._screenshotter.close()
        self._screenshotter = None


def _capture_mss_png(source: Region, *, display: str) -> bytes | None:
    try:
        capture = _MSSCaptureSession(display=display).grab(source)
    except ImportError:
        return None
    if capture is None:
        return None
    try:
        return _encode_mss_png(capture)
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
    return not options.show_cursor


def scaled_dimension(value: int, scale: float) -> int:
    return max(1, round(value * scale))


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000
