from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_CURRENT_SCREEN = -1
_XKB_USE_CORE_KBD = 0x0100


class X11InputUnavailableError(RuntimeError):
    """Raised before input emission when the native X11 adapter cannot be used."""


class X11InputInjectionError(RuntimeError):
    """Raised after native input emission may have started.

    Callers must not replay the operation through another adapter because doing so
    could duplicate an event that the X server already accepted.
    """


# Keep the established name import-compatible while callers migrate to the semantic name.
XTestUnavailableError = X11InputUnavailableError


class _X11DisplayNotReadyError(X11InputUnavailableError):
    """Raised when the configured display may still be starting."""


@dataclass(frozen=True, slots=True)
class KeyEvent:
    keycode: int
    pressed: bool


@dataclass(frozen=True, slots=True)
class ButtonEvent:
    button: int
    pressed: bool


@dataclass(frozen=True, slots=True)
class MotionEvent:
    x: int
    y: int
    screen: int = _CURRENT_SCREEN


type X11InputEvent = KeyEvent | ButtonEvent | MotionEvent


class _XkbStateRec(ctypes.Structure):
    _fields_ = [
        ("group", ctypes.c_ubyte),
        ("locked_group", ctypes.c_ubyte),
        ("base_group", ctypes.c_ushort),
        ("latched_group", ctypes.c_ushort),
        ("mods", ctypes.c_ubyte),
        ("base_mods", ctypes.c_ubyte),
        ("latched_mods", ctypes.c_ubyte),
        ("locked_mods", ctypes.c_ubyte),
        ("compat_state", ctypes.c_ubyte),
        ("grab_mods", ctypes.c_ubyte),
        ("compat_grab_mods", ctypes.c_ubyte),
        ("lookup_mods", ctypes.c_ubyte),
        ("compat_lookup_mods", ctypes.c_ubyte),
        ("ptr_buttons", ctypes.c_ushort),
    ]


