from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from io import BytesIO
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.desktop.screenshots import CapturedRawScreenshot, CapturedScreenshot
from modal_computer_use.daemon.routes import observations as observation_routes
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.errors import AuthenticationError, DaemonHTTPError
from modal_computer_use.models import CoordinateSpace, Region, sha256_bytes
from modal_computer_use.observations import ActionObservationResult, ObservationClient
from modal_computer_use.transports.observation import (
    ObservationFrame,
    ObservationStreamTransport,
    _decode_frame_envelope,
    _websocket_url,
)


def _app(tmp_path, **overrides):
    return create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            **overrides,
        )
    )


def test_observation_websocket_url_preserves_base_path_and_query() -> None:
    assert _websocket_url(
        "https://connect.modal.run/abc123?workspace=ws&_modal_connect_token=secret",
        "/v1/observations/stream",
    ) == (
        "wss://connect.modal.run/abc123/v1/observations/stream"
        "?workspace=ws&_modal_connect_token=secret"
    )


def test_observation_stream_rejects_missing_auth(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/v1/observations/stream"),
    ):
        pass

    assert exc.value.code == 1008


def test_observation_stream_connection_limit_is_global(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev", max_observation_connections=1)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as first,
    ):
        assert first.receive_json()["type"] == "ready"
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/v1/observations/stream"),
        ):
            pass

    assert exc_info.value.code == 1013


def test_observation_stream_rejects_display_restart_in_progress(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")
    app.state.display_restart_in_progress = True

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/v1/observations/stream"),
    ):
        pass

    assert exc_info.value.code == 1013
    assert exc_info.value.reason == "display_restart_busy"


def test_observation_stream_rechecks_restart_after_admission(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path, local_token="dev")
    released: list[str] = []

    async def acquire(kind: str) -> bool:
        assert kind == "observation"
        app.state.display_restart_in_progress = True
        return True

    async def release(kind: str) -> None:
        released.append(kind)

    monkeypatch.setattr(app.state.websocket_admission, "acquire", acquire)
    monkeypatch.setattr(app.state.websocket_admission, "release", release)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/v1/observations/stream"),
    ):
        pass

    assert exc_info.value.code == 1013
    assert exc_info.value.reason == "display_restart_busy"
    assert released == ["observation"]


def test_observation_stream_sends_metadata_then_binary_frame(test_client) -> None:
    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 10,
                    "max_frames": 1,
                },
            }
        )
        started = websocket.receive_json()
        header = websocket.receive_json()
        payload = websocket.receive_bytes()
        stopped = websocket.receive_json()

    assert started["type"] == "started"
    assert started["protocol"] == "computer-use.observation-stream.v1"
    assert header["type"] == "frame"
    assert header["seq"] == 1
    assert header["emit_version"] == 1
    assert header["source_version"] == 1
    assert header["previous_source_version"] == 0
    assert header["delivery"] == "latest"
    assert header["content_type"] == "image/png"
    assert header["width"] == 1024
    assert header["height"] == 768
    assert header["timing_ms"]["observation_total_ms"] >= 0
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert stopped["type"] == "stopped"
    assert stopped["reason"] == "max_frames"


def test_observation_stream_can_emit_transport_timing(test_client) -> None:
    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 10,
                    "max_frames": 1,
                    "transport_timing": True,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        header = websocket.receive_json()
        assert websocket.receive_bytes()
        timing = websocket.receive_json()
        stopped = websocket.receive_json()

    assert header["type"] == "frame"
    assert timing["type"] == "transport_timing"
    assert timing["seq"] == header["seq"]
    assert timing["server_emit_timing_ms"]["metadata_send_ms"] >= 0
    assert timing["server_emit_timing_ms"]["payload_send_ms"] >= 0
    assert timing["server_emit_timing_ms"]["emit_total_ms"] >= 0
    assert stopped["type"] == "stopped"


def test_observation_stream_can_emit_binary_envelope(test_client) -> None:
    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 10,
                    "max_frames": 1,
                    "transport_timing": True,
                    "frame_encoding": "binary-envelope",
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        envelope = websocket.receive_bytes()
        timing = websocket.receive_json()
        stopped = websocket.receive_json()

    header, payload = _decode_frame_envelope(envelope)
    assert header["type"] == "frame"
    assert header["seq"] == 1
    assert header["frame_encoding"] == "binary-envelope"
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert timing["type"] == "transport_timing"
    assert timing["seq"] == header["seq"]
    assert timing["server_emit_timing_ms"]["emit_total_ms"] >= 0
    assert stopped["type"] == "stopped"


def test_observation_stream_action_observe_can_override_frame_encoding(test_client) -> None:
    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 0.01,
                    "frame_encoding": "json-binary",
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        initial = websocket.receive_json()
        assert initial["frame_encoding"] == "json-binary"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "change_timeout_ms": 0,
                    "frame_encoding": "binary-envelope",
                    "source": "test",
                },
            }
        )
        envelope = websocket.receive_bytes()

    header, payload = _decode_frame_envelope(envelope)
    assert header["id"] == "2"
    assert header["trigger"] == "run_actions_observe_change"
    assert header["frame_encoding"] == "binary-envelope"
    assert header["frame_encoding_override"] == "binary-envelope"
    assert payload == b""


def test_observation_stream_transport_probe(test_client) -> None:
    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "transport_probe",
                "payload": {"size_bytes": 128},
            }
        )
        header = websocket.receive_json()
        payload = websocket.receive_bytes()
        timing = websocket.receive_json()

    assert header == {
        "type": "transport_probe",
        "id": "1",
        "ok": True,
        "size_bytes": 128,
        "frame_encoding": "json-binary",
    }
    assert payload == b"\0" * 128
    assert timing["type"] == "transport_timing"
    assert timing["id"] == "1"
    assert timing["server_emit_timing_ms"]["payload_send_ms"] >= 0


def test_observation_stream_transport_probe_can_use_binary_envelope(test_client) -> None:
    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "transport_probe",
                "payload": {"size_bytes": 128, "frame_encoding": "binary-envelope"},
            }
        )
        envelope = websocket.receive_bytes()
        timing = websocket.receive_json()

    header, payload = _decode_frame_envelope(envelope)
    assert header == {
        "type": "transport_probe",
        "id": "1",
        "ok": True,
        "size_bytes": 128,
        "frame_encoding": "binary-envelope",
    }
    assert payload == b"\0" * 128
    assert timing["type"] == "transport_timing"
    assert timing["id"] == "1"
    assert timing["server_emit_timing_ms"]["payload_send_ms"] == 0.0


def test_observation_http_transport_probe(test_client) -> None:
    response = test_client.post("/v1/observations/transport-probe", json={"size_bytes": 128})

    assert response.status_code == 200
    assert response.content == b"\0" * 128
    assert response.headers["x-computer-use-size-bytes"] == "128"
    timing = json.loads(response.headers["x-computer-use-transport-timing-ms"])
    assert timing["emit_total_ms"] >= 0


def test_observation_stream_screenshot_budget_blocks_first_frame(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev", max_screenshots=0)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {"format": "png", "max_frames": 1},
            }
        )
        started = websocket.receive_json()
        error = websocket.receive_json()

    assert started["type"] == "started"
    assert error["type"] == "error"
    assert error["error"]["code"] == "budget_exceeded"


def test_observation_stream_suppresses_unchanged_binary_frames(test_client, monkeypatch) -> None:
    decode_count = 0
    original_decode = observation_routes._decode_image

    def counting_decode(data: bytes):
        nonlocal decode_count
        decode_count += 1
        return original_decode(data)

    monkeypatch.setattr(observation_routes, "_decode_image", counting_decode)

    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 30,
                    "max_frames": 2,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        first = websocket.receive_json()
        assert websocket.receive_bytes()
        second = websocket.receive_json()
        websocket.send_json({"id": "2", "op": "stop", "payload": {}})
        stopped = websocket.receive_json()

    assert first["type"] == "frame"
    assert second["type"] == "unchanged"
    assert second["unchanged"] is True
    assert stopped["type"] in {"result", "stopped"}
    assert decode_count == 1


def test_observation_stream_sends_patch_for_small_dirty_region(test_client) -> None:
    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": True,
                    "fps": 1,
                    "max_frames": 2,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        first = websocket.receive_json()
        assert websocket.receive_bytes()
        test_client.post("/v1/mouse/move", json={"x": 100, "y": 100})
        second = websocket.receive_json()
        patch = websocket.receive_bytes()
        stopped = websocket.receive_json()

    assert first["kind"] == "keyframe"
    assert second["kind"] == "patch"
    assert second["dirty_rect"]["width"] > 0
    assert second["dirty_ratio"] < 0.35
    assert len(patch) < second["full_size_bytes"]
    assert stopped["reason"] == "max_frames"


def test_observation_stream_sends_keyframe_for_large_dirty_region(app) -> None:
    captures = iter(
        [
            _screenshot_bytes("white"),
            _screenshot_bytes("black"),
        ]
    )

    async def screenshot_bytes(*_args, **_kwargs):
        return next(captures)

    app.state.backend.screenshot_bytes = screenshot_bytes

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "id": "1",
                    "op": "start",
                    "payload": {
                        "format": "png",
                        "show_cursor": False,
                        "fps": 30,
                        "max_frames": 2,
                        "delta_max_ratio": 0.1,
                    },
                }
            )
            assert websocket.receive_json()["type"] == "started"
            first = websocket.receive_json()
            assert websocket.receive_bytes()
            second = websocket.receive_json()
            payload = websocket.receive_bytes()
            stopped = websocket.receive_json()

    assert first["kind"] == "keyframe"
    assert second["kind"] == "keyframe"
    assert second["dirty_ratio"] == 1.0
    assert len(payload) == second["full_size_bytes"]
    assert stopped["reason"] == "max_frames"


