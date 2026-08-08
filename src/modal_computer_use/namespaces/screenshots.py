from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from modal_computer_use.errors import FrameValidationError
from modal_computer_use.models import CoordinateSpace, ImageFormat, Point, Region, Screenshot

from .base import AsyncNamespace, Namespace


def _required_screenshot_header(headers: Mapping[str, str], name: str) -> str:
    try:
        return headers[name]
    except KeyError:
        normalized_name = name.lower()
        for key, value in headers.items():
            if key.lower() == normalized_name:
                return value
    raise ValueError("binary screenshot response metadata is incomplete") from None


def _screenshot_from_binary_response(
    data: bytes,
    headers: Mapping[str, str],
    *,
    requested_format: ImageFormat,
    requested_cursor_visibility: bool,
) -> Screenshot:
    if not data:
        raise FrameValidationError("binary screenshot response is empty")
    try:
        content_type = _required_screenshot_header(headers, "content-type")
        if not isinstance(content_type, str):
            raise ValueError
        media_type = content_type.partition(";")[0].strip().lower()
    except (AttributeError, TypeError, ValueError):
        raise FrameValidationError(
            "binary screenshot response content type is invalid"
        ) from None
    expected_media_type = f"image/{requested_format}"
    if media_type != expected_media_type:
        raise FrameValidationError(
            "binary screenshot response content type is invalid"
        )
    try:
        width = int(_required_screenshot_header(headers, "x-computer-use-width"))
        height = int(_required_screenshot_header(headers, "x-computer-use-height"))
        if width <= 0 or height <= 0:
            raise ValueError

        size_bytes = int(
            _required_screenshot_header(headers, "x-computer-use-size-bytes")
        )
        if size_bytes != len(data):
            raise ValueError

        sha256 = _required_screenshot_header(headers, "x-computer-use-sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError
        if sha256 != hashlib.sha256(data).hexdigest():
            raise ValueError

        captured_at = datetime.fromisoformat(
            _required_screenshot_header(headers, "x-computer-use-captured-at")
        )
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError

        coordinate_space = CoordinateSpace.model_validate_json(
            _required_screenshot_header(headers, "x-computer-use-coordinate-space")
        )
        if (
            coordinate_space.image_width != width
            or coordinate_space.image_height != height
        ):
            raise ValueError

        cursor_visible_value = _required_screenshot_header(
            headers,
            "x-computer-use-cursor-visible",
        )
        if cursor_visible_value not in {"true", "false"}:
            raise ValueError
        cursor_visible = cursor_visible_value == "true"
        if cursor_visible is not requested_cursor_visibility:
            raise ValueError

        cursor_position_value = json.loads(
            _required_screenshot_header(headers, "x-computer-use-cursor-position")
        )
        cursor_position = (
            Point.model_validate(cursor_position_value)
            if cursor_position_value is not None
            else None
        )
        if cursor_position is not None and (
            cursor_position.x >= coordinate_space.desktop_width
            or cursor_position.y >= coordinate_space.desktop_height
        ):
            raise ValueError

        timing_ms = json.loads(
            _required_screenshot_header(headers, "x-computer-use-timing-ms")
        )
        if not isinstance(timing_ms, dict) or any(
            not isinstance(name, str)
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
            for name, value in timing_ms.items()
        ):
            raise ValueError

        capture_backend = _required_screenshot_header(
            headers,
            "x-computer-use-capture-backend",
        )
        if not isinstance(capture_backend, str) or not capture_backend.strip():
            raise ValueError

        return Screenshot(
            format=requested_format,
            width=width,
            height=height,
            size_bytes=size_bytes,
            bytes=data,
            sha256=sha256,
            captured_at=captured_at,
            coordinate_space=coordinate_space,
            cursor_visible=cursor_visible,
            cursor_position=cursor_position,
        )
    except (TypeError, ValueError):
        raise FrameValidationError(
            "binary screenshot response metadata is invalid"
        ) from None


def _screenshot_payload(
    *,
    format: Literal["png", "jpeg", "webp"],
    quality: int,
    scale: float,
    show_cursor: bool,
    processing: Literal["daemon", "client", "auto"],
    storage: Literal["inline", "artifact", "auto"] = "inline",
) -> dict[str, object]:
    return {
        "format": format,
        "quality": quality,
        "scale": scale,
        "show_cursor": show_cursor,
        "processing": processing,
        "storage": storage,
    }


class ScreenshotsNamespace(Namespace):
    def _payload(
        self,
        *,
        format: Literal["png", "jpeg", "webp"],
        quality: int,
        scale: float,
        show_cursor: bool,
        processing: Literal["daemon", "client", "auto"],
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> dict[str, object]:
        return _screenshot_payload(
            format=format,
            quality=quality,
            scale=scale,
            show_cursor=show_cursor,
            processing=processing,
            storage=storage,
        )

    def full(
        self,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        payload = self._payload(
            format=format,
            quality=quality,
            scale=scale,
            show_cursor=show_cursor,
            processing=processing,
            storage=storage,
        )
        if storage == "inline":
            data, headers = self._client.post_bytes_with_headers(
                "/v1/screenshots/full/raw",
                json=payload,
            )
            return _screenshot_from_binary_response(
                data,
                headers,
                requested_format=format,
                requested_cursor_visibility=show_cursor,
            )
        return Screenshot.model_validate(
            self._client.post_json(
                "/v1/screenshots/full",
                json=payload,
                _mutation=storage in {"artifact", "auto"},
            )
        )

    def full_bytes(
        self,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
    ) -> bytes:
        return self._client.post_bytes(
            "/v1/screenshots/full/raw",
            json=self._payload(
                format=format,
                quality=quality,
                scale=scale,
                show_cursor=show_cursor,
                processing=processing,
            ),
        )

    def region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        return Screenshot.model_validate(
            self._client.post_json(
                "/v1/screenshots/region",
                json=self._payload(
                    format=format,
                    quality=quality,
                    scale=scale,
                    show_cursor=show_cursor,
                    processing=processing,
                    storage=storage,
                )
                | {
                    "region": {"x": x, "y": y, "width": width, "height": height},
                },
                _mutation=storage in {"artifact", "auto"},
            )
        )

    def region_bytes(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
    ) -> bytes:
        return self._client.post_bytes(
            "/v1/screenshots/region/raw",
            json=self._payload(
                format=format,
                quality=quality,
                scale=scale,
                show_cursor=show_cursor,
                processing=processing,
            )
            | {"region": {"x": x, "y": y, "width": width, "height": height}},
        )

    def zoom(
        self,
        region: Region,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        return Screenshot.model_validate(
            self._client.post_json(
                "/v1/screenshots/zoom",
                json={
                    "region": region.model_dump(),
                    "scale": scale,
                    "format": format,
                    "quality": quality,
                    "show_cursor": show_cursor,
                    "storage": storage,
                },
                _mutation=storage in {"artifact", "auto"},
            )
        )

    def zoom_bytes(
        self,
        region: Region,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
    ) -> bytes:
        return self._client.post_bytes(
            "/v1/screenshots/zoom/raw",
            json={
                "region": region.model_dump(),
                "scale": scale,
                "format": format,
                "quality": quality,
                "show_cursor": show_cursor,
            },
        )

    def zoom_around(
        self,
        center: tuple[int, int],
        width: int,
        height: int,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
    ) -> Screenshot:
        x = max(0, center[0] - width // 2)
        y = max(0, center[1] - height // 2)
        return self.zoom(
            Region(x=x, y=y, width=width, height=height),
            scale=scale,
            format=format,
            quality=quality,
            show_cursor=show_cursor,
        )


class AsyncScreenshotsNamespace(AsyncNamespace):
    async def full(
        self,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        payload = _screenshot_payload(
            format=format,
            quality=quality,
            scale=scale,
            show_cursor=show_cursor,
            processing=processing,
            storage=storage,
        )
        if storage == "inline":
            data, headers = await self._client.post_bytes_with_headers(
                "/v1/screenshots/full/raw",
                json=payload,
            )
            return _screenshot_from_binary_response(
                data,
                headers,
                requested_format=format,
                requested_cursor_visibility=show_cursor,
            )
        return Screenshot.model_validate(
            await self._client.post_json(
                "/v1/screenshots/full",
                json=payload,
                _mutation=storage in {"artifact", "auto"},
            )
        )

    async def full_bytes(
        self,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
    ) -> bytes:
        return await self._client.post_bytes(
            "/v1/screenshots/full/raw",
            json=_screenshot_payload(
                format=format,
                quality=quality,
                scale=scale,
                show_cursor=show_cursor,
                processing=processing,
            ),
        )

    async def region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        return Screenshot.model_validate(
            await self._client.post_json(
                "/v1/screenshots/region",
                json=_screenshot_payload(
                    format=format,
                    quality=quality,
                    scale=scale,
                    show_cursor=show_cursor,
                    processing=processing,
                    storage=storage,
                )
                | {"region": {"x": x, "y": y, "width": width, "height": height}},
                _mutation=storage in {"artifact", "auto"},
            )
        )

    async def region_bytes(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
    ) -> bytes:
        return await self._client.post_bytes(
            "/v1/screenshots/region/raw",
            json=_screenshot_payload(
                format=format,
                quality=quality,
                scale=scale,
                show_cursor=show_cursor,
                processing=processing,
            )
            | {"region": {"x": x, "y": y, "width": width, "height": height}},
        )

    async def zoom(
        self,
        region: Region,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        return Screenshot.model_validate(
            await self._client.post_json(
                "/v1/screenshots/zoom",
                json={
                    "region": region.model_dump(),
                    "scale": scale,
                    "format": format,
                    "quality": quality,
                    "show_cursor": show_cursor,
                    "storage": storage,
                },
                _mutation=storage in {"artifact", "auto"},
            )
        )

    async def zoom_bytes(
        self,
        region: Region,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
    ) -> bytes:
        return await self._client.post_bytes(
            "/v1/screenshots/zoom/raw",
            json={
                "region": region.model_dump(),
                "scale": scale,
                "format": format,
                "quality": quality,
                "show_cursor": show_cursor,
            },
        )

    async def zoom_around(
        self,
        center: tuple[int, int],
        width: int,
        height: int,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
    ) -> Screenshot:
        x = max(0, center[0] - width // 2)
        y = max(0, center[1] - height // 2)
        return await self.zoom(
            Region(x=x, y=y, width=width, height=height),
            scale=scale,
            format=format,
            quality=quality,
            show_cursor=show_cursor,
        )
