from __future__ import annotations

from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from modal_computer_use.daemon.actions import ActionBatchContext, run_with_screenshot_bytes
from modal_computer_use.daemon.actions import run as run_batch
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import run_screenshot_capture
from modal_computer_use.daemon.routes.screenshots import (
    _raw_screenshot_options,
    _screenshot_headers,
    enforce_screenshot_options_pixels,
)
from modal_computer_use.daemon.routes.websocket_auth import daemon_websocket_auth_error
from modal_computer_use.daemon.schemas import ScreenshotRequest
from modal_computer_use.models import ActionBatchRequest
from modal_computer_use.redaction import sanitize_payload, sanitize_text

router = APIRouter(prefix="/v1/session")


@router.websocket("/hot")
async def hot_session(websocket: WebSocket) -> None:
    auth_error = daemon_websocket_auth_error(websocket)
    if auth_error is not None:
        await websocket.close(code=1008, reason=auth_error)
        return
    await websocket.accept()
    await websocket.send_json({"type": "ready", "protocol": "computer-use.hot-session.v1"})
    try:
        while True:
            message = await websocket.receive_json()
            await _handle_hot_session_message(websocket, message)
    except WebSocketDisconnect:
        return


async def _handle_hot_session_message(websocket: WebSocket, message: Any) -> None:
    if not isinstance(message, dict):
        await _send_hot_error(websocket, None, "invalid_message", "message must be a JSON object")
        return
    request_id = message.get("id")
    if not isinstance(request_id, str) or not request_id:
        await _send_hot_error(websocket, None, "invalid_message", "message id is required")
        return
    op = message.get("op")
    payload = message.get("payload") or {}
    if not isinstance(payload, dict):
        await _send_hot_error(websocket, request_id, "invalid_payload", "payload must be an object")
        return
    try:
        if op == "ping":
            await websocket.send_json(
                {"type": "result", "id": request_id, "ok": True, "result": {}}
            )
        elif op == "run_actions":
            await _send_hot_action_result(
                websocket,
                request_id,
                payload,
                operation_sequence=message.get("sequence"),
            )
        elif op == "run_raw_screenshot":
            await _send_hot_action_screenshot_result(
                websocket,
                request_id,
                payload,
                operation_sequence=message.get("sequence"),
            )
        elif op == "screenshot_raw":
            await _send_hot_screenshot_result(websocket, request_id, payload)
        else:
            await _send_hot_error(websocket, request_id, "unsupported_op", f"unsupported op: {op}")
    except ValidationError as exc:
        await _send_hot_error(
            websocket,
            request_id,
            "validation_error",
            "request validation failed",
            details={"errors": exc.errors(include_input=False)},
        )
    except DaemonError as exc:
        await _send_hot_error(
            websocket,
            request_id,
            exc.code,
            sanitize_text(exc.message),
            details=sanitize_payload(exc.details),
        )
    except Exception as exc:
        await _send_hot_error(
            websocket,
            request_id,
            "internal_error",
            "internal server error",
            details={"type": type(exc).__name__},
        )


async def _send_hot_action_result(
    websocket: WebSocket,
    request_id: str,
    payload: dict[str, Any],
    *,
    operation_sequence: Any,
) -> None:
    request = ActionBatchRequest.model_validate(payload)
    result = await run_batch(
        request,
        ActionBatchContext(
            websocket.app.state,
            websocket.headers,
            operation_sequence=operation_sequence,
        ),
    )
    await websocket.send_json(
        {
            "type": "result",
            "id": request_id,
            "ok": result.ok,
            "result": result.model_dump(mode="json"),
        }
    )


async def _send_hot_action_screenshot_result(
    websocket: WebSocket,
    request_id: str,
    payload: dict[str, Any],
    *,
    operation_sequence: Any,
) -> None:
    request = ActionBatchRequest.model_validate(payload)
    result, shot = await run_with_screenshot_bytes(
        request,
        ActionBatchContext(
            websocket.app.state,
            websocket.headers,
            operation_sequence=operation_sequence,
        ),
    )
    if shot is None:
        await _send_hot_error(
            websocket,
            request_id,
            "raw_screenshot_after_not_captured",
            "action batch did not capture a raw screenshot",
            details={"result": result.model_dump(mode="json")},
        )
        return
    headers = _screenshot_headers(shot)
    await websocket.send_json(
        {
            "type": "binary",
            "id": request_id,
            "ok": result.ok,
            "result": result.model_dump(mode="json", exclude={"screenshot"}),
            "headers": headers,
            "encoding": "binary",
            "content_type": f"image/{shot.format}",
            "size_bytes": len(shot.data),
        }
    )
    await websocket.send_bytes(shot.data)


async def _send_hot_screenshot_result(
    websocket: WebSocket,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    request = ScreenshotRequest.model_validate(payload)
    options = _raw_screenshot_options(request)
    enforce_screenshot_options_pixels(
        websocket,
        source_width=websocket.app.state.backend.width,
        source_height=websocket.app.state.backend.height,
        scale=options.scale,
    )

    async def operation():
        return await websocket.app.state.backend.screenshot_bytes(options, prefer_native_png=True)

    shot = await run_screenshot_capture(websocket, operation)
    headers = _screenshot_headers(shot)
    await websocket.send_json(
        {
            "type": "binary",
            "id": request_id,
            "ok": True,
            "result": None,
            "headers": headers,
            "encoding": "binary",
            "content_type": f"image/{options.format}",
            "size_bytes": len(shot.data),
        }
    )
    await websocket.send_bytes(shot.data)


async def _send_hot_error(
    websocket: WebSocket,
    request_id: str | None,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    await websocket.send_json(
        {
            "type": "error",
            "id": request_id,
            "ok": False,
            "error": {
                "code": code,
                "message": sanitize_text(message),
                "details": sanitize_payload(details or {}),
            },
        }
    )
