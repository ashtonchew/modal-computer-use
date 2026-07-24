from __future__ import annotations

import asyncio
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
    X11EmissionResult,
    X11InputInjectionError,
    X11InputReleaseError,
    X11InputSession,
    X11InputStateConflictError,
    X11InputUnavailableError,
    X11KeyboardState,
)
from modal_computer_use.models import ActionResult, Point


class FakeX11:
    def __init__(self, *, pressed: set[int] | None = None) -> None:
        self.pressed = pressed or set()
        self.query_roots: list[int] = []
        self.keymap_queries = 0
        self.xkb_state_queries = 0
        self.group = 0
        self.modifiers = 0
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
        self.keymap_queries += 1
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

    def XkbGetState(
        self,
        _display: object,
        _device: int,
        state: Any,
    ) -> int:
        self.xkb_state_queries += 1
        state._obj.group = self.group
        state._obj.mods = self.modifiers
        return 0

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


def test_keyboard_state_reads_group_and_modifiers_with_one_xkb_query() -> None:
    session, x11, _xtst = session_with_fakes()
    x11.group = 2
    x11.modifiers = 5

    assert session.keyboard_state() == X11KeyboardState(group=2, modifiers=5)
    assert x11.xkb_state_queries == 1


def test_emit_preserves_previously_pressed_keys_and_syncs_once() -> None:
    session, x11, xtst = session_with_fakes(pressed={50})

    emitted = session.emit(
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
    assert x11.flushes == 0
    assert x11.syncs == 1
    assert x11.keymap_queries == 1
    assert emitted.initially_pressed_keycodes == frozenset({50})
    result = session.last_emission_result
    assert result is not None
    assert {
        name: getattr(result, name)
        for name in (
            "requested_event_count",
            "filtered_event_count",
            "emitted_event_count",
            "cleanup_event_count",
            "pressed_query_count",
            "explicit_flush_count",
            "sync_count",
        )
    } == {
        "requested_event_count": 4,
        "filtered_event_count": 2,
        "emitted_event_count": 2,
        "cleanup_event_count": 0,
        "pressed_query_count": 1,
        "explicit_flush_count": 0,
        "sync_count": 1,
    }
    assert set(result.__dataclass_fields__) == {
        "initially_pressed_keycodes",
        "requested_event_count",
        "filtered_event_count",
        "emitted_event_count",
        "cleanup_event_count",
        "pressed_query_count",
        "explicit_flush_count",
        "sync_count",
        "pressed_query_ms",
        "enqueue_ms",
        "sync_ms",
        "cleanup_ms",
        "total_ms",
    }
    assert result.initially_pressed_keycodes == frozenset({50})
    assert all(
        isinstance(getattr(result, name), int | float) and getattr(result, name) >= 0
        for name in result.__dataclass_fields__
        if name != "initially_pressed_keycodes"
    )


def test_emit_reports_partial_injection_and_releases_owned_state() -> None:
    session, x11, xtst = session_with_fakes(fail_at=2)

    with pytest.raises(X11InputInjectionError) as raised:
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
    assert x11.flushes == 0
    assert x11.syncs == 1
    result = session.last_emission_result
    assert result is not None
    assert raised.value.emission_result is result
    assert isinstance(result, X11EmissionResult)
    assert result.initially_pressed_keycodes == frozenset()
    assert result.requested_event_count == 4
    assert result.emitted_event_count == 2
    assert result.cleanup_event_count == 2
    assert result.explicit_flush_count == 0
    assert result.sync_count == 1
    assert result.enqueue_ms >= 0
    assert result.sync_ms >= 0
    assert result.cleanup_ms >= 0
    assert result.total_ms >= result.enqueue_ms


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
            "X=9\nY=8\nSCREEN=0\nWINDOW=1\n" if args[:2] == ("xdotool", "getmouselocation") else ""
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

    async def key_down(key: str) -> bool:
        key_actions.append(f"down:{key}")
        return True

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
        ("xdotool", "mousemove", "1", "2"),
        ("xdotool", "click", "--repeat", "1", "1"),
    ]
    assert key_actions == ["down:super", "up:super"]
    assert session.emissions == []


def test_modified_click_cleans_up_only_modifiers_acquired_before_failure() -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, _key_actions = make_mouse(session)
    actions: list[str] = []

    async def acquire(key: str) -> bool:
        actions.append(f"acquire:{key}")
        if key == "ctrl":
            raise RuntimeError("second modifier failed")
        return True

    async def release(key: str) -> None:
        actions.append(f"release:{key}")

    mouse._key_down = acquire
    mouse._key_up = release

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(
            _call,
            lambda: mouse.click(1, 2, modifiers=("shift", "ctrl")),
        )

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "second modifier failed"
    assert actions == ["acquire:shift", "acquire:ctrl", "release:shift"]
    assert commands == [("xdotool", "mousemove", "1", "2")]


