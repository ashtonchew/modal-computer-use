from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.desktop import screenshot_capture
from modal_computer_use.daemon.desktop.screenshots import X11ScreenshotController, _MSSCapture
from modal_computer_use.daemon.desktop.x11 import MockDesktopBackend
from modal_computer_use.daemon.leases import (
    LEASE_EPOCH_HEADER,
    LEASE_FENCE_HEADER,
    LEASE_ID_HEADER,
    LEASE_TOKEN_HEADER,
)
from modal_computer_use.daemon.receipts import OPERATION_SEQUENCE_HEADER
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import Point, Region, ScreenshotOptions

WIDTH = 10
HEIGHT = 8
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_bytes(size: tuple[int, int], color: str = "white") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


class _FakeNativeSession:
    instances: ClassVar[list[_FakeNativeSession]] = []
    capture_calls: ClassVar[list[tuple[int, int, int, int]]] = []
    fail_capture = False

    def __init__(self, _display: str, _width: int, _height: int) -> None:
        self.closed = False
        type(self).instances.append(self)

    def capture_png(self, x: int, y: int, width: int, height: int) -> bytes:
        type(self).capture_calls.append((x, y, width, height))
        if type(self).fail_capture:
            raise RuntimeError("X connection closed")
        return _png_bytes((width, height), color="white")

    def close(self) -> None:
        self.closed = True


class _ConsumerBackend(MockDesktopBackend):
    def __init__(self, controller: X11ScreenshotController) -> None:
        super().__init__(width=WIDTH, height=HEIGHT)
        self.controller = controller
        self.screenshot_backends: list[str | None] = []
        self.screenshot_calls: list[tuple[ScreenshotOptions, Region | None]] = []
        self.click_calls: list[tuple[int | None, int | None]] = []

    async def ready(self) -> tuple[bool, list[str]]:
        return True, []

    async def mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: tuple[str, ...] = (),
    ) -> Point:
        del button, count, modifiers
        self.click_calls.append((x, y))
        return await super().mouse_click(x, y)

    async def screenshot(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        artifact_store=None,
        call_id: str | None = None,
        retention_class: str = "ephemeral",
    ):
        self.screenshot_calls.append((options, region))
        result = await self.controller.capture(
            options,
            region=region,
            artifact_store=artifact_store,
            call_id=call_id,
            retention_class=retention_class,
        )
        return result

    async def screenshot_bytes(
        self,
        options: ScreenshotOptions,
        *,
        region: Region | None = None,
        include_cursor_position: bool = False,
        prefer_native_png: bool = False,
    ):
        self.screenshot_calls.append((options, region))
        result = await self.controller.capture_bytes(
            options,
            region=region,
            include_cursor_position=include_cursor_position,
            prefer_native_png=prefer_native_png,
        )
        self.screenshot_backends.append(result.capture_backend)
        return result

    async def screenshot_raw_pixels(self, *, region: Region | None = None):
        return await self.controller.capture_raw_pixels(region=region)

    def close(self) -> None:
        self.controller.close()


def _fake_mss_capture(source: Region) -> _MSSCapture:
    class Shot:
        rgb = b"\x00\x00\x00" * (source.width * source.height)
        bgra = b"\x00\x00\x00\xff" * (source.width * source.height)

    return _MSSCapture(shot=Shot(), width=source.width, height=source.height)


