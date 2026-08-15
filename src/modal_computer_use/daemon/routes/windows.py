from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, Request, Response

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import run_input_action
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready
from modal_computer_use.daemon.schemas import WaitForWindowRequest
from modal_computer_use.models import ActionResult, X11Window

router = APIRouter(prefix="/v1/windows")
_WINDOW_BACKEND_HEADER = "X-Computer-Use-Window-Backend"
_MAX_WINDOW_TITLE_LENGTH = 4096


@router.get("")
async def list_windows(request: Request, response: Response) -> list[X11Window]:
    await ensure_desktop_ready(request)
    backend = request.app.state.backend
    windows = await backend.windows()
    _report_window_backend(response, getattr(backend, "window_backend", None))
    return windows


def _report_window_backend(response: Response, backend_name: object) -> None:
    if isinstance(backend_name, str) and backend_name:
        response.headers[_WINDOW_BACKEND_HEADER] = backend_name


@router.get("/active")
async def active(request: Request, response: Response) -> X11Window | None:
    await ensure_desktop_ready(request)
    backend = request.app.state.backend
    window = await backend.active_window()
    _report_window_backend(response, getattr(backend, "window_backend", None))
    return window


@router.post("/{window_id}/activate")
async def activate(window_id: str, request: Request, response: Response) -> ActionResult:
    async def operation() -> ActionResult:
        backend = request.app.state.backend
        result = await backend.activate_window(window_id)
        _report_window_backend(response, getattr(backend, "window_backend", None))
        return result

    return await run_input_action(
        request,
        operation,
        semantic_data={"window_id": window_id},
        fallback_code="window_activate_failed",
        fallback_message="window activate failed",
    )


@router.post("/{window_id}/close")
async def close(window_id: str, request: Request, response: Response) -> ActionResult:
    async def operation() -> ActionResult:
        backend = request.app.state.backend
        result = await backend.close_window(window_id)
        _report_window_backend(response, getattr(backend, "window_backend", None))
        return result

    return await run_input_action(
        request,
        operation,
        semantic_data={"window_id": window_id},
        fallback_code="window_close_failed",
        fallback_message="window close failed",
    )


@router.post("/wait-for")
async def wait_for(
    payload: WaitForWindowRequest, request: Request, response: Response
) -> X11Window:
    deadline = time.monotonic() + payload.timeout
    pattern = re.compile(payload.title_regex) if payload.title_regex else None
    while True:
        await ensure_desktop_ready(request, force=True)
        backend = request.app.state.backend
        windows = await backend.windows()
        _report_window_backend(response, getattr(backend, "window_backend", None))
        for window in windows:
            candidate_title = window.title[:_MAX_WINDOW_TITLE_LENGTH]
            if pattern and not pattern.search(candidate_title):
                continue
            if payload.class_name is not None and window.class_name != payload.class_name:
                continue
            if payload.pid is not None and window.pid != payload.pid:
                continue
            return window
        if time.monotonic() >= deadline:
            raise DaemonError(
                "window not found before timeout", status_code=404, code="window_not_found"
            )
        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
