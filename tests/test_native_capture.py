from __future__ import annotations

import asyncio
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


def test_x11_shared_memory_adapter_uses_private_extension_abi(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeSession:
        def __init__(self, *args: object) -> None:
            calls.append(("init", *args))

        def capture_png(self, *args: object) -> bytes:
            calls.append(("capture", *args))
            return b"\x89PNG\r\n\x1a\nfixture"

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


def test_explicit_native_runtime_failure_does_not_fallback(monkeypatch) -> None:
    class BrokenSession:
        def __init__(self, *_args: object) -> None:
            pass

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
        capture_source="x11-shm",
    )

    with pytest.raises(screenshot_capture.ScreenshotCaptureFailed):
        asyncio.run(
            controller.capture_bytes(
                ScreenshotOptions(format="png", show_cursor=False), prefer_native_png=True
            )
        )
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
