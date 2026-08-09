"""Optional X11 shared-memory screenshot source.

The public screenshot route still owns options, metadata, hashing, fallback,
and response construction.  This module only adapts the private X11 shared-memory
capture session to complete lossless PNG bytes.  ``mss`` remains a supported
source and is the sticky fallback for ``auto`` when the preferred source cannot
be loaded or used.
"""

from __future__ import annotations

import fcntl
import importlib
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, cast

from ._x11_shm_worker import (
    CAPTURE_PAYLOAD,
    OP_CAPTURE,
    OP_CLOSE,
    PROTOCOL_MAGIC,
    REQUEST_HEADER,
    RESPONSE_HEADER,
    STATUS_CAPTURED,
    STATUS_CLOSED,
    STATUS_FAILED,
    STATUS_READY,
    STATUS_TIMED_OUT,
    STATUS_UNAVAILABLE,
    max_png_payload,
)

ScreenshotCaptureSource = Literal["auto", "mss", "x11-shm"]
ResolvedScreenshotCaptureSource = Literal["mss", "x11-shm"]


class ScreenshotCaptureError(RuntimeError):
    """Base error for the optional X11 shared-memory source."""


class ScreenshotCaptureUnavailable(ScreenshotCaptureError):
    """Raised when the X11 shared-memory source cannot be selected or opened."""


class ScreenshotCaptureFailed(ScreenshotCaptureError):
    """Raised when an active X11 shared-memory source cannot return a complete PNG."""


class ScreenshotCaptureTimedOut(ScreenshotCaptureFailed):
    """Raised when the X server does not complete a bounded native operation."""


@dataclass(frozen=True, slots=True)
class ScreenshotCaptureResolution:
    requested: ScreenshotCaptureSource
    selected: ResolvedScreenshotCaptureSource
    reason: str | None = None


_MODULE_NAME = "_modal_computer_use_x11_shm"
_module: Any | None = None
_module_checked = False

# This deadline covers the parent/child transport as well as an extension call.
# The extension has its own shorter X11 reply deadline; the parent deadline is
# deliberately a little wider so a typed child reply can cross the socket. A
# child that exceeds either bound is terminated and reaped before the typed
# error reaches the daemon.
_WORKER_OPERATION_TIMEOUT_SECONDS = 1.0
_WORKER_START_TIMEOUT_SECONDS = 2.0
_WORKER_REAP_TIMEOUT_SECONDS = 0.25


def normalize_capture_source(value: str) -> ScreenshotCaptureSource:
    normalized = value.strip().lower()
    choices = {"auto", "mss", "x11-shm"}
    if normalized not in choices:
        raise ValueError(
            "COMPUTER_USE_SCREENSHOT_CAPTURE_SOURCE must be one of: "
            + ", ".join(sorted(choices))
        )
    return cast(ScreenshotCaptureSource, normalized)


def resolve_capture_source(
    source: ScreenshotCaptureSource = "auto",
) -> ScreenshotCaptureResolution:
    """Resolve a source without opening an X connection.

    ``auto`` deliberately resolves to MSS when the optional extension is not
    importable.  Initialization and display capability failures are handled by
    the controller after the Xvfb supervisor has started.
    """

    requested = normalize_capture_source(source)
    if requested == "mss":
        return ScreenshotCaptureResolution(requested=requested, selected="mss")
    if _load_module() is None:
        if requested == "x11-shm":
            # Keep construction safe before Xvfb starts.  The controller
            # reports an explicit source as not ready when it performs the
            # post-start capability probe.
            return ScreenshotCaptureResolution(
                requested=requested,
                selected="x11-shm",
                reason="X11 shared-memory screenshot extension unavailable",
            )
        return ScreenshotCaptureResolution(
            requested=requested,
            selected="mss",
            reason="X11 shared-memory screenshot extension unavailable",
        )
    return ScreenshotCaptureResolution(requested=requested, selected="x11-shm")


