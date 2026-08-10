from __future__ import annotations

import asyncio
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.desktop import screenshots as screenshots_module
from modal_computer_use.daemon.desktop.screenshot_capture import ScreenshotCaptureResolution
from modal_computer_use.daemon.desktop.x11 import X11DesktopBackend
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import Point


class CaptureContractBackend(X11DesktopBackend):
    def __init__(self) -> None:
        super().__init__(width=10, height=10, display=":99")
        self.commands: list[tuple[str, ...]] = []

    async def ready(self) -> tuple[bool, list[str]]:
        return True, []

    async def mouse_position(self) -> Point:
        return Point(x=0, y=0)

    async def _run(self, *args: str, **_kwargs):
        self.commands.append(args)
        raise AssertionError("cursor-hidden MSS capture must not launch a subprocess")


def test_cursor_hidden_raw_routes_reuse_one_mss_session_and_close_it_at_shutdown(
    tmp_path,
    monkeypatch,
) -> None:
    native_png = _png_bytes()
    instances: list[object] = []
    open_arguments: list[dict[str, object]] = []

    class FakeMSS:
        def __init__(self, **kwargs: object) -> None:
            open_arguments.append(kwargs)
            self.closed = False
            self.grabs = 0
            instances.append(self)

        def grab(self, _monitor: dict[str, int]) -> object:
            self.grabs += 1
            return SimpleNamespace(rgb=b"\x00\x00\x00" * 100)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(
        sys.modules,
        "mss",
        SimpleNamespace(
            MSS=FakeMSS,
            tools=SimpleNamespace(to_png=lambda *_args, **_kwargs: native_png),
        ),
    )
    monkeypatch.setattr(
        screenshots_module.tempfile,
        "NamedTemporaryFile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cursor-hidden MSS capture must not create a temporary file")
        ),
    )
    backend = CaptureContractBackend()
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
        )
    )
    app.state.backend = backend

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post(
            "/v1/screenshots/full/raw",
            json={"format": "png", "show_cursor": False},
        )
        second = client.post(
            "/v1/screenshots/full/raw",
            json={"format": "png", "show_cursor": False},
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.content == native_png
        assert second.content == native_png
        assert first.headers["x-computer-use-capture-backend"] == "mss"
        assert second.headers["x-computer-use-capture-backend"] == "mss"
        assert backend.commands == []
        assert len(instances) == 1
        assert instances[0].grabs == 2
        assert instances[0].closed is False

    assert open_arguments == [{"display": ":99", "backend": "xshmgetimage"}]
    assert instances[0].closed is True


def test_cursor_visible_raw_route_uses_one_bounded_file_capture(tmp_path, monkeypatch) -> None:
    native_png = _png_bytes()
    backend = CaptureContractBackend()
    captured_paths: list[Path] = []

    class UnexpectedMSS:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("cursor-visible capture must not open an MSS session")

    async def capture_file(*args: str, **_kwargs):
        backend.commands.append(args)
        path = Path(args[-1])
        captured_paths.append(path)
        path.write_bytes(native_png)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=UnexpectedMSS))
    monkeypatch.setattr(backend, "_run", capture_file)
    app = _capture_app(tmp_path, backend)

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/screenshots/full/raw",
            json={"format": "png", "show_cursor": True},
        )

    assert response.status_code == 200
    assert response.content == native_png
    assert response.headers["x-computer-use-capture-backend"] == "maim"
    assert len(backend.commands) == 1
    assert backend.commands[0][0] == "maim"
    assert captured_paths and all(not path.exists() for path in captured_paths)


def test_failed_mss_display_connection_reopens_once_then_uses_file_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    native_png = _png_bytes()
    backend = CaptureContractBackend()
    instances: list[object] = []
    open_arguments: list[dict[str, object]] = []

    class UnavailableMSS:
        def __init__(self, **kwargs: object) -> None:
            open_arguments.append(kwargs)
            self.closed = False
            self.grabs = 0
            instances.append(self)

        def grab(self, _monitor: dict[str, int]) -> object:
            self.grabs += 1
            raise RuntimeError("display connection unavailable")

        def close(self) -> None:
            self.closed = True

    async def capture_file(*args: str, **_kwargs):
        backend.commands.append(args)
        Path(args[-1]).write_bytes(native_png)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=UnavailableMSS))
    monkeypatch.setattr(backend, "_run", capture_file)
    app = _capture_app(tmp_path, backend)

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/screenshots/full/raw",
            json={"format": "png", "show_cursor": False},
        )

    assert response.status_code == 200
    assert response.content == native_png
    assert response.headers["x-computer-use-capture-backend"] == "scrot"
    assert open_arguments == [
        {"display": ":99", "backend": "xshmgetimage"},
        {"display": ":99", "backend": "xshmgetimage"},
    ]
    assert len(instances) == 2
    assert all(instance.grabs == 1 for instance in instances)
    assert all(instance.closed is True for instance in instances)
    assert len(backend.commands) == 1
    assert backend.commands[0][0] == "scrot"


