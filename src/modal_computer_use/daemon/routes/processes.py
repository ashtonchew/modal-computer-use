from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import run_idle_only_mutation
from modal_computer_use.daemon.routes.lifecycle import mutate_display_generation
from modal_computer_use.models import ProcessStatus
from modal_computer_use.redaction import sanitize_text

router = APIRouter(prefix="/v1/processes")
LogTail = Annotated[int, Query(ge=1, le=1000)]


@router.get("/{name}/status")
async def process_status(name: str, request: Request) -> ProcessStatus:
    return request.app.state.supervisor.status(name)


@router.post("/{name}/restart", responses={404: {"description": "Unknown process"}})
async def process_restart(name: str, request: Request) -> ProcessStatus:
    if name not in request.app.state.supervisor.names:
        raise DaemonError(
            "unknown process",
            status_code=404,
            code="unknown_process",
            details={"name": name},
        )

    async def operation() -> ProcessStatus:
        if name == "xvfb":
            await mutate_display_generation(
                request,
                lambda: request.app.state.supervisor.restart(name),
                verify_readiness=True,
            )
        else:
            await request.app.state.supervisor.restart(name)
        return request.app.state.supervisor.status(name)

    return await run_idle_only_mutation(
        request,
        operation,
        semantic_data={"process": name},
    )


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