class X11SharedMemoryScreenshotSession:
    """Python owner for one persistent X11 shared-memory session.

    A real packaged extension is owned by one spawned child process. Fake
    modules used by local tests retain the direct in-process ABI seam.
    """

    def __init__(self, *, display: str, width: int, height: int) -> None:
        module = _load_module()
        if module is None:
            raise ScreenshotCaptureUnavailable(
                "X11 shared-memory screenshot extension is unavailable"
            )
        constructor = getattr(module, "X11SharedMemoryScreenshotSession", None)
        timeout_error = getattr(module, "X11ScreenshotTimeoutError", None)
        if getattr(module, "__name__", None) == _MODULE_NAME and not (
            isinstance(timeout_error, type)
            and issubclass(timeout_error, BaseException)
        ):
            raise ScreenshotCaptureUnavailable(
                "X11 shared-memory screenshot extension is incompatible"
            )
        if not callable(constructor):
            raise ScreenshotCaptureUnavailable(
                "X11 shared-memory screenshot extension has no session constructor"
            )
        if getattr(module, "__name__", None) == _MODULE_NAME:
            self._session = _SpawnedX11ScreenshotSession(display, width, height)
        else:
            try:
                self._session = constructor(display, width, height)
            except Exception as exc:
                if _is_native_timeout(exc, timeout_error):
                    raise ScreenshotCaptureTimedOut(
                        "X11 shared-memory screenshot startup exceeded its reply deadline"
                    ) from exc
                raise ScreenshotCaptureUnavailable(
                    "X11 shared-memory screenshot session could not start"
                ) from exc
        self._timeout_error = timeout_error
        self._closed = False

    def capture_png(self, *, x: int, y: int, width: int, height: int) -> bytes:
        if self._closed:
            raise ScreenshotCaptureFailed(
                "X11 shared-memory screenshot session is closed"
            )
        try:
            data = self._session.capture_png(x, y, width, height)
        except ScreenshotCaptureTimedOut:
            raise
        except ScreenshotCaptureFailed:
            raise
        except Exception as exc:
            if _is_native_timeout(exc, self._timeout_error):
                raise ScreenshotCaptureTimedOut(
                    "X11 shared-memory screenshot exceeded its reply deadline"
                ) from exc
            raise ScreenshotCaptureFailed(
                "X11 shared-memory screenshot capture failed"
            ) from exc
        if not isinstance(data, bytes) or not validate_png_dimensions(
            data,
            width=width,
            height=height,
        ):
            raise ScreenshotCaptureFailed(
                "X11 shared-memory screenshot returned an invalid PNG"
            )
        return data

    def close(self) -> None:
        if self._closed:
            return
        close = getattr(self._session, "close", None)
        try:
            if callable(close):
                close()
        except ScreenshotCaptureTimedOut:
            raise
        except ScreenshotCaptureFailed:
            raise
        except Exception as exc:
            raise ScreenshotCaptureFailed(
                "X11 shared-memory screenshot cleanup failed"
            ) from exc
        finally:
            self._closed = True


