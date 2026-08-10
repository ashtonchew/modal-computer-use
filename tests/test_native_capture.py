from __future__ import annotations

import asyncio
import os
import time
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from modal_computer_use.daemon.desktop import screenshot_capture
from modal_computer_use.daemon.desktop import screenshots as screenshots_module
from modal_computer_use.daemon.desktop.screenshots import X11ScreenshotController
from modal_computer_use.models import Point, ScreenshotOptions


def setup_function() -> None:
    screenshot_capture._reset_module_cache_for_tests()


def teardown_function() -> None:
    screenshot_capture._reset_module_cache_for_tests()


def _png_bytes(size: tuple[int, int], color: str = "white") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def test_mss_is_explicit_and_does_not_import_native(monkeypatch) -> None:
    monkeypatch.setattr(screenshot_capture.importlib, "import_module", lambda _name: pytest.fail())

    resolution = screenshot_capture.resolve_capture_source("mss")

    assert resolution.selected == "mss"
    assert resolution.requested == "mss"


def test_auto_selects_mss_when_native_extension_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        screenshot_capture.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("not built")),
    )

    resolution = screenshot_capture.resolve_capture_source("auto")

    assert resolution.selected == "mss"
    assert resolution.reason == "X11 shared-memory screenshot extension unavailable"


def test_explicit_x11_shm_remains_selected_for_readiness_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        screenshot_capture.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("not built")),
    )

    resolution = screenshot_capture.resolve_capture_source("x11-shm")

    assert resolution.selected == "x11-shm"
    assert resolution.reason == "X11 shared-memory screenshot extension unavailable"


def test_explicit_x11_shm_readiness_fails_closed_without_extension(monkeypatch) -> None:
    monkeypatch.setattr(screenshot_capture, "_load_module", lambda: None)
    monkeypatch.setattr(
        screenshots_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "maim" else None,
    )
    controller = X11ScreenshotController(
        run=lambda *_args, **_kwargs: pytest.fail("native readiness must fail first"),
        width=10,
        height=10,
        display=":99",
        cursor_position=lambda: _cursor_position(),
        capture_source="x11-shm",
    )

    ready, error = asyncio.run(controller.probe())

    assert ready is False
    assert error == "X11 shared-memory screenshot probe failed"
    controller.close()


def test_native_extension_without_typed_timeout_contract_is_unavailable(monkeypatch) -> None:
    module = SimpleNamespace(
        __name__="_modal_computer_use_x11_shm",
        X11SharedMemoryScreenshotSession=lambda *_args: pytest.fail(
            "an incompatible extension must not open a session"
        ),
    )
    monkeypatch.setattr(screenshot_capture, "_load_module", lambda: module)

    with pytest.raises(
        screenshot_capture.ScreenshotCaptureUnavailable,
        match="incompatible",
    ):
        screenshot_capture.X11SharedMemoryScreenshotSession(
            display=":99", width=10, height=10
        )


def test_x11_shared_memory_adapter_uses_private_extension_abi(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeSession:
        def __init__(self, *args: object) -> None:
            calls.append(("init", *args))

        def capture_png(self, *args: object) -> bytes:
            calls.append(("capture", *args))
            return _png_bytes((10, 11))

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(X11SharedMemoryScreenshotSession=FakeSession),
    )

    session = screenshot_capture.X11SharedMemoryScreenshotSession(
        display=":99", width=1024, height=768
    )

    assert session.capture_png(x=3, y=4, width=10, height=11).startswith(b"\x89PNG")
    session.close()
    session.close()
    assert calls == [
        ("init", ":99", 1024, 768),
        ("capture", 3, 4, 10, 11),
        ("close",),
    ]