def test_observation_stream_raw_path_suppresses_unchanged_without_png_encode(
    app,
    monkeypatch,
) -> None:
    raw = _raw_screenshot_bytes("white")
    encode_count = 0

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return raw

    async def screenshot_bytes(*_args, **_kwargs):
        raise AssertionError("raw observation path should not call encoded screenshot capture")

    def encode_rgb_png(rgb: bytes, size: tuple[int, int]) -> bytes:
        nonlocal encode_count
        encode_count += 1
        return b"encoded:" + rgb[:1] + str(size).encode()

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    app.state.backend.screenshot_bytes = screenshot_bytes
    monkeypatch.setattr(observation_routes, "encode_rgb_png", encode_rgb_png)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {"format": "png", "show_cursor": False, "fps": 30, "max_frames": 2},
            }
        )
        assert websocket.receive_json()["type"] == "started"
        first = websocket.receive_json()
        assert websocket.receive_bytes()
        second = websocket.receive_json()
        stopped = websocket.receive_json()

    assert first["kind"] == "keyframe"
    assert second["kind"] == "delta-suppressed"
    assert second["size_bytes"] == 0
    assert second["timing_ms"]["diff_ms"] == 0.0
    assert encode_count == 1
    assert stopped["reason"] == "max_frames"


def test_changed_delta_suppressed_frame_keeps_payload_boundary() -> None:
    raw = _raw_screenshot_bytes("white")
    request = observation_routes.ObservationStreamRequest()
    options = observation_routes.ScreenshotOptions(format="png", show_cursor=False)

    metadata, payload = observation_routes._raw_metadata(
        raw=raw,
        request=request,
        options=options,
        stream_id="stream-1",
        seq=2,
        kind="delta-suppressed",
        payload=b"",
        payload_sha256=raw.sha256,
        full_size_bytes=None,
        unchanged=False,
        dirty_rect=None,
        dirty_ratio=0.0,
        previous_seq=1,
        timing={"diff_ms": 1.0},
        captured_started=0.0,
        current_tile_hashes=None,
    )

    assert metadata["type"] == "frame"
    assert metadata["unchanged"] is False
    assert payload == b""


def test_observation_stream_raw_path_uses_tile_aligned_patch(app, monkeypatch) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    def encode_rgb_png(rgb: bytes, size: tuple[int, int]) -> bytes:
        return b"encoded:" + str(size).encode() + b":" + rgb[:3]

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "encode_rgb_png", encode_rgb_png)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 30,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        first = websocket.receive_json()
        assert websocket.receive_bytes()
        second = websocket.receive_json()
        patch = websocket.receive_bytes()
        stopped = websocket.receive_json()

    assert first["kind"] == "keyframe"
    assert second["kind"] == "patch"
    assert second["dirty_rect"] == {"x": 16, "y": 16, "width": 16, "height": 16}
    assert second["dirty_ratio"] == 0.0625
    assert patch.startswith(b"encoded:(16, 16):")
    assert stopped["reason"] == "max_frames"


def test_observation_stream_raw_path_uses_lossless_multi_rect_patches(app, monkeypatch) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_two_squares(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    def encode_rgb_png(rgb: bytes, size: tuple[int, int]) -> bytes:
        return b"encoded:" + str(size).encode() + b":" + rgb[:3]

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "encode_rgb_png", encode_rgb_png)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 30,
                    "max_frames": 2,
                    "tile_size": 16,
                    "max_patch_rects": 4,
                    "multi_rect_min_savings": 0.1,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        first = websocket.receive_json()
        assert websocket.receive_bytes()
        second = websocket.receive_json()
        patch_bundle = websocket.receive_bytes()
        stopped = websocket.receive_json()

    assert first["kind"] == "keyframe"
    assert second["kind"] == "patches"
    assert second["dirty_rect"] == {"x": 0, "y": 0, "width": 64, "height": 64}
    assert second["patch_count"] == 2
    assert second["patch_rects"] == [
        {"x": 0, "y": 0, "width": 16, "height": 16},
        {"x": 48, "y": 48, "width": 16, "height": 16},
    ]
    assert second["dirty_ratio"] == 0.125
    assert b"encoded:(16, 16):" in patch_bundle
    assert stopped["reason"] == "max_frames"


def test_observation_stream_reports_tile_hash_backend(app) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 30,
                    "max_frames": 2,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        first = websocket.receive_json()
        assert websocket.receive_bytes()
        second = websocket.receive_json()
        assert websocket.receive_bytes()

    assert first["tile_hash_backend"] in {"xxh3", "blake2b"}
    assert second["tile_hash_backend"] in {"xxh3", "blake2b"}


def test_observation_stream_capture_now_emits_immediate_frame(app) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        first = websocket.receive_json()
        assert websocket.receive_bytes()
        websocket.send_json({"id": "2", "op": "capture_now", "payload": {}})
        second = websocket.receive_json()
        assert websocket.receive_bytes()

    assert first["trigger"] == "start"
    assert second["trigger"] == "capture_now"
    assert second["id"] == "2"
    assert second["kind"] == "patch"


def test_observation_stream_run_actions_capture_emits_action_result(app) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_capture",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "capture_delay_ms": 0,
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        assert websocket.receive_bytes()

    assert second["trigger"] == "run_actions_capture"
    assert second["id"] == "2"
    assert second["kind"] == "patch"
    assert second["capture_delay_ms"] == 0
    assert second["action_result"]["ok"] is True
    assert second["action_result"]["results"][0]["type"] == "wait"


def test_observation_stream_run_actions_observe_change_waits_for_change(app) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 1,
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        assert websocket.receive_bytes()

    assert second["trigger"] == "run_actions_observe_change"
    assert second["id"] == "2"
    assert second["action_id"] == "2"
    assert second["causal_frame"] is True
    assert second["kind"] == "patch"
    assert second["change_detected"] is True
    assert second["change_timeout_reached"] is False
    assert second["change_attempts"] == 2
    assert second["change_signal"] == "auto"
    assert second["change_signal_active"] == "poll"
    assert second["change_signal_reason"] == "backend has no X11 display"
    assert second["change_wait_ms"] >= 0
    stage_timing = second["change_stage_timing_ms"]
    assert stage_timing["signal_prepare_ms"] >= 0
    assert stage_timing["action_wall_ms"] >= 0
    assert stage_timing["signal_wait_wall_ms"] == 0
    assert stage_timing["frame_poll_ms"] >= 0
    assert stage_timing["server_pre_emit_ms"] >= stage_timing["action_wall_ms"]
    assert second["action_result"]["ok"] is True


def test_observation_stream_run_actions_observe_change_reports_timeout(app) -> None:
    raw = _raw_screenshot_bytes("white")

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return raw

    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "change_timeout_ms": 0,
                    "poll_interval_ms": 1,
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    assert second["type"] == "unchanged"
    assert second["trigger"] == "run_actions_observe_change"
    assert second["change_detected"] is False
    assert second["change_timeout_reached"] is True
    assert second["change_attempts"] == 1
    assert second["action_result"]["ok"] is True


def test_dirty_frame_producer_wait_timeout_reserves_fallback_budget() -> None:
    assert (
        observation_routes._dirty_frame_producer_wait_timeout_ms(
            0, regional_capture=True
        )
        == 0
    )
    assert (
        observation_routes._dirty_frame_producer_wait_timeout_ms(
            3, regional_capture=True
        )
        == 1
    )
    assert (
        observation_routes._dirty_frame_producer_wait_timeout_ms(
            20, regional_capture=True
        )
        == 2
    )
    assert (
        observation_routes._dirty_frame_producer_wait_timeout_ms(
            21, regional_capture=True
        )
        == 2
    )
    assert (
        observation_routes._dirty_frame_producer_wait_timeout_ms(
            100, regional_capture=True
        )
        == 2
    )
    assert (
        observation_routes._dirty_frame_producer_wait_timeout_ms(
            100, regional_capture=False
        )
        == 92
    )
    assert (
        observation_routes._dirty_frame_producer_wait_timeout_ms(
            100, regional_capture=True, override_ms=1
        )
        == 1
    )
    assert (
        observation_routes._dirty_frame_producer_wait_timeout_ms(
            3, regional_capture=True, override_ms=10
        )
        == 3
    )


def test_observation_stream_run_actions_observe_change_can_detect_region(app) -> None:
    full_before = _raw_screenshot_bytes("white")
    full_after = _raw_screenshot_with_square()
    region_before = _raw_screenshot_bytes("white")
    region_after = _raw_screenshot_with_square()
    region_captures = iter([region_before, region_before, region_after])

    async def screenshot_raw_pixels(*_args, **kwargs):
        if kwargs.get("region") is not None:
            return next(region_captures)
        return full_before if screenshot_raw_pixels.full_count == 0 else full_after

    screenshot_raw_pixels.full_count = 0

    async def counting_screenshot_raw_pixels(*_args, **kwargs):
        result = await screenshot_raw_pixels(*_args, **kwargs)
        if kwargs.get("region") is None:
            screenshot_raw_pixels.full_count += 1
        return result

    app.state.backend.screenshot_raw_pixels = counting_screenshot_raw_pixels

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "change_detection": "region",
                    "change_detection_region": {"x": 0, "y": 0, "width": 32, "height": 32},
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 1,
                    "poll_strategy": "adaptive",
                    "change_signal": "poll",
                    "dirty_frame_producer": "off",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        assert websocket.receive_bytes()

    assert second["trigger"] == "run_actions_observe_change"
    assert second["change_region_detected"] is True
    assert second["change_region_attempts"] == 2
    assert second["change_detected"] is True
    assert second["change_detection"] == "region"
    assert second["poll_strategy"] == "adaptive"
    timing = second["change_stage_timing_ms"]
    assert timing["region_baseline_ms"] >= timing["region_baseline_capture_ms"] >= 0
    assert timing["region_baseline_capture_ready_ms"] >= 0
    assert timing["region_baseline_capture_lock_wait_ms"] >= 0
    assert timing["region_baseline_capture_operation_ms"] >= 0
    assert timing["region_poll_ms"] >= timing["region_poll_capture_ms"] >= 0
    assert timing["region_poll_capture_ready_ms"] >= 0
    assert timing["region_poll_capture_lock_wait_ms"] >= 0
    assert timing["region_poll_capture_operation_ms"] >= 0


