from __future__ import annotations

import base64
import json
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

    def post_bytes_with_headers(self, path: str, *, json: Any | None = None, headers=None):
        self.posts.append({"path": path, "json": json, "headers": headers})
        action_result = {
            "ok": True,
            "call_id": json.get("call_id") if isinstance(json, dict) else None,
            "results": [{"index": 0, "type": "move", "ok": True, "output": {}}],
        }
        return b"image-bytes", {
            "content-type": "image/png",
            "x-computer-use-width": "1024",
            "x-computer-use-height": "768",
            "x-computer-use-action-result": base64.b64encode(
                json_module_dumps(action_result).encode("utf-8")
            ).decode("ascii"),
            "x-computer-use-change-result": base64.b64encode(
                json_module_dumps({"detected": True, "attempts": 1}).encode("utf-8")
            ).decode("ascii"),
            "x-computer-use-change-timing-ms": json_module_dumps({"total_ms": 12.5}),
        }


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


def test_actions_namespace_run_and_screenshot_bytes_uses_raw_endpoint() -> None:
    client = _FakeClient()
    namespace = ActionsNamespace(client)  # type: ignore[arg-type]

    result = namespace.run_and_screenshot_bytes(
        [{"type": "move", "x": 1, "y": 2}],
        idempotency_key="idem",
        call_id="call_test",
    )

    assert result.data == b"image-bytes"
    assert result.width == 1024
    assert result.height == 768
    assert result.result.call_id == "call_test"
    assert client.posts[0]["path"] == "/v1/actions/run/raw-screenshot"
    assert client.posts[0]["headers"] == {"Idempotency-Key": "idem"}
    assert client.posts[0]["json"]["screenshot_after"] is True


def test_actions_namespace_run_and_observe_change_screenshot_bytes_uses_fast_path() -> None:
    client = _FakeClient()
    namespace = ActionsNamespace(client)  # type: ignore[arg-type]

    result = namespace.run_and_observe_change_screenshot_bytes(
        [{"type": "move", "x": 1, "y": 2}],
        previous_source_sha256="a" * 64,
        change_timeout_ms=25,
        poll_interval_ms=2,
        poll_strategy="adaptive",
        change_detection="auto_region",
        change_region_radius=64,
        change_signal="poll",
        idempotency_key="idem",
        call_id="call_test",
    )

    assert result.data == b"image-bytes"
    assert result.width == 1024
    assert result.height == 768
    assert result.result.call_id == "call_test"
    assert result.change_result == {"detected": True, "attempts": 1}
    assert result.change_timing_ms == {"total_ms": 12.5}
    assert client.posts[0]["path"] == "/v1/actions/run/observe-change/raw-screenshot"
    assert client.posts[0]["headers"] == {"Idempotency-Key": "idem"}
    assert client.posts[0]["json"]["screenshot_after"] is False
    assert client.posts[0]["json"]["previous_source_sha256"] == "a" * 64
    assert client.posts[0]["json"]["change_timeout_ms"] == 25
    assert client.posts[0]["json"]["poll_interval_ms"] == 2
    assert client.posts[0]["json"]["poll_strategy"] == "adaptive"
    assert client.posts[0]["json"]["change_detection"] == "auto_region"
    assert client.posts[0]["json"]["change_region_radius"] == 64
    assert client.posts[0]["json"]["change_signal"] == "poll"


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


def json_module_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))
