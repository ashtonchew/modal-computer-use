from __future__ import annotations

import json
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
from modal_computer_use.models import CoordinateSpace, sha256_bytes
from modal_computer_use.observations import ObservationClient
from modal_computer_use.transports.observation import (
    ObservationFrame,
    ObservationStreamTransport,
    _decode_frame_envelope,
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


def test_observation_stream_rejects_missing_auth(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/v1/observations/stream"),
    ):
        pass

    assert exc.value.code == 1008


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

        def __init__(self, *, display: str) -> None:
            self.display = display
            self.closed = False
            self.armed = 0
            FakeXDamageWatcher.instances.append(self)

        def arm(self) -> None:
            self.armed += 1

        def wait(self, timeout_ms: int):
            assert timeout_ms == 100
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
    assert second["change_stage_timing_ms"]["signal_wait_wall_ms"] >= 0
    assert second["change_stage_timing_ms"]["server_pre_emit_ms"] >= 0
    assert len(FakeXDamageWatcher.instances) == 1
    assert FakeXDamageWatcher.instances[0].armed == 1
    assert FakeXDamageWatcher.instances[0].closed is True


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

        def __init__(self, *, display: str) -> None:
            self.display = display
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

    assert len(FakeXDamageWatcher.instances) == 1
    assert FakeXDamageWatcher.instances[0].armed == 2
    assert FakeXDamageWatcher.instances[0].waits == 2
    assert FakeXDamageWatcher.instances[0].closed is True


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

        def __init__(self, *, display: str) -> None:
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


def test_observation_client_defaults_to_lossless_png() -> None:
    transport = _FakeObservationTransport([])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    list(client.frames())

    assert transport.payload["format"] == "png"
    assert transport.payload["show_cursor"] is False
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
    transport = _FakeObservationTransport([frame])
    client = ObservationClient(transport, max_frames=0)  # type: ignore[arg-type]

    result = client.act_and_observe(actions=[{"type": "wait", "duration_ms": 0}])

    assert result.frame is frame
    assert result.action_id == "2"
    assert result.action_result == {"ok": True}
    assert result.change_detected is True
    assert transport.change_payload["actions"] == [{"type": "wait", "duration_ms": 0}]
    assert transport.change_payload["change_signal"] == "auto"
    assert "continue_on_error" not in transport.change_payload


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
    websocket = _FakeWebSocket([json.dumps({"type": "ready"}), envelope])
    transport = ObservationStreamTransport(
        "http://daemon.test",
        websocket=websocket,  # type: ignore[arg-type]
    )

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
        return self._frames[0]

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
        return self._frames[0]

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
