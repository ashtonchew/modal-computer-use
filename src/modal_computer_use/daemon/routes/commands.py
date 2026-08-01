from __future__ import annotations

from time import perf_counter
from typing import Any

from fastapi import APIRouter, Request

from modal_computer_use.daemon.auth import require_privileged_auth
from modal_computer_use.daemon.routes.execution import run_input_action
from modal_computer_use.daemon.routes.validation import map_e2big, validate_command_vector
from modal_computer_use.daemon.schemas import CommandRunRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_text

router = APIRouter(prefix="/v1/commands")


@router.post("/run")
async def run(payload: CommandRunRequest, request: Request) -> ActionResult:
    require_privileged_auth(request)
    started = perf_counter()
    validate_command_vector(
        payload.command,
        maximum_arguments=request.app.state.settings.max_command_arguments,
    )

    async def operation() -> ActionResult:
        try:
            result = await request.app.state.backend.run_command(
                payload.command,
                timeout=payload.timeout,
            )
        except OSError as exc:
            mapped = map_e2big(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        known_secrets = tuple(
            secret
            for secret in (
                request.app.state.settings.local_token,
                request.app.state.settings.tunnel_token,
                request.app.state.settings.vnc_password,
            )
            if secret
        )
        return ActionResult(
            ok=result.ok,
            message=_sanitize_command_text(result.message, known_secrets)
            if result.message is not None
            else None,
            elapsed_ms=result.elapsed_ms,
            output=_sanitize_command_output(result.output, known_secrets),
        )

    result = await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="command_failed",
        fallback_message="command failed",
    )
    return result.model_copy(update={"elapsed_ms": (perf_counter() - started) * 1000})


def _sanitize_command_text(value: str, known_secrets: tuple[str, ...]) -> str:
    sanitized = sanitize_text(value)
    for secret in known_secrets:
        sanitized = sanitized.replace(secret, "[redacted]")
    return sanitized


def _sanitize_command_output(value: Any, known_secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _sanitize_command_text(value, known_secrets)
    if isinstance(value, dict):
        return {
            key: _sanitize_command_output(item, known_secrets) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_command_output(item, known_secrets) for item in value]
    return value