def test_observation_stream_run_actions_observe_change_uses_xdamage_signal(
    app,
    monkeypatch,
) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    class FakeXDamageWatcher:
        instances: ClassVar[list[FakeXDamageWatcher]] = []
        wait_timeouts: ClassVar[list[int]] = []

        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.closed = False
            self.armed = 0
            FakeXDamageWatcher.instances.append(self)

        def arm(self) -> None:
            self.armed += 1

        def wait(self, timeout_ms: int):
            self.wait_timeouts.append(timeout_ms)
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=1.0,
                version="1.1",
            )

        def close(self) -> None:
            self.closed = True

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 1,
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        assert websocket.receive_bytes()

    assert second["trigger"] == "run_actions_observe_change"
    assert second["kind"] == "patch"
    assert second["change_detected"] is True
    assert second["change_attempts"] == 1
    assert second["change_signal_active"] == "xdamage"
    assert second["change_signal_available"] is True
    assert second["change_signal_detected"] is True
    assert second["change_signal_version"] == "1.1"
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is True
    assert second["dirty_frame_producer_fallback_reason"] is None
    assert second["dirty_frame_age_ms"] >= 0
    assert second["dirty_frame_capture_region_source"] is None
    assert second["xdamage_dirty_rect"] is None
    assert second["xdamage_dirty_rects"] == []
    assert second["change_stage_timing_ms"]["signal_wait_wall_ms"] >= 0
    assert second["change_stage_timing_ms"]["dirty_producer_wait_ms"] >= 0
    assert second["change_stage_timing_ms"]["dirty_producer_capture_ms"] >= 0
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0
    assert second["change_stage_timing_ms"]["server_pre_emit_ms"] >= 0
    assert len(FakeXDamageWatcher.instances) == 2
    assert FakeXDamageWatcher.instances[0].rect_hints is False
    assert FakeXDamageWatcher.instances[0].armed == 0
    assert FakeXDamageWatcher.instances[1].rect_hints is True
    assert FakeXDamageWatcher.instances[1].armed == 1
    assert second["dirty_frame_producer_wait_budget_ms"] == 92
    assert FakeXDamageWatcher.wait_timeouts == [92]
    assert FakeXDamageWatcher.instances[0].closed is True
    assert FakeXDamageWatcher.instances[1].closed is True


