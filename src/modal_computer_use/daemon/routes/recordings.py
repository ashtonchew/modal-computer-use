from __future__ import annotations

from fastapi import APIRouter, Request, Response

from modal_computer_use.daemon.schemas import RecordingStartRequest
from modal_computer_use.models import Recording

router = APIRouter(prefix="/v1/recordings")


@router.post("")
async def start(payload: RecordingStartRequest, request: Request) -> Recording:
    return request.app.state.recordings.start(
        name=payload.name, fps=payload.fps, format=payload.format
    )


@router.post("/{recording_id}/stop")
async def stop(recording_id: str, request: Request) -> Recording:
    return request.app.state.recordings.stop(recording_id)


@router.get("")
async def list_recordings(request: Request) -> list[Recording]:
    return request.app.state.recordings.list()


@router.get("/{recording_id}")
async def get(recording_id: str, request: Request) -> Recording:
    return request.app.state.recordings.get(recording_id)


@router.get("/{recording_id}/download")
async def download(recording_id: str, request: Request) -> Response:
    rec = request.app.state.recordings.get(recording_id)
    return Response(
        f"mock recording {rec.id}\n".encode(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{rec.id}.{rec.format}"'},
    )


@router.delete("/{recording_id}")
async def delete(recording_id: str, request: Request) -> dict[str, bool]:
    request.app.state.recordings.delete(recording_id)
    return {"ok": True}
