from __future__ import annotations

import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from modal_computer_use.daemon.desktop import _x11_shm_worker as worker


def test_worker_owns_one_session_and_serves_fixed_binary_protocol(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []

    class NativeTimeoutError(RuntimeError):
        pass

    class NativeSession:
        def __init__(self, display: str, width: int, height: int) -> None:
            calls.append(("open", display, width, height))

        def capture_png(self, x: int, y: int, width: int, height: int) -> bytes:
            calls.append(("capture", x, y, width, height))
            return b"png-frame"

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setitem(
        sys.modules,
        worker.MODULE_NAME,
        SimpleNamespace(
            X11SharedMemoryScreenshotSession=NativeSession,
            X11ScreenshotTimeoutError=NativeTimeoutError,
        ),
    )
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child_fd = child.detach()
    owner = threading.Thread(
        target=worker.run_worker,
        kwargs={"fd": child_fd, "display": ":99", "width": 10, "height": 11},
    )
    owner.start()
    try:
        assert _response(parent) == (worker.STATUS_READY, 0, b"")
        parent.sendall(
            worker.REQUEST_HEADER.pack(
                worker.PROTOCOL_MAGIC,
                worker.OP_CAPTURE,
                1,
                worker.CAPTURE_PAYLOAD.size,
            )
            + worker.CAPTURE_PAYLOAD.pack(3, 4, 5, 6)
        )
        assert _response(parent) == (worker.STATUS_CAPTURED, 1, b"png-frame")
        parent.sendall(
            worker.REQUEST_HEADER.pack(worker.PROTOCOL_MAGIC, worker.OP_CLOSE, 2, 0)
        )
        assert _response(parent) == (worker.STATUS_CLOSED, 2, b"")
    finally:
        parent.close()
        owner.join(timeout=1)
    assert not owner.is_alive()
    assert calls == [
        ("open", ":99", 10, 11),
        ("capture", 3, 4, 5, 6),
        ("close",),
    ]


def test_worker_timeout_status_is_terminal_and_contains_no_error_text(monkeypatch) -> None:
    class NativeTimeoutError(RuntimeError):
        pass

    class TimedOutSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png(self, *_args: object) -> bytes:
            raise NativeTimeoutError("secret native detail")

        def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        worker.MODULE_NAME,
        SimpleNamespace(
            X11SharedMemoryScreenshotSession=TimedOutSession,
            X11ScreenshotTimeoutError=NativeTimeoutError,
        ),
    )
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child_fd = child.detach()
    owner = threading.Thread(
        target=worker.run_worker,
        kwargs={"fd": child_fd, "display": ":99", "width": 10, "height": 11},
    )
    owner.start()
    try:
        assert _response(parent) == (worker.STATUS_READY, 0, b"")
        parent.sendall(
            worker.REQUEST_HEADER.pack(
                worker.PROTOCOL_MAGIC,
                worker.OP_CAPTURE,
                7,
                worker.CAPTURE_PAYLOAD.size,
            )
            + worker.CAPTURE_PAYLOAD.pack(0, 0, 10, 11)
        )
        assert _response(parent) == (worker.STATUS_TIMED_OUT, 7, b"")
    finally:
        parent.close()
        owner.join(timeout=1)
    assert not owner.is_alive()


def test_worker_timed_capture_returns_fixed_stage_envelope_without_changing_png(
    monkeypatch,
) -> None:
    class NativeTimeoutError(RuntimeError):
        pass

    class NativeSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png_timed(
            self, *_args: object
        ) -> tuple[bytes, tuple[int, int, int, int]]:
            return b"png-frame", (11, 13, 17, 47)

        def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        worker.MODULE_NAME,
        SimpleNamespace(
            X11SharedMemoryScreenshotSession=NativeSession,
            X11ScreenshotTimeoutError=NativeTimeoutError,
        ),
    )
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child_fd = child.detach()
    owner = threading.Thread(
        target=worker.run_worker,
        kwargs={"fd": child_fd, "display": ":99", "width": 10, "height": 11},
    )
    owner.start()
    try:
        assert _response(parent) == (worker.STATUS_READY, 0, b"")
        parent.sendall(
            worker.REQUEST_HEADER.pack(
                worker.PROTOCOL_MAGIC,
                worker.OP_CAPTURE_TIMED,
                9,
                worker.CAPTURE_PAYLOAD.size,
            )
            + worker.CAPTURE_PAYLOAD.pack(3, 4, 5, 6)
        )
        status, request_id, payload = _response(parent)
        assert status == worker.STATUS_CAPTURED
        assert request_id == 9
        envelope = payload[: worker.TIMING_HEADER.size]
        png = payload[worker.TIMING_HEADER.size :]
        assert worker.TIMING_HEADER.unpack(envelope)[:2] == (
            worker.TIMING_MAGIC,
            worker.TIMING_VERSION,
        )
        stages = worker.TIMING_HEADER.unpack(envelope)[2:]
        assert stages[:4] == (11, 13, 17, 47)
        assert all(isinstance(value, int) and value >= 0 for value in stages)
        assert png == b"png-frame"
    finally:
        parent.close()
        owner.join(timeout=1)
    assert not owner.is_alive()