def _make_controller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    capture_source: str = "x11-shm",
    native_failure: bool = False,
    file_commands: list[tuple[str, ...]] | None = None,
) -> tuple[X11ScreenshotController, _ConsumerBackend]:
    _FakeNativeSession.instances = []
    _FakeNativeSession.capture_calls = []
    _FakeNativeSession.fail_capture = native_failure
    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(X11SharedMemoryScreenshotSession=_FakeNativeSession),
    )
    monkeypatch.setitem(
        sys.modules,
        "mss",
        SimpleNamespace(
            tools=SimpleNamespace(
                to_png=lambda _rgb, size, level=1: _png_bytes(size),
            )
        ),
    )

    async def run(*args: str, **_kwargs: Any):
        assert file_commands is not None
        file_commands.append(args)
        Path(args[-1]).write_bytes(_png_bytes((WIDTH, HEIGHT)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def cursor_position() -> Point:
        return Point(x=1, y=2)

    controller = X11ScreenshotController(
        run=run,
        width=WIDTH,
        height=HEIGHT,
        display=":99",
        cursor_position=cursor_position,
        capture_source=capture_source,
    )
    monkeypatch.setattr(controller._mss, "grab", _fake_mss_capture)
    backend = _ConsumerBackend(controller)
    return controller, backend


def _app(tmp_path: Path, backend: _ConsumerBackend):
    app = create_app(
        DaemonSettings(
            backend="mock",
            desktop_width=WIDTH,
            desktop_height=HEIGHT,
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
        )
    )
    app.state.backend = backend
    return app


@pytest.fixture(autouse=True)
def reset_native_module_cache() -> None:
    screenshot_capture._reset_module_cache_for_tests()
    yield
    screenshot_capture._reset_module_cache_for_tests()


def test_native_source_reaches_all_complete_screenshot_consumers(tmp_path, monkeypatch) -> None:
    controller, backend = _make_controller(monkeypatch)
    app = _app(tmp_path, backend)
    region = {"x": 2, "y": 1, "width": 5, "height": 4}
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        full_raw = client.post(
            "/v1/screenshots/full/raw",
            json={"format": "png", "show_cursor": False, "scale": 1.0},
        )
        region_raw = client.post(
            "/v1/screenshots/region/raw",
            json={
                "format": "png",
                "show_cursor": False,
                "scale": 1.0,
                "region": region,
            },
        )
        structured_inline = client.post(
            "/v1/screenshots/full",
            json={
                "format": "png",
                "show_cursor": False,
                "scale": 1.0,
                "storage": "inline",
            },
        )
        structured_artifact = client.post(
            "/v1/screenshots/region",
            json={
                "format": "png",
                "show_cursor": False,
                "scale": 1.0,
                "storage": "artifact",
                "region": region,
            },
        )
        action_raw = client.post(
            "/v1/actions/run/raw-screenshot",
            json={
                "actions": [{"type": "click", "x": 2, "y": 2}],
                "screenshot_after": True,
                "screenshot_options": {"format": "png", "show_cursor": False},
            },
        )
        action_structured = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "click", "x": 3, "y": 3}],
                "screenshot_after": True,
                "screenshot_options": {
                    "format": "png",
                    "show_cursor": False,
                    "storage": "inline",
                },
            },
        )

        with client.websocket_connect("/v1/session/hot") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "id": "hot-screenshot",
                    "op": "screenshot_raw",
                    "payload": {"format": "png", "show_cursor": False},
                }
            )
            hot_screenshot = websocket.receive_json()
            assert websocket.receive_bytes().startswith(PNG_SIGNATURE)
            websocket.send_json(
                {
                    "id": "hot-action",
                    "op": "run_raw_screenshot",
                    "payload": {
                        "actions": [{"type": "click", "x": 4, "y": 4}],
                        "screenshot_after": True,
                        "screenshot_options": {"format": "png", "show_cursor": False},
                    },
                }
            )
            hot_action = websocket.receive_json()
            assert websocket.receive_bytes().startswith(PNG_SIGNATURE)

    assert full_raw.status_code == 200
    assert region_raw.status_code == 200
    assert structured_inline.status_code == 200
    assert structured_artifact.status_code == 200
    assert action_raw.status_code == 200
    assert action_structured.status_code == 200
    assert hot_screenshot["type"] == "binary"
    assert hot_action["type"] == "binary"
    assert structured_inline.json()["data_base64"]
    assert structured_artifact.json()["artifact_uri"].startswith("artifact://")
    assert full_raw.headers["x-computer-use-capture-backend"] == "x11-shm"
    assert region_raw.headers["x-computer-use-capture-backend"] == "x11-shm"
    assert action_raw.headers["x-computer-use-capture-backend"] == "x11-shm"
    assert len(_FakeNativeSession.capture_calls) == 8
    assert backend.click_calls == [(2, 2), (3, 3), (4, 4)]
    assert _FakeNativeSession.capture_calls[0] == (0, 0, WIDTH, HEIGHT)
    assert _FakeNativeSession.capture_calls[1] == (2, 1, 5, 4)
    controller.close()


@pytest.mark.parametrize(
    "options",
    [
        {"format": "png", "show_cursor": True},
        {"format": "png", "show_cursor": False, "scale": 0.5},
        {"format": "jpeg", "show_cursor": False},
        {"format": "webp", "show_cursor": False},
    ],
)
def test_non_native_options_keep_existing_capture_paths(
    tmp_path,
    monkeypatch,
    options: dict[str, object],
) -> None:
    commands: list[tuple[str, ...]] = []
    controller, backend = _make_controller(monkeypatch, file_commands=commands)
    app = _app(tmp_path, backend)

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/screenshots/full/raw", json=options)

    assert response.status_code == 200
    assert _FakeNativeSession.capture_calls == []
    # Opening the optional X connection is itself avoidable work for formats,
    # scaling, and cursor-visible captures that deliberately stay on the
    # existing path.
    assert _FakeNativeSession.instances == []
    if options["show_cursor"]:
        assert commands and commands[0][0] == "maim"
    else:
        assert not commands
    controller.close()


