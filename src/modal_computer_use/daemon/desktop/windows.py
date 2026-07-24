from __future__ import annotations

import asyncio
import builtins
import ctypes
import ctypes.util
import shutil
import subprocess
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol

from modal_computer_use.models import ActionResult, X11Window

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]

_ANY_PROPERTY_TYPE = 0
_CLIENT_MESSAGE = 33
_CURRENT_TIME = 0
_SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
_SUBSTRUCTURE_REDIRECT_MASK = 1 << 20
_WINDOW_ID_MAX = (1 << 32) - 1


class X11WindowNativeError(RuntimeError):
    """Base error for native X11 window operations."""


class X11WindowNativeUnavailableError(X11WindowNativeError):
    """Raised when the native X11 window adapter is unavailable."""


class X11WindowNativeOperationError(X11WindowNativeError):
    """Raised when a native X11 window operation cannot be completed."""


class _X11WindowDisplayNotReadyError(X11WindowNativeUnavailableError):
    """Raised while the configured X display may still be starting."""


@dataclass(frozen=True)
class NativeWindowRequest:
    verified: bool


class NativeWindowAdapter(Protocol):
    @property
    def failure(self) -> str | None: ...

    def available(self) -> bool: ...

    def close_display(self) -> None: ...

    def list(self) -> list[X11Window]: ...

    def activate(self, window_id: int) -> NativeWindowRequest: ...

    def close(self, window_id: int) -> NativeWindowRequest: ...


class _ClientMessageData(ctypes.Union):
    _fields_: ClassVar = [
        ("bytes", ctypes.c_char * 20),
        ("shorts", ctypes.c_short * 10),
        ("longs", ctypes.c_long * 5),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_: ClassVar = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _ClientMessageData),
    ]


class _XEvent(ctypes.Union):
    _fields_: ClassVar = [
        ("client_message", _XClientMessageEvent),
        ("padding", ctypes.c_long * 24),
    ]


