from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from modal_computer_use.models import Recording


class RecordingRegistry:
    def __init__(self) -> None:
        self._recordings: dict[str, Recording] = {}

    def start(self, *, name: str | None = None, fps: int = 12, format: str = "mp4") -> Recording:
        recording_id = f"rec_{uuid4().hex[:12]}"
        rec = Recording(
            id=recording_id,
            name=name,
            status="recording",
            format=format,
            fps=fps,
            path=f"recordings/{recording_id}.{format}",
            size_bytes=0,
            started_at=datetime.now(UTC),
        )
        self._recordings[recording_id] = rec
        return rec

    def stop(self, recording_id: str) -> Recording:
        rec = self._recordings[recording_id]
        stopped = rec.model_copy(
            update={
                "status": "stopped",
                "stopped_at": datetime.now(UTC),
                "duration_seconds": (datetime.now(UTC) - rec.started_at).total_seconds(),
            }
        )
        self._recordings[recording_id] = stopped
        return stopped

    def list(self) -> list[Recording]:
        return list(self._recordings.values())

    def get(self, recording_id: str) -> Recording:
        return self._recordings[recording_id]

    def delete(self, recording_id: str) -> None:
        del self._recordings[recording_id]
