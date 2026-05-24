from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
from dataclasses import dataclass
from time import perf_counter
from typing import ClassVar


class XDamageUnavailableError(RuntimeError):
    """Raised when XDamage cannot be used for display change events."""


@dataclass(frozen=True)
class XDamageWaitResult:
    available: bool
    detected: bool
    wait_ms: float
    reason: str | None = None
    version: str | None = None


class _XEvent(ctypes.Union):
    _fields_: ClassVar = [("type", ctypes.c_int), ("pad", ctypes.c_long * 24)]


class XDamageWatcher:
    def __init__(self, *, display: str) -> None:
        self._display_name = display.encode()
        self._x11: ctypes.CDLL | None = None
        self._xdamage: ctypes.CDLL | None = None
        self._display: int | None = None
        self._damage: int | None = None
        self._event_base: int | None = None
        self._version: str | None = None
        self._failure: str | None = None

    @property
    def failure(self) -> str | None:
        return self._failure

    @property
    def version(self) -> str | None:
        return self._version

    def available(self) -> bool:
        try:
            self.start()
        except XDamageUnavailableError:
            return False
        return True

    def arm(self) -> None:
        self.start()
        self._subtract_damage()
        self._sync()
        self._drain_events()

    def start(self) -> None:
        if self._display is not None and self._damage is not None:
            return
        try:
            self._load_libraries()
            assert self._x11 is not None
            assert self._xdamage is not None
            display = self._x11.XOpenDisplay(ctypes.c_char_p(self._display_name))
            if not display:
                raise XDamageUnavailableError("XOpenDisplay failed")

            event_base = ctypes.c_int()
            error_base = ctypes.c_int()
            if not self._xdamage.XDamageQueryExtension(
                ctypes.c_void_p(display),
                ctypes.byref(event_base),
                ctypes.byref(error_base),
            ):
                self._x11.XCloseDisplay(ctypes.c_void_p(display))
                raise XDamageUnavailableError("XDamage extension unavailable")

            major = ctypes.c_int()
            minor = ctypes.c_int()
            if self._xdamage.XDamageQueryVersion(
                ctypes.c_void_p(display),
                ctypes.byref(major),
                ctypes.byref(minor),
            ):
                self._version = f"{major.value}.{minor.value}"

            root = self._x11.XDefaultRootWindow(ctypes.c_void_p(display))
            damage = self._xdamage.XDamageCreate(
                ctypes.c_void_p(display),
                ctypes.c_ulong(root),
                3,  # XDamageReportNonEmpty: one event per empty -> non-empty transition.
            )
            if not damage:
                self._x11.XCloseDisplay(ctypes.c_void_p(display))
                raise XDamageUnavailableError("XDamageCreate failed")

            self._display = int(display)
            self._damage = int(damage)
            self._event_base = event_base.value
            self.arm()
            self._failure = None
        except XDamageUnavailableError as exc:
            self._failure = str(exc)
            self.close()
            raise
        except Exception as exc:
            self._failure = f"{type(exc).__name__}: {exc}"
            self.close()
            raise XDamageUnavailableError(self._failure) from exc

    def wait(self, timeout_ms: int) -> XDamageWaitResult:
        started = perf_counter()
        try:
            self.start()
        except XDamageUnavailableError:
            return XDamageWaitResult(
                available=False,
                detected=False,
                wait_ms=(perf_counter() - started) * 1000,
                reason=self._failure or "unavailable",
                version=self._version,
            )
        assert self._x11 is not None
        assert self._display is not None
        assert self._event_base is not None

        deadline = started + timeout_ms / 1000
        while True:
            if self._next_damage_event():
                self._subtract_damage()
                self._sync()
                return XDamageWaitResult(
                    available=True,
                    detected=True,
                    wait_ms=(perf_counter() - started) * 1000,
                    version=self._version,
                )
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return XDamageWaitResult(
                    available=True,
                    detected=False,
                    wait_ms=(perf_counter() - started) * 1000,
                    reason="timeout",
                    version=self._version,
                )
            fd = self._x11.XConnectionNumber(ctypes.c_void_p(self._display))
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                return XDamageWaitResult(
                    available=True,
                    detected=False,
                    wait_ms=(perf_counter() - started) * 1000,
                    reason="timeout",
                    version=self._version,
                )

    def close(self) -> None:
        if self._display is not None and self._xdamage is not None and self._damage is not None:
            self._xdamage.XDamageDestroy(
                ctypes.c_void_p(self._display),
                ctypes.c_ulong(self._damage),
            )
        if self._display is not None and self._x11 is not None:
            self._x11.XCloseDisplay(ctypes.c_void_p(self._display))
        self._display = None
        self._damage = None
        self._event_base = None

    def _next_damage_event(self) -> bool:
        assert self._x11 is not None
        assert self._display is not None
        assert self._event_base is not None
        while self._x11.XPending(ctypes.c_void_p(self._display)) > 0:
            event = _XEvent()
            self._x11.XNextEvent(ctypes.c_void_p(self._display), ctypes.byref(event))
            if event.type == self._event_base:
                return True
        return False

    def _subtract_damage(self) -> None:
        assert self._xdamage is not None
        assert self._display is not None
        assert self._damage is not None
        self._xdamage.XDamageSubtract(
            ctypes.c_void_p(self._display),
            ctypes.c_ulong(self._damage),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )

    def _sync(self) -> None:
        assert self._x11 is not None
        assert self._display is not None
        self._x11.XSync(ctypes.c_void_p(self._display), 0)

    def _drain_events(self) -> None:
        assert self._x11 is not None
        assert self._display is not None
        while self._x11.XPending(ctypes.c_void_p(self._display)) > 0:
            event = _XEvent()
            self._x11.XNextEvent(ctypes.c_void_p(self._display), ctypes.byref(event))

    def _load_libraries(self) -> None:
        if self._x11 is not None and self._xdamage is not None:
            return
        x11_path = ctypes.util.find_library("X11")
        xdamage_path = ctypes.util.find_library("Xdamage")
        if not x11_path or not xdamage_path:
            raise XDamageUnavailableError("libX11 or libXdamage not found")
        x11 = ctypes.CDLL(x11_path)
        xdamage = ctypes.CDLL(xdamage_path)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XConnectionNumber.argtypes = [ctypes.c_void_p]
        x11.XConnectionNumber.restype = ctypes.c_int
        x11.XPending.argtypes = [ctypes.c_void_p]
        x11.XPending.restype = ctypes.c_int
        x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
        x11.XNextEvent.restype = ctypes.c_int
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XSync.restype = ctypes.c_int
        xdamage.XDamageQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        xdamage.XDamageQueryExtension.restype = ctypes.c_int
        xdamage.XDamageQueryVersion.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        xdamage.XDamageQueryVersion.restype = ctypes.c_int
        xdamage.XDamageCreate.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
        xdamage.XDamageCreate.restype = ctypes.c_ulong
        xdamage.XDamageSubtract.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        xdamage.XDamageSubtract.restype = None
        xdamage.XDamageDestroy.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        xdamage.XDamageDestroy.restype = None
        self._x11 = x11
        self._xdamage = xdamage


def xdamage_supported_by_xdpyinfo(output: str) -> bool:
    return any(line.strip() == "DAMAGE" for line in output.splitlines())


def xdamage_display_from_env(default: str = ":99") -> str:
    return os.getenv("DISPLAY") or default
