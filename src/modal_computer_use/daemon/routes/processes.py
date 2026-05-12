from __future__ import annotations

from fastapi import APIRouter, Request, Response

from modal_computer_use.models import ProcessStatus

router = APIRouter(prefix="/v1/processes")


@router.get("/{name}/status")
async def process_status(name: str, request: Request) -> ProcessStatus:
    return request.app.state.supervisor.status(name)


@router.post("/{name}/restart")
async def process_restart(name: str, request: Request) -> ProcessStatus:
    await request.app.state.supervisor.restart(name)
    return request.app.state.supervisor.status(name)


@router.get("/{name}/logs")
async def process_logs(name: str, request: Request, tail: int = 200) -> Response:
    return Response(request.app.state.supervisor.logs(name, tail=tail), media_type="text/plain")


@router.get("/{name}/stderr")
async def process_stderr(name: str, request: Request, tail: int = 200) -> Response:
    return Response(request.app.state.supervisor.stderr(name, tail=tail), media_type="text/plain")


@router.get("/{name}/errors")
async def process_errors(name: str, request: Request, tail: int = 200) -> Response:
    return Response(request.app.state.supervisor.stderr(name, tail=tail), media_type="text/plain")
