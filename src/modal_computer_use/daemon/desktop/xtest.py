from __future__ import annotations

import ctypes
import ctypes.util
import threading


class XTestUnavailableError(RuntimeError):
    """Raised when the XTest pointer backend cannot be used."""


class XTestPointerController:
    def __init__(self, *, display: str) -> None:
        self._display_name = display.encode()
        self._lock = threading.Lock()
        self._x11: ctypes.CDLL | None = None
        self._xtst: ctypes.CDLL | None = None
        self._display: int | None = None
        self._available: bool | None = None
        self._failure: str | None = None

    @property
    def failure(self) -> str | None:
        return self._failure

    def available(self) -> bool:
        try:
            self._ensure_open()
        except XTestUnavailableError:
            return False
        return True

    def close(self) -> None:
        with self._lock:
            if self._display is not None and self._x11 is not None:
                self._x11.XCloseDisplay(ctypes.c_void_p(self._display))
            self._display = None
            self._available = None

    def move(self, x: int, y: int) -> None:
        with self._lock:
            display = self._ensure_open()
            self._fake_motion(display, x, y)
            self._flush(display)

    def click(
        self,
        *,
        button: int,
        count: int = 1,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        with self._lock:
            display = self._ensure_open()
            if x is not None and y is not None:
                self._fake_motion(display, x, y)
            for _ in range(count):
                self._fake_button(display, button, True)
                self._fake_button(display, button, False)
            self._flush(display)

    def down(self, *, button: int, x: int | None = None, y: int | None = None) -> None:
        with self._lock:
            display = self._ensure_open()
            if x is not None and y is not None:
                self._fake_motion(display, x, y)
            self._fake_button(display, button, True)
            self._flush(display)

    def up(self, *, button: int, x: int | None = None, y: int | None = None) -> None:
        with self._lock:
            display = self._ensure_open()
            if x is not None and y is not None:
                self._fake_motion(display, x, y)
            self._fake_button(display, button, False)
            self._flush(display)

    def scroll(
        self,
        *,
        button: int,
        amount: int = 1,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        with self._lock:
            display = self._ensure_open()
            if x is not None and y is not None:
                self._fake_motion(display, x, y)
            for _ in range(amount):
                self._fake_button(display, button, True)
                self._fake_button(display, button, False)
            self._flush(display)

    def _ensure_open(self) -> int:
        if self._available is False:
            raise XTestUnavailableError(self._failure or "XTest unavailable")
        if self._display is not None:
            return self._display
        try:
            self._load_libraries()
            assert self._x11 is not None
            assert self._xtst is not None
            display = self._x11.XOpenDisplay(ctypes.c_char_p(self._display_name))
            if not display:
                raise XTestUnavailableError("XOpenDisplay failed")
            event_base = ctypes.c_int()
            error_base = ctypes.c_int()
            major = ctypes.c_int()
            minor = ctypes.c_int()
            ok = self._xtst.XTestQueryExtension(
                ctypes.c_void_p(display),
                ctypes.byref(event_base),
                ctypes.byref(error_base),
                ctypes.byref(major),
                ctypes.byref(minor),
            )
            if not ok:
                self._x11.XCloseDisplay(ctypes.c_void_p(display))
                raise XTestUnavailableError("XTest extension unavailable")
            self._display = int(display)
            self._available = True
            self._failure = None
            return self._display
        except XTestUnavailableError as exc:
            self._available = False
            self._failure = str(exc)
            raise
        except Exception as exc:
            self._available = False
            self._failure = f"{type(exc).__name__}: {exc}"
            raise XTestUnavailableError(self._failure) from exc

    def _load_libraries(self) -> None:
        if self._x11 is not None and self._xtst is not None:
            return
        x11_path = ctypes.util.find_library("X11")
        xtst_path = ctypes.util.find_library("Xtst")
        if not x11_path or not xtst_path:
            raise XTestUnavailableError("libX11 or libXtst not found")
        x11 = ctypes.CDLL(x11_path)
        xtst = ctypes.CDLL(xtst_path)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XFlush.restype = ctypes.c_int
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XSync.restype = ctypes.c_int
        xtst.XTestQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        xtst.XTestQueryExtension.restype = ctypes.c_int
        xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xtst.XTestFakeMotionEvent.restype = ctypes.c_int
        xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xtst.XTestFakeButtonEvent.restype = ctypes.c_int
        self._x11 = x11
        self._xtst = xtst

    def _fake_motion(self, display: int, x: int, y: int) -> None:
        assert self._xtst is not None
        ok = self._xtst.XTestFakeMotionEvent(ctypes.c_void_p(display), -1, x, y, 0)
        if not ok:
            raise XTestUnavailableError("XTestFakeMotionEvent failed")

    def _fake_button(self, display: int, button: int, pressed: bool) -> None:
        assert self._xtst is not None
        ok = self._xtst.XTestFakeButtonEvent(ctypes.c_void_p(display), button, int(pressed), 0)
        if not ok:
            raise XTestUnavailableError("XTestFakeButtonEvent failed")

    def _flush(self, display: int) -> None:
        assert self._x11 is not None
        self._x11.XFlush(ctypes.c_void_p(display))
        self._x11.XSync(ctypes.c_void_p(display), 0)