def test_observation_stream_dirty_producer_captures_action_region(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    changed = Image.new("RGB", (64, 64), "white")
    for y in range(18, 22):
        for x in range(18, 22):
            changed.putpixel((x, y), (0, 0, 0))
    capture_images = iter([white, changed])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=1.0,
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        first_payload = websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 1,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        second_payload = websocket.receive_bytes()

    assert capture_regions[0] is None
    assert capture_regions[1] == Region(x=0, y=0, width=32, height=32)
    assert len(capture_regions) == 2
    assert second["trigger"] == "run_actions_observe_change"
    assert second["dirty_frame_capture_region"] == {"x": 0, "y": 0, "width": 32, "height": 32}
    assert second["dirty_frame_capture_region_source"] == "action_region"
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is True
    assert second["change_stage_timing_ms"]["region_baseline_ms"] == 0
    assert second["change_stage_timing_ms"]["region_baseline_capture_ms"] == 0
    assert second["change_stage_timing_ms"]["region_baseline_capture_ready_ms"] == 0
    assert second["change_stage_timing_ms"]["region_baseline_capture_lock_wait_ms"] == 0
    assert second["change_stage_timing_ms"]["region_baseline_capture_operation_ms"] == 0
    assert second["source_hash_kind"] == "tile-fingerprint"
    assert second["change_stage_timing_ms"]["dirty_region_native_ms"] >= 0
    assert second["change_stage_timing_ms"]["dirty_region_reconstruct_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0
    attribution = second["action_observe_attribution_ms"]
    assert attribution["action_wall_ms"] >= 0
    assert attribution["action_end_to_signal_detect_ms"] >= 0
    assert attribution["signal_detect_to_capture_start_ms"] >= 0
    assert attribution["capture_start_to_delta_ready_ms"] >= 0
    assert attribution["delta_ready_to_pre_emit_ms"] >= 0
    assert second["width"] == 64
    assert second["height"] == 64
    composed = ObservationFrame(
        payload=second_payload,
        metadata=second,
    ).compose(first_payload)
    assert composed == _image_png_bytes(changed)


def test_observation_stream_dirty_producer_does_not_poll_frame_after_deadline(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    capture_images = iter([white, white])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        if region is not None:
            time.sleep(0.005)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=0.1,
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 3,
                    "poll_interval_ms": 1,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    assert capture_regions[0] is None
    assert capture_regions[1] == Region(x=0, y=0, width=32, height=32)
    assert len(capture_regions) == 2
    assert second["trigger"] == "run_actions_observe_change"
    assert second["type"] == "unchanged"
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is False
    assert second["dirty_frame_producer_fallback_reason"] == "producer_same_region"
    assert second["frame_poll_skipped_reason"] == "deadline_exhausted_after_dirty_producer"
    assert second["change_timeout_reached"] is True
    assert second["dirty_frame_producer_wait_budget_ms"] == 1
    assert second["change_stage_timing_ms"]["dirty_producer_wait_ms"] >= 1
    assert second["change_stage_timing_ms"]["dirty_region_native_ms"] >= 0
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0


def test_observation_stream_confirms_dirty_region_after_producer_timeout(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    changed = Image.new("RGB", (64, 64), "white")
    for y in range(18, 22):
        for x in range(18, 22):
            changed.putpixel((x, y), (0, 0, 0))
    capture_images = iter([white, changed])
    capture_regions: list[Region | None] = []
    wait_timeouts: list[int] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            wait_timeouts.append(timeout_ms)
            time.sleep(0.05)
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=False,
                wait_ms=float(timeout_ms),
                reason="timeout",
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        first_payload = websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 1,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        second_payload = websocket.receive_bytes()

    assert capture_regions[0] is None
    assert capture_regions[1] == Region(x=0, y=0, width=32, height=32)
    assert len(capture_regions) == 2
    assert wait_timeouts == [2]
    assert second["trigger"] == "run_actions_observe_change"
    assert second["change_detected"] is True
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is False
    assert second["dirty_frame_producer_fallback_reason"] == "no_changed_frame"
    assert second["dirty_frame_producer_wait_budget_ms"] == 2
    assert second["dirty_region_confirmation_result"] == "changed"
    assert second["frame_poll_skipped_reason"] == "dirty_region_confirmation_changed"
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_ms"] >= 0
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_capture_ms"] >= 0
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_capture_ready_ms"] >= 0
    assert (
        second["change_stage_timing_ms"]["dirty_region_confirmation_capture_lock_wait_ms"] >= 0
    )
    assert (
        second["change_stage_timing_ms"]["dirty_region_confirmation_capture_operation_ms"] >= 0
    )
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_native_ms"] >= 0
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_ready_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_lock_wait_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_operation_ms"] == 0
    composed = ObservationFrame(
        payload=second_payload,
        metadata=second,
    ).compose(first_payload)
    assert composed == _image_png_bytes(changed)


def test_observation_stream_confirms_unchanged_dirty_region_after_deadline(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    capture_images = iter([white, white])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        if region is not None:
            time.sleep(0.005)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=False,
                wait_ms=float(timeout_ms),
                reason="timeout",
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 3,
                    "poll_interval_ms": 1,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    assert capture_regions[0] is None
    assert capture_regions[1] == Region(x=0, y=0, width=32, height=32)
    assert len(capture_regions) == 2
    assert second["trigger"] == "run_actions_observe_change"
    assert second["type"] == "unchanged"
    assert second["change_detected"] is False
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is False
    assert second["dirty_frame_producer_fallback_reason"] == "no_changed_frame"
    assert second["dirty_region_confirmation_result"] == "unchanged"
    assert second["frame_poll_skipped_reason"] == "deadline_exhausted_after_region_confirmation"
    assert second["change_timeout_reached"] is True
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_ms"] >= 3
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_capture_ms"] >= 3
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_capture_ready_ms"] >= 0
    assert (
        second["change_stage_timing_ms"]["dirty_region_confirmation_capture_lock_wait_ms"] >= 0
    )
    assert (
        second["change_stage_timing_ms"]["dirty_region_confirmation_capture_operation_ms"] >= 3
    )
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0


def test_observation_stream_bounds_frame_poll_after_unchanged_dirty_region(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    capture_images = iter([white, white, white, white, white, white])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=False,
                wait_ms=float(timeout_ms),
                reason="timeout",
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 50,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    assert capture_regions[0] is None
    assert capture_regions[1] == Region(x=0, y=0, width=32, height=32)
    assert capture_regions[:2] == [
        None,
        Region(x=0, y=0, width=32, height=32),
    ]
    assert len(capture_regions) >= 4
    assert all(region is None for region in capture_regions[2:])
    assert second["trigger"] == "run_actions_observe_change"
    assert second["type"] == "unchanged"
    assert second["dirty_region_confirmation_result"] == "unchanged"
    assert second["frame_poll_budget_ms"] > 50
    assert second["frame_poll_deadline_reason"] == "after_unchanged_dirty_region_confirmation"
    assert second["change_timeout_reached"] is True
    assert second["change_wait_ms"] >= 80
    assert second["change_stage_timing_ms"]["frame_poll_ms"] >= 50
    assert second["change_stage_timing_ms"]["frame_poll_capture_ms"] > 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_ready_ms"] >= 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_lock_wait_ms"] >= 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_operation_ms"] > 0


def test_observation_stream_can_skip_full_frame_fallback_after_unchanged_dirty_region(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    capture_images = iter([white, white, white, white, white, white])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=False,
                wait_ms=float(timeout_ms),
                reason="timeout",
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 50,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "full_frame_fallback": False,
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    assert capture_regions == [
        None,
        Region(x=0, y=0, width=32, height=32),
    ]
    assert second["trigger"] == "run_actions_observe_change"
    assert second["type"] == "unchanged"
    assert second["full_frame_fallback"] is False
    assert second["dirty_region_confirmation_result"] == "unchanged"
    assert second["frame_poll_skipped_reason"] == "dirty_region_confirmation_unchanged"
    assert second["frame_poll_deadline_reason"] is None
    assert second["frame_poll_budget_ms"] is None
    assert second["change_detected"] is False
    assert second["change_timeout_reached"] is False
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_ms"] == 0


def test_observation_stream_skips_confirmation_after_unchanged_region_poll(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    capture_images = iter([white, white, white])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=False,
                wait_ms=float(timeout_ms),
                reason="timeout",
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 50,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "dirty_frame_producer": "off",
                    "full_frame_fallback": False,
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    region = Region(x=12, y=12, width=16, height=16)
    assert capture_regions == [None, region, region]
    assert second["trigger"] == "run_actions_observe_change"
    assert second["type"] == "unchanged"
    assert second["dirty_frame_producer"] is False
    assert second["dirty_region_confirmation"] == "auto"
    assert second["dirty_region_confirmation_result"] == "skipped_region_poll_unchanged"
    assert second["frame_poll_skipped_reason"] == "region_poll_unchanged"
    assert second["source_hash_kind"] == "tile-fingerprint"
    assert second["change_stage_timing_ms"]["region_poll_capture_ms"] > 0
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_capture_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0


def test_observation_stream_can_disable_dirty_region_confirmation(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    capture_images = iter([white])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=False,
                wait_ms=float(timeout_ms),
                reason="timeout",
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 50,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "dirty_region_confirmation": "off",
                    "full_frame_fallback": False,
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    assert capture_regions == [None]
    assert second["trigger"] == "run_actions_observe_change"
    assert second["type"] == "unchanged"
    assert second["dirty_frame_producer"] is True
    assert second["dirty_region_confirmation"] == "off"
    assert second["dirty_region_confirmation_result"] == "disabled"
    assert second["frame_poll_skipped_reason"] == "dirty_region_confirmation_disabled"
    assert second["source_hash_kind"] == "tile-fingerprint"
    assert second["change_stage_timing_ms"]["dirty_region_confirmation_capture_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0


def test_observation_stream_bounds_frame_poll_after_unchanged_dirty_producer(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    capture_images = iter([white, white, white, white, white, white])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=1.0,
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 50,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    assert capture_regions[:2] == [
        None,
        Region(x=0, y=0, width=32, height=32),
    ]
    assert len(capture_regions) >= 4
    assert all(region is None for region in capture_regions[2:])
    assert second["trigger"] == "run_actions_observe_change"
    assert second["type"] == "unchanged"
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is False
    assert second["dirty_frame_producer_fallback_reason"] == "producer_same_region"
    assert second["frame_poll_budget_ms"] > 50
    assert second["frame_poll_deadline_reason"] == "after_unchanged_dirty_producer"
    assert second["change_timeout_reached"] is True
    assert second["change_wait_ms"] >= 80
    assert second["change_stage_timing_ms"]["frame_poll_ms"] >= 50
    assert second["change_stage_timing_ms"]["frame_poll_capture_ms"] > 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_ready_ms"] >= 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_lock_wait_ms"] >= 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_operation_ms"] > 0


def test_observation_stream_can_skip_full_frame_fallback_after_unchanged_dirty_producer(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    capture_images = iter([white, white])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=1.0,
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "click", "x": 20, "y": 20, "button": "left"}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 50,
                    "change_detection": "auto_region",
                    "change_region_radius": 8,
                    "change_signal": "xdamage",
                    "full_frame_fallback": False,
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()

    assert capture_regions == [
        None,
        Region(x=0, y=0, width=32, height=32),
    ]
    assert second["trigger"] == "run_actions_observe_change"
    assert second["type"] == "unchanged"
    assert second["full_frame_fallback"] is False
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is False
    assert second["dirty_frame_producer_fallback_reason"] == "producer_same_region"
    assert second["frame_poll_skipped_reason"] == "dirty_producer_same_region"
    assert second["frame_poll_deadline_reason"] is None
    assert second["frame_poll_budget_ms"] is None
    assert second["change_stage_timing_ms"]["frame_poll_ms"] == 0
    assert second["change_stage_timing_ms"]["frame_poll_capture_ms"] == 0


def test_observation_stream_dirty_producer_can_capture_xdamage_region(
    app,
    monkeypatch,
) -> None:
    white = Image.new("RGB", (64, 64), "white")
    changed = Image.new("RGB", (64, 64), "white")
    for y in range(40, 44):
        for x in range(40, 44):
            changed.putpixel((x, y), (0, 0, 0))
    capture_images = iter([white, changed])
    capture_regions: list[Region | None] = []

    async def screenshot_raw_pixels(*_args, region=None, **_kwargs):
        capture_regions.append(region)
        return _raw_screenshot_from_image(next(capture_images), region=region)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=1.0,
                version="1.1",
                dirty_rect=observation_routes.XDamageRect(40, 40, 4, 4),
                dirty_rects=(observation_routes.XDamageRect(40, 40, 4, 4),),
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        first_payload = websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 1,
                    "change_detection": "full",
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        second_payload = websocket.receive_bytes()

    assert capture_regions[0] is None
    assert capture_regions[1] == Region(x=32, y=32, width=16, height=16)
    assert len(capture_regions) == 2
    assert second["trigger"] == "run_actions_observe_change"
    assert second["dirty_frame_capture_region"] == {"x": 32, "y": 32, "width": 16, "height": 16}
    assert second["dirty_frame_capture_region_source"] == "xdamage_dirty_rect"
    assert second["xdamage_dirty_rect"] == {"x": 40, "y": 40, "width": 4, "height": 4}
    assert second["xdamage_dirty_rects"] == [{"x": 40, "y": 40, "width": 4, "height": 4}]
    assert second["xdamage_dirty_ratio"] == 0.0625
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is True
    composed = ObservationFrame(
        payload=second_payload,
        metadata=second,
    ).compose(first_payload)
    assert composed == _image_png_bytes(changed)


def test_dirty_frame_producer_uses_separate_xdamage_watchers(monkeypatch) -> None:
    class FakeXDamageWatcher:
        instances: ClassVar[list[FakeXDamageWatcher]] = []

        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.armed = 0
            self.waits = 0
            self.closed = False
            self.mode_switches = 0
            FakeXDamageWatcher.instances.append(self)

        def set_rect_hints(self, enabled: bool) -> None:
            self.mode_switches += 1
            self.rect_hints = enabled

        def arm(self) -> None:
            self.armed += 1

        def wait(self, timeout_ms: int):
            self.waits += 1
            dirty_rect = (
                observation_routes.XDamageRect(40, 40, 4, 4) if self.rect_hints else None
            )
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=1.0,
                version="1.1",
                dirty_rect=dirty_rect,
                dirty_rects=(dirty_rect,) if dirty_rect is not None else (),
            )

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    async def run() -> None:
        async def capture_raw(region=None):
            return _raw_screenshot_from_image(Image.new("RGB", (64, 64), "white"), region=region)

        producer = observation_routes._DirtyFrameProducer(capture_raw=capture_raw, display=":99")
        request = observation_routes.ObservationStreamRequest(
            format="png",
            show_cursor=False,
            fps=1,
            tile_size=16,
        )
        await producer.arm(
            request,
            timeout_ms=100,
            capture_region=Region(x=0, y=0, width=16, height=16),
        )
        first = await producer.wait_for_change(baseline_source_sha256=None, timeout_ms=100)
        await producer.arm(
            request,
            timeout_ms=100,
            xdamage_region_frame=(64, 64, 16, 0.5),
        )
        second = await producer.wait_for_change(baseline_source_sha256=None, timeout_ms=100)
        await producer.close()

        assert first is not None
        assert first.capture_region_source == "action_region"
        assert second is not None
        assert second.capture_region_source == "xdamage_dirty_rect"
        assert second.capture_region == Region(x=32, y=32, width=16, height=16)

    asyncio.run(run())

    assert len(FakeXDamageWatcher.instances) == 2
    wakeup, rect = FakeXDamageWatcher.instances
    assert wakeup.rect_hints is False
    assert wakeup.armed == 1
    assert wakeup.waits == 1
    assert rect.rect_hints is True
    assert rect.armed == 1
    assert rect.waits == 1
    assert wakeup.mode_switches == 0
    assert rect.mode_switches == 0
    assert wakeup.closed is True
    assert rect.closed is True


def test_region_native_dirty_patch_can_advance_from_stale_raw_cache() -> None:
    white = Image.new("RGB", (64, 64), "white")
    changed = Image.new("RGB", (64, 64), "white")
    for y in range(18, 22):
        for x in range(18, 22):
            changed.putpixel((x, y), (0, 0, 0))
    changed_again = changed.copy()
    for y in range(34, 38):
        for x in range(34, 38):
            changed_again.putpixel((x, y), (0, 0, 0))

    request = observation_routes.ObservationStreamRequest(
        format="png",
        show_cursor=False,
        fps=1,
        tile_size=16,
    )
    previous_raw = _raw_screenshot_from_image(white)
    state = observation_routes._StreamState(
        request=request,
        stream_id="test",
        last_raw_frame=previous_raw,
        last_raw_frame_current=False,
        last_tile_hashes=observation_routes.tile_hashes_rgb(
            changed.tobytes(),
            changed.width,
            changed.height,
            request.tile_size,
        ),
        last_frame_seq=2,
    )
    frame = observation_routes._capture_region_native_delta_frame(
        state=state,
        region_raw=_raw_screenshot_from_image(
            changed_again,
            region=Region(x=16, y=16, width=32, height=32),
        ),
        region=Region(x=16, y=16, width=32, height=32),
        request=request,
        options=observation_routes._stream_screenshot_options(request),
        seq=3,
        previous_seq=2,
        stream_id="test",
        captured_started=0.0,
    )

    assert frame is not None
    metadata, payload = frame
    assert metadata["kind"] == "patch"
    assert metadata["source_hash_kind"] == "tile-fingerprint"
    assert metadata["dirty_rect"] == {"x": 32, "y": 32, "width": 16, "height": 16}
    composed = ObservationFrame(payload=payload, metadata=metadata).compose(
        _image_png_bytes(changed)
    )
    assert composed == _image_png_bytes(changed_again)


def test_tile_fingerprint_includes_off_origin_tile_hashes() -> None:
    first = Image.new("RGB", (64, 64), "white")
    second = first.copy()
    for y in range(34, 38):
        for x in range(34, 38):
            second.putpixel((x, y), (0, 0, 0))

    tile_size = 16
    first_hashes = observation_routes.tile_hashes_rgb(
        first.tobytes(),
        first.width,
        first.height,
        tile_size,
    )
    second_hashes = observation_routes.tile_hashes_rgb(
        second.tobytes(),
        second.width,
        second.height,
        tile_size,
    )

    assert first_hashes[(0, 0)] == second_hashes[(0, 0)]
    assert first_hashes[(32, 32)] != second_hashes[(32, 32)]
    assert observation_routes._source_fingerprint_from_tile_hashes(
        first_hashes,
        width=first.width,
        height=first.height,
        tile_size=tile_size,
    ) != observation_routes._source_fingerprint_from_tile_hashes(
        second_hashes,
        width=second.width,
        height=second.height,
        tile_size=tile_size,
    )


def test_observation_stream_reuses_xdamage_watcher_across_action_observe_calls(
    app,
    monkeypatch,
) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
            _raw_screenshot_bytes("black"),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    class FakeXDamageWatcher:
        instances: ClassVar[list[FakeXDamageWatcher]] = []

        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.closed = False
            self.armed = 0
            self.waits = 0
            FakeXDamageWatcher.instances.append(self)

        def arm(self) -> None:
            self.armed += 1

        def wait(self, timeout_ms: int):
            self.waits += 1
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=1.0,
                version="1.1",
            )

        def close(self) -> None:
            self.closed = True

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 0.01,
                    "max_frames": 3,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        for request_id in ("2", "3"):
            websocket.send_json(
                {
                    "id": request_id,
                    "op": "run_actions_observe_change",
                    "payload": {
                        "actions": [{"type": "wait", "duration_ms": 0}],
                        "change_timeout_ms": 100,
                        "poll_interval_ms": 1,
                        "change_signal": "xdamage",
                        "source": "test",
                    },
                }
            )
            frame = websocket.receive_json()
            assert frame["trigger"] == "run_actions_observe_change"
            assert frame["change_signal_active"] == "xdamage"
            assert websocket.receive_bytes()

    assert len(FakeXDamageWatcher.instances) == 2
    assert FakeXDamageWatcher.instances[0].rect_hints is False
    assert FakeXDamageWatcher.instances[0].armed == 0
    assert FakeXDamageWatcher.instances[0].waits == 0
    assert FakeXDamageWatcher.instances[1].rect_hints is True
    assert FakeXDamageWatcher.instances[1].armed == 2
    assert FakeXDamageWatcher.instances[1].waits == 2
    assert FakeXDamageWatcher.instances[0].closed is True
    assert FakeXDamageWatcher.instances[1].closed is True


def test_observation_stream_dirty_producer_ignores_unchanged_frame_and_falls_back(
    app,
    monkeypatch,
) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    class FakeXDamageWatcher:
        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display
            self.rect_hints = rect_hints
            self.failure = None

        def arm(self) -> None:
            pass

        def wait(self, timeout_ms: int):
            return observation_routes.XDamageWaitResult(
                available=True,
                detected=True,
                wait_ms=1.0,
                version="1.1",
            )

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 0.01,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 1,
                    "change_signal": "xdamage",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        assert websocket.receive_bytes()

    assert second["trigger"] == "run_actions_observe_change"
    assert second["change_detected"] is True
    assert second["dirty_frame_producer"] is True
    assert second["dirty_frame_producer_used"] is False
    assert second["dirty_frame_producer_fallback_reason"] == "no_changed_frame"
    assert second["change_stage_timing_ms"]["frame_poll_ms"] >= 0


def test_observation_stream_run_actions_observe_change_auto_falls_back_to_poll(
    app,
    monkeypatch,
) -> None:
    captures = iter(
        [
            _raw_screenshot_bytes("white"),
            _raw_screenshot_bytes("white"),
            _raw_screenshot_with_square(),
        ]
    )

    async def screenshot_raw_pixels(*_args, **_kwargs):
        return next(captures)

    class FakeXDamageWatcher:
        failure = "XDamage extension unavailable"

        def __init__(self, *, display: str, rect_hints: bool = False) -> None:
            self.display = display

        def arm(self) -> None:
            raise RuntimeError("XDamage extension unavailable")

        def close(self) -> None:
            pass

    app.state.backend.display = ":99"
    app.state.backend.screenshot_raw_pixels = screenshot_raw_pixels
    monkeypatch.setattr(observation_routes, "XDamageWatcher", FakeXDamageWatcher)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/observations/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "start",
                "payload": {
                    "format": "png",
                    "show_cursor": False,
                    "fps": 1,
                    "max_frames": 2,
                    "tile_size": 16,
                },
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["trigger"] == "start"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "2",
                "op": "run_actions_observe_change",
                "payload": {
                    "actions": [{"type": "wait", "duration_ms": 0}],
                    "change_timeout_ms": 100,
                    "poll_interval_ms": 1,
                    "change_signal": "auto",
                    "source": "test",
                },
            }
        )
        second = websocket.receive_json()
        assert websocket.receive_bytes()

    assert second["change_detected"] is True
    assert second["change_attempts"] == 2
    assert second["change_signal_active"] == "poll"
    assert second["change_signal_available"] is False
    assert second["change_signal_reason"] == "XDamage extension unavailable"


def test_adaptive_change_poll_schedule_is_bounded() -> None:
    assert observation_routes._change_poll_sleep_ms(
        attempt=1,
        poll_interval_ms=8,
        poll_strategy="adaptive",
    ) == 4
    assert observation_routes._change_poll_sleep_ms(
        attempt=2,
        poll_interval_ms=8,
        poll_strategy="adaptive",
    ) == 8
    assert observation_routes._change_poll_sleep_ms(
        attempt=4,
        poll_interval_ms=8,
        poll_strategy="adaptive",
    ) == 8


def test_latest_delivery_coalesces_overdue_scheduled_frames() -> None:
    request = observation_routes.ObservationStreamRequest(fps=10, delivery="latest")
    state = observation_routes._StreamState(request=request, next_frame_at=100.0)

    assert observation_routes._coalesced_scheduled_frames(state, request, now=100.05) == 0
    assert observation_routes._coalesced_scheduled_frames(state, request, now=100.35) == 3

    reliable = observation_routes.ObservationStreamRequest(fps=10, delivery="reliable")
    assert observation_routes._coalesced_scheduled_frames(state, reliable, now=100.35) == 0


def test_observation_client_marshals_options_and_frames() -> None:
    transport = _FakeObservationTransport(
        [
            ObservationFrame(payload=b"jpeg", metadata={"seq": 1, "unchanged": False}),
            ObservationFrame(payload=None, metadata={"seq": 2, "unchanged": True}),
        ]
    )
    client = ObservationClient(
        transport,  # type: ignore[arg-type]
        options={"format": "jpeg", "quality": 60},
        fps=2,
        max_frames=2,
        delta_mode="auto",
        delta_max_ratio=0.2,
        keyframe_interval=10,
        tile_size=32,
        delivery="reliable",
        max_patch_rects=2,
        multi_rect_min_savings=0.4,
        transport_timing=True,
        frame_encoding="binary-envelope",
    )

    frames = list(client.frames())

    assert frames[0].payload == b"jpeg"
    assert frames[1].unchanged is True
    assert transport.payload["format"] == "jpeg"
    assert transport.payload["quality"] == 60
    assert transport.payload["fps"] == 2
    assert transport.payload["max_frames"] == 2
    assert transport.payload["delta_mode"] == "auto"
    assert transport.payload["delta_max_ratio"] == 0.2
    assert transport.payload["keyframe_interval"] == 10
    assert transport.payload["tile_size"] == 32
    assert transport.payload["delivery"] == "reliable"
    assert transport.payload["max_patch_rects"] == 2
    assert transport.payload["multi_rect_min_savings"] == 0.4
    assert transport.payload["transport_timing"] is True
    assert transport.payload["frame_encoding"] == "binary-envelope"
    assert transport.requested_frame is False
    client.request_frame()
    assert transport.requested_frame is True
    client.run_actions_capture(actions=[{"type": "wait", "duration_ms": 0}])
    assert transport.action_payload == {"actions": [{"type": "wait", "duration_ms": 0}]}
    client.run_actions_observe_change(actions=[{"type": "wait", "duration_ms": 0}])
    assert transport.change_payload == {"actions": [{"type": "wait", "duration_ms": 0}]}


def test_observation_client_defaults_to_lossless_png_binary_envelope() -> None:
    transport = _FakeObservationTransport([])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    list(client.frames())

    assert transport.payload["format"] == "png"
    assert transport.payload["show_cursor"] is False
    assert transport.payload["frame_encoding"] == "binary-envelope"


def test_observation_client_can_use_daemon_default_frame_encoding() -> None:
    transport = _FakeObservationTransport([])
    client = ObservationClient(
        transport,  # type: ignore[arg-type]
        max_frames=0,
        frame_encoding=None,
    )

    list(client.frames())

    assert "frame_encoding" not in transport.payload


def test_observation_client_can_force_binary_envelope_encoding() -> None:
    transport = _FakeObservationTransport([])
    client = ObservationClient(
        transport,  # type: ignore[arg-type]
        max_frames=0,
        frame_encoding="binary-envelope",
    )

    list(client.frames())

    assert transport.payload["frame_encoding"] == "binary-envelope"


def test_observation_client_act_and_observe_returns_causal_result() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(
        payload=b"png",
        metadata={
            "id": "2",
            "action_id": "2",
            "trigger": "run_actions_observe_change",
            "causal_frame": True,
            "change_detected": True,
            "action_result": {"ok": True},
        },
    )
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    result = client.act_and_observe(actions=[{"type": "wait", "duration_ms": 0}])

    assert result.frame is frame
    assert result.action_id == "2"
    assert result.action_result == {"ok": True}
    assert result.change_detected is True
    assert transport.change_payload["actions"] == [{"type": "wait", "duration_ms": 0}]
    assert transport.change_payload["change_detection"] == "full"
    assert transport.change_payload["change_signal"] == "auto"
    assert "continue_on_error" not in transport.change_payload


def test_observation_client_experimental_visual_change_returns_same_contract() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(
        payload=b"png",
        metadata={
            "id": "2",
            "action_id": "2",
            "trigger": "run_actions_observe_change",
            "causal_frame": True,
            "change_detected": True,
            "action_result": {"ok": True},
        },
    )
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    result = client._experimental_act_until_visual_change(
        actions=[
            {"type": "click", "x": 12, "y": 34},
            {"type": "wait", "duration_ms": 25},
        ],
        continue_on_error=True,
    )

    assert result.frame is frame
    assert result.action_id == "2"
    assert result.action_result == {"ok": True}
    assert result.change_detected is True
    assert transport.change_payload["actions"] == [
        {"type": "click", "x": 12, "y": 34},
        {"type": "wait", "duration_ms": 25},
    ]
    assert transport.change_payload["continue_on_error"] is True
    assert transport.change_payload["change_detection"] == "auto_region"


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {"unchanged": False, "change_detected": True, "change_timeout_reached": False},
            (True, False, False),
        ),
        (
            {"unchanged": True, "change_detected": False, "change_timeout_reached": False},
            (False, False, True),
        ),
        (
            {"unchanged": True, "change_detected": False, "change_timeout_reached": True},
            (False, True, True),
        ),
    ],
)
def test_experimental_visual_change_result_keeps_outcomes_distinct(
    metadata: dict[str, bool],
    expected: tuple[bool, bool, bool],
) -> None:
    result = ActionObservationResult(
        frame=ObservationFrame(payload=None, metadata=metadata),
    )

    assert (
        result.change_detected,
        result.change_timeout_reached,
        result.frame.unchanged,
    ) == expected


def test_observation_client_act_and_observe_delegates_once(monkeypatch) -> None:
    transport = _FakeObservationTransport([])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]
    frame = ObservationFrame(payload=b"png", metadata={})
    expected = ActionObservationResult(frame=frame, elapsed_ms=1.0)
    calls: list[dict[str, object]] = []

    def experimental(**payload: object) -> ActionObservationResult:
        calls.append(payload)
        return expected

    monkeypatch.setattr(client, "_experimental_act_until_visual_change", experimental)

    result = client.act_and_observe(actions=[{"type": "wait", "duration_ms": 10}])

    assert result is expected
    assert len(calls) == 1
    assert calls[0]["actions"] == [{"type": "wait", "duration_ms": 10}]
    assert transport.change_payload is None


def test_observation_client_act_and_observe_can_override_frame_encoding() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "wait", "duration_ms": 0}],
        frame_encoding="json-binary",
    )

    assert transport.change_payload["frame_encoding"] == "json-binary"


