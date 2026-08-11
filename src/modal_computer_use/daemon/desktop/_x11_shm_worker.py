"""Private process owner for one X11 shared-memory screenshot session."""

from __future__ import annotations

import argparse
import importlib
import socket
import struct
from contextlib import suppress
from time import perf_counter_ns
from typing import Any

MODULE_NAME = "_modal_computer_use_x11_shm"
PROTOCOL_MAGIC = b"XSM1"
REQUEST_HEADER = struct.Struct("!4sBII")
RESPONSE_HEADER = struct.Struct("!4sBII")
CAPTURE_PAYLOAD = struct.Struct("!iiii")
TIMING_MAGIC = b"XST1"
TIMING_VERSION = 1
# magic, version, X11 reply, RGB conversion, PNG encode, native total,
# worker dispatch, and response preparation. All durations are nanoseconds.
TIMING_HEADER = struct.Struct("!4sB6Q")

OP_CAPTURE = 1
OP_CLOSE = 2
OP_CAPTURE_TIMED = 3

STATUS_READY = 1
STATUS_CAPTURED = 2
STATUS_CLOSED = 3
STATUS_UNAVAILABLE = 4
STATUS_TIMED_OUT = 5
STATUS_FAILED = 6

MAX_GLOBAL_PAYLOAD_BYTES = 64 * 1024 * 1024


def max_png_payload(width: int, height: int) -> int:
    return min(MAX_GLOBAL_PAYLOAD_BYTES, width * height * 4 + 1024 * 1024)


def run_worker(*, fd: int, display: str, width: int, height: int) -> None:
    """Serve a fixed binary protocol while owning the only native session."""

    connection = socket.socket(fileno=fd)
    session: Any | None = None
    timeout_error: type[BaseException] | None = None
    try:
        module = importlib.import_module(MODULE_NAME)
        constructor = getattr(module, "X11SharedMemoryScreenshotSession", None)
        candidate_timeout = getattr(module, "X11ScreenshotTimeoutError", None)
        if not callable(constructor) or not _is_exception_type(candidate_timeout):
            _send_response(connection, STATUS_UNAVAILABLE, 0)
            return
        timeout_error = candidate_timeout
        try:
            session = constructor(display, width, height)
        except Exception as exc:
            _send_response(connection, _error_status(exc, timeout_error), 0)
            return
        _send_response(connection, STATUS_READY, 0)
        if _serve(connection, session, timeout_error, max_png_payload(width, height)):
            session = None
    except (EOFError, OSError, ValueError):
        return
    finally:
        if session is not None:
            with suppress(Exception):
                session.close()
        connection.close()


def _serve(
    connection: socket.socket,
    session: Any,
    timeout_error: type[BaseException],
    payload_limit: int,
) -> bool:
    while True:
        header = _recv_exact(connection, REQUEST_HEADER.size)
        magic, operation, request_id, payload_length = REQUEST_HEADER.unpack(header)
        if magic != PROTOCOL_MAGIC or request_id == 0:
            return False
        if operation == OP_CAPTURE and payload_length == CAPTURE_PAYLOAD.size:
            x, y, width, height = CAPTURE_PAYLOAD.unpack(
                _recv_exact(connection, payload_length)
            )
            try:
                data = session.capture_png(x, y, width, height)
            except Exception as exc:
                _send_response(connection, _error_status(exc, timeout_error), request_id)
                return False
            if not isinstance(data, bytes) or len(data) > payload_limit:
                _send_response(connection, STATUS_FAILED, request_id)
                return False
            _send_response(connection, STATUS_CAPTURED, request_id, data)
            continue
        if operation == OP_CAPTURE_TIMED and payload_length == CAPTURE_PAYLOAD.size:
            decoded_ns = perf_counter_ns()
            x, y, width, height = CAPTURE_PAYLOAD.unpack(
                _recv_exact(connection, payload_length)
            )
            try:
                called_ns = perf_counter_ns()
                result = session.capture_png_timed(x, y, width, height)
                returned_ns = perf_counter_ns()
                data, native_timing = _validate_timed_capture(result, payload_limit)
                prepared_ns = perf_counter_ns()
            except Exception as exc:
                _send_response(connection, _error_status(exc, timeout_error), request_id)
                return False
            envelope = TIMING_HEADER.pack(
                TIMING_MAGIC,
                TIMING_VERSION,
                *native_timing,
                called_ns - decoded_ns,
                prepared_ns - returned_ns,
            )
            _send_response(connection, STATUS_CAPTURED, request_id, envelope + data)
            continue
        if operation == OP_CLOSE and payload_length == 0:
            try:
                session.close()
            except Exception as exc:
                _send_response(connection, _error_status(exc, timeout_error), request_id)
            else:
                _send_response(connection, STATUS_CLOSED, request_id)
                return True
            return False
        return False


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        chunk = connection.recv(length - len(output))
        if not chunk:
            raise EOFError("X11 screenshot worker connection closed")
        output.extend(chunk)
    return bytes(output)


def _send_response(
    connection: socket.socket,
    status: int,
    request_id: int,
    payload: bytes = b"",
) -> None:
    connection.sendall(
        RESPONSE_HEADER.pack(PROTOCOL_MAGIC, status, request_id, len(payload)) + payload
    )


def _is_exception_type(value: Any) -> bool:
    return isinstance(value, type) and issubclass(value, BaseException)


def _validate_timed_capture(
    result: object, payload_limit: int
) -> tuple[bytes, tuple[int, int, int, int]]:
    if not isinstance(result, tuple) or len(result) != 2:
        raise ValueError("invalid timed capture result")
    data, raw_timing = result
    if not isinstance(data, bytes) or not data or len(data) > payload_limit:
        raise ValueError("invalid timed capture payload")
    if not isinstance(raw_timing, tuple) or len(raw_timing) != 4:
        raise ValueError("invalid timed capture stages")
    stages: list[int] = []
    for value in raw_timing:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > 2**64 - 1
        ):
            raise ValueError("invalid timed capture stage")
        stages.append(value)
    x11_reply_ns, rgb_convert_ns, png_encode_ns, native_total_ns = stages
    if native_total_ns < x11_reply_ns + rgb_convert_ns + png_encode_ns:
        raise ValueError("invalid timed capture stage algebra")
    return data, (x11_reply_ns, rgb_convert_ns, png_encode_ns, native_total_ns)


def _error_status(exc: Exception, timeout_error: type[BaseException]) -> int:
    return STATUS_TIMED_OUT if isinstance(exc, timeout_error) else STATUS_FAILED


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--display", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    arguments = parser.parse_args()
    if arguments.fd < 3 or arguments.width <= 0 or arguments.height <= 0:
        raise SystemExit(2)
    run_worker(
        fd=arguments.fd,
        display=arguments.display,
        width=arguments.width,
        height=arguments.height,
    )


if __name__ == "__main__":
    main()