class _XWindowAttributes(ctypes.Structure):
    _fields_: ClassVar = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("window_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class NativeEwmhWindowAdapter:
    """Persistent Xlib adapter for EWMH window-manager requests and properties."""

    def __init__(self, *, display: str | None = None) -> None:
        self._display_name = display.encode() if display is not None else None
        self._lock = threading.Lock()
        self._x11: ctypes.CDLL | None = None
        self._display: int | None = None
        self._root: int | None = None
        self._atoms: dict[str, int] = {}
        self._failure: str | None = None
        self._permanently_unavailable = False

    @property
    def failure(self) -> str | None:
        return self._failure

    def available(self) -> bool:
        try:
            with self._lock:
                self._ensure_open()
                self._client_window_ids()
        except X11WindowNativeError as exc:
            self._failure = str(exc)
            return False
        except Exception as exc:
            self._failure = f"native X11 window probe failed: {type(exc).__name__}"
            return False
        self._failure = None
        return True

    def close_display(self) -> None:
        with self._lock:
            if self._display is not None and self._x11 is not None:
                self._x11.XCloseDisplay(ctypes.c_void_p(self._display))
            self._display = None
            self._root = None
            self._atoms.clear()

    def list(self) -> list[X11Window]:
        with self._lock:
            try:
                self._ensure_open()
                window_ids = self._client_window_ids()
                active_id = self._active_window_id()
                windows: list[X11Window] = []
                for window_id in window_ids:
                    window = self._read_window(window_id, active_id=active_id)
                    if window is not None:
                        windows.append(window)
                return windows
            except X11WindowNativeError:
                raise
            except Exception as exc:
                raise X11WindowNativeOperationError(
                    f"native X11 window listing failed: {type(exc).__name__}"
                ) from exc

    def activate(self, window_id: int) -> NativeWindowRequest:
        with self._lock:
            try:
                display, root = self._ensure_open()
                managed_windows = self._client_window_ids()
                if window_id not in managed_windows:
                    raise X11WindowNativeOperationError(
                        "window is not managed by the window manager"
                    )
                current_active = self._active_window_id() or 0
                self._send_client_message(
                    display=display,
                    root=root,
                    window_id=window_id,
                    message_name="_NET_ACTIVE_WINDOW",
                    data=(1, _CURRENT_TIME, current_active, 0, 0),
                )
                self._sync(display)
                return NativeWindowRequest(verified=self._active_window_id() == window_id)
            except X11WindowNativeError:
                raise
            except Exception as exc:
                raise X11WindowNativeOperationError(
                    f"native X11 window activation failed: {type(exc).__name__}"
                ) from exc

    def close(self, window_id: int) -> NativeWindowRequest:
        with self._lock:
            try:
                display, root = self._ensure_open()
                managed_windows = self._client_window_ids()
                if window_id not in managed_windows:
                    raise X11WindowNativeOperationError(
                        "window is not managed by the window manager"
                    )
                self._send_client_message(
                    display=display,
                    root=root,
                    window_id=window_id,
                    message_name="_NET_CLOSE_WINDOW",
                    data=(_CURRENT_TIME, 1, 0, 0, 0),
                )
                self._sync(display)
                return NativeWindowRequest(verified=window_id not in self._client_window_ids())
            except X11WindowNativeError:
                raise
            except Exception as exc:
                raise X11WindowNativeOperationError(
                    f"native X11 window close failed: {type(exc).__name__}"
                ) from exc

    def _ensure_open(self) -> tuple[int, int]:
        if self._permanently_unavailable:
            raise X11WindowNativeUnavailableError(self._failure)
        if self._display is not None and self._root is not None:
            return self._display, self._root
        try:
            self._load_library()
            assert self._x11 is not None
            display_name = (
                ctypes.c_char_p(self._display_name) if self._display_name is not None else None
            )
            display_pointer = self._x11.XOpenDisplay(display_name)
            if not display_pointer:
                raise _X11WindowDisplayNotReadyError("XOpenDisplay failed")
            display = int(display_pointer)
            root = int(self._x11.XDefaultRootWindow(ctypes.c_void_p(display)))
            if not root:
                self._x11.XCloseDisplay(ctypes.c_void_p(display))
                raise X11WindowNativeUnavailableError("XDefaultRootWindow failed")
            self._display = display
            self._root = root
            return display, root
        except _X11WindowDisplayNotReadyError as exc:
            self._failure = str(exc)
            raise
        except X11WindowNativeUnavailableError as exc:
            self._failure = str(exc)
            self._permanently_unavailable = True
            raise
        except Exception as exc:
            self._failure = f"native X11 initialization failed: {type(exc).__name__}"
            self._permanently_unavailable = True
            raise X11WindowNativeUnavailableError(self._failure) from exc

    def _load_library(self) -> None:
        if self._x11 is not None:
            return
        path = ctypes.util.find_library("X11")
        if not path:
            raise X11WindowNativeUnavailableError("libX11 not found")
        x11 = ctypes.CDLL(path)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        x11.XGetWindowProperty.restype = ctypes.c_int
        x11.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_XWindowAttributes),
        ]
        x11.XGetWindowAttributes.restype = ctypes.c_int
        x11.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        x11.XTranslateCoordinates.restype = ctypes.c_int
        x11.XSendEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.POINTER(_XEvent),
        ]
        x11.XSendEvent.restype = ctypes.c_int
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XSync.restype = ctypes.c_int
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XFree.restype = ctypes.c_int
        self._x11 = x11

    def _atom(self, name: str, *, only_if_exists: bool = False) -> int:
        if name in self._atoms:
            return self._atoms[name]
        assert self._x11 is not None
        assert self._display is not None
        atom = int(
            self._x11.XInternAtom(
                ctypes.c_void_p(self._display),
                name.encode(),
                int(only_if_exists),
            )
        )
        self._atoms[name] = atom
        return atom

    def _property(
        self,
        window_id: int,
        name: str,
        *,
        expected_type: int = _ANY_PROPERTY_TYPE,
        length: int = 4096,
    ) -> tuple[int, bytes] | None:
        assert self._x11 is not None
        assert self._display is not None
        property_atom = self._atom(name, only_if_exists=True)
        if not property_atom:
            return None
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        item_count = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self._x11.XGetWindowProperty(
            ctypes.c_void_p(self._display),
            ctypes.c_ulong(window_id),
            ctypes.c_ulong(property_atom),
            0,
            length,
            0,
            ctypes.c_ulong(expected_type),
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(item_count),
            ctypes.byref(bytes_after),
            ctypes.byref(data),
        )
        if status != 0 or actual_type.value == 0:
            if data:
                self._x11.XFree(ctypes.cast(data, ctypes.c_void_p))
            return None
        try:
            item_size = (
                ctypes.sizeof(ctypes.c_ulong)
                if actual_format.value == 32
                else actual_format.value // 8
            )
            if item_size <= 0:
                return None
            return actual_format.value, ctypes.string_at(data, item_count.value * item_size)
        finally:
            if data:
                self._x11.XFree(ctypes.cast(data, ctypes.c_void_p))

    def _property_values(
        self,
        window_id: int,
        name: str,
        *,
        expected_type_name: str | None = None,
    ) -> builtins.list[int] | None:
        expected_type = (
            self._atom(expected_type_name, only_if_exists=True)
            if expected_type_name is not None
            else _ANY_PROPERTY_TYPE
        )
        if expected_type_name is not None and not expected_type:
            return None
        value = self._property(window_id, name, expected_type=expected_type)
        if value is None:
            return None
        value_format, data = value
        if value_format != 32:
            return None
        item_size = ctypes.sizeof(ctypes.c_ulong)
        count = len(data) // item_size
        if count == 0:
            return []
        values_type = ctypes.c_ulong * count
        return list(values_type.from_buffer_copy(data))

    def _client_window_ids(self) -> builtins.list[int]:
        assert self._root is not None
        for name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
            values = self._property_values(
                self._root,
                name,
                expected_type_name="WINDOW",
            )
            if values is not None:
                return values
        raise X11WindowNativeUnavailableError("window manager does not expose _NET_CLIENT_LIST")

    def _active_window_id(self) -> int | None:
        assert self._root is not None
        values = self._property_values(
            self._root,
            "_NET_ACTIVE_WINDOW",
            expected_type_name="WINDOW",
        )
        if not values or values[0] == 0:
            return None
        return values[0]

    def _read_window(self, window_id: int, *, active_id: int | None) -> X11Window | None:
        geometry = self._geometry(window_id)
        if geometry is None:
            return None
        x, y, width, height = geometry
        return X11Window(
            id=normalize_window_id(str(window_id)),
            title=self._window_title(window_id),
            class_name=self._window_class(window_id),
            pid=self._first_integer_property(window_id, "_NET_WM_PID"),
            x=x,
            y=y,
            width=width,
            height=height,
            workspace=self._workspace(window_id),
            is_active=window_id == active_id,
        )

    def _window_title(self, window_id: int) -> str:
        utf8_string = self._atom("UTF8_STRING", only_if_exists=True)
        value = self._property(
            window_id,
            "_NET_WM_NAME",
            expected_type=utf8_string or _ANY_PROPERTY_TYPE,
        )
        if value is not None and value[0] == 8:
            return value[1].rstrip(b"\0").decode("utf-8", errors="replace")
        value = self._property(window_id, "WM_NAME")
        if value is None or value[0] != 8:
            return ""
        return value[1].rstrip(b"\0").decode("latin-1", errors="replace")

    def _window_class(self, window_id: int) -> str | None:
        value = self._property(window_id, "WM_CLASS")
        if value is None or value[0] != 8:
            return None
        fields = [
            field.decode("latin-1", errors="replace")
            for field in value[1].rstrip(b"\0").split(b"\0")
            if field
        ]
        return ".".join(fields) if fields else None

    def _first_integer_property(self, window_id: int, name: str) -> int | None:
        values = self._property_values(
            window_id,
            name,
            expected_type_name="CARDINAL",
        )
        return values[0] if values else None

    def _workspace(self, window_id: int) -> int | None:
        workspace = self._first_integer_property(window_id, "_NET_WM_DESKTOP")
        return -1 if workspace == _WINDOW_ID_MAX else workspace

    def _geometry(self, window_id: int) -> tuple[int, int, int, int] | None:
        assert self._x11 is not None
        assert self._display is not None
        assert self._root is not None
        attributes = _XWindowAttributes()
        if not self._x11.XGetWindowAttributes(
            ctypes.c_void_p(self._display),
            ctypes.c_ulong(window_id),
            ctypes.byref(attributes),
        ):
            return None
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        child = ctypes.c_ulong()
        translated = self._x11.XTranslateCoordinates(
            ctypes.c_void_p(self._display),
            ctypes.c_ulong(window_id),
            ctypes.c_ulong(self._root),
            0,
            0,
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(child),
        )
        if not translated:
            return None
        return root_x.value, root_y.value, attributes.width, attributes.height

    def _send_client_message(
        self,
        *,
        display: int,
        root: int,
        window_id: int,
        message_name: str,
        data: tuple[int, int, int, int, int],
    ) -> None:
        assert self._x11 is not None
        message_atom = self._atom(message_name, only_if_exists=True)
        if not message_atom:
            raise X11WindowNativeUnavailableError(f"window manager does not support {message_name}")
        supported_atoms = self._property_values(
            root,
            "_NET_SUPPORTED",
            expected_type_name="ATOM",
        )
        if supported_atoms is None or message_atom not in supported_atoms:
            raise X11WindowNativeUnavailableError(
                f"window manager does not advertise {message_name}"
            )
        event = _XEvent()
        event.client_message.type = _CLIENT_MESSAGE
        event.client_message.serial = 0
        event.client_message.send_event = 1
        event.client_message.display = ctypes.c_void_p(display)
        event.client_message.window = window_id
        event.client_message.message_type = message_atom
        event.client_message.format = 32
        for index, item in enumerate(data):
            event.client_message.data.longs[index] = item
        sent = self._x11.XSendEvent(
            ctypes.c_void_p(display),
            ctypes.c_ulong(root),
            0,
            _SUBSTRUCTURE_NOTIFY_MASK | _SUBSTRUCTURE_REDIRECT_MASK,
            ctypes.byref(event),
        )
        if not sent:
            raise X11WindowNativeOperationError(f"window manager rejected {message_name} request")

    def _sync(self, display: int) -> None:
        assert self._x11 is not None
        self._x11.XSync(ctypes.c_void_p(display), 0)