def test_canonical_extension_uses_spawned_process_owner(monkeypatch) -> None:
    extension_constructor_calls: list[tuple[object, ...]] = []
    owner_calls: list[tuple[object, ...]] = []

    class CanonicalExtensionSession:
        def __init__(self, *args: object) -> None:
            extension_constructor_calls.append(args)

    class SpawnedOwner:
        def __init__(self, *args: object) -> None:
            owner_calls.append(("init", *args))

        def capture_png(self, x: int, y: int, width: int, height: int) -> bytes:
            owner_calls.append(("capture", x, y, width, height))
            return _png_bytes((width, height))

        def close(self) -> None:
            owner_calls.append(("close",))

    module = SimpleNamespace(
        __name__="_modal_computer_use_x11_shm",
        X11ScreenshotTimeoutError=RuntimeError,
        X11SharedMemoryScreenshotSession=CanonicalExtensionSession,
    )
    monkeypatch.setattr(screenshot_capture, "_load_module", lambda: module)
    monkeypatch.setattr(screenshot_capture, "_SpawnedX11ScreenshotSession", SpawnedOwner)

    session = screenshot_capture.X11SharedMemoryScreenshotSession(
        display=":99", width=1024, height=768
    )

    assert session.capture_png(x=3, y=4, width=10, height=11).startswith(b"\x89PNG")
    session.close()

    assert extension_constructor_calls == []
    assert owner_calls == [
        ("init", ":99", 1024, 768),
        ("capture", 3, 4, 10, 11),
        ("close",),
    ]


def test_spawned_process_owner_round_trips_png_and_reaps_child(tmp_path, monkeypatch) -> None:
    png = _png_bytes((5, 6))
    (tmp_path / "_modal_computer_use_x11_shm.py").write_text(
        "\n".join(
            [
                "class X11ScreenshotTimeoutError(RuntimeError):",
                "    pass",
                "class X11SharedMemoryScreenshotSession:",
                "    def __init__(self, display, width, height):",
                "        self.dimensions = (width, height)",
                "    def capture_png(self, x, y, width, height):",
                f"        return bytes.fromhex({png.hex()!r})",
                "    def close(self):",
                "        pass",
            ]
        )
        + "\n"
    )
    existing_path = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(tmp_path) if not existing_path else f"{tmp_path}{os.pathsep}{existing_path}",
    )
    module = SimpleNamespace(
        __name__="_modal_computer_use_x11_shm",
        X11ScreenshotTimeoutError=RuntimeError,
        X11SharedMemoryScreenshotSession=object,
    )
    monkeypatch.setattr(screenshot_capture, "_load_module", lambda: module)

    session = screenshot_capture.X11SharedMemoryScreenshotSession(
        display=":99", width=10, height=11
    )
    owner = session._session
    process = owner._process

    assert session.capture_png(x=1, y=2, width=5, height=6) == png
    session.close()

    assert process is not None
    assert process.poll() == 0


def test_worker_deadline_keeps_headroom_above_native_reply_budget() -> None:
    assert screenshot_capture._WORKER_OPERATION_TIMEOUT_SECONDS == 1.5


def test_spawned_process_owner_times_out_and_reaps_stalled_child(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "_modal_computer_use_x11_shm.py").write_text(
        "\n".join(
            [
                "import time",
                "class X11ScreenshotTimeoutError(RuntimeError):",
                "    pass",
                "class X11SharedMemoryScreenshotSession:",
                "    def __init__(self, display, width, height):",
                "        pass",
                "    def capture_png(self, x, y, width, height):",
                "        time.sleep(10)",
                "    def close(self):",
                "        pass",
            ]
        )
        + "\n"
    )
    existing_path = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(tmp_path) if not existing_path else f"{tmp_path}{os.pathsep}{existing_path}",
    )
    monkeypatch.setattr(screenshot_capture, "_WORKER_OPERATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(
            __name__="_modal_computer_use_x11_shm",
            X11ScreenshotTimeoutError=RuntimeError,
            X11SharedMemoryScreenshotSession=object,
        ),
    )
    session = screenshot_capture.X11SharedMemoryScreenshotSession(
        display=":99", width=10, height=11
    )
    owner = session._session
    process = owner._process

    started = time.monotonic()
    with pytest.raises(screenshot_capture.ScreenshotCaptureTimedOut) as caught:
        session.capture_png(x=0, y=0, width=10, height=11)

    assert caught.value.timeout_origin == "worker_process_deadline"
    assert time.monotonic() - started < 1
    assert process is not None
    assert process.poll() is not None
    session.close()


