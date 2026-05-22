from __future__ import annotations

import ipaddress
import json
import time
from contextlib import suppress
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
from modal_computer_use.daemon.schemas import ScreenshotRequest
from modal_computer_use.models import ActionBatchRequest

router = APIRouter(prefix="/v1/session")


@router.websocket("/hot")
async def hot_session(websocket: WebSocket) -> None:
    auth_error = _hot_session_auth_error(websocket)
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
            await _send_hot_action_result(websocket, request_id, payload)
        elif op == "run_raw_screenshot":
            await _send_hot_action_screenshot_result(websocket, request_id, payload)
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
            exc.message,
            details=exc.details,
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
) -> None:
    request = ActionBatchRequest.model_validate(payload)
    result = await run_batch(request, ActionBatchContext(websocket.app.state))
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
) -> None:
    request = ActionBatchRequest.model_validate(payload)
    result, shot = await run_with_screenshot_bytes(request, ActionBatchContext(websocket.app.state))
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
                "message": message,
                "details": details or {},
            },
        }
    )


def _hot_session_auth_error(websocket: WebSocket) -> str | None:
    settings = websocket.app.state.settings
    if settings.reject_query_tokens and "_modal_connect_token" in websocket.query_params:
        return "query_token_rejected"
    if _has_valid_hot_tunnel_token(websocket):
        return None
    if settings.local_token:
        if not _is_hot_loopback_websocket(websocket):
            return "local_token_requires_loopback"
        if websocket.headers.get("authorization") != f"Bearer {settings.local_token}":
            return "unauthorized"
        return None
    if settings.require_connect_user:
        if not _is_hot_trusted_connect_proxy(
            websocket,
            trust_private=settings.trust_private_connect_proxy,
        ):
            return "connect_token_required"
        raw = websocket.headers.get("x-verified-user-data")
        if not raw:
            return "connect_token_required"
        with suppress(json.JSONDecodeError):
            metadata = json.loads(raw)
            if isinstance(metadata, dict) and metadata.get("sdk") == "modal-computer-use":
                return None
        return "invalid_verified_user_data"
    return None


def _is_hot_loopback_websocket(websocket: WebSocket) -> bool:
    host = websocket.client.host if websocket.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_hot_trusted_connect_proxy(websocket: WebSocket, *, trust_private: bool) -> bool:
    host = websocket.client.host if websocket.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return trust_private and (address.is_private or address.is_link_local)


def _has_valid_hot_tunnel_token(websocket: WebSocket) -> bool:
    token = _hot_bearer_token(websocket)
    if not token:
        return False
    settings = websocket.app.state.settings
    if settings.tunnel_token and token == settings.tunnel_token:
        return True
    sessions = getattr(websocket.app.state, "tunnel_sessions", {})
    expires_at = sessions.get(token) if isinstance(sessions, dict) else None
    if not isinstance(expires_at, int | float):
        return False
    if expires_at <= time.time():
        with suppress(Exception):
            sessions.pop(token, None)
        return False
    return True


def _hot_bearer_token(websocket: WebSocket) -> str | None:
    value = websocket.headers.get("authorization", "")
    prefix = "Bearer "
    if not value.startswith(prefix):
        return None
    token = value[len(prefix) :].strip()
    return token or None
