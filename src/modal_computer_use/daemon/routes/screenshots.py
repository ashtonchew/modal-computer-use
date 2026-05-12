from __future__ import annotations

from fastapi import APIRouter, Request

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
        request.app.state.screenshot_count += 1
        shot = await request.app.state.backend.screenshot(
            options,
            artifact_store=request.app.state.artifacts,
        )
        _enforce_budgets(request)
        return shot


@router.post("/region")
async def region(payload: ScreenshotRequest, request: Request) -> Screenshot:
    if payload.region is None:
        raise DaemonError("region screenshot requires region", code="missing_region")
    validate_region(request, payload.region)
    _enforce_pixels(request, payload.region.width, payload.region.height)
    options = ScreenshotOptions.model_validate(payload.model_dump(exclude={"region"}))
    async with request.app.state.input_lock:
        request.app.state.screenshot_count += 1
        shot = await request.app.state.backend.screenshot(
            options,
            region=payload.region,
            artifact_store=request.app.state.artifacts,
        )
        _enforce_budgets(request)
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
        request.app.state.screenshot_count += 1
        shot = await request.app.state.backend.screenshot(
            options,
            region=region,
            artifact_store=request.app.state.artifacts,
        )
        _enforce_budgets(request)
        return shot


def _enforce_budgets(request: Request) -> None:
    settings = request.app.state.settings
    if (
        settings.max_screenshots is not None
        and request.app.state.screenshot_count > settings.max_screenshots
    ):
        raise DaemonError("screenshot budget exceeded", status_code=429, code="budget_exceeded")
    if settings.max_artifact_bytes is not None:
        artifact_total = sum((item.size_bytes or 0) for item in request.app.state.artifacts.list())
        recording_total = request.app.state.recordings.total_size_bytes()
        if artifact_total + recording_total > settings.max_artifact_bytes:
            raise DaemonError(
                "artifact byte budget exceeded",
                status_code=429,
                code="budget_exceeded",
            )
