from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon.routes.execution import run_input_action
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready, ready_input_lock
from modal_computer_use.daemon.schemas import TextRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload_with_secrets

router = APIRouter(prefix="/v1/clipboard")


@router.get("/text")
async def get_text(request: Request) -> dict[str, str]:
    await ensure_desktop_ready(request)
    async with ready_input_lock(request):
        return {"text": await request.app.state.backend.clipboard_get()}


@router.put("/text")
async def set_text(payload: TextRequest, request: Request) -> ActionResult:
    async def operation() -> ActionResult:
        result = await request.app.state.backend.clipboard_set(payload.text)
        return _sanitize_action_result(
            result,
            secret=payload.text,
            replacement="[redacted clipboard text]",
        )

    return await run_input_action(
        request,
        operation,
        fallback_code="clipboard_set_failed",
        fallback_message="clipboard set failed",
    )


@router.delete("/text")
async def clear_text(request: Request) -> ActionResult:
    async def operation() -> ActionResult:
        return await request.app.state.backend.clipboard_clear()

    return await run_input_action(
        request,
        operation,
        fallback_code="clipboard_clear_failed",
        fallback_message="clipboard clear failed",
    )


def _sanitize_action_result(
    result: ActionResult, *, secret: str, replacement: str
) -> ActionResult:
    payload = sanitize_payload_with_secrets(result.model_dump(mode="json"), [(secret, replacement)])
    return ActionResult.model_validate(payload)
