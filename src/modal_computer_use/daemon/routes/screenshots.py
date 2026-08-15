from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import (
    ScreenshotCaptureTiming,
    run_screenshot_capture,
    run_screenshot_capture_with_timing,
)
from modal_computer_use.daemon.routes.validation import (
    validate_region,
)
from modal_computer_use.daemon.schemas import ScreenshotRequest, ZoomScreenshotRequest
from modal_computer_use.models import Region, Screenshot, ScreenshotOptions

router = APIRouter(prefix="/v1/screenshots")

_RAW_SCREENSHOT_RESPONSE: dict[str, Any] = {
    "content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}},
    "headers": {
        "x-computer-use-width": {
            "required": True,
            "schema": {"type": "integer", "minimum": 1},
        },
        "x-computer-use-height": {
            "required": True,
            "schema": {"type": "integer", "minimum": 1},
        },
        "x-computer-use-size-bytes": {
            "required": True,
            "schema": {"type": "integer", "minimum": 0},
        },
        "x-computer-use-sha256": {
            "required": True,
            "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "x-computer-use-captured-at": {
            "required": True,
            "schema": {"type": "string", "format": "date-time"},
        },
        "x-computer-use-coordinate-space": {
            "description": "JSON-encoded coordinate-space metadata.",
            "required": True,
            "schema": {"type": "string"},
        },
        "x-computer-use-cursor-visible": {
            "required": True,
            "schema": {"type": "boolean"},
        },
        "x-computer-use-cursor-position": {
            "description": "JSON-encoded cursor point, or null when it was not captured.",
            "required": True,
            "schema": {"type": "string"},
        },
        "x-computer-use-timing-ms": {
            "description": "JSON-encoded daemon timing metadata.",
            "required": True,
            "schema": {"type": "string"},
        },
        "x-computer-use-capture-backend": {
            "required": True,
            "schema": {"type": "string"},
        },
    },
}


def scaled_dimension(value: int, scale: float) -> int:
    return max(1, round(value * scale))


def enforce_screenshot_pixels(request: Request, width: int, height: int) -> None:
    if width * height > request.app.state.settings.screenshot_max_pixels:
        raise DaemonError(
            "screenshot exceeds max pixel budget",
            status_code=413,
            code="screenshot_too_large",
            details={"width": width, "height": height},
        )


def enforce_screenshot_options_pixels(
    request: Request,
    *,
    source_width: int,
    source_height: int,
    scale: float,
) -> None:
    enforce_screenshot_pixels(
        request,
        scaled_dimension(source_width, scale),
        scaled_dimension(source_height, scale),
    )


def _screenshot_headers(
    shot,
    *,
    capture_timing: ScreenshotCaptureTiming | None = None,
) -> dict[str, str]:
    cursor_position = (
        shot.cursor_position.model_dump(mode="json")
        if shot.cursor_position is not None
        else None
    )
    timing_ms = dict(shot.timings_ms)
    if capture_timing is not None:
        timing_ms.update(
            {
                "route_ready_ms": capture_timing.ready_ms,
                "route_lock_wait_ms": capture_timing.lock_wait_ms,
                "route_operation_ms": capture_timing.operation_ms,
                "route_total_ms": capture_timing.total_ms,
            }
        )
    return {
        "x-computer-use-width": str(shot.width),
        "x-computer-use-height": str(shot.height),
        "x-computer-use-size-bytes": str(len(shot.data)),
        "x-computer-use-sha256": shot.sha256,
        "x-computer-use-captured-at": shot.captured_at.isoformat(),
        "x-computer-use-coordinate-space": shot.coordinate_space.model_dump_json(),
        "x-computer-use-timing-ms": json.dumps(timing_ms, separators=(",", ":")),
        "x-computer-use-capture-backend": shot.capture_backend or "unknown",
        "x-computer-use-cursor-visible": str(shot.cursor_visible).lower(),
        "x-computer-use-cursor-position": json.dumps(
            cursor_position,
            separators=(",", ":"),
        ),
    }


def _raw_screenshot_options(payload: ScreenshotRequest) -> ScreenshotOptions:
    if payload.storage != "inline":
        raise DaemonError(
            "raw screenshot response requires inline storage",
            status_code=422,
            code="invalid_screenshot_storage",
            details={"storage": payload.storage},
        )
    return ScreenshotOptions.model_validate(payload.model_dump(exclude={"region"}))


@router.post("/full")
async def full(payload: ScreenshotRequest, request: Request) -> Screenshot:
    options = ScreenshotOptions.model_validate(payload.model_dump(exclude={"region"}))
    enforce_screenshot_options_pixels(
        request,
        source_width=request.app.state.backend.width,
        source_height=request.app.state.backend.height,
        scale=options.scale,
    )
    async def operation() -> Screenshot:
        return await request.app.state.backend.screenshot(
            options,
            artifact_store=request.app.state.artifacts,
        )

    return await run_screenshot_capture(
        request,
        operation,
        mutation_semantic_data=payload if options.storage in {"artifact", "auto"} else None,
    )


