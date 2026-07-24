from __future__ import annotations

from time import perf_counter
from typing import Any

from fastapi import APIRouter, Request

from modal_computer_use.daemon.routes.execution import run_input_action
from modal_computer_use.daemon.schemas import CommandRunRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_text

router = APIRouter(prefix="/v1/commands")


@router.post("/run")
async def run(payload: CommandRunRequest, request: Request) -> ActionResult:
    started = perf_counter()

    async def operation() -> ActionResult:
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

    result = await run_input_action(
        request,
        operation,
        fallback_code="command_failed",
        fallback_message="command failed",
    )
    return result.model_copy(update={"elapsed_ms": (perf_counter() - started) * 1000})


def _sanitize_command_output(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {key: _sanitize_command_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_command_output(item) for item in value]
    return value