class X11InputSession:
    """Persistent, serialized access to raw Xlib and XTest input primitives."""

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
        with self._lock:
            try:
                self._ensure_open()
            except X11InputUnavailableError:
                return False
            return True

    def close(self) -> None:
        with self._lock:
            if self._display is not None and self._x11 is not None:
                self._x11.XCloseDisplay(ctypes.c_void_p(self._display))
            self._display = None
            self._available = None

    def resolve_keysym(self, name: str) -> int:
        encoded_name = name.encode()
        with self._lock:
            self._ensure_open()
            assert self._x11 is not None
            keysym = int(self._x11.XStringToKeysym(ctypes.c_char_p(encoded_name)))
        if keysym == 0:
            raise X11InputUnavailableError(f"X11 keysym is not mapped: {name!r}")
        return keysym

    def keysym_to_keycode(self, keysym: int) -> int:
        with self._lock:
            display = self._ensure_open()
            assert self._x11 is not None
            keycode = int(
                self._x11.XKeysymToKeycode(
                    ctypes.c_void_p(display),
                    ctypes.c_ulong(keysym),
                )
            )
        if keycode == 0:
            raise X11InputUnavailableError(f"X11 keysym has no keycode: {keysym:#x}")
        return keycode

    def resolve_keycode(self, name: str) -> int:
        return self.keysym_to_keycode(self.resolve_keysym(name))

    def keycode_to_keysym(self, keycode: int, group: int, level: int) -> int:
        self._validate_keycode(keycode)
        with self._lock:
            display = self._ensure_open()
            assert self._x11 is not None
            return int(
                self._x11.XkbKeycodeToKeysym(
                    ctypes.c_void_p(display),
                    ctypes.c_ubyte(keycode),
                    group,
                    level,
                )
            )

    def keyboard_group(self) -> int:
        return self._xkb_state().group

    def modifier_state(self) -> int:
        return self._xkb_state().mods

    def pressed_keycodes(self, keycodes: Iterable[int] | None = None) -> frozenset[int]:
        requested = None if keycodes is None else tuple(keycodes)
        if requested is not None:
            for keycode in requested:
                self._validate_keycode(keycode)
        with self._lock:
            display = self._ensure_open()
            pressed = self._query_pressed_keycodes(display)
        if requested is None:
            return pressed
        return frozenset(keycode for keycode in requested if keycode in pressed)

    def pointer_position(self) -> tuple[int, int]:
        with self._lock:
            display = self._ensure_open()
            assert self._x11 is not None
            screen_count = int(self._x11.XScreenCount(ctypes.c_void_p(display)))
            for screen in range(screen_count):
                root = self._x11.XRootWindow(ctypes.c_void_p(display), screen)
                root_return = ctypes.c_ulong()
                child_return = ctypes.c_ulong()
                root_x = ctypes.c_int()
                root_y = ctypes.c_int()
                window_x = ctypes.c_int()
                window_y = ctypes.c_int()
                mask = ctypes.c_uint()
                found = self._x11.XQueryPointer(
                    ctypes.c_void_p(display),
                    root,
                    ctypes.byref(root_return),
                    ctypes.byref(child_return),
                    ctypes.byref(root_x),
                    ctypes.byref(root_y),
                    ctypes.byref(window_x),
                    ctypes.byref(window_y),
                    ctypes.byref(mask),
                )
                if found:
                    return root_x.value, root_y.value
        raise X11InputUnavailableError("XQueryPointer could not locate the pointer")

    def emit(
        self,
        events: Sequence[X11InputEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
    ) -> None:
        sequence = tuple(events)
        preserved = frozenset(preserve_pressed_keycodes)
        self._validate_events(sequence)
        for keycode in preserved:
            self._validate_keycode(keycode)

        with self._lock:
            display = self._ensure_open()
            already_pressed = (
                self._query_pressed_keycodes(display).intersection(preserved)
                if preserved
                else frozenset()
            )
            filtered = tuple(
                event
                for event in sequence
                if not (isinstance(event, KeyEvent) and event.keycode in already_pressed)
            )
            if not filtered:
                return
            self._emit_locked(display, filtered)

    def _xkb_state(self) -> _XkbStateRec:
        with self._lock:
            display = self._ensure_open()
            assert self._x11 is not None
            state = _XkbStateRec()
            status = self._x11.XkbGetState(
                ctypes.c_void_p(display),
                _XKB_USE_CORE_KBD,
                ctypes.byref(state),
            )
            if status != 0:
                raise X11InputUnavailableError(f"XkbGetState failed with status {status}")
            return state

    def _ensure_open(self) -> int:
        if self._available is False:
            raise X11InputUnavailableError(self._failure or "native X11 input unavailable")
        if self._display is not None:
            return self._display
        try:
            self._load_libraries()
            assert self._x11 is not None
            assert self._xtst is not None
            display = self._x11.XOpenDisplay(ctypes.c_char_p(self._display_name))
            if not display:
                raise _X11DisplayNotReadyError("XOpenDisplay failed")
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
                raise X11InputUnavailableError("XTest extension unavailable")
            self._display = int(display)
            self._available = True
            self._failure = None
            return self._display
        except _X11DisplayNotReadyError as exc:
            self._available = None
            self._failure = str(exc)
            raise
        except X11InputUnavailableError as exc:
            self._available = False
            self._failure = str(exc)
            raise
        except Exception as exc:
            self._available = False
            self._failure = f"{type(exc).__name__}: {exc}"
            raise X11InputUnavailableError(self._failure) from exc

    def _load_libraries(self) -> None:
        if self._x11 is not None and self._xtst is not None:
            return
        x11_path = ctypes.util.find_library("X11")
        xtst_path = ctypes.util.find_library("Xtst")
        if not x11_path or not xtst_path:
            raise X11InputUnavailableError("libX11 or libXtst not found")
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
        x11.XScreenCount.argtypes = [ctypes.c_void_p]
        x11.XScreenCount.restype = ctypes.c_int
        x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XRootWindow.restype = ctypes.c_ulong
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x11.XStringToKeysym.restype = ctypes.c_ulong
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        x11.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char)]
        x11.XQueryKeymap.restype = ctypes.c_int
        x11.XkbKeycodeToKeysym.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_int,
            ctypes.c_int,
        ]
        x11.XkbKeycodeToKeysym.restype = ctypes.c_ulong
        x11.XkbGetState.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(_XkbStateRec),
        ]
        x11.XkbGetState.restype = ctypes.c_int

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
        xtst.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xtst.XTestFakeKeyEvent.restype = ctypes.c_int
        self._x11 = x11
        self._xtst = xtst

    def _query_pressed_keycodes(self, display: int) -> frozenset[int]:
        assert self._x11 is not None
        keymap = (ctypes.c_char * 32)()
        ok = self._x11.XQueryKeymap(ctypes.c_void_p(display), keymap)
        if not ok:
            raise X11InputUnavailableError("XQueryKeymap failed")
        return frozenset(
            keycode
            for keycode in range(256)
            if keymap[keycode >> 3][0] & (1 << (keycode & 7))
        )

    def _emit_locked(self, display: int, events: Sequence[X11InputEvent]) -> None:
        assert self._x11 is not None
        assert self._xtst is not None
        held_by_sequence: list[KeyEvent | ButtonEvent] = []
        attempted = False
        try:
            for event in events:
                attempted = True
                if isinstance(event, (KeyEvent, ButtonEvent)) and event.pressed:
                    # Treat a failed press call as indeterminate and release it during cleanup.
                    held_by_sequence.append(event)
                ok = self._fake_event(display, event)
                if not ok:
                    raise X11InputInjectionError(
                        f"{type(event).__name__} may not have been injected"
                    )
                if isinstance(event, (KeyEvent, ButtonEvent)) and not event.pressed:
                    self._discard_matching_press(held_by_sequence, event)
            self._x11.XFlush(ctypes.c_void_p(display))
            self._x11.XSync(ctypes.c_void_p(display), 0)
        except Exception as exc:
            if attempted:
                self._release_after_failure(display, held_by_sequence)
                if isinstance(exc, X11InputInjectionError):
                    raise
                raise X11InputInjectionError(
                    f"native X11 input sequence may be partially injected: {exc}"
                ) from exc
            raise

    def _fake_event(self, display: int, event: X11InputEvent) -> int:
        assert self._xtst is not None
        display_ptr = ctypes.c_void_p(display)
        if isinstance(event, KeyEvent):
            return int(
                self._xtst.XTestFakeKeyEvent(
                    display_ptr,
                    event.keycode,
                    int(event.pressed),
                    0,
                )
            )
        if isinstance(event, ButtonEvent):
            return int(
                self._xtst.XTestFakeButtonEvent(
                    display_ptr,
                    event.button,
                    int(event.pressed),
                    0,
                )
            )
        return int(
            self._xtst.XTestFakeMotionEvent(
                display_ptr,
                event.screen,
                event.x,
                event.y,
                0,
            )
        )

    def _release_after_failure(
        self,
        display: int,
        held_by_sequence: Sequence[KeyEvent | ButtonEvent],
    ) -> None:
        assert self._x11 is not None
        for event in reversed(held_by_sequence):
            with contextlib.suppress(Exception):
                self._fake_event(
                    display,
                    KeyEvent(event.keycode, False)
                    if isinstance(event, KeyEvent)
                    else ButtonEvent(event.button, False),
                )
        with contextlib.suppress(Exception):
            self._x11.XFlush(ctypes.c_void_p(display))
            self._x11.XSync(ctypes.c_void_p(display), 0)

    @staticmethod
    def _discard_matching_press(
        held_by_sequence: list[KeyEvent | ButtonEvent],
        release: KeyEvent | ButtonEvent,
    ) -> None:
        for index in range(len(held_by_sequence) - 1, -1, -1):
            pressed = held_by_sequence[index]
            if isinstance(release, KeyEvent) and isinstance(pressed, KeyEvent):
                if pressed.keycode == release.keycode:
                    held_by_sequence.pop(index)
                    return
            elif (
                isinstance(release, ButtonEvent)
                and isinstance(pressed, ButtonEvent)
                and pressed.button == release.button
            ):
                held_by_sequence.pop(index)
                return

    @classmethod
    def _validate_events(cls, events: Sequence[X11InputEvent]) -> None:
        for event in events:
            if isinstance(event, KeyEvent):
                cls._validate_keycode(event.keycode)
            elif isinstance(event, ButtonEvent):
                if not 1 <= event.button <= 255:
                    raise ValueError("button must be between 1 and 255")
            elif not isinstance(event, MotionEvent):
                raise TypeError(f"unsupported X11 input event: {type(event).__name__}")

    @staticmethod
    def _validate_keycode(keycode: int) -> None:
        if not 1 <= keycode <= 255:
            raise ValueError("keycode must be between 1 and 255")


# Compatibility for integrations that imported the pointer-specific class before it was deepened.
XTestPointerController = X11InputSession


__all__ = [
    "ButtonEvent",
    "KeyEvent",
    "MotionEvent",
    "X11InputEvent",
    "X11InputInjectionError",
    "X11InputSession",
    "X11InputUnavailableError",
    "XTestPointerController",
    "XTestUnavailableError",
]
