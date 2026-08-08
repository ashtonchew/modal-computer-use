from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.desktop import screenshots as screenshots_module
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


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (10, 10), "black").save(output, format="PNG")
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