def test_observation_client_act_and_observe_defaults_pointer_actions_to_auto_region() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(actions=[{"type": "click", "x": 12, "y": 34}])

    assert transport.change_payload["change_detection"] == "auto_region"
    assert transport.change_payload["full_frame_fallback"] is True
    assert transport.change_payload["change_region_radius"] == 64


def test_observation_client_act_and_observe_defaults_drag_actions_to_auto_region() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "drag", "start_x": 1, "start_y": 2, "end_x": 120, "end_y": 140}]
    )

    assert transport.change_payload["change_detection"] == "auto_region"
    assert transport.change_payload["full_frame_fallback"] is True
    assert transport.change_payload["change_region_radius"] == 64


def test_observation_client_act_and_observe_ignores_trailing_wait_for_auto_region() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[
            {"type": "click", "x": 12, "y": 34},
            {"type": "wait", "duration_ms": 10},
        ]
    )

    assert transport.change_payload["change_detection"] == "auto_region"
    assert transport.change_payload["full_frame_fallback"] is True
    assert transport.change_payload["change_region_radius"] == 64


def test_observation_client_act_and_observe_uses_full_frame_after_global_action() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[
            {"type": "click", "x": 12, "y": 34},
            {"type": "keypress", "key": "enter"},
        ]
    )

    assert transport.change_payload["change_detection"] == "full"
    assert transport.change_payload["full_frame_fallback"] is True
    assert "change_region_radius" not in transport.change_payload


