from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.schemas import RecordingStartRequest
from modal_computer_use.models import Recording

router = APIRouter(prefix="/v1/recordings")


@router.post("")
async def start(payload: RecordingStartRequest, request: Request) -> Recording:
    rec = request.app.state.recordings.start(
        name=payload.name, fps=payload.fps, format=payload.format
    )
    _enforce_recording_budget(request)
    return rec


@router.post("/{recording_id}/stop")
async def stop(recording_id: str, request: Request) -> Recording:
    rec = request.app.state.recordings.stop(recording_id)
    _enforce_recording_budget(request)
    _enforce_artifact_budget(request)
    return rec


@router.get("")
async def list_recordings(request: Request) -> list[Recording]:
    return request.app.state.recordings.list()


@router.get("/{recording_id}")
async def get(recording_id: str, request: Request) -> Recording:
    return request.app.state.recordings.get(recording_id)


@router.get("/{recording_id}/download")
async def download(recording_id: str, request: Request) -> FileResponse:
    rec = request.app.state.recordings.get(recording_id)
    path = Path(rec.path)
    if not path.is_file():
        raise FileNotFoundError(recording_id)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{rec.id}.{rec.format}"'},
    )


@router.delete("/{recording_id}")
async def delete(recording_id: str, request: Request) -> dict[str, bool]:
    request.app.state.recordings.delete(recording_id)
    return {"ok": True}


def _enforce_artifact_budget(request: Request) -> None:
    limit = request.app.state.settings.max_artifact_bytes
    if limit is None:
        return
    artifact_total = sum((item.size_bytes or 0) for item in request.app.state.artifacts.list())
    recording_total = request.app.state.recordings.total_size_bytes()
    if artifact_total + recording_total > limit:
        raise DaemonError("artifact byte budget exceeded", status_code=429, code="budget_exceeded")


def _enforce_recording_budget(request: Request) -> None:
    limit = request.app.state.settings.max_recording_seconds
    if limit is None:
        return
    if request.app.state.recordings.total_duration_seconds() > limit:
        raise DaemonError(
            "recording duration budget exceeded",
            status_code=429,
            code="budget_exceeded",
        )
