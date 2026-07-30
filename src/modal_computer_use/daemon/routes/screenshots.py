from __future__ import annotations

import json

from fastapi import APIRouter, Request, Response

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import run_screenshot_capture
from modal_computer_use.daemon.routes.validation import (
    validate_region,
)
from modal_computer_use.daemon.schemas import ScreenshotRequest, ZoomScreenshotRequest
from modal_computer_use.models import Region, Screenshot, ScreenshotOptions

router = APIRouter(prefix="/v1/screenshots")


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


def _screenshot_headers(shot) -> dict[str, str]:
    return {
        "x-computer-use-width": str(shot.width),
        "x-computer-use-height": str(shot.height),
        "x-computer-use-size-bytes": str(len(shot.data)),
        "x-computer-use-sha256": shot.sha256,
        "x-computer-use-captured-at": shot.captured_at.isoformat(),
        "x-computer-use-coordinate-space": shot.coordinate_space.model_dump_json(),
        "x-computer-use-timing-ms": json.dumps(shot.timings_ms, separators=(",", ":")),
        "x-computer-use-capture-backend": shot.capture_backend or "unknown",
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
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}}},
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
        return await request.app.state.backend.screenshot_bytes(options, prefer_native_png=True)

    shot = await run_screenshot_capture(request, operation)
    return Response(
        content=shot.data,
        media_type=f"image/{options.format}",
        headers=_screenshot_headers(shot),
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
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}}},
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

    shot = await run_screenshot_capture(request, operation)
    return Response(
        content=shot.data,
        media_type=f"image/{options.format}",
        headers=_screenshot_headers(shot),
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
        format=payload.format,  # type: ignore[arg-type]
        quality=payload.quality,
        scale=payload.scale,
        show_cursor=payload.show_cursor,
        storage=payload.storage,  # type: ignore[arg-type]
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
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}}},
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
        format=payload.format,  # type: ignore[arg-type]
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

    shot = await run_screenshot_capture(request, operation)
    return Response(
        content=shot.data,
        media_type=f"image/{options.format}",
        headers=_screenshot_headers(shot),
    )
