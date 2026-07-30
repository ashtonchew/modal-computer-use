from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon.desktop.browser import DEFAULT_BROWSER_PROFILE_DIR
from modal_computer_use.daemon.routes.execution import run_input_action
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready, mutation_lock
from modal_computer_use.daemon.schemas import BrowserOpenUrlRequest, BrowserRenderMetricsRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload

router = APIRouter(prefix="/v1/browser")


@router.post("/open-url")
async def open_url(payload: BrowserOpenUrlRequest, request: Request) -> ActionResult:
    async def operation() -> ActionResult:
        result = await request.app.state.backend.open_url(
            payload.url, wait_for_window=payload.wait_for_window
        )
        return ActionResult.model_validate(sanitize_payload(result.model_dump(mode="json")))

    return await run_input_action(
        request,
        operation,
        fallback_code="browser_open_failed",
        fallback_message="browser open failed",
    )


@router.post("/render-metrics")
async def render_metrics(
    payload: BrowserRenderMetricsRequest,
    request: Request,
) -> dict[str, object]:
    await ensure_desktop_ready(request)
    async with mutation_lock(request):
        return await request.app.state.backend.browser_render_metrics(
            payload.url,
            timeout_seconds=payload.timeout_seconds,
        )


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    await ensure_desktop_ready(request)
    prewarm_result = getattr(request.app.state, "browser_prewarm", None)
    return {
        "configured_browser": request.app.state.settings.browser,
        "prewarm": request.app.state.settings.browser_prewarm,
        "profile_dir": request.app.state.settings.browser_profile_dir
        or DEFAULT_BROWSER_PROFILE_DIR,
        "gpu_mode": request.app.state.settings.browser_gpu_mode,
        "launch_args": request.app.state.settings.browser_launch_args,
        "open_url_on_start": request.app.state.settings.browser_open_url_on_start,
        "prewarm_result": (
            prewarm_result.model_dump(mode="json") if prewarm_result is not None else None
        ),
        "windows": len(await request.app.state.backend.windows()),
    }
