from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

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
from modal_computer_use.transports.observation import ObservationFrame


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
    assert header["content_type"] == "image/png"
    assert header["width"] == 1024
    assert header["height"] == 768
    assert header["timing_ms"]["observation_total_ms"] >= 0
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert stopped["type"] == "stopped"
    assert stopped["reason"] == "max_frames"


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
    assert second["kind"] == "patch"
    assert second["change_detected"] is True
    assert second["change_timeout_reached"] is False
    assert second["change_attempts"] == 2
    assert second["change_wait_ms"] >= 0
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

    def frames(self, payload):
        self.payload = payload
        yield from self._frames

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

    def configure(self, payload):
        self.payload.update(payload)


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
