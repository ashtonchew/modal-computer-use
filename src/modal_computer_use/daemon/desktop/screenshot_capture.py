"""Optional X11 shared-memory screenshot source.

The public screenshot route still owns options, metadata, hashing, fallback,
and response construction.  This module only adapts the private X11 shared-memory
capture session to complete lossless PNG bytes.  ``mss`` remains a supported
source and is the sticky fallback for ``auto`` when the preferred source cannot
be loaded or used.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Literal, cast

ScreenshotCaptureSource = Literal["auto", "mss", "x11-shm"]
ResolvedScreenshotCaptureSource = Literal["mss", "x11-shm"]


class ScreenshotCaptureError(RuntimeError):
    """Base error for the optional X11 shared-memory source."""


class ScreenshotCaptureUnavailable(ScreenshotCaptureError):
    """Raised when the X11 shared-memory source cannot be selected or opened."""


class ScreenshotCaptureFailed(ScreenshotCaptureError):
    """Raised when an active X11 shared-memory source cannot return a complete PNG."""


@dataclass(frozen=True, slots=True)
class ScreenshotCaptureResolution:
    requested: ScreenshotCaptureSource
    selected: ResolvedScreenshotCaptureSource
    reason: str | None = None


_MODULE_NAME = "_modal_computer_use_x11_shm"
_module: Any | None = None
_module_checked = False


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
    """Python owner for one persistent X11 shared-memory session."""

    def __init__(self, *, display: str, width: int, height: int) -> None:
        module = _load_module()
        if module is None:
            raise ScreenshotCaptureUnavailable(
                "X11 shared-memory screenshot extension is unavailable"
            )
        constructor = getattr(module, "X11SharedMemoryScreenshotSession", None)
        if not callable(constructor):
            raise ScreenshotCaptureUnavailable(
                "X11 shared-memory screenshot extension has no session constructor"
            )
        try:
            self._session = constructor(display, width, height)
        except Exception as exc:
            raise ScreenshotCaptureUnavailable(
                "X11 shared-memory screenshot session could not start"
            ) from exc
        self._closed = False

    def capture_png(self, *, x: int, y: int, width: int, height: int) -> bytes:
        if self._closed:
            raise ScreenshotCaptureFailed(
                "X11 shared-memory screenshot session is closed"
            )
        try:
            data = self._session.capture_png(x, y, width, height)
        except Exception as exc:
            raise ScreenshotCaptureFailed(
                "X11 shared-memory screenshot capture failed"
            ) from exc
        if not isinstance(data, bytes) or not data.startswith(PNG_SIGNATURE):
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
        except Exception as exc:
            raise ScreenshotCaptureFailed(
                "X11 shared-memory screenshot cleanup failed"
            ) from exc
        finally:
            self._closed = True


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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
