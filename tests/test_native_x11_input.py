from __future__ import annotations

import ctypes
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import anyio
import pytest

from modal_computer_use.daemon.desktop.mouse import X11MouseController
from modal_computer_use.daemon.desktop.xtest import (
    ButtonEvent,
    KeyEvent,
    MotionEvent,
    X11InputInjectionError,
    X11InputSession,
    X11InputUnavailableError,
)
from modal_computer_use.models import ActionResult, Point


class FakeX11:
    def __init__(self, *, pressed: set[int] | None = None) -> None:
        self.pressed = pressed or set()
        self.query_roots: list[int] = []
        self.flushes = 0
        self.syncs = 0

    def XScreenCount(self, _display: object) -> int:
        return 2

    def XRootWindow(self, _display: object, screen: int) -> int:
        return 100 + screen

    def XQueryPointer(
        self,
        _display: object,
        root: int,
        _root_return: object,
        _child_return: object,
        root_x: object,
        root_y: object,
        _window_x: object,
        _window_y: object,
        _mask: object,
    ) -> int:
        self.query_roots.append(root)
        if root == 100:
            return 0
        ctypes.cast(root_x, ctypes.POINTER(ctypes.c_int))[0] = 321
        ctypes.cast(root_y, ctypes.POINTER(ctypes.c_int))[0] = 654
        return 1

    def XQueryKeymap(self, _display: object, keymap: Any) -> int:
        for keycode in self.pressed:
            byte_index = keycode >> 3
            value = keymap[byte_index][0] | (1 << (keycode & 7))
            keymap[byte_index] = bytes((value,))
        return 1

    def XDisplayKeycodes(
        self,
        _display: object,
        minimum: object,
        maximum: object,
    ) -> int:
        ctypes.cast(minimum, ctypes.POINTER(ctypes.c_int))[0] = 8
        ctypes.cast(maximum, ctypes.POINTER(ctypes.c_int))[0] = 9
        return 1

    def XkbKeycodeToKeysym(
        self,
        _display: object,
        keycode: object,
        group: int,
        level: int,
    ) -> int:
        return group * 1000 + int(getattr(keycode, "value", keycode)) * 10 + level

    def XFlush(self, _display: object) -> int:
        self.flushes += 1
        return 0

    def XSync(self, _display: object, _discard: int) -> int:
        self.syncs += 1
        return 0


class FakeXtst:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[tuple[object, ...]] = []

    def XTestFakeMotionEvent(
        self,
        _display: object,
        screen: int,
        x: int,
        y: int,
        _delay: int,
    ) -> int:
        return self._record("motion", screen, x, y)

    def XTestFakeButtonEvent(
        self,
        _display: object,
        button: int,
        pressed: int,
        _delay: int,
    ) -> int:
        return self._record("button", button, bool(pressed))

    def XTestFakeKeyEvent(
        self,
        _display: object,
        keycode: int,
        pressed: int,
        _delay: int,
    ) -> int:
        return self._record("key", keycode, bool(pressed))

    def _record(self, *call: object) -> int:
        self.calls.append(call)
        return int(self.fail_at is None or len(self.calls) != self.fail_at)


def session_with_fakes(
    *,
    pressed: set[int] | None = None,
    fail_at: int | None = None,
) -> tuple[X11InputSession, FakeX11, FakeXtst]:
    session = X11InputSession(display=":99")
    x11 = FakeX11(pressed=pressed)
    xtst = FakeXtst(fail_at=fail_at)
    session._display = 1
    session._available = True
    session._x11 = x11
    session._xtst = xtst
    return session, x11, xtst


def test_pointer_position_checks_each_x11_screen() -> None:
    session, x11, _xtst = session_with_fakes()

    assert session.pointer_position() == (321, 654)
    assert x11.query_roots == [100, 101]


def test_unmapped_keysyms_return_zero_without_marking_backend_unavailable() -> None:
    session, x11, _xtst = session_with_fakes()
    x11.XStringToKeysym = lambda _name: 0  # type: ignore[attr-defined]
    x11.XKeysymToKeycode = lambda _display, _keysym: 0  # type: ignore[attr-defined]

    assert session.resolve_keysym("U1F642") == 0
    assert session.keysym_to_keycode(0x1F642) == 0
    assert session.failure is None


