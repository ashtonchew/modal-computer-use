from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.schemas import RecordingStartRequest
from modal_computer_use.models import Recording

router = APIRouter(prefix="/v1/recordings")


@router.post("")
async def start(payload: RecordingStartRequest, request: Request) -> Recording:
    rec = request.app.state.recordings.start(
        name=payload.name, fps=payload.fps, format=payload.format
    )
    budgets.enforce(request, "recordings")
    return rec


@router.post("/{recording_id}/stop")
async def stop(recording_id: str, request: Request) -> Recording:
    rec = request.app.state.recordings.stop(recording_id)
    budgets.enforce(request, "recordings", "artifacts")
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
