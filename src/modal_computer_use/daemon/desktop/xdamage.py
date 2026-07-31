from __future__ import annotations

import ctypes
import ctypes.util
import select
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import ClassVar, Literal

X_DAMAGE_REPORT_DELTA_RECTANGLES = 1
X_DAMAGE_REPORT_NON_EMPTY = 3
_MAX_DAMAGE_RECTS = 64
ChangeSignal = Literal["poll", "xdamage", "auto"]
ActiveChangeSignal = Literal["poll", "xdamage"]


class XDamageUnavailableError(RuntimeError):
    """Raised when XDamage cannot be used for display change events."""


@dataclass(frozen=True)
class XDamageRect:
    x: int
    y: int
    width: int
    height: int

    def valid(self) -> bool:
        return self.width > 0 and self.height > 0

    def model_dump(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class XDamageWaitResult:
    available: bool
    detected: bool
    wait_ms: float
    reason: str | None = None
    version: str | None = None
    dirty_rect: XDamageRect | None = None
    dirty_rects: tuple[XDamageRect, ...] = ()


class _XEvent(ctypes.Union):
    _fields_: ClassVar = [("type", ctypes.c_int), ("pad", ctypes.c_long * 24)]


class _XRectangle(ctypes.Structure):
    _fields_: ClassVar = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
    ]


class _XDamageNotifyEvent(ctypes.Structure):
    _fields_: ClassVar = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("drawable", ctypes.c_ulong),
        ("damage", ctypes.c_ulong),
        ("level", ctypes.c_int),
        ("more", ctypes.c_int),
        ("timestamp", ctypes.c_ulong),
        ("area", _XRectangle),
        ("geometry", _XRectangle),
    ]


class XDamageWatcher:
    def __init__(self, *, display: str, rect_hints: bool = False) -> None:
        self._display_name = display.encode()
        self._rect_hints = rect_hints
        self._x11: ctypes.CDLL | None = None
        self._xdamage: ctypes.CDLL | None = None
        self._xfixes: ctypes.CDLL | None = None
        self._display: int | None = None
        self._damage: int | None = None
        self._parts_region: int | None = None
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
                X_DAMAGE_REPORT_DELTA_RECTANGLES
                if self._rect_hints
                else X_DAMAGE_REPORT_NON_EMPTY,
            )
            if not damage:
                self._x11.XCloseDisplay(ctypes.c_void_p(display))
                raise XDamageUnavailableError("XDamageCreate failed")

            self._display = int(display)
            self._damage = int(damage)
            self._event_base = event_base.value
            self._parts_region = self._create_xfixes_region()
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
            event_rects = self._next_damage_rects()
            if event_rects is not None:
                fetched_rects = self._subtract_damage(fetch_rects=self._rect_hints)
                self._sync()
                dirty_rects = tuple(fetched_rects or event_rects)
                dirty_rect = _union_rects(dirty_rects)
                return XDamageWaitResult(
                    available=True,
                    detected=True,
                    wait_ms=(perf_counter() - started) * 1000,
                    version=self._version,
                    dirty_rect=dirty_rect,
                    dirty_rects=dirty_rects,
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
        if (
            self._display is not None
            and self._xfixes is not None
            and self._parts_region is not None
        ):
            self._xfixes.XFixesDestroyRegion(
                ctypes.c_void_p(self._display),
                ctypes.c_ulong(self._parts_region),
            )
            self._parts_region = None
        if self._display is not None and self._xdamage is not None and self._damage is not None:
            self._xdamage.XDamageDestroy(
                ctypes.c_void_p(self._display),
                ctypes.c_ulong(self._damage),
            )
        if self._display is not None and self._x11 is not None:
            self._x11.XCloseDisplay(ctypes.c_void_p(self._display))
        self._display = None
        self._damage = None
        self._parts_region = None
        self._event_base = None

    def _next_damage_rects(self) -> tuple[XDamageRect, ...] | None:
        assert self._x11 is not None
        assert self._display is not None
        assert self._event_base is not None
        rects: list[XDamageRect] = []
        detected = False
        while self._x11.XPending(ctypes.c_void_p(self._display)) > 0:
            event = _XEvent()
            self._x11.XNextEvent(ctypes.c_void_p(self._display), ctypes.byref(event))
            if event.type == self._event_base:
                detected = True
                if not self._rect_hints:
                    continue
                rect = _rect_from_damage_event(event)
                if rect is not None and rect.valid():
                    rects.append(rect)
                    if len(rects) >= _MAX_DAMAGE_RECTS:
                        break
        return tuple(rects) if detected else None

    def _subtract_damage(self, *, fetch_rects: bool = False) -> tuple[XDamageRect, ...]:
        assert self._xdamage is not None
        assert self._display is not None
        assert self._damage is not None
        parts_region = self._parts_region if fetch_rects else None
        self._xdamage.XDamageSubtract(
            ctypes.c_void_p(self._display),
            ctypes.c_ulong(self._damage),
            ctypes.c_ulong(0),
            ctypes.c_ulong(parts_region or 0),
        )
        if not fetch_rects or parts_region is None:
            return ()
        return self._fetch_xfixes_region_rects(parts_region)

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
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XFree.restype = ctypes.c_int
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
        self._xfixes = self._load_xfixes()
        self._x11 = x11
        self._xdamage = xdamage

    def _load_xfixes(self) -> ctypes.CDLL | None:
        xfixes_path = ctypes.util.find_library("Xfixes")
        if not xfixes_path:
            return None
        xfixes = ctypes.CDLL(xfixes_path)
        xfixes.XFixesCreateRegion.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_XRectangle),
            ctypes.c_int,
        ]
        xfixes.XFixesCreateRegion.restype = ctypes.c_ulong
        xfixes.XFixesFetchRegionAndBounds.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(_XRectangle),
        ]
        xfixes.XFixesFetchRegionAndBounds.restype = ctypes.POINTER(_XRectangle)
        xfixes.XFixesDestroyRegion.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        xfixes.XFixesDestroyRegion.restype = None
        return xfixes

    def _create_xfixes_region(self) -> int | None:
        if self._xfixes is None or self._display is None:
            return None
        region = self._xfixes.XFixesCreateRegion(
            ctypes.c_void_p(self._display),
            None,
            0,
        )
        return int(region) if region else None

    def _fetch_xfixes_region_rects(self, region: int) -> tuple[XDamageRect, ...]:
        if self._xfixes is None or self._x11 is None or self._display is None:
            return ()
        count = ctypes.c_int()
        bounds = _XRectangle()
        rect_ptr = self._xfixes.XFixesFetchRegionAndBounds(
            ctypes.c_void_p(self._display),
            ctypes.c_ulong(region),
            ctypes.byref(count),
            ctypes.byref(bounds),
        )
        if not rect_ptr:
            return ()
        try:
            rects = [
                _rect_from_xrectangle(rect_ptr[index])
                for index in range(min(count.value, _MAX_DAMAGE_RECTS))
            ]
            return tuple(rect for rect in rects if rect.valid())
        finally:
            self._x11.XFree(ctypes.cast(rect_ptr, ctypes.c_void_p))


