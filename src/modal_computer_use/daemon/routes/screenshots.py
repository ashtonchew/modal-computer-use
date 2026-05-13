from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.validation import validate_region
from modal_computer_use.daemon.schemas import ScreenshotRequest, ZoomScreenshotRequest
from modal_computer_use.models import Region, Screenshot, ScreenshotOptions

router = APIRouter(prefix="/v1/screenshots")


def _enforce_pixels(request: Request, width: int, height: int) -> None:
    if width * height > request.app.state.settings.screenshot_max_pixels:
        raise DaemonError(
            "screenshot exceeds max pixel budget",
            status_code=413,
            code="screenshot_too_large",
            details={"width": width, "height": height},
        )


@router.post("/full")
async def full(payload: ScreenshotRequest, request: Request) -> Screenshot:
    _enforce_pixels(request, request.app.state.backend.width, request.app.state.backend.height)
    options = ScreenshotOptions.model_validate(payload.model_dump(exclude={"region"}))
    async with request.app.state.input_lock:
        error = budgets.screenshot_reservation_error(request)
        if error is not None:
            raise error
        shot = await request.app.state.backend.screenshot(
            options,
            artifact_store=request.app.state.artifacts,
        )
        request.app.state.screenshot_count += 1
        budgets.enforce(request, "screenshots", "artifacts")
        return shot


@router.post("/region")
async def region(payload: ScreenshotRequest, request: Request) -> Screenshot:
    if payload.region is None:
        raise DaemonError("region screenshot requires region", code="missing_region")
    validate_region(request, payload.region)
    _enforce_pixels(request, payload.region.width, payload.region.height)
    options = ScreenshotOptions.model_validate(payload.model_dump(exclude={"region"}))
    async with request.app.state.input_lock:
        error = budgets.screenshot_reservation_error(request)
        if error is not None:
            raise error
        shot = await request.app.state.backend.screenshot(
            options,
            region=payload.region,
            artifact_store=request.app.state.artifacts,
        )
        request.app.state.screenshot_count += 1
        budgets.enforce(request, "screenshots", "artifacts")
        return shot


@router.post("/zoom")
async def zoom(payload: ZoomScreenshotRequest, request: Request) -> Screenshot:
    region = Region.model_validate(payload.region)
    validate_region(request, region)
    _enforce_pixels(
        request, round(region.width * payload.scale), round(region.height * payload.scale)
    )
    options = ScreenshotOptions(
        format=payload.format,  # type: ignore[arg-type]
        quality=payload.quality,
        scale=payload.scale,
        show_cursor=payload.show_cursor,
        storage=payload.storage,  # type: ignore[arg-type]
    )
    async with request.app.state.input_lock:
        error = budgets.screenshot_reservation_error(request)
        if error is not None:
            raise error
        shot = await request.app.state.backend.screenshot(
            options,
            region=region,
            artifact_store=request.app.state.artifacts,
        )
        request.app.state.screenshot_count += 1
        budgets.enforce(request, "screenshots", "artifacts")
        return shot
