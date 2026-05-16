from __future__ import annotations

from html import escape
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from modal_computer_use.daemon.routes.execution import run_idle_only_mutation, run_recording_start
from modal_computer_use.daemon.schemas import RecordingStartRequest
from modal_computer_use.models import Recording

router = APIRouter(prefix="/v1/recordings")
dashboard_router = APIRouter()


@router.post("")
async def start(payload: RecordingStartRequest, request: Request) -> Recording:
    async def operation() -> Recording:
        return request.app.state.recordings.start(
            name=payload.name, fps=payload.fps, format=payload.format
        )

    return await run_recording_start(
        request,
        operation,
        rollback=lambda rec: request.app.state.recordings.delete(rec.id),
    )


@router.post("/{recording_id}/stop")
async def stop(recording_id: str, request: Request) -> Recording:
    async def operation() -> Recording:
        return request.app.state.recordings.stop(recording_id, append_manifest=False)

    rec = await run_idle_only_mutation(
        request,
        operation,
        enforce_after=("recordings", "artifacts"),
        rollback=lambda _rec: request.app.state.recordings.delete(recording_id),
    )
    request.app.state.recordings.append_manifest(recording_id)
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
    async def operation() -> dict[str, bool]:
        request.app.state.recordings.delete(recording_id)
        return {"ok": True}

    return await run_idle_only_mutation(request, operation)


@dashboard_router.get("/recordings/ui", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    recordings = request.app.state.recordings.list()
    rows = "\n".join(_recording_row(recording) for recording in recordings)
    if not rows:
        rows = '<tr><td colspan="9" class="empty">No recordings</td></tr>'
    return HTMLResponse(
        _dashboard_html(rows),
        headers={"Cache-Control": "no-store"},
    )


def _recording_row(recording: Recording) -> str:
    recording_id = escape(recording.id)
    name = escape(recording.name or "")
    size = "" if recording.size_bytes is None else str(recording.size_bytes)
    duration = (
        ""
        if recording.duration_seconds is None
        else f"{recording.duration_seconds:.3f}".rstrip("0").rstrip(".")
    )
    started = escape(recording.started_at.isoformat())
    stopped = escape(recording.stopped_at.isoformat() if recording.stopped_at else "")
    status = escape(recording.status)
    digest = escape(recording.sha256 or "")
    artifact_backed = "yes" if recording.artifact_uri else "no"
    return f"""
      <tr>
        <td>{name or recording_id}</td>
        <td>{status}</td>
        <td>{size}</td>
        <td>{duration}</td>
        <td>{started}</td>
        <td>{stopped}</td>
        <td class="digest">{digest}</td>
        <td class="artifact">{artifact_backed}</td>
        <td class="actions">
          <a href="/v1/recordings/{recording_id}/download">download</a>
          <a href="/v1/recordings/{recording_id}" data-method="DELETE">delete API</a>
        </td>
      </tr>
    """


def _dashboard_html(rows: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Recordings</title>
  <style>
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #1f2933;
      background: #f6f8fa;
    }}
    main {{
      padding: 24px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: white;
      border: 1px solid #d8dee4;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid #d8dee4;
      text-align: left;
      font-size: 14px;
      vertical-align: top;
    }}
    th {{
      font-size: 12px;
      text-transform: uppercase;
      color: #57606a;
      background: #f6f8fa;
    }}
    .digest, .artifact {{
      max-width: 260px;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }}
    .actions {{
      white-space: nowrap;
    }}
    a {{
      margin-right: 8px;
      color: #0969da;
      font: inherit;
    }}
    .empty {{
      color: #57606a;
      text-align: center;
    }}
  </style>
</head>
<body>
  <main>
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Status</th>
          <th>Size</th>
          <th>Duration</th>
          <th>Created</th>
          <th>Stopped</th>
          <th>SHA-256</th>
          <th>Artifact URI</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
  </main>
</body>
</html>"""
