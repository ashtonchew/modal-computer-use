from __future__ import annotations

from pathlib import Path
from typing import Any

from modal_computer_use.namespaces.actions import ActionsNamespace
from modal_computer_use.namespaces.recordings import RecordingsNamespace
from modal_computer_use.namespaces.screenshots import ScreenshotsNamespace


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.downloads: list[tuple[str, Path]] = []

    def post_json(self, path: str, *, json: Any | None = None, headers=None):
        self.posts.append({"path": path, "json": json, "headers": headers})
        if path == "/v1/actions/validate":
            return {"ok": True, "errors": []}
        return {
            "ok": True,
            "call_id": json.get("call_id") if isinstance(json, dict) else None,
            "results": [{"index": 0, "type": "move", "ok": True, "output": {}}],
        }

    def download(self, path: str, local_path: str | Path) -> Path:
        target = Path(local_path)
        self.downloads.append((path, target))
        return target

    def get_bytes(self, *_args, **_kwargs):
        raise AssertionError("recording downloads must stream instead of buffering")

    def post_bytes(self, path: str, *, json: Any | None = None, headers=None):
        self.posts.append({"path": path, "json": json, "headers": headers})
        return b"image-bytes"


def test_actions_namespace_forwards_batch_timeout_and_trace_metadata() -> None:
    client = _FakeClient()
    namespace = ActionsNamespace(client)  # type: ignore[arg-type]

    namespace.run(
        [{"type": "move", "x": 1, "y": 2}],
        max_action_timeout_ms=123,
        idempotency_key="idem",
        call_id="call_test",
        run_id="run_test",
        sequence=7,
    )

    payload = client.posts[0]["json"]
    assert payload["max_action_timeout_ms"] == 123
    assert payload["call_id"] == "call_test"
    assert payload["run_id"] == "run_test"
    assert payload["sequence"] == 7
    assert client.posts[0]["headers"] == {"Idempotency-Key": "idem"}


def test_actions_namespace_validate_accepts_full_batch_options() -> None:
    client = _FakeClient()
    namespace = ActionsNamespace(client)  # type: ignore[arg-type]

    namespace.validate(
        [{"type": "move", "x": 1, "y": 2}],
        screenshot_after=True,
        max_action_timeout_ms=123,
    )

    payload = client.posts[0]["json"]
    assert client.posts[0]["path"] == "/v1/actions/validate"
    assert payload["screenshot_after"] is True
    assert payload["max_action_timeout_ms"] == 123


def test_recordings_namespace_download_streams_to_target(tmp_path) -> None:
    client = _FakeClient()
    namespace = RecordingsNamespace(client)  # type: ignore[arg-type]
    target = tmp_path / "recording.mp4"

    result = namespace.download("rec_123", target)

    assert result == target
    assert client.downloads == [("/v1/recordings/rec_123/download", target)]


def test_screenshots_namespace_full_bytes_uses_raw_endpoint() -> None:
    client = _FakeClient()
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    result = namespace.full_bytes(format="jpeg", quality=80, scale=0.5)

    assert result == b"image-bytes"
    assert client.posts == [
        {
            "path": "/v1/screenshots/full/raw",
            "json": {
                "format": "jpeg",
                "quality": 80,
                "scale": 0.5,
                "show_cursor": False,
                "processing": "auto",
                "storage": "inline",
            },
            "headers": None,
        }
    ]