def test_spawned_process_owner_classifies_native_reply_deadline(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "_modal_computer_use_x11_shm.py").write_text(
        "\n".join(
            [
                "class X11ScreenshotTimeoutError(RuntimeError):",
                "    pass",
                "class X11SharedMemoryScreenshotSession:",
                "    def __init__(self, display, width, height):",
                "        pass",
                "    def capture_png(self, x, y, width, height):",
                "        raise X11ScreenshotTimeoutError('private native detail')",
                "    def close(self):",
                "        pass",
            ]
        )
        + "\n"
    )
    existing_path = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(tmp_path) if not existing_path else f"{tmp_path}{os.pathsep}{existing_path}",
    )
    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(
            __name__="_modal_computer_use_x11_shm",
            X11ScreenshotTimeoutError=RuntimeError,
            X11SharedMemoryScreenshotSession=object,
        ),
    )
    session = screenshot_capture.X11SharedMemoryScreenshotSession(
        display=":99", width=10, height=11
    )
    owner = session._session
    process = owner._process

    with pytest.raises(screenshot_capture.ScreenshotCaptureTimedOut) as caught:
        session.capture_png(x=0, y=0, width=10, height=11)

    assert caught.value.timeout_origin == "native_x11_reply_deadline"
    assert "private native detail" not in str(caught.value)
    assert process is not None
    assert process.poll() is not None
    session.close()


def test_spawned_process_owner_classifies_native_close_deadline(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "_modal_computer_use_x11_shm.py").write_text(
        "\n".join(
            [
                "class X11ScreenshotTimeoutError(RuntimeError):",
                "    pass",
                "class X11SharedMemoryScreenshotSession:",
                "    def __init__(self, display, width, height):",
                "        pass",
                "    def capture_png(self, x, y, width, height):",
                "        return b'unused'",
                "    def close(self):",
                "        raise X11ScreenshotTimeoutError('private close detail')",
            ]
        )
        + "\n"
    )
    existing_path = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(tmp_path) if not existing_path else f"{tmp_path}{os.pathsep}{existing_path}",
    )
    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(
            __name__="_modal_computer_use_x11_shm",
            X11ScreenshotTimeoutError=RuntimeError,
            X11SharedMemoryScreenshotSession=object,
        ),
    )
    session = screenshot_capture.X11SharedMemoryScreenshotSession(
        display=":99", width=10, height=11
    )
    owner = session._session
    process = owner._process

    with pytest.raises(screenshot_capture.ScreenshotCaptureTimedOut) as caught:
        session.close()

    assert caught.value.timeout_origin == "native_x11_reply_deadline"
    assert "private close detail" not in str(caught.value)
    assert process is not None
    assert process.poll() is not None


def test_x11_shared_memory_adapter_rejects_wrong_png_dimensions(monkeypatch) -> None:
    class FakeSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png(self, *_args: object) -> bytes:
            return _png_bytes((9, 11))

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(X11SharedMemoryScreenshotSession=FakeSession),
    )
    session = screenshot_capture.X11SharedMemoryScreenshotSession(
        display=":99", width=1024, height=768
    )

    with pytest.raises(screenshot_capture.ScreenshotCaptureFailed, match="invalid PNG"):
        session.capture_png(x=3, y=4, width=10, height=11)

    session.close()


def test_x11_setup_timeout_closes_the_probe_socket(monkeypatch) -> None:
    closed = False

    class FakeSocket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _path: str) -> None:
            raise TimeoutError("display stalled")

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(screenshot_capture.socket, "socket", lambda *_args: FakeSocket())

    with pytest.raises(screenshot_capture.ScreenshotCaptureTimedOut):
        screenshot_capture._probe_x11_setup(":99")

    assert closed is True


def test_x11_setup_timeout_in_announced_body_closes_the_probe_socket(monkeypatch) -> None:
    closed = False
    responses: list[bytes | Exception] = [
        b"\x01\x00\x0b\x00\x00\x00\x01\x00",
        TimeoutError("setup body stalled"),
    ]

    class FakeSocket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _path: str) -> None:
            pass

        def sendall(self, _data: bytes) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(screenshot_capture.socket, "socket", lambda *_args: FakeSocket())

    with pytest.raises(screenshot_capture.ScreenshotCaptureTimedOut):
        screenshot_capture._probe_x11_setup(":99")

    assert closed is True


