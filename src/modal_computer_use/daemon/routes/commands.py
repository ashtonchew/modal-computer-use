from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready
from modal_computer_use.daemon.schemas import CommandRunRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_text

router = APIRouter(prefix="/v1/commands")


@router.post("/run")
async def run(payload: CommandRunRequest, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        result = await request.app.state.backend.run_command(
            payload.command,
            timeout=payload.timeout,
        )
    return ActionResult(
        ok=result.ok,
        message=sanitize_text(result.message) if result.message is not None else None,
        elapsed_ms=result.elapsed_ms,
        output=_sanitize_command_output(result.output),
    )


def _sanitize_command_output(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {key: _sanitize_command_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_command_output(item) for item in value]
    return value
