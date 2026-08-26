from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from PIL import Image

from modal_computer_use.artifacts import ArtifactStore
from modal_computer_use.daemon.process_environment import prepare_desktop_output_file
from modal_computer_use.models import (
    CoordinateSpace,
    Point,
    Region,
    Screenshot,
    ScreenshotOptions,
    sha256_bytes,
)

from .screenshot_capture import (
    ScreenshotCaptureError,
    ScreenshotCaptureFailed,
    ScreenshotCaptureResolution,
    ScreenshotCaptureSource,
    ScreenshotCaptureTimedOut,
    ScreenshotCaptureTimeoutOrigin,
    ScreenshotCaptureUnavailable,
    X11SharedMemoryScreenshotSession,
    resolve_capture_source,
    validate_png_dimensions,
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


@dataclass(frozen=True)
class CapturedRawScreenshot:
    width: int
    height: int
    rgb: bytes
    sha256: str
    captured_at: datetime
    coordinate_space: CoordinateSpace
    cursor_visible: bool
    capture_backend: str | None = None
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
        capture_source: str = "auto",
    ) -> None:
        self._run = run
        self.width = width
        self.height = height
        self.display = display
        self._cursor_position = cursor_position
        self._mss = _MSSCaptureSession(display=display)
        self._capture_resolution = resolve_capture_source(
            cast(ScreenshotCaptureSource, capture_source)
        )
        self._x11_shm: X11SharedMemoryScreenshotSession | None = None
        self._x11_shm_fallback = False
        self._x11_shm_failed = False
        self._x11_shm_timed_out = False
        self._x11_shm_timeout_origin: ScreenshotCaptureTimeoutOrigin | None = None
        self._readiness_generation = 0

    @property
    def readiness_generation(self) -> int:
        """Identify capture state whose readiness proof remains valid."""

        return self._readiness_generation

    def _invalidate_readiness(self) -> None:
        self._readiness_generation += 1

    def _raise_if_display_timed_out(self) -> None:
        if self._x11_shm_timed_out:
            assert self._x11_shm_timeout_origin is not None
            raise ScreenshotCaptureTimedOut(
                "X11 screenshot capture is quarantined until display restart",
                timeout_origin=self._x11_shm_timeout_origin,
            )

    async def _ensure_x11_shm(self) -> None:
        """Open the XCB/MIT-SHM session after the supervisor starts Xvfb.

        ``X11DesktopBackend`` is constructed while the ASGI app is imported,
        before the daemon lifespan starts the display supervisor.  Opening
        XCB in ``__init__`` therefore races a fresh Xvfb and makes an explicit
        X11 shared-memory source kill the daemon before ``/readyz`` can report the failure.
        Keep selection fail-closed, but defer the capability probe to the
        first readiness/screenshot call when the display is live.
        """
        if self._x11_shm is not None:
            return
        if self._x11_shm_failed:
            if self._x11_shm_timed_out:
                self._raise_if_display_timed_out()
            raise ScreenshotCaptureFailed(
                "X11 shared-memory screenshot source is quarantined until display restart"
            )
        selected = self._capture_resolution.selected
        if selected == "mss":
            return
        try:
            self._x11_shm = await asyncio.to_thread(
                X11SharedMemoryScreenshotSession,
                display=self.display,
                width=self.width,
                height=self.height,
            )
        except ScreenshotCaptureTimedOut as exc:
            self._x11_shm_failed = True
            self._x11_shm_timed_out = True
            self._x11_shm_timeout_origin = exc.timeout_origin
            self._invalidate_readiness()
            raise
        except ScreenshotCaptureUnavailable:
            if self._capture_resolution.requested != "auto":
                self._x11_shm_failed = True
                self._invalidate_readiness()
                raise
            self._select_mss_fallback(
                "X11 shared-memory screenshot session could not start"
            )

    def _select_mss_fallback(self, reason: str) -> None:
        """Quarantine X11 shared memory for this controller after an auto failure."""
        self._close_x11_shm(suppress=True)
        self._capture_resolution = ScreenshotCaptureResolution(
            requested=self._capture_resolution.requested,
            selected="mss",
            reason=reason,
        )
        self._x11_shm_fallback = True
        self._x11_shm_failed = False
        self._x11_shm_timed_out = False
        self._x11_shm_timeout_origin = None

    def _close_x11_shm(self, *, suppress: bool) -> None:
        session = self._x11_shm
        self._x11_shm = None
        if session is None:
            return
        try:
            session.close()
        except ScreenshotCaptureFailed:
            if not suppress:
                raise

    def close(self) -> None:
        """Release capture resources without changing source selection."""

        try:
            self._close_x11_shm(suppress=False)
        finally:
            self._mss.close()

    def reset_capture_session(self) -> None:
        """Release state and re-resolve the source for a new display generation."""

        requested = self._capture_resolution.requested
        try:
            self.close()
        finally:
            self._invalidate_readiness()
            self._capture_resolution = resolve_capture_source(requested)
            self._x11_shm_fallback = False
            self._x11_shm_failed = False
            self._x11_shm_timed_out = False
            self._x11_shm_timeout_origin = None

    async def probe(self) -> tuple[bool, str | None]:
        """Verify that the public cursor-visible screenshot path can capture."""
        if shutil.which("maim") is None:
            return False, "missing required tools: maim"
        if self._capture_resolution.selected != "mss":
            try:
                # This is a real hidden full-frame GetImage probe.  It runs
                # after Supervisor.start(), so the display is live even though
                # this controller was constructed during app creation.
                await self._ensure_x11_shm()
                captured = await self.capture_bytes(
                    ScreenshotOptions(format="png", show_cursor=False),
                    prefer_native_png=True,
                )
                if (
                    captured.capture_backend != "x11-shm"
                    or not validate_png_dimensions(
                        captured.data,
                        width=self.width,
                        height=self.height,
                    )
                ):
                    raise ScreenshotCaptureUnavailable(
                        "X11 shared-memory screenshot probe returned invalid dimensions"
                    )
            except ScreenshotCaptureTimedOut:
                return False, "X11 shared-memory screenshot probe exceeded its reply deadline"
            except ScreenshotCaptureError:
                if self._capture_resolution.requested != "auto":
                    return False, "X11 shared-memory screenshot probe failed"
                self._select_mss_fallback(
                    "X11 shared-memory screenshot probe failed"
                )
        if self._capture_resolution.selected == "mss":
            try:
                captured = await self.capture_bytes(
                    ScreenshotOptions(format="png", show_cursor=False),
                    prefer_native_png=True,
                )
            except Exception:
                return False, "hidden screenshot capture failed"
            if (
                captured.capture_backend not in {"mss", "mss-fallback"}
                or not validate_png_dimensions(
                    captured.data,
                    width=self.width,
                    height=self.height,
                )
            ):
                return False, "hidden screenshot capture failed"
        try:
            # Keep the existing cursor-visible path in readiness: maim must be
            # usable for screenshot options that X11 shared memory cannot
            # satisfy.
            captured = await self.capture_bytes(
                ScreenshotOptions(format="png", show_cursor=True),
                prefer_native_png=True,
            )
        except Exception:
            return False, "screenshot capture failed"
        if not captured.data:
            return False, "screenshot capture failed"
        return True, None

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
        self._raise_if_display_timed_out()
        started_total = perf_counter()
        timings_ms: dict[str, float] = {}
        source = region or Region(x=0, y=0, width=self.width, height=self.height)
        data = None
        capture_backend = None
        x11_shm_eligible = _can_use_mss_fast_path(options) and _can_preserve_native_png(
            options
        )
        if x11_shm_eligible:
            await self._ensure_x11_shm()
        x11_shm_requested = x11_shm_eligible and (
            self._x11_shm_fallback or self._capture_resolution.selected == "x11-shm"
        )
        session = self._x11_shm
        if session is not None and x11_shm_eligible:
            started = perf_counter()
            try:
                data = await asyncio.to_thread(
                    session.capture_png,
                    x=source.x,
                    y=source.y,
                    width=source.width,
                    height=source.height,
                )
            except ScreenshotCaptureTimedOut as exc:
                self._close_x11_shm(suppress=True)
                self._x11_shm_failed = True
                self._x11_shm_timed_out = True
                self._x11_shm_timeout_origin = exc.timeout_origin
                self._invalidate_readiness()
                raise
            except ScreenshotCaptureFailed:
                if self._capture_resolution.requested != "auto":
                    self._close_x11_shm(suppress=True)
                    self._x11_shm_failed = True
                    self._invalidate_readiness()
                    raise
                self._select_mss_fallback(
                    "X11 shared-memory screenshot capture failed"
                )
            else:
                timings_ms["x11_shm_capture_encode_ms"] = _elapsed_ms(started)
                capture_backend = "x11-shm"
                image_width, image_height = source.width, source.height

        if data is None and _can_use_mss_fast_path(options):
            started = perf_counter()
            mss_capture = self._mss.grab(source)
            if mss_capture is not None:
                timings_ms["capture_ms"] = _elapsed_ms(started)
                capture_backend = "mss-fallback" if x11_shm_requested else "mss"
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
            if x11_shm_requested:
                capture_backend = f"{capture_backend}-fallback"

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
        started = perf_counter()
        digest = sha256_bytes(data)
        timings_ms["hash_ms"] = _elapsed_ms(started)
        timings_ms["total_ms"] = _elapsed_ms(started_total)
        return CapturedScreenshot(
            format=options.format,
            width=coordinate_space.image_width,
            height=coordinate_space.image_height,
            data=data,
            sha256=digest,
            captured_at=datetime.now(UTC),
            coordinate_space=coordinate_space,
            cursor_visible=options.show_cursor,
            capture_backend=capture_backend,
            cursor_position=cursor_position,
            timings_ms=timings_ms,
        )

    async def capture_raw_pixels(
        self,
        *,
        region: Region | None = None,
    ) -> CapturedRawScreenshot | None:
        self._raise_if_display_timed_out()
        started_total = perf_counter()
        timings_ms: dict[str, float] = {}
        source = region or Region(x=0, y=0, width=self.width, height=self.height)
        started = perf_counter()
        mss_capture = self._mss.grab(source)
        if mss_capture is None:
            return None
        timings_ms["capture_ms"] = _elapsed_ms(started)
        started = perf_counter()
        rgb = bytes(mss_capture.shot.rgb)
        timings_ms["pixel_copy_ms"] = _elapsed_ms(started)
        coordinate_space = CoordinateSpace.from_dimensions(
            desktop_width=self.width,
            desktop_height=self.height,
            image_width=mss_capture.width,
            image_height=mss_capture.height,
            source_region=region,
        )
        started = perf_counter()
        source_sha256 = sha256_bytes(rgb)
        timings_ms["source_hash_ms"] = _elapsed_ms(started)
        timings_ms["total_ms"] = _elapsed_ms(started_total)
        return CapturedRawScreenshot(
            width=coordinate_space.image_width,
            height=coordinate_space.image_height,
            rgb=rgb,
            sha256=source_sha256,
            captured_at=datetime.now(UTC),
            coordinate_space=coordinate_space,
            cursor_visible=False,
            capture_backend="mss-raw",
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
            prepare_desktop_output_file(handle.fileno())
            temp_path = Path(handle.name)
        try:
            started = perf_counter()
            capture_backend = await self._capture_native_png(
                temp_path,
                options=options,
                region=region,
                prefer_scrot=prefer_native_png,
            )
            timings_ms["capture_ms"] = _elapsed_ms(started)
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
    return encode_rgb_png(capture.shot.rgb, capture.size)


def encode_rgb_png(rgb: bytes, size: tuple[int, int]) -> bytes:
    from mss import tools

    data = tools.to_png(rgb, size, level=1)
    if not isinstance(data, bytes):
        raise RuntimeError("mss png encoder returned non-bytes")
    return data


def _mss_capture_to_image(capture: _MSSCapture) -> Image.Image:
    return Image.frombytes("RGB", capture.size, capture.shot.bgra, "raw", "BGRX")


class _MSSCaptureSession:
    def __init__(self, *, display: str) -> None:
        self._display = display
        self._screenshotter: Any | None = None

    def grab(self, source: Region) -> _MSSCapture | None:
        monitor = {
            "left": source.x,
            "top": source.y,
            "width": source.width,
            "height": source.height,
        }
        for _ in range(2):
            try:
                screenshotter = self._screenshotter or self._open()
                self._screenshotter = screenshotter
                shot = screenshotter.grab(monitor)
                return _MSSCapture(shot=shot, width=source.width, height=source.height)
            except Exception:
                self._reset()
        return None

    def _open(self) -> Any:
        import mss

        return mss.MSS(display=self._display, backend="xshmgetimage")

    def close(self) -> None:
        self._reset()

    def _reset(self) -> None:
        if self._screenshotter is not None:
            with suppress(Exception):
                self._screenshotter.close()
        self._screenshotter = None


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