def test_forced_native_click_fails_closed_when_unavailable() -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, key_actions = make_mouse(session, input_backend="xtest")

    with pytest.raises(X11InputUnavailableError):
        anyio.run(_call, lambda: mouse.click(1, 2, modifiers=("shift",)))

    assert commands == []
    assert key_actions == []


def test_drag_best_effort_releases_button_after_partial_native_down() -> None:
    original_error = X11InputInjectionError("original partial down")

    class PartialDownSession(FakeInputSession):
        def emit(
            self,
            events: Sequence[object],
            *,
            preserve_pressed_keycodes: Sequence[int] = (),
        ) -> None:
            self.emissions.append((tuple(events), frozenset(preserve_pressed_keycodes)))
            if len(self.emissions) == 1:
                raise original_error
            raise X11InputInjectionError("cleanup release failed")

    session = PartialDownSession()
    mouse, commands, _key_actions = make_mouse(session)

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(_call, lambda: mouse.drag(end=Point(x=8, y=9), duration_ms=0))

    assert raised.value is original_error
    assert commands == []
    assert session.emissions == [
        ((ButtonEvent(1, True),), frozenset()),
        ((ButtonEvent(1, False),), frozenset()),
    ]


def test_drag_does_not_release_button_after_pre_emission_unavailable() -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, _key_actions = make_mouse(session, input_backend="xtest")
    release_attempts: list[str] = []

    async def release(button: str = "left", **_kwargs: object) -> ActionResult:
        release_attempts.append(button)
        return ActionResult(ok=True)

    mouse.up = release  # type: ignore[method-assign]

    with pytest.raises(X11InputUnavailableError):
        anyio.run(_call, lambda: mouse.drag(end=Point(x=8, y=9), duration_ms=0))

    assert release_attempts == []
    assert commands == []
    assert session.emissions == []


def test_drag_reclassifies_unavailable_after_start_motion_and_preserves_cause() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session)
    original_error = X11InputUnavailableError("modifier became unavailable")

    async def unavailable_modifier(_key: str) -> bool:
        raise original_error

    mouse._key_down = unavailable_modifier

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(
            _call,
            lambda: mouse.drag(
                start=Point(x=1, y=2),
                end=Point(x=8, y=9),
                duration_ms=0,
                modifiers=("shift",),
            ),
        )

    assert raised.value.__cause__ is original_error
    assert session.emissions == [((MotionEvent(1, 2),), frozenset())]
    assert commands == []


def test_drag_reclassifies_button_unavailable_after_modifier_and_cleans_modifier() -> None:
    session = FakeInputSession()
    mouse, commands, key_actions = make_mouse(session)
    original_error = X11InputUnavailableError("button unavailable")
    button_releases: list[str] = []

    async def unavailable_down(
        _button: str = "left",
        **_kwargs: object,
    ) -> ActionResult:
        raise original_error

    async def record_up(button: str = "left", **_kwargs: object) -> ActionResult:
        button_releases.append(button)
        return ActionResult(ok=True)

    mouse.down = unavailable_down  # type: ignore[method-assign]
    mouse.up = record_up  # type: ignore[method-assign]

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(
            _call,
            lambda: mouse.drag(
                end=Point(x=8, y=9),
                duration_ms=0,
                modifiers=("shift",),
            ),
        )

    assert raised.value.__cause__ is original_error
    assert key_actions == ["down:shift", "up:shift"]
    assert button_releases == []
    assert session.emissions == []
    assert commands == []


