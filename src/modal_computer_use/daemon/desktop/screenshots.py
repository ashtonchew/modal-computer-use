from __future__ import annotations

import base64
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

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


class X11ScreenshotController:
    def __init__(
        self,
        *,
        run: RunCommand,
        width: int,
        height: int,
        cursor_position: Callable[[], Awaitable[Point]],
    ) -> None:
        self._run = run
        self.width = width
        self.height = height
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
    ) -> CapturedScreenshot:
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
            image.load()
            image_width = scaled_dimension(image.width, options.scale)
            image_height = scaled_dimension(image.height, options.scale)
            native_png = temp_path.read_bytes() if _can_preserve_native_png(options) else None
            if options.scale != 1.0:
                image = image.resize((image_width, image_height))
            encoded = encode_image(image.convert("RGB"), options.format, options.quality)
            data = _smallest_png(native_png, encoded) if native_png is not None else encoded
        finally:
            temp_path.unlink(missing_ok=True)

        coordinate_space = CoordinateSpace.from_dimensions(
            desktop_width=self.width,
            desktop_height=self.height,
            image_width=image_width,
            image_height=image_height,
            source_region=region,
        )
        cursor_position = await self._cursor_position() if include_cursor_position else None
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
        )


def encode_image(image: Image.Image, image_format: str, quality: int) -> bytes:
    output = BytesIO()
    fmt = "JPEG" if image_format == "jpeg" else image_format.upper()
    image.save(output, format=fmt, quality=quality)
    return output.getvalue()


def _smallest_png(native_png: bytes, encoded_png: bytes) -> bytes:
    return native_png if len(native_png) < len(encoded_png) else encoded_png


def _can_preserve_native_png(options: ScreenshotOptions) -> bool:
    return options.format == "png" and options.scale == 1.0


def scaled_dimension(value: int, scale: float) -> int:
    return max(1, round(value * scale))