class _SpawnedX11ScreenshotSession:
    """Own one real native session in a deadline-bound helper process."""

    def __init__(self, display: str, width: int, height: int) -> None:
        self._lock = threading.Lock()
        self._request_id = 0
        self._closed = False
        self._payload_limit = max_png_payload(width, height)
        parent_socket, child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket = parent_socket
        self._process: subprocess.Popen[bytes] | None = None
        try:
            child_fd = child_socket.fileno()
            if child_fd < 3:
                duplicated = fcntl.fcntl(child_fd, fcntl.F_DUPFD_CLOEXEC, 3)
                child_socket.close()
                child_socket = socket.socket(fileno=duplicated)
                child_fd = duplicated
            self._process = subprocess.Popen(  # noqa: S603 - fixed private module argv.
                [
                    sys.executable,
                    "-m",
                    "modal_computer_use.daemon.desktop._x11_shm_worker",
                    "--fd",
                    str(child_fd),
                    "--display",
                    display,
                    "--width",
                    str(width),
                    "--height",
                    str(height),
                ],
                close_fds=True,
                pass_fds=(child_fd,),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_socket.close()
            status, request_id, payload = self._receive(
                monotonic() + _WORKER_START_TIMEOUT_SECONDS
            )
            if request_id != 0 or payload:
                raise ScreenshotCaptureUnavailable(
                    "X11 shared-memory screenshot worker returned an invalid startup response"
                )
            if status == STATUS_TIMED_OUT:
                raise ScreenshotCaptureTimedOut(
                    "X11 shared-memory screenshot startup exceeded its process deadline"
                )
            if status != STATUS_READY:
                raise ScreenshotCaptureUnavailable(
                    "X11 shared-memory screenshot session could not start"
                )
        except ScreenshotCaptureError:
            self._terminate()
            raise
        except (OSError, TimeoutError, ValueError) as exc:
            self._terminate()
            if isinstance(exc, TimeoutError):
                raise ScreenshotCaptureTimedOut(
                    "X11 shared-memory screenshot startup exceeded its process deadline"
                ) from exc
            raise ScreenshotCaptureUnavailable(
                "X11 shared-memory screenshot worker could not start"
            ) from exc
        finally:
            child_socket.close()

    def capture_png(self, x: int, y: int, width: int, height: int) -> bytes:
        with self._lock:
            if self._closed:
                raise ScreenshotCaptureFailed(
                    "X11 shared-memory screenshot worker is closed"
                )
            request_id = self._next_request_id()
            deadline = monotonic() + _WORKER_OPERATION_TIMEOUT_SECONDS
            try:
                self._send(
                    OP_CAPTURE,
                    request_id,
                    CAPTURE_PAYLOAD.pack(x, y, width, height),
                    deadline,
                )
                status, response_id, payload = self._receive(deadline)
            except TimeoutError as exc:
                self._terminate()
                raise ScreenshotCaptureTimedOut(
                    "X11 shared-memory screenshot exceeded its process deadline"
                ) from exc
            except (OSError, ValueError, struct.error) as exc:
                self._terminate()
                raise ScreenshotCaptureFailed(
                    "X11 shared-memory screenshot worker failed"
                ) from exc
            if response_id != request_id:
                self._terminate()
                raise ScreenshotCaptureFailed(
                    "X11 shared-memory screenshot worker returned an invalid response"
                )
            if status == STATUS_TIMED_OUT:
                self._terminate()
                raise ScreenshotCaptureTimedOut(
                    "X11 shared-memory screenshot exceeded its reply deadline"
                )
            if status != STATUS_CAPTURED or not payload:
                self._terminate()
                raise ScreenshotCaptureFailed(
                    "X11 shared-memory screenshot worker could not capture"
                )
            return payload

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            request_id = self._next_request_id()
            deadline = monotonic() + _WORKER_OPERATION_TIMEOUT_SECONDS
            try:
                self._send(OP_CLOSE, request_id, b"", deadline)
                status, response_id, payload = self._receive(deadline)
                if status != STATUS_CLOSED or response_id != request_id or payload:
                    raise ScreenshotCaptureFailed(
                        "X11 shared-memory screenshot worker cleanup failed"
                    )
                process = self._process
                if process is not None:
                    process.wait(timeout=_WORKER_REAP_TIMEOUT_SECONDS)
            except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
                self._terminate()
                raise ScreenshotCaptureFailed(
                    "X11 shared-memory screenshot worker cleanup failed"
                ) from exc
            except ScreenshotCaptureFailed:
                self._terminate()
                raise
            finally:
                self._closed = True
                self._socket.close()

    def _next_request_id(self) -> int:
        self._request_id = (self._request_id % (2**32 - 1)) + 1
        return self._request_id

    def _send(self, operation: int, request_id: int, payload: bytes, deadline: float) -> None:
        self._socket.settimeout(_remaining(deadline))
        self._socket.sendall(
            REQUEST_HEADER.pack(PROTOCOL_MAGIC, operation, request_id, len(payload))
            + payload
        )

    def _receive(self, deadline: float) -> tuple[int, int, bytes]:
        header = _recv_exact(self._socket, RESPONSE_HEADER.size, deadline)
        magic, status, request_id, payload_length = RESPONSE_HEADER.unpack(header)
        if magic != PROTOCOL_MAGIC or payload_length > self._payload_limit:
            raise ValueError("invalid X11 screenshot worker response")
        payload = _recv_exact(self._socket, payload_length, deadline)
        if status not in {
            STATUS_READY,
            STATUS_CAPTURED,
            STATUS_CLOSED,
            STATUS_UNAVAILABLE,
            STATUS_TIMED_OUT,
            STATUS_FAILED,
        }:
            raise ValueError("unknown X11 screenshot worker status")
        return status, request_id, payload

    def _terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._socket.close()
        process = self._process
        if process is None or process.poll() is not None:
            if process is not None:
                process.wait()
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=_WORKER_REAP_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("X11 screenshot worker deadline expired")
    return remaining


def _recv_exact(connection: socket.socket, length: int, deadline: float) -> bytes:
    output = bytearray()
    while len(output) < length:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(length - len(output))
        if not chunk:
            raise OSError("X11 screenshot worker connection closed")
        output.extend(chunk)
    return bytes(output)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _is_native_timeout(exc: Exception, exception_type: Any) -> bool:
    return (
        isinstance(exception_type, type)
        and issubclass(exception_type, BaseException)
        and isinstance(exc, exception_type)
    )


def validate_png_dimensions(data: bytes, *, width: int, height: int) -> bool:
    """Validate PNG signature and IHDR dimensions without decoding a frame."""

    if not data.startswith(PNG_SIGNATURE) or len(data) < 33:
        return False
    ihdr_length = int.from_bytes(data[8:12], "big")
    if ihdr_length < 8 or data[12:16] != b"IHDR":
        return False
    actual_width = int.from_bytes(data[16:20], "big")
    actual_height = int.from_bytes(data[20:24], "big")
    return actual_width == width and actual_height == height


def _probe_x11_setup(display: str) -> None:
    """Bound the X11 setup handshake before libxcb opens its persistent connection."""

    host, separator, display_part = display.rpartition(":")
    if not separator:
        raise ScreenshotCaptureUnavailable("X11 display address is invalid")
    try:
        display_number = int(display_part.split(".", 1)[0])
    except ValueError as exc:
        raise ScreenshotCaptureUnavailable("X11 display address is invalid") from exc
    connection: socket.socket | None = None
    deadline = monotonic() + 0.5

    def remaining() -> float:
        value = deadline - monotonic()
        if value <= 0:
            raise TimeoutError("X11 setup deadline expired")
        return value

    try:
        if host in {"", "unix"}:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(remaining())
            connection.connect(
                f"/tmp/.X11-unix/X{display_number}"  # noqa: S108 - standard X11 socket.
            )
        else:
            connection = socket.create_connection(
                (host, 6000 + display_number), timeout=remaining()
            )
        connection.settimeout(remaining())
        connection.sendall(struct.pack("<BBHHHHH", ord("l"), 0, 11, 0, 0, 0, 0))
        response = bytearray()
        expected_size = 8
        while len(response) < expected_size:
            connection.settimeout(remaining())
            chunk = connection.recv(expected_size - len(response))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) >= 8 and expected_size == 8:
                setup_words = int.from_bytes(response[6:8], "little")
                expected_size += setup_words * 4
    except TimeoutError as exc:
        raise ScreenshotCaptureTimedOut(
            "X11 shared-memory screenshot startup exceeded its reply deadline"
        ) from exc
    except OSError as exc:
        raise ScreenshotCaptureUnavailable(
            "X11 shared-memory screenshot could not connect to the display"
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    if len(response) != expected_size or response[0] != 1:
        raise ScreenshotCaptureUnavailable("X11 display rejected the setup handshake")


def _load_module() -> Any | None:
    global _module, _module_checked
    if _module_checked:
        return _module
    _module_checked = True
    try:
        _module = importlib.import_module(_MODULE_NAME)
    except Exception:  # optional extension import is an auto-fallback boundary
        _module = None
    return _module


def _reset_module_cache_for_tests() -> None:
    global _module, _module_checked
    _module = None
    _module_checked = False