class _CommandWindowAdapter:
    def __init__(
        self,
        *,
        run: RunCommand,
        fallback_windows: Callable[[], Awaitable[list[X11Window]]],
    ) -> None:
        self._run = run
        self._fallback_windows = fallback_windows

    def available(self) -> bool:
        return shutil.which("wmctrl") is not None and shutil.which("xdotool") is not None

    async def list(self) -> list[X11Window]:
        result = await self._run("wmctrl", "-lpGx", timeout=2, check=False)
        if result.returncode != 0:
            return await self._fallback_windows()
        active = await self._run("xdotool", "getactivewindow", timeout=2, check=False)
        active_id = normalize_window_id(active.stdout) if active.returncode == 0 else ""
        windows: list[X11Window] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            window_id, desktop, pid, x, y, width, height, class_name, title = parts
            try:
                windows.append(
                    X11Window(
                        id=normalize_window_id(window_id),
                        title=title,
                        class_name=class_name,
                        pid=int(pid) if pid != "0" else None,
                        x=int(x),
                        y=int(y),
                        width=int(width),
                        height=int(height),
                        workspace=int(desktop),
                        is_active=normalize_window_id(window_id) == active_id,
                    )
                )
            except ValueError:
                continue
        return windows

    async def activate(self, window_id: str) -> ActionResult:
        result = await self._run("wmctrl", "-ia", window_id, timeout=5, check=False)
        return ActionResult(
            ok=result.returncode == 0,
            message=None if result.returncode == 0 else "failed to activate window",
            output={"window_id": window_id, "window_backend": "wmctrl"},
        )

    async def close(self, window_id: str) -> ActionResult:
        result = await self._run("wmctrl", "-ic", window_id, timeout=5, check=False)
        return ActionResult(
            ok=result.returncode == 0,
            message=None if result.returncode == 0 else "failed to close window",
            output={"window_id": window_id, "window_backend": "wmctrl"},
        )