def test_observation_client_act_and_observe_keeps_keyboard_actions_full_frame() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(actions=[{"type": "keypress", "key": "enter"}])

    assert transport.change_payload["change_detection"] == "full"
    assert transport.change_payload["full_frame_fallback"] is True
    assert "change_region_radius" not in transport.change_payload


def test_observation_client_act_and_observe_respects_explicit_change_detection() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "click", "x": 12, "y": 34}],
        change_detection="full",
    )

    assert transport.change_payload["change_detection"] == "full"
    assert transport.change_payload["full_frame_fallback"] is True
    assert "change_region_radius" not in transport.change_payload


def test_observation_client_act_and_observe_can_disable_full_frame_fallback() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "click", "x": 12, "y": 34}],
        full_frame_fallback=False,
    )

    assert transport.change_payload["full_frame_fallback"] is False


def test_observation_client_act_and_observe_can_force_full_frame_fallback() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "click", "x": 12, "y": 34}],
        full_frame_fallback=True,
    )

    assert transport.change_payload["change_detection"] == "auto_region"
    assert transport.change_payload["full_frame_fallback"] is True
    assert transport.change_payload["change_region_radius"] == 64


def test_observation_client_act_and_observe_respects_explicit_region_radius() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "click", "x": 12, "y": 34}],
        change_region_radius=144,
    )

    assert transport.change_payload["change_detection"] == "auto_region"
    assert transport.change_payload["change_region_radius"] == 144


def test_observation_client_act_and_observe_can_override_dirty_producer_wait() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "click", "x": 12, "y": 34}],
        dirty_frame_producer_wait_ms=1,
    )

    assert transport.change_payload["change_detection"] == "auto_region"
    assert transport.change_payload["dirty_frame_producer_wait_ms"] == 1


def test_observation_client_act_and_observe_can_disable_dirty_region_confirmation() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "click", "x": 12, "y": 34}],
        dirty_region_confirmation="off",
    )

    assert transport.change_payload["change_detection"] == "auto_region"
    assert transport.change_payload["dirty_region_confirmation"] == "off"


def test_observation_client_act_and_observe_uses_explicit_region_with_auto_policy() -> None:
    initial = ObservationFrame(payload=b"initial", metadata={"seq": 1, "kind": "keyframe"})
    frame = ObservationFrame(payload=b"png", metadata={"trigger": "run_actions_observe_change"})
    transport = _FakeObservationTransport([initial, frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    client.act_and_observe(
        actions=[{"type": "keypress", "key": "enter"}],
        change_detection_region={"x": 10, "y": 20, "width": 30, "height": 40},
    )

    assert transport.change_payload["change_detection"] == "auto_region"
    assert transport.change_payload["full_frame_fallback"] is True
    assert transport.change_payload["change_region_radius"] == 64
    assert transport.change_payload["change_detection_region"] == {
        "x": 10,
        "y": 20,
        "width": 30,
        "height": 40,
    }


def test_observation_transport_splits_receive_timing() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "frame", "seq": 7, "kind": "keyframe", "unchanged": False}),
            b"png",
            json.dumps(
                {
                    "type": "transport_timing",
                    "seq": 7,
                    "server_emit_timing_ms": {
                        "metadata_send_ms": 1.0,
                        "payload_send_ms": 2.0,
                        "emit_total_ms": 3.0,
                    },
                }
            ),
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )

    frame = transport.recv_frame_with_timing()

    assert frame.payload == b"png"
    assert frame.transport_timing is not None
    assert frame.transport_timing["server_emit_timing_ms"]["emit_total_ms"] == 3.0
    client_timing = frame.transport_timing["client_receive_timing_ms"]
    assert client_timing["wait_metadata_ms"] >= 0
    assert client_timing["parse_metadata_ms"] >= 0
    assert client_timing["wait_payload_ms"] >= 0
    assert client_timing["wait_transport_timing_ms"] >= 0
    assert client_timing["receive_total_ms"] >= 0