def test_keyboard_mapping_snapshots_all_keycodes_in_one_active_group() -> None:
    session, _x11, _xtst = session_with_fakes()

    assert session.keyboard_mapping(2, levels=2) == (
        (8, (2080, 2081)),
        (9, (2090, 2091)),
    )


def test_emit_preserves_previously_pressed_keys_and_syncs_once() -> None:
    session, x11, xtst = session_with_fakes(pressed={50})

    session.emit(
        (
            KeyEvent(50, True),
            ButtonEvent(1, True),
            ButtonEvent(1, False),
            KeyEvent(50, False),
        ),
        preserve_pressed_keycodes=(50,),
    )

    assert xtst.calls == [
        ("button", 1, True),
        ("button", 1, False),
    ]
    assert x11.flushes == 1
    assert x11.syncs == 1


def test_emit_reports_partial_injection_and_releases_owned_state() -> None:
    session, x11, xtst = session_with_fakes(fail_at=2)

    with pytest.raises(X11InputInjectionError):
        session.emit(
            (
                KeyEvent(50, True),
                ButtonEvent(1, True),
                ButtonEvent(1, False),
                KeyEvent(50, False),
            )
        )

    assert xtst.calls == [
        ("key", 50, True),
        ("button", 1, True),
        ("button", 1, False),
        ("key", 50, False),
    ]
    assert x11.flushes == 1
    assert x11.syncs == 1


def test_emit_validates_the_whole_sequence_before_emission() -> None:
    session, _x11, xtst = session_with_fakes()

    with pytest.raises(ValueError, match="keycode"):
        session.emit((MotionEvent(1, 2), KeyEvent(0, True)))

    assert xtst.calls == []


def test_input_session_retries_when_display_becomes_ready() -> None:
    session = X11InputSession(display=":99")

    class StartingX11:
        attempts = 0

        def XOpenDisplay(self, _display_name: object) -> int:
            self.attempts += 1
            return 0 if self.attempts == 1 else 1

    class AvailableXtst:
        def XTestQueryExtension(self, *_args: object) -> int:
            return 1

    session._x11 = StartingX11()
    session._xtst = AvailableXtst()

    assert session.available() is False
    assert session.failure == "XOpenDisplay failed"
    assert session.available() is True
    assert session.failure is None


class FakeInputSession:
    def __init__(
        self,
        *,
        available: bool = True,
        pointer: tuple[int, int] = (12, 34),
        unavailable: bool = False,
        partial: bool = False,
        unmapped_modifier: bool = False,
    ) -> None:
        self._available = available
        self._pointer = pointer
        self._unavailable = unavailable
        self._partial = partial
        self._unmapped_modifier = unmapped_modifier
        self.failure = "unavailable" if not available else None
        self.emissions: list[tuple[tuple[object, ...], frozenset[int]]] = []
        self.resolutions: list[str] = []

    def available(self) -> bool:
        return self._available

    def pointer_position(self) -> tuple[int, int]:
        if self._unavailable:
            raise X11InputUnavailableError("unavailable")
        return self._pointer

    def resolve_keycode(self, name: str) -> int:
        if self._unavailable:
            raise X11InputUnavailableError("unavailable")
        self.resolutions.append(name)
        if self._unmapped_modifier:
            return 0
        return {"Shift_L": 50, "Control_L": 37}[name]

    def emit(
        self,
        events: Sequence[object],
        *,
        preserve_pressed_keycodes: Sequence[int] = (),
    ) -> None:
        if self._unavailable:
            raise X11InputUnavailableError("unavailable")
        self.emissions.append((tuple(events), frozenset(preserve_pressed_keycodes)))
        if self._partial:
            raise X11InputInjectionError("partial")