def test_capture_failure_is_request_scoped_and_next_capture_can_succeed(
    tmp_path,
    monkeypatch,
) -> None:
    native_png = _png_bytes()
    backend = CaptureContractBackend()

    class UnavailableMSS:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def grab(self, _monitor: dict[str, int]) -> object:
            raise RuntimeError("transient Xlib capture race")

        def close(self) -> None:
            pass

    async def fail_one_request_then_capture(*args: str, **_kwargs):
        backend.commands.append(args)
        if len(backend.commands) <= 2:
            raise RuntimeError("transient file capture failure")
        Path(args[-1]).write_bytes(native_png)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=UnavailableMSS))
    monkeypatch.setattr(backend, "_run", fail_one_request_then_capture)
    app = _capture_app(tmp_path, backend)

    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        failed = client.post(
            "/v1/screenshots/full/raw",
            json={"format": "png", "show_cursor": False},
        )
        health = client.get("/healthz")
        recovered = client.post(
            "/v1/screenshots/full/raw",
            json={"format": "png", "show_cursor": False},
        )

    assert failed.status_code == 500
    assert health.status_code == 200
    assert health.json() == {"ok": True}
    assert recovered.status_code == 200
    assert recovered.content == native_png
    assert recovered.headers["x-computer-use-capture-backend"] == "scrot"


@pytest.mark.asyncio
async def test_native_capture_wait_does_not_block_daemon_health(tmp_path, monkeypatch) -> None:
    enter_timeout = 3.0
    release_delay = 1.0
    health_timeout = 2.0
    # Keep this below the release delay so healthy offload answers while the
    # capture remains pending; synchronous capture completes first and fails
    # the pending-screenshot assertion below.
    health_max_elapsed = 0.5
    entered = threading.Event()
    release = threading.Event()
    releaser_errors: list[str] = []

    class BlockingNativeSession:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def capture_png(self, **kwargs: int) -> bytes:
            entered.set()
            release.wait(timeout=enter_timeout + release_delay)
            return _png_bytes(width=kwargs["width"], height=kwargs["height"])

        def close(self) -> None:
            pass

    class NativeCaptureBackend(CaptureContractBackend):
        def __init__(self) -> None:
            X11DesktopBackend.__init__(
                self,
                width=10,
                height=10,
                display=":99",
                capture_source="x11-shm",
            )
            self.commands = []

    monkeypatch.setattr(
        screenshots_module,
        "resolve_capture_source",
        lambda _source: ScreenshotCaptureResolution(requested="x11-shm", selected="x11-shm"),
    )
    monkeypatch.setattr(
        screenshots_module,
        "X11SharedMemoryScreenshotSession",
        BlockingNativeSession,
    )
    backend = NativeCaptureBackend()
    app = _capture_app(tmp_path, backend)

    def release_capture() -> None:
        if not entered.wait(timeout=enter_timeout):
            releaser_errors.append("capture did not enter the blocking session")
            release.set()
            return
        time.sleep(release_delay)
        release.set()

    releaser = threading.Thread(target=release_capture)
    releaser.start()
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"Authorization": "Bearer dev"},
            ) as screenshot_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"Authorization": "Bearer dev"},
            ) as health_client,
        ):
            screenshot = asyncio.create_task(
                screenshot_client.post(
                    "/v1/screenshots/full/raw",
                    json={"format": "png", "show_cursor": False},
                )
            )
            assert await asyncio.wait_for(
                asyncio.to_thread(entered.wait, enter_timeout),
                timeout=enter_timeout,
            )
            started = time.perf_counter()
            health_request = asyncio.create_task(health_client.get("/healthz"))
            health = await asyncio.wait_for(health_request, timeout=health_timeout)
            elapsed = time.perf_counter() - started
            assert elapsed < health_max_elapsed
            assert health.status_code == 200
            assert not screenshot.done()
            release.set()
            response = await screenshot
            assert response.status_code == 200
            assert response.headers["x-computer-use-capture-backend"] == "x11-shm"
    finally:
        release.set()
        releaser.join(timeout=enter_timeout)
        assert not releaser.is_alive()
        assert releaser_errors == []


def _png_bytes(*, width: int = 10, height: int = 10) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), "black").save(output, format="PNG")
    return output.getvalue()


def _capture_app(tmp_path, backend: CaptureContractBackend):
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
        )
    )
    app.state.backend = backend
    return app