def test_observation_transport_retries_websocket_open_timeout(monkeypatch) -> None:
    calls: list[str] = []

    def fake_connect(*_args, **_kwargs):
        calls.append("connect")
        if len(calls) == 1:
            raise TimeoutError("timed out while waiting for handshake response")
        return _FakeWebSocket([json.dumps({"type": "ready"})])

    monkeypatch.setattr("modal_computer_use.transports.observation.connect", fake_connect)

    transport = ObservationStreamTransport(
        "https://daemon.example",
        connect_attempts=2,
        connect_backoff_seconds=0,
    )

    assert calls == ["connect", "connect"]
    assert transport.setup_attempts == 2
    assert transport.setup_retry_errors == [
        {
            "type": "TimeoutError",
            "message": "timed out while waiting for handshake response",
        }
    ]


def test_observation_transport_does_not_retry_authentication_error(monkeypatch) -> None:
    calls: list[str] = []

    def fake_connect(*_args, **_kwargs):
        calls.append("connect")
        raise AuthenticationError("observation stream authentication failed")

    monkeypatch.setattr("modal_computer_use.transports.observation.connect", fake_connect)

    with pytest.raises(AuthenticationError):
        ObservationStreamTransport(
            "https://daemon.example",
            connect_attempts=3,
            connect_backoff_seconds=0,
        )

    assert calls == ["connect"]


def test_observation_transport_validates_setup_retry_options() -> None:
    with pytest.raises(ValueError, match="connect_attempts"):
        ObservationStreamTransport("https://daemon.example", connect_attempts=0)

    with pytest.raises(ValueError, match="connect_backoff_seconds"):
        ObservationStreamTransport("https://daemon.example", connect_backoff_seconds=-0.1)


def test_observation_transport_receives_binary_envelope_timing() -> None:
    envelope = observation_routes._encode_frame_envelope(
        {
            "type": "frame",
            "seq": 8,
            "kind": "keyframe",
            "unchanged": False,
            "server_emit_timing_ms": {
                "metadata_send_ms": 1.0,
                "payload_send_ms": 0.0,
                "emit_total_ms": 2.0,
            },
        },
        b"png",
    )
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            envelope,
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"frame_encoding": "binary-envelope"})

    frame = transport.recv_frame_with_timing()

    assert frame.payload == b"png"
    assert frame.metadata["seq"] == 8
    assert frame.transport_timing is not None
    assert frame.transport_timing["server_emit_timing_ms"]["emit_total_ms"] == 2.0
    client_timing = frame.transport_timing["client_receive_timing_ms"]
    assert client_timing["wait_payload_ms"] == 0.0
    assert client_timing["wait_transport_timing_ms"] == 0.0


def test_observation_transport_run_actions_observe_change_receives_correlated_frame() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "frame", "id": "other", "seq": 1, "kind": "keyframe"}),
            b"old",
            json.dumps(
                {
                    "type": "frame",
                    "id": "1",
                    "seq": 2,
                    "kind": "patch",
                    "trigger": "run_actions_observe_change",
                    "causal_frame": True,
                }
            ),
            b"png",
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )

    frame = transport.run_actions_observe_change_and_recv({"actions": []})

    assert frame.payload == b"png"
    assert frame.metadata["id"] == "1"
    assert frame.metadata["causal_frame"] is True
    assert transport.receive_frame().payload == b"old"


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("pause", None),
        ("resume", None),
        ("configure", {"fps": 10}),
        ("stop", None),
    ],
)
def test_observation_transport_controls_consume_correlated_result_and_buffer_frames(
    method: str,
    payload: dict[str, object] | None,
) -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "frame", "seq": 1, "kind": "keyframe"}),
            b"before-result",
            json.dumps({"type": "result", "id": "1", "ok": True, "result": {}}),
            json.dumps({"type": "frame", "seq": 2, "kind": "keyframe"}),
            b"after-result",
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )

    if payload is None:
        getattr(transport, method)()
    else:
        getattr(transport, method)(payload)

    assert transport.receive_frame().payload == b"before-result"
    assert transport.receive_frame().payload == b"after-result"
    sent = json.loads(websocket.sent[0])
    assert sent["op"] == method


def test_observation_transport_control_raises_correlated_error() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps(
                {
                    "type": "error",
                    "id": "1",
                    "ok": False,
                    "error": {"code": "stream_not_started", "message": "start first"},
                }
            ),
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )

    with pytest.raises(DaemonHTTPError) as exc_info:
        transport.pause()

    assert exc_info.value.code == "stream_not_started"


def test_observation_transport_control_dispatch_preserves_binary_timing() -> None:
    before = observation_routes._encode_frame_envelope(
        {
            "type": "frame",
            "seq": 1,
            "kind": "keyframe",
            "server_emit_timing_ms": {"emit_total_ms": 1.0},
        },
        b"before-result",
    )
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            before,
            json.dumps({"type": "result", "id": "2", "ok": True, "result": {}}),
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"transport_timing": True, "frame_encoding": "binary-envelope"})

    transport.pause()
    frame = transport.recv_frame_with_timing()

    assert frame.payload == b"before-result"
    assert frame.transport_timing is not None
    assert frame.transport_timing["server_emit_timing_ms"]["emit_total_ms"] == 1.0


def test_observation_configure_uses_new_timing_for_interleaved_frame() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            json.dumps({"type": "frame", "seq": 1, "kind": "keyframe"}),
            b"after-configure",
            json.dumps(
                {
                    "type": "transport_timing",
                    "seq": 1,
                    "server_emit_timing_ms": {"emit_total_ms": 2.0},
                }
            ),
            json.dumps({"type": "result", "id": "2", "ok": True, "result": {}}),
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"transport_timing": False})

    transport.configure({"transport_timing": True})
    frame = transport.recv_frame_with_timing()

    assert frame.payload == b"after-configure"
    assert frame.transport_timing is not None
    assert frame.transport_timing["server_emit_timing_ms"]["emit_total_ms"] == 2.0


def test_observation_configure_accepts_old_timing_frame_during_transition() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            json.dumps({"type": "frame", "seq": 1, "kind": "keyframe"}),
            b"before-configure",
            json.dumps(
                {
                    "type": "transport_timing",
                    "seq": 1,
                    "server_emit_timing_ms": {"emit_total_ms": 3.0},
                }
            ),
            json.dumps({"type": "result", "id": "2", "ok": True, "result": {}}),
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"transport_timing": True})

    transport.configure({"transport_timing": False})
    frame = transport.recv_frame_with_timing()

    assert frame.payload == b"before-configure"
    assert frame.transport_timing is not None
    assert frame.transport_timing["server_emit_timing_ms"]["emit_total_ms"] == 3.0


def test_leased_sync_observation_waits_for_match_and_buffers_unrelated_frames() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "frame", "id": "other", "seq": 1, "kind": "keyframe"}),
            b"old",
            json.dumps(
                {
                    "type": "frame",
                    "id": "1",
                    "seq": 2,
                    "kind": "patch",
                    "trigger": "run_actions_capture",
                }
            ),
            b"new",
        ]
    )
    events: list[str] = []

    def execute(request):
        events.append("entered")
        result = request({"x-computer-use-operation-sequence": "7"})
        events.append("completed")
        return result

    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
        _metadata_headers={"x-computer-use-lease-id": "lease"},
        _mutation_executor=execute,
    )

    transport.run_actions_capture({"actions": []})

    assert events == ["entered", "completed"]
    sent = json.loads(websocket.sent[0])
    assert sent["sequence"] == "7"
    assert "x-computer-use-lease-id" not in sent
    assert transport.receive_frame().payload == b"old"
    assert transport.receive_frame().payload == b"new"


def test_observation_transport_rejects_invalid_binary_payload_at_metadata_boundary() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            b"orphan-payload",
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"frame_encoding": "json-binary"})

    with pytest.raises(DaemonHTTPError, match="invalid observation binary envelope"):
        transport.receive_frame()


def test_observation_transport_timing_rejects_invalid_binary_payload_at_metadata_boundary() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            b"orphan-payload",
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"frame_encoding": "json-binary"})

    with pytest.raises(DaemonHTTPError, match="invalid observation binary envelope"):
        transport.recv_frame_with_timing()


def test_observation_transport_json_binary_stream_accepts_binary_envelope_frame() -> None:
    envelope = observation_routes._encode_frame_envelope(
        {
            "type": "frame",
            "id": "1",
            "seq": 2,
            "kind": "patch",
            "trigger": "run_actions_observe_change",
            "frame_encoding": "binary-envelope",
        },
        b"png",
    )
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            envelope,
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"frame_encoding": "json-binary"})

    frame = transport.receive_frame()

    assert frame.payload == b"png"
    assert frame.metadata["frame_encoding"] == "binary-envelope"


