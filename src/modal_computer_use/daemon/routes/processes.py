from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from modal_computer_use.daemon import budgets
from modal_computer_use.models import ProcessStatus
from modal_computer_use.redaction import sanitize_text

router = APIRouter(prefix="/v1/processes")
LogTail = Annotated[int, Query(ge=1, le=1000)]


@router.get("/{name}/status")
async def process_status(name: str, request: Request) -> ProcessStatus:
    return request.app.state.supervisor.status(name)


@router.post("/{name}/restart")
async def process_restart(name: str, request: Request) -> ProcessStatus:
    budgets.enforce_idle(request)
    await request.app.state.supervisor.restart(name)
    budgets.touch_activity(request)
    return request.app.state.supervisor.status(name)


@router.get("/{name}/logs")
async def process_logs(name: str, request: Request, tail: LogTail = 200) -> Response:
    return Response(
        sanitize_text(request.app.state.supervisor.logs(name, tail=tail)),
        media_type="text/plain",
    )


@router.get("/{name}/stderr")
async def process_stderr(name: str, request: Request, tail: LogTail = 200) -> Response:
    return Response(
        sanitize_text(request.app.state.supervisor.stderr(name, tail=tail)),
        media_type="text/plain",
    )


@router.get("/{name}/errors")
async def process_errors(name: str, request: Request, tail: LogTail = 200) -> Response:
    return Response(
        sanitize_text(request.app.state.supervisor.stderr(name, tail=tail)),
        media_type="text/plain",
    )
