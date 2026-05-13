from __future__ import annotations

from pathlib import Path

from modal_computer_use.models import Recording

from .base import Namespace


class RecordingsNamespace(Namespace):
    def start(self, name: str | None = None, fps: int = 12, format: str = "mp4") -> Recording:
        return Recording.model_validate(
            self._client.post_json(
                "/v1/recordings", json={"name": name, "fps": fps, "format": format}
            )
        )

    def stop(self, recording_id: str) -> Recording:
        return Recording.model_validate(
            self._client.post_json(f"/v1/recordings/{recording_id}/stop")
        )

    def list(self) -> list[Recording]:
        return [Recording.model_validate(item) for item in self._client.get_json("/v1/recordings")]

    def get(self, recording_id: str) -> Recording:
        return Recording.model_validate(self._client.get_json(f"/v1/recordings/{recording_id}"))

    def download(self, recording_id: str, local_path: str | Path) -> Path:
        return self._client.download(f"/v1/recordings/{recording_id}/download", local_path)

    def delete(self, recording_id: str) -> None:
        self._client.delete_json(f"/v1/recordings/{recording_id}")
