from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from modal_computer_use.daemon import budgets
from modal_computer_use.errors import ArtifactPathError
from modal_computer_use.models import ArtifactInfo, ArtifactSyncResult

router = APIRouter(prefix="/v1/artifacts")


@router.get("")
async def list_artifacts(request: Request, prefix: str = "") -> list[ArtifactInfo]:
    return request.app.state.artifacts.list(prefix)


@router.get("/manifest")
async def manifest(request: Request, prefix: str = "") -> list[ArtifactInfo]:
    return request.app.state.artifacts.manifest(prefix)


@router.post("/sync")
async def sync(request: Request) -> ArtifactSyncResult:
    with request.app.state.tracer.span("daemon.artifact.sync"):
        return request.app.state.artifacts.sync()


@router.get("/{path:path}")
async def read_artifact(path: str, request: Request) -> Response:
    data = request.app.state.artifacts.read_bytes(path)
    return Response(data, media_type="application/octet-stream")


@router.put("/{path:path}")
async def write_artifact(path: str, request: Request) -> ArtifactInfo:
    try:
        data = await request.body()
        with request.app.state.tracer.span(
            "daemon.artifact.write",
            {"artifact.size_bytes": len(data)},
        ):
            budgets.enforce_artifact_write(request, path, len(data))
            info = request.app.state.artifacts.write_bytes(
                path,
                data,
                content_type=request.headers.get("content-type"),
            )
            budgets.enforce(request, "artifacts")
            return info
    except ArtifactPathError:
        raise


@router.delete("/{path:path}")
async def delete_artifact(path: str, request: Request) -> dict[str, bool]:
    request.app.state.artifacts.delete(path)
    return {"ok": True}