@dataclass(frozen=True)
class PreparedChangeSignal:
    requested: ChangeSignal
    active: ActiveChangeSignal
    watcher: XDamageWatcher | None = None
    unavailable_reason: str | None = None

    @property
    def wait_watcher(self) -> XDamageWatcher | None:
        return self.watcher if self.active == "xdamage" else None

    @property
    def reusable_watcher(self) -> XDamageWatcher | None:
        return self.watcher

    def metadata(self, result: XDamageWaitResult | None) -> dict[str, object]:
        if self.active == "poll":
            available: bool | None = False if self.requested != "poll" else None
        else:
            available = None if result is None else result.available
        return {
            "change_signal_requested": self.requested,
            "change_signal_active": self.active,
            "change_signal_available": available,
            "change_signal_detected": None if result is None else result.detected,
            "change_signal_wait_ms": None if result is None else result.wait_ms,
            "change_signal_reason": (
                result.reason if result is not None else self.unavailable_reason
            ),
            "change_signal_version": None if result is None else result.version,
        }

    def close(self) -> None:
        if self.watcher is not None:
            self.watcher.close()


def prepare_change_signal(
    requested: ChangeSignal,
    *,
    display: str | None,
    watcher: XDamageWatcher | None = None,
    watcher_factory: Callable[..., XDamageWatcher] = XDamageWatcher,
) -> PreparedChangeSignal:
    if requested == "poll":
        return PreparedChangeSignal(requested=requested, active="poll")
    if not isinstance(display, str) or not display:
        return PreparedChangeSignal(
            requested=requested,
            active="poll",
            unavailable_reason="backend has no X11 display",
        )
    prepared_watcher = watcher or watcher_factory(display=display)
    try:
        prepared_watcher.arm()
    except Exception:
        unavailable_reason = prepared_watcher.failure or "XDamage unavailable"
        if requested == "auto":
            return PreparedChangeSignal(
                requested=requested,
                active="poll",
                watcher=prepared_watcher,
                unavailable_reason=unavailable_reason,
            )
        return PreparedChangeSignal(
            requested=requested,
            active="xdamage",
            watcher=prepared_watcher,
            unavailable_reason=unavailable_reason,
        )
    return PreparedChangeSignal(
        requested=requested,
        active="xdamage",
        watcher=prepared_watcher,
    )


def _rect_from_damage_event(event: _XEvent) -> XDamageRect | None:
    notify = ctypes.cast(ctypes.byref(event), ctypes.POINTER(_XDamageNotifyEvent)).contents
    return _rect_from_xrectangle(notify.area)


def _rect_from_xrectangle(rect: _XRectangle) -> XDamageRect:
    return XDamageRect(
        x=int(rect.x),
        y=int(rect.y),
        width=int(rect.width),
        height=int(rect.height),
    )


def _union_rects(rects: tuple[XDamageRect, ...]) -> XDamageRect | None:
    valid_rects = [rect for rect in rects if rect.valid()]
    if not valid_rects:
        return None
    x0 = min(rect.x for rect in valid_rects)
    y0 = min(rect.y for rect in valid_rects)
    x1 = max(rect.x + rect.width for rect in valid_rects)
    y1 = max(rect.y + rect.height for rect in valid_rects)
    if x1 <= x0 or y1 <= y0:
        return None
    return XDamageRect(x=x0, y=y0, width=x1 - x0, height=y1 - y0)
