from __future__ import annotations

import ctypes
import threading


class _XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resource_id", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


_XErrorHandler = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(_XErrorEvent),
)


@_XErrorHandler
def _nonfatal_xlib_error_handler(
    _display: ctypes.c_void_p,
    _error: ctypes.POINTER(_XErrorEvent),
) -> int:
    # Native adapters validate synchronous return values. The process-wide Xlib
    # default instead exits on ordinary races such as a window disappearing
    # between _NET_CLIENT_LIST and XGetWindowAttributes.
    return 0


_configuration_lock = threading.Lock()
_configured = False


def configure_xlib_runtime(x11: ctypes.CDLL) -> None:
    """Configure process-wide Xlib behavior before the first display is opened."""

    global _configured
    with _configuration_lock:
        if _configured:
            return
        x11.XInitThreads.argtypes = []
        x11.XInitThreads.restype = ctypes.c_int
        if not x11.XInitThreads():
            raise RuntimeError("XInitThreads failed")
        x11.XSetErrorHandler.argtypes = [_XErrorHandler]
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler(_nonfatal_xlib_error_handler)
        _configured = True


__all__ = ["configure_xlib_runtime"]