def test_observation_transport_json_binary_stream_buffers_mixed_correlated_frame() -> None:
    target = observation_routes._encode_frame_envelope(
        {
            "type": "frame",
            "id": "2",
            "seq": 2,
            "kind": "patch",
            "trigger": "run_actions_observe_change",
            "causal_frame": True,
            "frame_encoding": "binary-envelope",
        },
        b"png",
    )
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            json.dumps({"type": "frame", "id": "other", "seq": 1, "kind": "keyframe"}),
            b"old",
            target,
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"frame_encoding": "json-binary"})

    frame = transport.run_actions_observe_change_and_recv({"actions": []})

    assert frame.payload == b"png"
    assert frame.metadata["id"] == "2"
    assert frame.metadata["frame_encoding"] == "binary-envelope"
    assert transport.receive_frame().payload == b"old"


def test_observation_transport_binary_envelope_receives_correlated_frame() -> None:
    old = observation_routes._encode_frame_envelope(
        {"type": "frame", "id": "other", "seq": 1, "kind": "keyframe"},
        b"old",
    )
    target = observation_routes._encode_frame_envelope(
        {
            "type": "frame",
            "id": "2",
            "seq": 2,
            "kind": "patch",
            "trigger": "run_actions_observe_change",
            "causal_frame": True,
        },
        b"png",
    )
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "started", "id": "1"}),
            old,
            target,
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )
    transport.start({"frame_encoding": "binary-envelope"})

    frame = transport.run_actions_observe_change_and_recv({"actions": []})

    assert frame.payload == b"png"
    assert frame.metadata["id"] == "2"
    assert frame.metadata["causal_frame"] is True
    assert transport.receive_frame().payload == b"old"


def test_observation_frame_composes_lossless_patch_bundle() -> None:
    base = Image.new("RGB", (8, 8), "white")
    patch_a = Image.new("RGB", (2, 2), "black")
    patch_b = Image.new("RGB", (2, 2), "red")
    base_bytes = _image_png_bytes(base)
    patch_a_bytes = _image_png_bytes(patch_a)
    patch_b_bytes = _image_png_bytes(patch_b)
    manifest = {
        "patches": [
            {"x": 0, "y": 0, "width": 2, "height": 2, "size_bytes": len(patch_a_bytes)},
            {"x": 6, "y": 6, "width": 2, "height": 2, "size_bytes": len(patch_b_bytes)},
        ]
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    payload = (
        len(manifest_bytes).to_bytes(4, "big")
        + manifest_bytes
        + patch_a_bytes
        + patch_b_bytes
    )
    frame = ObservationFrame(
        payload=payload,
        metadata={"kind": "patches", "format": "png", "seq": 2},
    )

    composed_payload = frame.compose(base_bytes)
    assert composed_payload is not None
    composed = Image.open(BytesIO(composed_payload)).convert("RGB")

    assert composed.getpixel((0, 0)) == (0, 0, 0)
    assert composed.getpixel((6, 6)) == (255, 0, 0)
    assert composed.getpixel((4, 4)) == (255, 255, 255)


def test_observation_transport_probe_splits_receive_timing() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "transport_probe", "id": "1", "ok": True, "size_bytes": 3}),
            b"abc",
            json.dumps(
                {
                    "type": "transport_timing",
                    "id": "1",
                    "seq": None,
                    "server_emit_timing_ms": {
                        "metadata_send_ms": 1.0,
                        "payload_send_ms": 2.0,
                        "emit_total_ms": 3.0,
                    },
                }
            ),
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )

    result = transport.transport_probe(size_bytes=3)

    assert result["size_bytes"] == 3
    assert result["requested_size_bytes"] == 3
    assert result["server_emit_timing_ms"]["emit_total_ms"] == 3.0
    assert result["client_receive_timing_ms"]["wait_metadata_ms"] >= 0


def test_observation_transport_probe_receives_binary_envelope() -> None:
    envelope = observation_routes._encode_frame_envelope(
        {
            "type": "transport_probe",
            "id": "1",
            "ok": True,
            "size_bytes": 3,
            "frame_encoding": "binary-envelope",
        },
        b"abc",
    )
    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "ready"}),
            envelope,
            json.dumps(
                {
                    "type": "transport_timing",
                    "id": "1",
                    "seq": None,
                    "server_emit_timing_ms": {
                        "metadata_send_ms": 1.0,
                        "payload_send_ms": 0.0,
                        "emit_total_ms": 2.0,
                    },
                }
            ),
        ]
    )
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )

    result = transport.transport_probe(size_bytes=3, frame_encoding="binary-envelope")

    assert result["size_bytes"] == 3
    assert result["frame_encoding"] == "binary-envelope"
    assert result["server_emit_timing_ms"]["emit_total_ms"] == 2.0
    assert result["client_receive_timing_ms"]["wait_payload_ms"] == 0.0


def test_observation_frame_compose_applies_patch() -> None:
    from io import BytesIO

    from PIL import Image

    base = Image.new("RGB", (8, 8), "white")
    patch = Image.new("RGB", (2, 2), "black")
    base_io = BytesIO()
    patch_io = BytesIO()
    base.save(base_io, format="PNG")
    patch.save(patch_io, format="PNG")

    frame = ObservationFrame(
        payload=patch_io.getvalue(),
        metadata={
            "kind": "patch",
            "format": "png",
            "dirty_rect": {"x": 3, "y": 4, "width": 2, "height": 2},
        },
    )

    composed = frame.compose(base_io.getvalue())
    image = Image.open(BytesIO(composed or b"")).convert("RGB")

    assert image.getpixel((3, 4)) == (0, 0, 0)
    assert image.getpixel((0, 0)) == (255, 255, 255)


class _FakeObservationTransport:
    def __init__(self, frames):
        self._frames = frames
        self._frame_index = 0
        self.payload = {}
        self.requested_frame = False
        self.action_payload = None
        self.change_payload = None
        self.started = False

    def frames(self, payload):
        self.payload = payload
        yield from self._frames

    def start(self, payload):
        self.payload = payload
        self.started = True

    def receive_frame(self, *, transport_timing=False):
        frame = self._frames[self._frame_index]
        self._frame_index += 1
        return frame

    def close(self):
        pass

    def pause(self):
        pass

    def resume(self):
        pass

    def request_frame(self):
        self.requested_frame = True

    def run_actions_capture(self, payload):
        self.action_payload = payload

    def run_actions_observe_change(self, payload):
        self.change_payload = payload

    def run_actions_observe_change_and_recv(self, payload, *, transport_timing=False):
        self.change_payload = payload
        return self.receive_frame(transport_timing=transport_timing)

    def configure(self, payload):
        self.payload.update(payload)


class _FakeWebSocket:
    def __init__(self, messages):
        self._messages = iter(messages)
        self.sent = []

    def recv(self, **_kwargs):
        return next(self._messages)

    def send(self, message):
        self.sent.append(message)

    def close(self):
        pass


def _screenshot_bytes(color: str) -> CapturedScreenshot:
    image = Image.new("RGB", (8, 8), color)
    output = BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    return CapturedScreenshot(
        format="png",
        width=8,
        height=8,
        data=data,
        sha256=sha256_bytes(data),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=8,
            desktop_height=8,
            image_width=8,
            image_height=8,
        ),
        cursor_visible=False,
        capture_backend="test",
        timings_ms={"total_ms": 0.0},
    )


def _raw_screenshot_bytes(color: str) -> CapturedRawScreenshot:
    image = Image.new("RGB", (64, 64), color)
    return _raw_screenshot_from_image(image)


def _raw_screenshot_from_image(
    image: Image.Image,
    *,
    region: Region | None = None,
) -> CapturedRawScreenshot:
    source = image.crop(
        (
            region.x,
            region.y,
            region.x + region.width,
            region.y + region.height,
        )
    ) if region is not None else image
    rgb = image.tobytes()
    if region is not None:
        rgb = source.tobytes()
    return CapturedRawScreenshot(
        width=source.width,
        height=source.height,
        rgb=rgb,
        sha256=sha256_bytes(rgb),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=image.width,
            desktop_height=image.height,
            image_width=source.width,
            image_height=source.height,
            source_region=region,
        ),
        cursor_visible=False,
        capture_backend="test-raw",
        timings_ms={"total_ms": 0.0},
    )


def _image_png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _raw_screenshot_with_square() -> CapturedRawScreenshot:
    image = Image.new("RGB", (64, 64), "white")
    for y in range(18, 22):
        for x in range(18, 22):
            image.putpixel((x, y), (0, 0, 0))
    rgb = image.tobytes()
    return CapturedRawScreenshot(
        width=64,
        height=64,
        rgb=rgb,
        sha256=sha256_bytes(rgb),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=64,
            desktop_height=64,
            image_width=64,
            image_height=64,
        ),
        cursor_visible=False,
        capture_backend="test-raw",
        timings_ms={"total_ms": 0.0},
    )


def _raw_screenshot_with_two_squares() -> CapturedRawScreenshot:
    image = Image.new("RGB", (64, 64), "white")
    for y in range(2, 6):
        for x in range(2, 6):
            image.putpixel((x, y), (0, 0, 0))
    for y in range(50, 54):
        for x in range(50, 54):
            image.putpixel((x, y), (0, 0, 0))
    rgb = image.tobytes()
    return CapturedRawScreenshot(
        width=64,
        height=64,
        rgb=rgb,
        sha256=sha256_bytes(rgb),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=64,
            desktop_height=64,
            image_width=64,
            image_height=64,
        ),
        cursor_visible=False,
        capture_backend="test-raw",
        timings_ms={"total_ms": 0.0},
    )