def test_xdotool_mouse_down_cancellation_directly_releases_untracked_button() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")
    mousedown_started = asyncio.Event()
    state_calls: list[str] = []

    async def run(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args == ("xdotool", "mousedown", "1"):
            mousedown_started.set()
            await asyncio.Future()
        return subprocess.CompletedProcess(args, 0, "", "")

    async def down_state(*_args: object, **_kwargs: object) -> ActionResult:
        state_calls.append("down")
        return ActionResult(ok=True)

    async def up_state(*_args: object, **_kwargs: object) -> ActionResult:
        state_calls.append("up")
        return ActionResult(ok=True)

    mouse._run = run
    mouse._button_down_state = down_state
    mouse._button_up_state = up_state

    async def scenario() -> None:
        task = asyncio.create_task(mouse.down())
        await mousedown_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert commands == [
        ("xdotool", "mousedown", "1"),
        ("xdotool", "mouseup", "1"),
    ]
    assert state_calls == ["up"]


def test_native_mouse_down_cancellation_reconciles_emitted_and_tracked_state() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xtest")
    state_started = asyncio.Event()
    held_buttons: set[str] = set()

    async def cancelling_state(
        button: str,
        **_kwargs: object,
    ) -> ActionResult:
        held_buttons.add(button)
        state_started.set()
        await asyncio.Future()
        return ActionResult(ok=True)

    async def up_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.discard(button)
        return ActionResult(ok=True)

    mouse._button_down_state = cancelling_state
    mouse._button_up_state = up_state

    async def scenario() -> None:
        task = asyncio.create_task(mouse.down())
        await state_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert session.emissions == [
        ((ButtonEvent(1, True),), frozenset()),
        ((ButtonEvent(1, False),), frozenset()),
    ]
    assert held_buttons == set()
    assert commands == []


def test_native_partial_down_preserves_ownership_when_compensation_fails() -> None:
    original_error = X11InputInjectionError("partial button down")

    class PartialDownSession(FakeInputSession):
        release_succeeds = False

        def emit(
            self,
            events: Sequence[object],
            *,
            preserve_pressed_keycodes: Sequence[int] = (),
        ) -> None:
            self.emissions.append((tuple(events), frozenset(preserve_pressed_keycodes)))
            button_event = next(event for event in events if isinstance(event, ButtonEvent))
            if button_event.pressed:
                raise original_error
            if not self.release_succeeds:
                raise X11InputInjectionError("compensating button up failed")

    session = PartialDownSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xtest")
    held_buttons: set[str] = set()

    async def down_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.add(button)
        return ActionResult(ok=True)

    async def up_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.discard(button)
        return ActionResult(ok=True)

    mouse._button_down_state = down_state
    mouse._button_up_state = up_state

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(mouse.down)

    assert raised.value is original_error
    assert held_buttons == {"left"}
    assert session.emissions == [
        ((ButtonEvent(1, True),), frozenset()),
        ((ButtonEvent(1, False),), frozenset()),
    ]

    session.release_succeeds = True
    anyio.run(mouse.up)

    assert held_buttons == set()
    assert session.emissions[-1] == ((ButtonEvent(1, False),), frozenset())
    assert commands == []


def test_xdotool_down_cancellation_preserves_ownership_when_compensation_fails() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")
    mousedown_started = asyncio.Event()
    held_buttons: set[str] = set()
    release_succeeds = False

    async def run(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args == ("xdotool", "mousedown", "1"):
            mousedown_started.set()
            await asyncio.Future()
        if args == ("xdotool", "mouseup", "1") and not release_succeeds:
            raise RuntimeError("compensating button up failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    async def down_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.add(button)
        return ActionResult(ok=True)

    async def up_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.discard(button)
        return ActionResult(ok=True)

    mouse._run = run
    mouse._button_down_state = down_state
    mouse._button_up_state = up_state

    async def scenario() -> None:
        task = asyncio.create_task(mouse.down())
        await mousedown_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert held_buttons == {"left"}
    assert commands == [
        ("xdotool", "mousedown", "1"),
        ("xdotool", "mouseup", "1"),
    ]

    release_succeeds = True
    anyio.run(mouse.up)

    assert held_buttons == set()
    assert commands[-1] == ("xdotool", "mouseup", "1")


def test_xdotool_down_failure_is_partial_and_preserves_failed_compensation() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")
    held_buttons: set[str] = set()
    original_error = RuntimeError("button down command failed after dispatch")

    async def run(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args == ("xdotool", "mousedown", "1"):
            raise original_error
        raise RuntimeError("compensating button up failed")

    async def down_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.add(button)
        return ActionResult(ok=True)

    async def up_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.discard(button)
        return ActionResult(ok=True)

    mouse._run = run
    mouse._button_down_state = down_state
    mouse._button_up_state = up_state

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(mouse.down)

    assert raised.value.input_backend == "xdotool"
    assert raised.value.__cause__ is original_error
    assert held_buttons == {"left"}
    assert commands == [
        ("xdotool", "mousedown", "1"),
        ("xdotool", "mouseup", "1"),
    ]


def test_xdotool_down_does_not_track_process_spawn_failure() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")

    async def missing_process(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        raise FileNotFoundError("xdotool")

    mouse._run = missing_process

    with pytest.raises(X11InputUnavailableError) as raised:
        anyio.run(mouse.down)

    assert raised.value.input_backend == "xdotool"
    assert commands == [("xdotool", "mousedown", "1")]


def test_repeated_mouse_down_preserves_the_existing_hold() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xtest")

    anyio.run(mouse.down, "left")

    with pytest.raises(X11InputStateConflictError, match="already held"):
        anyio.run(mouse.down, "left")

    assert session.emissions == [((ButtonEvent(1, True),), frozenset())]
    assert commands == []


@pytest.mark.parametrize("operation", ["click", "drag"])
def test_held_mouse_button_conflicts_before_click_or_drag_emission(operation: str) -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xtest")
    anyio.run(mouse.down, "left")

    with pytest.raises(X11InputStateConflictError, match="already held"):
        if operation == "click":
            anyio.run(mouse.click, 10, 20)
        else:
            anyio.run(_call, lambda: mouse.drag(start=Point(x=1, y=2), end=Point(x=3, y=4)))

    assert session.emissions == [((ButtonEvent(1, True),), frozenset())]
    assert commands == []


@pytest.mark.parametrize(
    ("operation", "tracked_button", "emission_command", "release_command"),
    [
        (
            "click",
            "left",
            ("xdotool", "click", "--repeat", "1", "1"),
            ("xdotool", "mouseup", "1"),
        ),
        (
            "scroll",
            "scroll_up",
            ("xdotool", "click", "--repeat", "1", "4"),
            ("xdotool", "mouseup", "4"),
        ),
    ],
)
def test_partial_xdotool_click_retains_button_when_compensation_fails(
    operation: str,
    tracked_button: str,
    emission_command: tuple[str, ...],
    release_command: tuple[str, ...],
) -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")
    held_buttons: set[str] = set()

    async def fail(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        raise RuntimeError("post-dispatch failure")

    async def down_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.add(button)
        return ActionResult(ok=True)

    mouse._run = fail
    mouse._button_down_state = down_state

    with pytest.raises(X11InputInjectionError):
        if operation == "click":
            anyio.run(mouse.click)
        else:
            anyio.run(mouse.scroll, "up")

    assert held_buttons == {tracked_button}
    assert mouse._held_buttons == {tracked_button}
    assert commands == [emission_command, release_command]


def test_mouse_release_preserves_forced_native_unavailable_error() -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, _key_actions = make_mouse(session, input_backend="xtest")

    with pytest.raises(X11InputUnavailableError):
        anyio.run(mouse.up, "left")

    assert commands == []


def test_xdotool_mouse_release_preserves_process_spawn_failure() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")

    async def missing(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        raise FileNotFoundError("xdotool")

    mouse._run = missing

    with pytest.raises(X11InputUnavailableError) as raised:
        anyio.run(mouse.up, "left")

    assert raised.value.input_backend == "xdotool"
    assert commands == [("xdotool", "mouseup", "1")]


def test_xdotool_down_state_unavailable_after_dispatch_is_partial() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")
    state_error = X11InputUnavailableError(
        "private state callback detail",
        input_backend="xdotool",
    )

    async def fail_state(*_args: object, **_kwargs: object) -> ActionResult:
        raise state_error

    mouse._button_down_state = fail_state

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(mouse.down)

    assert raised.value.input_backend == "xdotool"
    assert raised.value.__cause__ is state_error
    assert "private state callback detail" not in str(raised.value)
    assert commands == [
        ("xdotool", "mousedown", "1"),
        ("xdotool", "mouseup", "1"),
    ]


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (FileNotFoundError("xdotool"), X11InputUnavailableError),
        (RuntimeError("xdotool move failed after dispatch"), X11InputInjectionError),
    ],
)
def test_modified_click_pre_move_uses_public_xdotool_error_contract(
    failure: Exception,
    error_type: type[Exception],
) -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, key_actions = make_mouse(session)

    async def fail_move(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        raise failure

    mouse._run = fail_move

    with pytest.raises(error_type) as raised:
        anyio.run(_call, lambda: mouse.click(1, 2, modifiers=("shift",)))

    assert raised.value.input_backend == "xdotool"  # type: ignore[attr-defined]
    assert raised.value.__cause__ is failure
    assert commands == [("xdotool", "mousemove", "1", "2")]
    assert key_actions == []


def test_modified_click_becomes_partial_when_modifier_fails_after_pre_move() -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, key_actions = make_mouse(session)
    modifier_error = X11InputUnavailableError(
        "modifier backend unavailable",
        input_backend="xdotool",
    )

    async def fail_modifier(_key: str) -> bool:
        raise modifier_error

    mouse._key_down = fail_modifier

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(_call, lambda: mouse.click(1, 2, modifiers=("shift",)))

    assert raised.value.input_backend == "xdotool"
    assert raised.value.__cause__ is modifier_error
    assert commands == [("xdotool", "mousemove", "1", "2")]
    assert key_actions == []


def test_xdotool_release_failures_are_retry_safe_and_preserve_ownership() -> None:
    failure = RuntimeError("xdotool release failed after dispatch")
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")
    held_buttons = {"left"}

    async def fail_release(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        raise failure

    async def up_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.discard(button)
        return ActionResult(ok=True)

    mouse._run = fail_release
    mouse._button_up_state = up_state

    with pytest.raises(X11InputReleaseError) as raised:
        anyio.run(mouse.up)

    assert raised.value.input_backend == "xdotool"
    assert raised.value.__cause__ is failure
    assert held_buttons == {"left"}
    assert commands == [("xdotool", "mouseup", "1")]


def test_xdotool_release_cancellation_is_preserved_without_clearing_ownership() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")
    release_started = asyncio.Event()
    held_buttons = {"left"}

    async def cancel_release(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        release_started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def up_state(button: str, **_kwargs: object) -> ActionResult:
        held_buttons.discard(button)
        return ActionResult(ok=True)

    mouse._run = cancel_release
    mouse._button_up_state = up_state

    async def scenario() -> None:
        task = asyncio.create_task(mouse.up())
        await release_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert held_buttons == {"left"}
    assert commands == [("xdotool", "mouseup", "1")]


def test_release_state_failure_uses_retry_safe_public_contract() -> None:
    session = FakeInputSession()
    mouse, commands, _key_actions = make_mouse(session, input_backend="xdotool")
    state_error = RuntimeError("private state callback detail")

    async def fail_state(*_args: object, **_kwargs: object) -> ActionResult:
        raise state_error

    mouse._button_up_state = fail_state

    with pytest.raises(X11InputReleaseError) as raised:
        anyio.run(mouse.up)

    assert raised.value.input_backend == "xdotool"
    assert raised.value.__cause__ is state_error
    assert "private state callback detail" not in str(raised.value)
    assert commands == [("xdotool", "mouseup", "1")]


def test_modified_click_cancellation_shields_owned_modifier_cleanup() -> None:
    session = FakeInputSession(unavailable=True)
    mouse, commands, key_actions = make_mouse(session)
    click_started = anyio.Event()

    async def run(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[:2] == ("xdotool", "click"):
            click_started.set()
            await anyio.sleep_forever()
        return subprocess.CompletedProcess(args, 0, "", "")

    async def release(key: str) -> None:
        await anyio.sleep(0)
        key_actions.append(f"up:{key}")

    mouse._run = run
    mouse._key_up = release

    async def run_click() -> None:
        await mouse.click(1, 2, modifiers=("shift",))

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_click)
            await click_started.wait()
            tasks.cancel_scope.cancel()

    anyio.run(scenario)

    assert commands == [
        ("xdotool", "mousemove", "1", "2"),
        ("xdotool", "click", "--repeat", "1", "1"),
    ]
    assert key_actions == ["down:shift", "up:shift"]


def test_drag_cancellation_shields_owned_button_and_modifier_cleanup() -> None:
    delay_started = anyio.Event()

    class MotionSignalingSession(FakeInputSession):
        def emit(
            self,
            events: Sequence[object],
            *,
            preserve_pressed_keycodes: Sequence[int] = (),
        ) -> None:
            super().emit(
                events,
                preserve_pressed_keycodes=preserve_pressed_keycodes,
            )
            if any(isinstance(event, MotionEvent) for event in events):
                delay_started.set()

    session = MotionSignalingSession()
    mouse, commands, key_actions = make_mouse(session)

    async def release(key: str) -> None:
        await anyio.sleep(0)
        key_actions.append(f"up:{key}")

    mouse._key_up = release

    async def run_drag() -> None:
        await mouse.drag(
            end=Point(x=8, y=9),
            duration_ms=60_000,
            modifiers=("shift",),
        )

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_drag)
            await delay_started.wait()
            tasks.cancel_scope.cancel()

    anyio.run(scenario)

    assert session.emissions == [
        ((ButtonEvent(1, True),), frozenset()),
        ((MotionEvent(8, 9),), frozenset()),
        ((ButtonEvent(1, False),), frozenset()),
    ]
    assert key_actions == ["down:shift", "up:shift"]
    assert commands == []


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