def test_raw_rgb_pixels_remain_mss_owned(tmp_path, monkeypatch) -> None:
    controller, backend = _make_controller(monkeypatch)
    # This seam is intentionally not a public HTTP route; change observation
    # and raw-pixel consumers call it directly rather than asking for PNG.
    import asyncio

    captured = asyncio.run(backend.screenshot_raw_pixels())

    assert captured is not None
    assert captured.capture_backend == "mss-raw"
    assert _FakeNativeSession.capture_calls == []
    controller.close()


def test_auto_failure_sticks_to_mss_fallback_for_complete_surfaces(tmp_path, monkeypatch) -> None:
    controller, backend = _make_controller(monkeypatch, capture_source="auto", native_failure=True)
    app = _app(tmp_path, backend)
    region = {"x": 1, "y": 1, "width": 4, "height": 3}

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        full = client.post(
            "/v1/screenshots/full/raw", json={"format": "png", "show_cursor": False}
        )
        structured_inline = client.post(
            "/v1/screenshots/full",
            json={"format": "png", "show_cursor": False, "storage": "inline"},
        )
        structured_artifact = client.post(
            "/v1/screenshots/region",
            json={
                "format": "png",
                "show_cursor": False,
                "storage": "artifact",
                "region": region,
            },
        )
        region_raw = client.post(
            "/v1/screenshots/region/raw",
            json={"format": "png", "show_cursor": False, "region": region},
        )
        action = client.post(
            "/v1/actions/run/raw-screenshot",
            json={
                "actions": [{"type": "click", "x": 1, "y": 1}],
                "screenshot_after": True,
                "screenshot_options": {"format": "png", "show_cursor": False},
            },
        )
        with client.websocket_connect("/v1/session/hot") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "id": "fallback-hot-screenshot",
                    "op": "screenshot_raw",
                    "payload": {"format": "png", "show_cursor": False},
                }
            )
            hot_screenshot = websocket.receive_json()
            assert websocket.receive_bytes().startswith(PNG_SIGNATURE)
            websocket.send_json(
                {
                    "id": "fallback-hot-action",
                    "op": "run_raw_screenshot",
                    "payload": {
                        "actions": [{"type": "click", "x": 2, "y": 2}],
                        "screenshot_after": True,
                        "screenshot_options": {"format": "png", "show_cursor": False},
                    },
                }
            )
            hot_action = websocket.receive_json()
            assert websocket.receive_bytes().startswith(PNG_SIGNATURE)

    assert full.status_code == 200
    assert structured_inline.status_code == 200
    assert structured_artifact.status_code == 200
    assert region_raw.status_code == 200
    assert action.status_code == 200
    assert full.headers["x-computer-use-capture-backend"] == "mss-fallback"
    assert region_raw.headers["x-computer-use-capture-backend"] == "mss-fallback"
    assert action.headers["x-computer-use-capture-backend"] == "mss-fallback"
    assert hot_screenshot["headers"]["x-computer-use-capture-backend"] == "mss-fallback"
    assert hot_action["headers"]["x-computer-use-capture-backend"] == "mss-fallback"
    assert len(_FakeNativeSession.capture_calls) == 1
    assert len(_FakeNativeSession.instances) == 1
    assert backend.click_calls == [(1, 1), (2, 2)]
    controller.close()


def test_explicit_native_failure_does_not_replay_action(tmp_path, monkeypatch) -> None:
    controller, backend = _make_controller(
        monkeypatch,
        capture_source="x11-shm",
        native_failure=True,
    )
    app = _app(tmp_path, backend)
    payload = {
        "actions": [{"type": "click", "x": 2, "y": 2}],
        "screenshot_after": True,
        "screenshot_options": {"format": "png", "show_cursor": False},
    }

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        grant_response = client.post(
            "/v1/leases/acquire", json={"run_id": "native-failure-run"}
        )
        assert grant_response.status_code == 200, grant_response.text
        grant = grant_response.json()
        lease_headers = {
            LEASE_ID_HEADER: grant["lease_id"],
            LEASE_EPOCH_HEADER: grant["daemon_epoch"],
            LEASE_FENCE_HEADER: str(grant["fence"]),
            LEASE_TOKEN_HEADER: grant_response.headers[LEASE_TOKEN_HEADER],
            OPERATION_SEQUENCE_HEADER: "0",
        }
        first = client.post(
            "/v1/actions/run/raw-screenshot",
            json=payload,
            headers=lease_headers,
        )
        second = client.post(
            "/v1/actions/run/raw-screenshot",
            json=payload,
            headers=lease_headers,
        )

    assert first.status_code >= 400
    assert second.status_code >= 400
    assert backend.click_calls == [(2, 2)]
    assert len(_FakeNativeSession.capture_calls) == 1
    controller.close()