def test_native_runtime_failure_sticks_to_mss_fallback(monkeypatch) -> None:
    constructor_calls = 0

    class BrokenSession:
        def __init__(self, *_args: object) -> None:
            nonlocal constructor_calls
            constructor_calls += 1

        def capture_png(self, *_args: object) -> bytes:
            raise RuntimeError("display disconnected")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(X11SharedMemoryScreenshotSession=BrokenSession),
    )
    controller = X11ScreenshotController(
        run=lambda *_args, **_kwargs: pytest.fail("file capture is not expected"),
        width=10,
        height=10,
        display=":99",
        cursor_position=lambda: _cursor_position(),
        capture_source="auto",
    )
    monkeypatch.setattr(
        controller._mss,
        "grab",
        lambda _source: _fake_mss_capture(10, 10),
    )
    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(X11SharedMemoryScreenshotSession=BrokenSession),
    )

    first = asyncio.run(
        controller.capture_bytes(
            ScreenshotOptions(format="png", show_cursor=False), prefer_native_png=True
        )
    )
    second = asyncio.run(
        controller.capture_bytes(
            ScreenshotOptions(format="png", show_cursor=False), prefer_native_png=True
        )
    )

    assert first.capture_backend == "mss-fallback"
    assert second.capture_backend == "mss-fallback"
    assert constructor_calls == 1
    controller.close()


def test_display_reset_clears_auto_fallback_and_reprobes_native(monkeypatch) -> None:
    module: object

    class BrokenSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png(self, *_args: object) -> bytes:
            raise RuntimeError("display disconnected")

        def close(self) -> None:
            pass

    class HealthySession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png(self, *_args: object) -> bytes:
            return _png_bytes((10, 10))

        def close(self) -> None:
            pass

    module = SimpleNamespace(X11SharedMemoryScreenshotSession=BrokenSession)
    monkeypatch.setattr(screenshot_capture, "_load_module", lambda: module)
    controller = X11ScreenshotController(
        run=lambda *_args, **_kwargs: pytest.fail("file capture is not expected"),
        width=10,
        height=10,
        display=":99",
        cursor_position=lambda: _cursor_position(),
        capture_source="auto",
    )
    monkeypatch.setattr(controller._mss, "grab", lambda _source: _fake_mss_capture(10, 10))

    failed_generation = asyncio.run(
        controller.capture_bytes(
            ScreenshotOptions(format="png", show_cursor=False), prefer_native_png=True
        )
    )
    module = SimpleNamespace(X11SharedMemoryScreenshotSession=HealthySession)
    controller.reset_capture_session()
    restarted_generation = asyncio.run(
        controller.capture_bytes(
            ScreenshotOptions(format="png", show_cursor=False), prefer_native_png=True
        )
    )

    assert failed_generation.capture_backend == "mss-fallback"
    assert restarted_generation.capture_backend == "x11-shm"
    controller.close()


def test_explicit_native_runtime_failure_does_not_fallback(monkeypatch) -> None:
    capture_calls = 0
    close_calls = 0

    class BrokenSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png(self, *_args: object) -> bytes:
            nonlocal capture_calls
            capture_calls += 1
            raise RuntimeError("display disconnected")

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(X11SharedMemoryScreenshotSession=BrokenSession),
    )
    controller = X11ScreenshotController(
        run=lambda *_args, **_kwargs: pytest.fail("file capture is not expected"),
        width=10,
        height=10,
        display=":99",
        cursor_position=lambda: _cursor_position(),
        capture_source="x11-shm",
    )

    for _ in range(2):
        with pytest.raises(screenshot_capture.ScreenshotCaptureFailed):
            asyncio.run(
                controller.capture_bytes(
                    ScreenshotOptions(format="png", show_cursor=False),
                    prefer_native_png=True,
                )
            )
    assert capture_calls == 1
    assert close_calls == 1
    controller.close()


def test_auto_native_timeout_fails_closed_until_display_reset(monkeypatch) -> None:
    capture_calls = 0
    close_calls = 0

    class NativeTimeoutError(RuntimeError):
        pass

    class TimedOutSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png(self, *_args: object) -> bytes:
            nonlocal capture_calls
            capture_calls += 1
            raise NativeTimeoutError("XShm GetImage reply exceeded its deadline")

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(
            X11SharedMemoryScreenshotSession=TimedOutSession,
            X11ScreenshotTimeoutError=NativeTimeoutError,
        ),
    )
    controller = X11ScreenshotController(
        run=lambda *_args, **_kwargs: pytest.fail("file capture is not expected"),
        width=10,
        height=10,
        display=":99",
        cursor_position=lambda: _cursor_position(),
        capture_source="auto",
    )
    monkeypatch.setattr(
        controller._mss,
        "grab",
        lambda _source: pytest.fail("an unresponsive display must not fall through to MSS"),
    )

    for _ in range(2):
        with pytest.raises(screenshot_capture.ScreenshotCaptureTimedOut):
            asyncio.run(
                controller.capture_bytes(
                    ScreenshotOptions(format="png", show_cursor=False),
                    prefer_native_png=True,
                )
            )

    assert capture_calls == 1
    assert close_calls == 1
    assert controller.readiness_generation == 1
    with pytest.raises(screenshot_capture.ScreenshotCaptureTimedOut):
        asyncio.run(
            controller.capture_bytes(
                ScreenshotOptions(format="jpeg", show_cursor=True)
            )
        )
    with pytest.raises(screenshot_capture.ScreenshotCaptureTimedOut):
        asyncio.run(controller.capture_raw_pixels())
    controller.reset_capture_session()
    assert controller.readiness_generation == 2
    controller.close()