def test_worker_timed_capture_rejects_invalid_native_stage_algebra(monkeypatch) -> None:
    class NativeTimeoutError(RuntimeError):
        pass

    class NativeSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png_timed(self, *_args: object) -> tuple[bytes, tuple[int, ...]]:
            return b"png-frame", (2, 3, 5, 9)

        def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        worker.MODULE_NAME,
        SimpleNamespace(
            X11SharedMemoryScreenshotSession=NativeSession,
            X11ScreenshotTimeoutError=NativeTimeoutError,
        ),
    )
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child_fd = child.detach()
    owner = threading.Thread(
        target=worker.run_worker,
        kwargs={"fd": child_fd, "display": ":99", "width": 10, "height": 11},
    )
    owner.start()
    try:
        assert _response(parent) == (worker.STATUS_READY, 0, b"")
        parent.sendall(
            worker.REQUEST_HEADER.pack(
                worker.PROTOCOL_MAGIC,
                worker.OP_CAPTURE_TIMED,
                9,
                worker.CAPTURE_PAYLOAD.size,
            )
            + worker.CAPTURE_PAYLOAD.pack(3, 4, 5, 6)
        )
        assert _response(parent) == (worker.STATUS_FAILED, 9, b"")
    finally:
        parent.close()
        owner.join(timeout=1)
    assert not owner.is_alive()


def test_worker_timed_capture_preserves_typed_native_timeout(monkeypatch) -> None:
    class NativeTimeoutError(RuntimeError):
        pass

    class NativeSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png_timed(self, *_args: object) -> object:
            raise NativeTimeoutError("private native detail")

        def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        worker.MODULE_NAME,
        SimpleNamespace(
            X11SharedMemoryScreenshotSession=NativeSession,
            X11ScreenshotTimeoutError=NativeTimeoutError,
        ),
    )
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child_fd = child.detach()
    owner = threading.Thread(
        target=worker.run_worker,
        kwargs={"fd": child_fd, "display": ":99", "width": 10, "height": 11},
    )
    owner.start()
    try:
        assert _response(parent) == (worker.STATUS_READY, 0, b"")
        parent.sendall(
            worker.REQUEST_HEADER.pack(
                worker.PROTOCOL_MAGIC,
                worker.OP_CAPTURE_TIMED,
                12,
                worker.CAPTURE_PAYLOAD.size,
            )
            + worker.CAPTURE_PAYLOAD.pack(0, 0, 10, 11)
        )
        assert _response(parent) == (worker.STATUS_TIMED_OUT, 12, b"")
    finally:
        parent.close()
        owner.join(timeout=1)
    assert not owner.is_alive()


@pytest.mark.parametrize(
    "timing",
    [
        (0, 0, 0, 2**64),
        (0, 0, 0, True),
        (1, 2, 3),
    ],
)
def test_worker_timed_capture_rejects_unencodable_stage_values(
    timing: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError):
        worker._validate_timed_capture((b"png", timing), 1024)


def _response(connection: socket.socket) -> tuple[int, int, bytes]:
    header = worker._recv_exact(connection, worker.RESPONSE_HEADER.size)
    magic, status, request_id, length = worker.RESPONSE_HEADER.unpack(header)
    assert magic == worker.PROTOCOL_MAGIC
    return status, request_id, worker._recv_exact(connection, length)
