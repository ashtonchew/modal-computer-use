from __future__ import annotations

import base64
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from modal_computer_use.models import ScreenshotOptions
from modal_computer_use.namespaces.actions import ActionsNamespace, AsyncActionsNamespace
from modal_computer_use.namespaces.artifacts import ArtifactsNamespace
from modal_computer_use.namespaces.recordings import RecordingsNamespace
from modal_computer_use.namespaces.screenshots import ScreenshotsNamespace


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.downloads: list[tuple[str, Path]] = []
        self.request_timeouts: list[float | None] = []

    def post_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers=None,
        _mutation: bool = False,
        _timeout: float | None = None,
    ):
        self.request_timeouts.append(_timeout)
        self.posts.append({"path": path, "json": json, "headers": headers})
        if path == "/v1/actions/validate":
            return {"ok": True, "errors": []}
        if path == "/v1/artifacts/sync":
            return {"ok": True, "persistent": True, "synced_paths": ["artifact-root"]}
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

    def post_bytes(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers=None,
        _mutation: bool = False,
    ):
        self.posts.append({"path": path, "json": json, "headers": headers})
        return b"image-bytes"

    def post_bytes_with_headers(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers=None,
        _mutation: bool = False,
    ):
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

    options = {
        "continue_on_error": True,
        "screenshot_after": True,
        "screenshot_options": ScreenshotOptions(format="jpeg", quality=80),
        "max_action_timeout_ms": 123,
        "call_id": "call_validate",
        "run_id": "run_validate",
        "sequence": 7,
        "source": "test",
    }
    actions = [{"type": "move", "x": 1, "y": 2}]
    namespace.validate(actions, **options)
    namespace.run(actions, **options)

    assert client.posts[0]["path"] == "/v1/actions/validate"
    payload = client.posts[0]["json"]
    assert payload == client.posts[1]["json"]
    assert payload["continue_on_error"] is True
    assert payload["screenshot_after"] is True
    assert payload["screenshot_options"]["format"] == "jpeg"
    assert payload["screenshot_options"]["quality"] == 80
    assert payload["max_action_timeout_ms"] == 123
    assert payload["call_id"] == "call_validate"
    assert payload["run_id"] == "run_validate"
    assert payload["sequence"] == 7
    assert payload["source"] == "test"


def test_actions_namespace_run_and_screenshot_bytes_uses_raw_endpoint() -> None:
    client = _FakeClient()
    namespace = ActionsNamespace(client)  # type: ignore[arg-type]

    result = namespace.run_and_screenshot_bytes(
        [{"type": "move", "x": 1, "y": 2}],
        call_id="call_test",
    )

    assert result.data == b"image-bytes"
    assert result.width == 1024
    assert result.height == 768
    assert result.result.call_id == "call_test"
    assert client.posts[0]["path"] == "/v1/actions/run/raw-screenshot"
    assert client.posts[0]["headers"] is None
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
        call_id="call_test",
    )

    assert result.data == b"image-bytes"
    assert result.width == 1024
    assert result.height == 768
    assert result.result.call_id == "call_test"
    assert result.change_result == {"detected": True, "attempts": 1}
    assert result.change_timing_ms == {"total_ms": 12.5}
    assert client.posts[0]["path"] == "/v1/actions/run/observe-change/raw-screenshot"
    assert client.posts[0]["headers"] is None
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


def test_artifact_sync_uses_extended_request_timeout() -> None:
    client = _FakeClient()
    namespace = ArtifactsNamespace(client)  # type: ignore[arg-type]

    result = namespace.sync()

    assert result.ok is True
    assert client.posts[0]["path"] == "/v1/artifacts/sync"
    assert client.request_timeouts == [60.0]


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


class _AsyncFakeClient(_FakeClient):
    async def post_bytes_with_headers(self, *args, **kwargs):
        return super().post_bytes_with_headers(*args, **kwargs)


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def action_namespace(request):
    client = _AsyncFakeClient() if request.param else _FakeClient()
    namespace_type = AsyncActionsNamespace if request.param else ActionsNamespace
    return namespace_type(client), client


async def _invoke_raw(namespace, observe: bool):
    method = (
        namespace.run_and_observe_change_screenshot_bytes
        if observe
        else namespace.run_and_screenshot_bytes
    )
    result = method([{"type": "move", "x": 1, "y": 2}], call_id="call_test")
    return await result if inspect.isawaitable(result) else result


@pytest.mark.asyncio
@pytest.mark.parametrize("observe", [False, True], ids=["raw", "observe-change"])
async def test_action_screenshot_preserves_defaults_and_failed_batch(
    action_namespace, monkeypatch, observe
) -> None:
    namespace, client = action_namespace
    response_headers = {
        "x-computer-use-width": "unknown",
        "x-computer-use-action-result": base64.b64encode(
            json.dumps(
                {
                    "ok": False,
                    "results": [
                        {"index": 0, "type": "move", "ok": False, "error": "action failed"}
                    ],
                }
            ).encode()
        ).decode(),
        "x-computer-use-change-result": base64.b64encode(b'{"detected": false}').decode(),
        "x-computer-use-change-timing-ms": '{"total_ms": 2, "valid": true, "text": "3"}',
    }
    original = client.post_bytes_with_headers

    async def response(*args, **kwargs):
        assert kwargs["_mutation"] is True
        pending = original(*args, **kwargs)
        if inspect.isawaitable(pending):
            await pending
        return b"frame", response_headers

    def sync_response(*args, **kwargs):
        assert kwargs["_mutation"] is True
        original(*args, **kwargs)
        return b"frame", response_headers

    monkeypatch.setattr(
        client,
        "post_bytes_with_headers",
        response if isinstance(client, _AsyncFakeClient) else sync_response,
    )
    result = await _invoke_raw(namespace, observe)
    assert (result.data, result.size_bytes, result.format) == (b"frame", 5, "png")
    assert result.width is None and result.height is None
    assert result.result.ok is False
    assert result.result.results[0].error == "action failed"
    assert result.change_result == ({"detected": False} if observe else None)
    assert result.change_timing_ms == ({"total_ms": 2.0} if observe else None)
    expected_route = "observe-change/raw-screenshot" if observe else "raw-screenshot"
    assert client.posts[0]["path"] == f"/v1/actions/run/{expected_route}"
    assert client.posts[0]["json"]["screenshot_after"] is (not observe)
    assert client.posts[0]["json"]["call_id"] == "call_test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observe", "encoded_result", "error", "error_type"),
    [
        (False, None, ValidationError, "missing"),
        (False, base64.b64encode(b"null").decode(), ValidationError, "model_type"),
        (True, base64.b64encode(b"null").decode(), ValidationError, "missing"),
        (True, base64.b64encode(b"invalid json").decode(), json.JSONDecodeError, None),
    ],
)
async def test_action_screenshot_preserves_result_decoding_errors(
    action_namespace, monkeypatch, observe, encoded_result, error, error_type
) -> None:
    namespace, client = action_namespace
    headers = {} if encoded_result is None else {"x-computer-use-action-result": encoded_result}

    def response(*_args, **_kwargs):
        return b"frame", headers

    async def async_response(*args, **kwargs):
        return response(*args, **kwargs)

    monkeypatch.setattr(
        client,
        "post_bytes_with_headers",
        async_response if isinstance(client, _AsyncFakeClient) else response,
    )
    with pytest.raises(error) as exc:
        await _invoke_raw(namespace, observe)
    if error_type is not None:
        assert exc.value.errors()[0]["type"] == error_type