def test_native_readiness_gets_hidden_full_png_and_preserves_cursor_probe(monkeypatch) -> None:
    captured: list[tuple[int, int, int, int]] = []

    class FakeSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png(self, *args: int) -> bytes:
            captured.append(tuple(args))
            return _png_bytes((10, 10))

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        screenshot_capture,
        "_load_module",
        lambda: SimpleNamespace(X11SharedMemoryScreenshotSession=FakeSession),
    )
    monkeypatch.setattr(
        screenshots_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "maim" else None,
    )
    commands: list[tuple[str, ...]] = []

    async def run(*args: str, **_kwargs: object):
        commands.append(args)
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return SimpleNamespace(returncode=0)

    controller = X11ScreenshotController(
        run=run,
        width=10,
        height=10,
        display=":99",
        cursor_position=lambda: _cursor_position(),
        capture_source="x11-shm",
    )

    ready, error = asyncio.run(controller.probe())

    assert ready is True
    assert error is None
    assert captured == [(0, 0, 10, 10)]
    assert commands and commands[0][0] == "maim"
    assert "-u" not in commands[0]
    controller.close()


def test_mss_readiness_probes_hidden_png_and_cursor_visible_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        screenshots_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "maim" else None,
    )
    commands: list[tuple[str, ...]] = []

    async def run(*args: str, **_kwargs: object):
        commands.append(args)
        Image.new("RGB", (10, 10), "white").save(args[-1])
        return SimpleNamespace(returncode=0)

    controller = X11ScreenshotController(
        run=run,
        width=10,
        height=10,
        display=":99",
        cursor_position=lambda: _cursor_position(),
        capture_source="mss",
    )
    grabs = 0

    def grab(_source):
        nonlocal grabs
        grabs += 1
        return _fake_mss_capture(10, 10)

    monkeypatch.setattr(controller._mss, "grab", grab)

    ready, error = asyncio.run(controller.probe())

    assert ready is True
    assert error is None
    assert grabs == 1
    assert commands and commands[0][0] == "maim"
    controller.close()


def test_png_capture_hash_is_included_in_total_timing(monkeypatch) -> None:
    controller = X11ScreenshotController(
        run=lambda *_args, **_kwargs: pytest.fail("file capture is not expected"),
        width=10,
        height=10,
        display=":99",
        cursor_position=lambda: _cursor_position(),
        capture_source="mss",
    )
    monkeypatch.setattr(controller._mss, "grab", lambda _source: _fake_mss_capture(10, 10))

    def slow_hash(_data: bytes) -> str:
        time.sleep(0.01)
        return "digest"

    monkeypatch.setattr("modal_computer_use.daemon.desktop.screenshots.sha256_bytes", slow_hash)

    result = asyncio.run(
        controller.capture_bytes(
            ScreenshotOptions(format="png", show_cursor=False), prefer_native_png=True
        )
    )

    assert result.sha256 == "digest"
    assert result.timings_ms["hash_ms"] >= 8
    assert result.timings_ms["total_ms"] >= result.timings_ms["hash_ms"]
    controller.close()


def _fake_mss_capture(width: int, height: int):
    class Shot:
        rgb = bytes((255, 255, 255)) * width * height
        bgra = bytes((255, 255, 255, 255)) * width * height

    from modal_computer_use.daemon.desktop.screenshots import _MSSCapture

    return _MSSCapture(shot=Shot(), width=width, height=height)


async def _cursor_position() -> Point:
    return Point(x=0, y=0)