class X11WindowController:
    def __init__(
        self,
        *,
        run: RunCommand,
        fallback_windows: Callable[[], Awaitable[list[X11Window]]],
        display: str | None = None,
        native: NativeWindowAdapter | None = None,
    ) -> None:
        self._native = native or NativeEwmhWindowAdapter(display=display)
        self._commands = _CommandWindowAdapter(
            run=run,
            fallback_windows=fallback_windows,
        )
        self._backend_name = "xlib-ewmh"

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def probe_backend(self) -> tuple[bool, str | None]:
        if self._native.available():
            self._backend_name = "xlib-ewmh"
            return True, None
        if self._commands.available():
            self._backend_name = "wmctrl"
            return True, None
        return (
            False,
            self._native.failure
            or "native X11 window management and wmctrl/xdotool are unavailable",
        )

    def close(self) -> None:
        self._native.close_display()

    async def list(self) -> list[X11Window]:
        try:
            windows = await asyncio.to_thread(self._native.list)
            self._backend_name = "xlib-ewmh"
            return windows
        except X11WindowNativeError:
            self._backend_name = "wmctrl"
            return await self._commands.list()

    async def active(self) -> X11Window | None:
        windows = await self.list()
        for window in windows:
            if window.is_active:
                return window
        return windows[0] if windows else None

    async def activate(self, window_id: str) -> ActionResult:
        parsed_id = parse_window_id(window_id)
        if parsed_id is None:
            return _invalid_window_id_result(window_id)
        normalized_id = normalize_window_id(window_id)
        try:
            request = await asyncio.to_thread(self._native.activate, parsed_id)
        except X11WindowNativeError:
            self._backend_name = "wmctrl"
            return await self._commands.activate(normalized_id)
        self._backend_name = "xlib-ewmh"
        return ActionResult(
            ok=True,
            message=None
            if request.verified
            else "activation requested; window manager has not confirmed it yet",
            output={
                "window_id": normalized_id,
                "window_backend": "xlib-ewmh",
                "verified": request.verified,
            },
        )

    async def close_window(self, window_id: str) -> ActionResult:
        parsed_id = parse_window_id(window_id)
        if parsed_id is None:
            return _invalid_window_id_result(window_id)
        normalized_id = normalize_window_id(window_id)
        try:
            request = await asyncio.to_thread(self._native.close, parsed_id)
        except X11WindowNativeError:
            self._backend_name = "wmctrl"
            return await self._commands.close(normalized_id)
        self._backend_name = "xlib-ewmh"
        return ActionResult(
            ok=True,
            message=None
            if request.verified
            else "close requested; window manager has not confirmed it yet",
            output={
                "window_id": normalized_id,
                "window_backend": "xlib-ewmh",
                "verified": request.verified,
            },
        )


def parse_window_id(value: str) -> int | None:
    raw = value.strip().lower()
    if not raw:
        return None
    try:
        parsed = int(raw, 16 if raw.startswith("0x") else 10)
    except ValueError:
        return None
    if parsed <= 0 or parsed > _WINDOW_ID_MAX:
        return None
    return parsed


def normalize_window_id(value: str) -> str:
    parsed = parse_window_id(value)
    if parsed is None:
        return value.strip().lower()
    return f"0x{parsed:08x}"


def _invalid_window_id_result(window_id: str) -> ActionResult:
    return ActionResult(
        ok=False,
        message="invalid window id",
        output={"window_id": window_id},
    )
