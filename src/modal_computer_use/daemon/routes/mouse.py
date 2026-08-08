from __future__ import annotations

from fastapi import APIRouter, Request, Response

from modal_computer_use.daemon.routes.execution import run_input_action
from modal_computer_use.daemon.routes.validation import (
    ensure_desktop_ready,
    ready_input_lock,
    validate_collection_size,
    validate_keys,
    validate_optional_point,
    validate_point,
)
from modal_computer_use.daemon.schemas import (
    MouseButtonRequest,
    MouseClickRequest,
    MouseDragRequest,
    MouseMoveRequest,
    MouseScrollRequest,
)
from modal_computer_use.models import ActionResult, Point

router = APIRouter(prefix="/v1/mouse")
_INPUT_BACKEND_HEADER = "X-Computer-Use-Input-Backend"


@router.post("/move")
async def move(payload: MouseMoveRequest, request: Request, response: Response) -> Point:
    validate_point(request, payload)

    async def operation() -> Point:
        backend = request.app.state.backend
        point = await backend.mouse_move(payload.x, payload.y)
        _report_input_backend(response, backend.input_backend)
        return point

    return await run_input_action(request, operation, semantic_data=payload)


def _report_input_backend(response: Response, backend_name: object) -> None:
    if isinstance(backend_name, str) and backend_name:
        response.headers[_INPUT_BACKEND_HEADER] = backend_name


@router.post("/click")
async def click(payload: MouseClickRequest, request: Request, response: Response) -> Point:
    validate_optional_point(request, x=payload.x, y=payload.y)
    validate_collection_size(
        payload.modifiers,
        maximum=request.app.state.settings.max_key_collection_size,
        field="modifiers",
        code="too_many_keys",
    )
    validate_keys(*payload.modifiers)

    async def operation() -> Point:
        backend = request.app.state.backend
        point = await backend.mouse_click(
            payload.x,
            payload.y,
            button=payload.button,
            count=2 if payload.double else 1,
            modifiers=payload.modifiers,
        )
        _report_input_backend(response, backend.input_backend)
        return point

    return await run_input_action(request, operation, semantic_data=payload)


@router.post("/drag")
async def drag(payload: MouseDragRequest, request: Request, response: Response) -> Point:
    validate_collection_size(
        payload.path or [],
        maximum=request.app.state.settings.max_drag_points,
        field="path",
        code="too_many_drag_points",
    )
    start = None
    end = None
    if payload.start_x is not None and payload.start_y is not None:
        start = Point(x=payload.start_x, y=payload.start_y)
        validate_point(request, start, field="start")
    if payload.end_x is not None and payload.end_y is not None:
        end = Point(x=payload.end_x, y=payload.end_y)
        validate_point(request, end, field="end")
    for index, point in enumerate(payload.path or []):
        validate_point(request, point, field=f"path[{index}]")
    validate_collection_size(
        payload.modifiers,
        maximum=request.app.state.settings.max_key_collection_size,
        field="modifiers",
        code="too_many_keys",
    )
    validate_keys(*payload.modifiers)

    async def operation() -> Point:
        backend = request.app.state.backend
        point = await backend.mouse_drag(
            start=start,
            end=end,
            path=payload.path,
            button=payload.button,
            duration_ms=payload.duration_ms,
            modifiers=payload.modifiers,
        )
        _report_input_backend(response, backend.input_backend)
        return point

    return await run_input_action(request, operation, semantic_data=payload)


@router.post("/scroll")
async def scroll(
    payload: MouseScrollRequest, request: Request, response: Response
) -> ActionResult:
    validate_optional_point(request, x=payload.x, y=payload.y)

    async def operation() -> ActionResult:
        backend = request.app.state.backend
        result = await backend.mouse_scroll(
            payload.direction,
            amount=payload.amount,
            x=payload.x,
            y=payload.y,
        )
        _report_input_backend(response, backend.input_backend)
        return result

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="mouse_scroll_failed",
        fallback_message="mouse scroll failed",
    )


@router.post("/down")
async def down(payload: MouseButtonRequest, request: Request, response: Response) -> ActionResult:
    validate_optional_point(request, x=payload.x, y=payload.y)

    async def operation() -> ActionResult:
        backend = request.app.state.backend
        result = await backend.mouse_down(payload.button, payload.x, payload.y)
        _report_input_backend(response, backend.input_backend)
        return result

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="mouse_down_failed",
        fallback_message="mouse down failed",
    )


@router.post("/up")
async def up(payload: MouseButtonRequest, request: Request, response: Response) -> ActionResult:
    validate_optional_point(request, x=payload.x, y=payload.y)

    async def operation() -> ActionResult:
        backend = request.app.state.backend
        result = await backend.mouse_up(payload.button, payload.x, payload.y)
        _report_input_backend(response, backend.input_backend)
        return result

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="mouse_up_failed",
        fallback_message="mouse up failed",
    )


@router.get("/position")
async def position(request: Request, response: Response) -> Point:
    await ensure_desktop_ready(request)
    async with ready_input_lock(request):
        backend = request.app.state.backend
        point = await backend.mouse_position()
        _report_input_backend(response, backend.input_backend)
    return point