@router.post(
    "/full/raw",
    responses={200: _RAW_SCREENSHOT_RESPONSE},
)
async def full_raw(payload: ScreenshotRequest, request: Request) -> Response:
    options = _raw_screenshot_options(payload)
    enforce_screenshot_options_pixels(
        request,
        source_width=request.app.state.backend.width,
        source_height=request.app.state.backend.height,
        scale=options.scale,
    )

    async def operation():
        return await request.app.state.backend.screenshot_bytes(
            options,
            include_cursor_position=True,
            prefer_native_png=True,
        )

    shot, capture_timing = await run_screenshot_capture_with_timing(
        request, operation
    )
    return Response(
        content=shot.data,
        media_type=f"image/{options.format}",
        headers=_screenshot_headers(shot, capture_timing=capture_timing),
    )


@router.post("/region")
async def region(payload: ScreenshotRequest, request: Request) -> Screenshot:
    if payload.region is None:
        raise DaemonError("region screenshot requires region", code="missing_region")
    validate_region(request, payload.region)
    options = ScreenshotOptions.model_validate(payload.model_dump(exclude={"region"}))
    enforce_screenshot_options_pixels(
        request,
        source_width=payload.region.width,
        source_height=payload.region.height,
        scale=options.scale,
    )
    async def operation() -> Screenshot:
        return await request.app.state.backend.screenshot(
            options,
            region=payload.region,
            artifact_store=request.app.state.artifacts,
        )

    return await run_screenshot_capture(
        request,
        operation,
        mutation_semantic_data=payload if options.storage in {"artifact", "auto"} else None,
    )


@router.post(
    "/region/raw",
    responses={200: _RAW_SCREENSHOT_RESPONSE},
)
async def region_raw(payload: ScreenshotRequest, request: Request) -> Response:
    if payload.region is None:
        raise DaemonError("region screenshot requires region", code="missing_region")
    validate_region(request, payload.region)
    options = _raw_screenshot_options(payload)
    enforce_screenshot_options_pixels(
        request,
        source_width=payload.region.width,
        source_height=payload.region.height,
        scale=options.scale,
    )

    async def operation():
        return await request.app.state.backend.screenshot_bytes(
            options,
            region=payload.region,
            prefer_native_png=True,
        )

    shot, capture_timing = await run_screenshot_capture_with_timing(
        request, operation
    )
    return Response(
        content=shot.data,
        media_type=f"image/{options.format}",
        headers=_screenshot_headers(shot, capture_timing=capture_timing),
    )


@router.post("/zoom")
async def zoom(payload: ZoomScreenshotRequest, request: Request) -> Screenshot:
    region = Region.model_validate(payload.region)
    validate_region(request, region)
    enforce_screenshot_pixels(
        request,
        scaled_dimension(region.width, payload.scale),
        scaled_dimension(region.height, payload.scale),
    )
    options = ScreenshotOptions(
        format=payload.format,
        quality=payload.quality,
        scale=payload.scale,
        show_cursor=payload.show_cursor,
        storage=payload.storage,
    )
    async def operation() -> Screenshot:
        return await request.app.state.backend.screenshot(
            options,
            region=region,
            artifact_store=request.app.state.artifacts,
        )

    return await run_screenshot_capture(
        request,
        operation,
        mutation_semantic_data=payload if options.storage in {"artifact", "auto"} else None,
    )


@router.post(
    "/zoom/raw",
    responses={200: _RAW_SCREENSHOT_RESPONSE},
)
async def zoom_raw(payload: ZoomScreenshotRequest, request: Request) -> Response:
    if payload.storage != "inline":
        raise DaemonError(
            "raw screenshot response requires inline storage",
            status_code=422,
            code="invalid_screenshot_storage",
            details={"storage": payload.storage},
        )
    region = Region.model_validate(payload.region)
    validate_region(request, region)
    enforce_screenshot_pixels(
        request,
        scaled_dimension(region.width, payload.scale),
        scaled_dimension(region.height, payload.scale),
    )
    options = ScreenshotOptions(
        format=payload.format,
        quality=payload.quality,
        scale=payload.scale,
        show_cursor=payload.show_cursor,
        storage="inline",
    )

    async def operation():
        return await request.app.state.backend.screenshot_bytes(
            options,
            region=region,
            prefer_native_png=True,
        )

    shot, capture_timing = await run_screenshot_capture_with_timing(
        request, operation
    )
    return Response(
        content=shot.data,
        media_type=f"image/{options.format}",
        headers=_screenshot_headers(shot, capture_timing=capture_timing),
    )