def make_mouse(
    session: FakeInputSession,
    *,
    input_backend: str = "auto",
) -> tuple[X11MouseController, list[tuple[str, ...]], list[str]]:
    commands: list[tuple[str, ...]] = []
    key_actions: list[str] = []
    current = Point(x=0, y=0)

    async def run(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        stdout = (
            "X=9\nY=8\nSCREEN=0\nWINDOW=1\n"
            if args[:2] == ("xdotool", "getmouselocation")
            else ""
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    async def move_state(x: int, y: int) -> Point:
        nonlocal current
        current = Point(x=x, y=y)
        return current

    async def position_state() -> Point:
        return current

    async def point_state(
        x: int | None = None,
        y: int | None = None,
        **_kwargs: object,
    ) -> Point:
        if x is not None and y is not None:
            return await move_state(x, y)
        return current

    async def action_state(*_args: object, **_kwargs: object) -> ActionResult:
        return ActionResult(ok=True)

    async def key_down(key: str) -> None:
        key_actions.append(f"down:{key}")

    async def key_up(key: str) -> None:
        key_actions.append(f"up:{key}")

    mouse = X11MouseController(
        run=run,
        move_state=move_state,
        position_state=position_state,
        click_state=point_state,
        drag_state=point_state,
        scroll_state=action_state,
        button_down_state=action_state,
        button_up_state=action_state,
        key_down=key_down,
        key_up=key_up,
        input_backend=input_backend,  # type: ignore[arg-type]
        xtest=session,  # type: ignore[arg-type]
    )
    return mouse, commands, key_actions


def test_modified_click_is_one_native_sequence_with_modifier_preservation() -> None:
    session = FakeInputSession()
    mouse, commands, key_actions = make_mouse(session)

    result = anyio.run(
        _call,
        lambda: mouse.click(10, 20, button="right", count=2, modifiers=("shift", "ctrl")),
    )

    assert result == Point(x=10, y=20)
    assert commands == []
    assert key_actions == []
    assert session.resolutions == ["Shift_L", "Control_L"]
    assert session.emissions == [
        (
            (
                MotionEvent(10, 20),
                KeyEvent(50, True),
                KeyEvent(37, True),
                ButtonEvent(3, True),
                ButtonEvent(3, False),
                ButtonEvent(3, True),
                ButtonEvent(3, False),
                KeyEvent(37, False),
                KeyEvent(50, False),
            ),
            frozenset({37, 50}),
        )
    ]


def test_partial_native_click_is_never_replayed_through_xdotool() -> None:
    session = FakeInputSession(partial=True)
    mouse, commands, _key_actions = make_mouse(session)

    with pytest.raises(X11InputInjectionError):
        anyio.run(_call, lambda: mouse.click(1, 2))

    assert commands == []


def test_auto_falls_back_when_native_click_is_unavailable_before_emission() -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, key_actions = make_mouse(session)

    anyio.run(_call, lambda: mouse.click(1, 2, modifiers=("shift",)))

    assert commands == [
        ("xdotool", "mousemove", "1", "2"),
        ("xdotool", "click", "--repeat", "1", "1"),
    ]
    assert key_actions == ["down:shift", "up:shift"]


def test_auto_falls_back_when_click_modifier_is_unmapped() -> None:
    session = FakeInputSession(unmapped_modifier=True)
    mouse, commands, key_actions = make_mouse(session)

    anyio.run(_call, lambda: mouse.click(1, 2, modifiers=("super",)))

    assert commands == [
        ("xdotool", "click", "--repeat", "1", "1"),
    ]
    assert key_actions == ["down:super", "up:super"]
    assert session.emissions == [((MotionEvent(1, 2),), frozenset())]


def test_forced_native_click_fails_closed_when_unavailable() -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, key_actions = make_mouse(session, input_backend="xtest")

    with pytest.raises(X11InputUnavailableError):
        anyio.run(_call, lambda: mouse.click(1, 2, modifiers=("shift",)))

    assert commands == []
    assert key_actions == []


def test_position_uses_native_query_without_a_subprocess() -> None:
    session = FakeInputSession(pointer=(71, 82))
    mouse, commands, _key_actions = make_mouse(session)

    point = anyio.run(mouse.position)

    assert point == Point(x=71, y=82)
    assert commands == []
    assert mouse.backend_name == "xtest"


def test_explicit_xdotool_position_does_not_probe_native_input() -> None:
    session = FakeInputSession(pointer=(71, 82), unavailable=True)
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")

    point = anyio.run(mouse.position)

    assert point == Point(x=9, y=8)
    assert commands == [("xdotool", "getmouselocation", "--shell")]
    assert mouse.backend_name == "xdotool"


async def _call(result: Callable[[], Awaitable[Any]]) -> Any:
    return await result()
