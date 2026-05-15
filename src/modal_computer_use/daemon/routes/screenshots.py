from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.validation import (
    ensure_desktop_ready,
    ready_input_lock,
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


@router.post("/full")
async def full(payload: ScreenshotRequest, request: Request) -> Screenshot:
    await ensure_desktop_ready(request)
    options = ScreenshotOptions.model_validate(payload.model_dump(exclude={"region"}))
    enforce_screenshot_options_pixels(
        request,
        source_width=request.app.state.backend.width,
        source_height=request.app.state.backend.height,
        scale=options.scale,
    )
    budget_error = budgets.screenshot_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with ready_input_lock(request):
        error = budgets.screenshot_reservation_error(request)
        if error is not None:
            raise error
        budgets.reserve_screenshot(request)
        shot = await request.app.state.backend.screenshot(
            options,
            artifact_store=request.app.state.artifacts,
        )
        budgets.enforce(request, "screenshots", "artifacts")
        return shot


@router.post("/region")
async def region(payload: ScreenshotRequest, request: Request) -> Screenshot:
    await ensure_desktop_ready(request)
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
    budget_error = budgets.screenshot_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with ready_input_lock(request):
        error = budgets.screenshot_reservation_error(request)
        if error is not None:
            raise error
        budgets.reserve_screenshot(request)
        shot = await request.app.state.backend.screenshot(
            options,
            region=payload.region,
            artifact_store=request.app.state.artifacts,
        )
        budgets.enforce(request, "screenshots", "artifacts")
        return shot


@router.post("/zoom")
async def zoom(payload: ZoomScreenshotRequest, request: Request) -> Screenshot:
    await ensure_desktop_ready(request)
    region = Region.model_validate(payload.region)
    validate_region(request, region)
    enforce_screenshot_pixels(
        request,
        scaled_dimension(region.width, payload.scale),
        scaled_dimension(region.height, payload.scale),
    )
    budget_error = budgets.screenshot_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    options = ScreenshotOptions(
        format=payload.format,  # type: ignore[arg-type]
        quality=payload.quality,
        scale=payload.scale,
        show_cursor=payload.show_cursor,
        storage=payload.storage,  # type: ignore[arg-type]
    )
    async with ready_input_lock(request):
        error = budgets.screenshot_reservation_error(request)
        if error is not None:
            raise error
        budgets.reserve_screenshot(request)
        shot = await request.app.state.backend.screenshot(
            options,
            region=region,
            artifact_store=request.app.state.artifacts,
        )
        budgets.enforce(request, "screenshots", "artifacts")
        return shot
