"""Build-time capture canary for the packaged X11 shared-memory extension."""

from __future__ import annotations

import importlib
import subprocess
import time
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DISPLAY = ":199"
WIDTH = 1024
HEIGHT = 768


def _wait_for_display(process: subprocess.Popen[bytes]) -> None:
    socket = Path("/tmp/.X11-unix/X199")  # noqa: S108 - X11's standard socket path.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Xvfb exited before the screenshot canary connected")
        if socket.exists():
            return
        time.sleep(0.05)
    raise RuntimeError("Xvfb did not become ready for the screenshot canary")


def _validate_png(data: bytes) -> None:
    if (
        not data.startswith(PNG_SIGNATURE)
        or len(data) < 24
        or data[12:16] != b"IHDR"
        or int.from_bytes(data[16:20], "big") != WIDTH
        or int.from_bytes(data[20:24], "big") != HEIGHT
    ):
        raise RuntimeError("X11 shared-memory screenshot canary returned an invalid PNG")


def main() -> None:
    process = subprocess.Popen(  # noqa: S603 - fixed build-time Xvfb command.
        [
            "/usr/bin/Xvfb",
            DISPLAY,
            "-screen",
            "0",
            f"{WIDTH}x{HEIGHT}x24",
            "-nolisten",
            "tcp",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session = None
    try:
        _wait_for_display(process)
        x11_shm = importlib.import_module("_modal_computer_use_x11_shm")
        session = x11_shm.X11SharedMemoryScreenshotSession(DISPLAY, WIDTH, HEIGHT)
        _validate_png(session.capture_png(0, 0, WIDTH, HEIGHT))
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
