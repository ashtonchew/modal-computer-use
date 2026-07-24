from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence

import anyio
import pytest

from modal_computer_use.daemon.desktop import keyboard as keyboard_module
from modal_computer_use.daemon.desktop.keyboard import X11KeyboardController
from modal_computer_use.daemon.desktop.x11 import X11DesktopBackend
from modal_computer_use.daemon.desktop.xtest import (
    KeyEvent,
    X11InputInjectionError,
    X11InputUnavailableError,
)
from modal_computer_use.models import ActionResult


class FakeX11InputSession:
    def __init__(
        self,
        *,
        available: bool = True,
        pressed: Iterable[int] = (),
        fail_injection_once: bool = False,
    ) -> None:
        self._available = available
        self._pressed = set(pressed)
        self._fail_injection_once = fail_injection_once
        self.failure = None if available else "XTest unavailable"
        self.emissions: list[list[KeyEvent]] = []
        self.modifiers = 0
        self.group = 0
        self._keysyms = {
            "a": ord("a"),
            "A": ord("A"),
            "e": ord("e"),
            "E": ord("E"),
            "t": ord("t"),
            "T": ord("T"),
            "v": ord("v"),
            "V": ord("V"),
            "1": ord("1"),
            "exclam": ord("!"),
            "minus": ord("-"),
            "U00E9": ord("é"),
            "U00C9": ord("É"),
            "Control_L": 0xFFE3,
            "Shift_L": 0xFFE1,
            "Alt_L": 0xFFE9,
            "Super_L": 0xFFEB,
            "ISO_Level3_Shift": 0xFE03,
            "BackSpace": 0xFF08,
            "Delete": 0xFFFF,
            "Down": 0xFF54,
            "Escape": 0xFF1B,
            "Left": 0xFF51,
            "Next": 0xFF56,
            "Prior": 0xFF55,
            "Return": 0xFF0D,
            "Right": 0xFF53,
            "Tab": 0xFF09,
            "Up": 0xFF52,
        }
        self._keycodes = {
            ord("a"): 38,
            ord("A"): 38,
            ord("e"): 26,
            ord("E"): 26,
            ord("é"): 26,
            ord("É"): 26,
            ord("t"): 28,
            ord("T"): 28,
            ord("v"): 55,
            ord("V"): 55,
            ord("1"): 10,
            ord("!"): 10,
            ord("-"): 20,
            0xFFE3: 37,
            0xFFE1: 50,
            0xFFE9: 64,
            0xFFEB: 133,
            0xFE03: 108,
            0xFF08: 22,
            0xFFFF: 119,
            0xFF54: 116,
            0xFF1B: 9,
            0xFF51: 113,
            0xFF56: 117,
            0xFF55: 112,
            0xFF0D: 36,
            0xFF53: 114,
            0xFF09: 23,
            0xFF52: 111,
        }
        self._levels = {
            38: (ord("a"), ord("A"), 0, 0),
            26: (ord("e"), ord("E"), ord("é"), ord("É")),
            28: (ord("t"), ord("T"), 0, 0),
            55: (ord("v"), ord("V"), 0, 0),
            10: (ord("1"), ord("!"), 0, 0),
            20: (ord("-"), ord("_"), 0, 0),
        }
        for keysym, keycode in self._keycodes.items():
            self._levels.setdefault(keycode, (keysym, 0, 0, 0))

    def available(self) -> bool:
        return self._available

    def resolve_keysym(self, name: str) -> int:
        return self._keysyms.get(name, 0)

    def keysym_to_keycode(self, keysym: int) -> int:
        return self._keycodes.get(keysym, 0)

    def resolve_keycode(self, name: str) -> int:
        return self.keysym_to_keycode(self.resolve_keysym(name))

    def keycode_to_keysym(self, keycode: int, group: int, level: int) -> int:
        assert group == self.group
        levels = self._levels.get(keycode, ())
        return levels[level] if level < len(levels) else 0

    def keyboard_mapping(
        self,
        group: int,
        *,
        levels: int = 4,
    ) -> tuple[tuple[int, tuple[int, ...]], ...]:
        assert group == self.group
        return tuple(
            (keycode, tuple(keysyms[:levels]))
            for keycode, keysyms in sorted(self._levels.items())
        )

    def keyboard_group(self) -> int:
        return self.group

    def modifier_state(self) -> int:
        return self.modifiers

    def pressed_keycodes(self, keycodes: Iterable[int] | None = None) -> frozenset[int]:
        if keycodes is None:
            return frozenset(self._pressed)
        return frozenset(self._pressed.intersection(keycodes))

    def emit(
        self,
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
    ) -> None:
        already_pressed = self._pressed.intersection(preserve_pressed_keycodes)
        filtered = [event for event in events if event.keycode not in already_pressed]
        if self._fail_injection_once:
            self._fail_injection_once = False
            if filtered:
                event = filtered[0]
                self.emissions.append([event])
                self._update_pressed(event)
            raise X11InputInjectionError("partial injection")
        self.emissions.append(filtered)
        for event in filtered:
            self._update_pressed(event)

    def _update_pressed(self, event: KeyEvent) -> None:
        if event.pressed:
            self._pressed.add(event.keycode)
        else:
            self._pressed.discard(event.keycode)


class KeyboardHarness:
    def __init__(
        self,
        *,
        input_backend: str = "xtest",
        session: FakeX11InputSession | None = None,
    ) -> None:
        self.session = session or FakeX11InputSession()
        self.commands: list[tuple[str, ...]] = []
        self.held: set[str] = set()
        self.clipboard = "previous clipboard"
        self.typed: list[tuple[str, int, str]] = []
        self.controller = X11KeyboardController(
            run=self.run,
            type_state=self.type_state,
            press_state=self.press_state,
            hotkey_state=self.hotkey_state,
            key_down_state=self.key_down_state,
            key_up_state=self.key_up_state,
            clipboard_get=self.clipboard_get,
            clipboard_set=self.clipboard_set,
            input_backend=input_backend,
            xtest=self.session,
        )

    async def run(self, *args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    async def type_state(self, text: str, *, delay_ms: int, method: str) -> ActionResult:
        self.typed.append((text, delay_ms, method))
        return ActionResult(
            ok=True,
            output={"length": len(text), "delay_ms": delay_ms, "method": method},
        )

    async def press_state(self, key: str, **_kwargs) -> ActionResult:
        return ActionResult(ok=True, output={"key": key})

    async def hotkey_state(self, keys: Sequence[str], **_kwargs) -> ActionResult:
        return ActionResult(ok=True, output={"keys": list(keys)})

    async def key_down_state(self, key: str) -> None:
        self.held.add(key)

    async def key_up_state(self, key: str) -> None:
        self.held.discard(key)

    async def clipboard_get(self) -> str:
        return self.clipboard

    async def clipboard_set(self, text: str) -> ActionResult:
        self.clipboard = text
        return ActionResult(ok=True)


def _event_pairs(events: Sequence[KeyEvent]) -> list[tuple[int, bool]]:
    return [(event.keycode, event.pressed) for event in events]


def test_native_press_resolves_aliases_and_releases_modifiers_in_reverse() -> None:
    harness = KeyboardHarness()

    result = anyio.run(harness.controller.press, "a", ["control", "alt"], 0)

    assert result.ok is True
    assert harness.commands == []
    assert _event_pairs(harness.session.emissions[0]) == [
        (37, True),
        (64, True),
        (38, True),
        (38, False),
        (64, False),
        (37, False),
    ]


@pytest.mark.parametrize(
    ("key", "expected_keycode"),
    [
        ("backspace", 22),
        ("delete", 119),
        ("down", 116),
        ("escape", 9),
        ("left", 113),
        ("pagedown", 117),
        ("pageup", 112),
        ("enter", 36),
        ("right", 114),
        ("tab", 23),
        ("up", 111),
    ],
)
def test_native_press_maps_every_semantic_action_key(
    key: str,
    expected_keycode: int,
) -> None:
    harness = KeyboardHarness()

    anyio.run(harness.controller.press, key)

    assert _event_pairs(harness.session.emissions[0]) == [
        (expected_keycode, True),
        (expected_keycode, False),
    ]


def test_character_press_does_not_resolve_from_an_inactive_xkb_group() -> None:
    harness = KeyboardHarness(input_backend="auto")
    harness.session.keyboard_mapping = (  # type: ignore[method-assign]
        lambda _group, **_kwargs: ()
    )

    anyio.run(harness.controller.press, "a")

    assert harness.commands == [("xdotool", "key", "a")]
    assert harness.session.emissions == []


def test_forced_native_press_searches_the_active_group_instead_of_global_lookup() -> None:
    session = FakeX11InputSession()
    session.group = 1

    def french_group_mapping(
        group: int,
        *,
        levels: int = 4,
    ) -> tuple[tuple[int, tuple[int, ...]], ...]:
        assert group == 1
        assert levels == 4
        return (
            (24, (ord("a"), ord("A"), 0, 0)),
            (38, (ord("q"), ord("Q"), 0, 0)),
            (50, (0xFFE1, 0, 0, 0)),
        )

    # XKeysymToKeycode is global and points at keycode 38 from another group.
    assert session.keysym_to_keycode(ord("A")) == 38
    session.keyboard_mapping = french_group_mapping  # type: ignore[method-assign]
    harness = KeyboardHarness(input_backend="xtest", session=session)

    anyio.run(harness.controller.press, "A")

    assert harness.commands == []
    assert _event_pairs(session.emissions[0]) == [
        (50, True),
        (24, True),
        (24, False),
        (50, False),
    ]


def test_native_typing_uses_active_layout_for_shift_and_level_three() -> None:
    harness = KeyboardHarness()

    result = anyio.run(harness.controller.type_text, "A!é", 0, "keystrokes")

    assert result.output == {"length": 3, "delay_ms": 0, "method": "keystrokes"}
    assert _event_pairs(harness.session.emissions[0]) == [
        (50, True),
        (38, True),
        (38, False),
        (50, False),
        (50, True),
        (10, True),
        (10, False),
        (50, False),
        (108, True),
        (26, True),
        (26, False),
        (108, False),
    ]


def test_native_typing_resolves_x11_punctuation_names() -> None:
    harness = KeyboardHarness()

    result = anyio.run(harness.controller.type_text, "a-a", 0, "keystrokes")

    assert result.output["method"] == "keystrokes"
    assert _event_pairs(harness.session.emissions[0]) == [
        (38, True),
        (38, False),
        (20, True),
        (20, False),
        (38, True),
        (38, False),
    ]


def test_native_typing_accounts_for_caps_lock() -> None:
    session = FakeX11InputSession()
    session.modifiers = 1 << 1
    harness = KeyboardHarness(session=session)

    anyio.run(harness.controller.type_text, "Aa", 0, "keystrokes")

    assert _event_pairs(session.emissions[0]) == [
        (38, True),
        (38, False),
        (50, True),
        (38, True),
        (38, False),
        (50, False),
    ]


def test_auto_typing_preflights_full_text_then_uses_and_restores_clipboard() -> None:
    harness = KeyboardHarness()

    result = anyio.run(harness.controller.type_text, "a🙂", 0, "auto")

    assert result.output["method"] == "clipboard"
    assert harness.clipboard == "previous clipboard"
    assert _event_pairs(harness.session.emissions[0]) == [
        (37, True),
        (55, True),
        (55, False),
        (37, False),
    ]


def test_explicit_keystrokes_reject_unmapped_text_without_partial_injection() -> None:
    harness = KeyboardHarness()

    with pytest.raises(ValueError, match="not mapped"):
        anyio.run(harness.controller.type_text, "a🙂", 0, "keystrokes")

    assert harness.session.emissions == []
    assert harness.clipboard == "previous clipboard"


def test_hotkey_honors_duration_and_preserves_preheld_modifier(monkeypatch) -> None:
    session = FakeX11InputSession(pressed={37})
    harness = KeyboardHarness(session=session)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(keyboard_module.asyncio, "sleep", record_sleep)

    result = anyio.run(harness.controller.hotkey, ["control", "shift", "t"], 25)

    assert result.ok is True
    assert sleeps == [0.025]
    assert [_event_pairs(events) for events in session.emissions] == [
        [(50, True), (28, True)],
        [(28, False), (50, False)],
    ]
    assert session.pressed_keycodes() == frozenset({37})


def test_positive_duration_press_never_replays_after_native_release_failure() -> None:
    harness = KeyboardHarness(input_backend="auto")
    original_emit = harness.session.emit
    calls = 0

    def disconnect_during_release(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
    ) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise X11InputUnavailableError("display disconnected during release")
        original_emit(events, preserve_pressed_keycodes=preserve_pressed_keycodes)

    harness.session.emit = disconnect_during_release  # type: ignore[method-assign]

    with pytest.raises(X11InputInjectionError, match="release failed"):
        anyio.run(harness.controller.press, "a", ["ctrl"], 1)

    assert harness.commands == []


def test_positive_duration_hotkey_never_replays_after_native_release_failure() -> None:
    harness = KeyboardHarness(input_backend="auto")
    original_emit = harness.session.emit
    calls = 0

    def disconnect_during_release(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
    ) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise X11InputUnavailableError("display disconnected during release")
        original_emit(events, preserve_pressed_keycodes=preserve_pressed_keycodes)

    harness.session.emit = disconnect_during_release  # type: ignore[method-assign]

    with pytest.raises(X11InputInjectionError, match="release failed"):
        anyio.run(harness.controller.hotkey, ["ctrl", "t"], 1)

    assert harness.commands == []


def test_partial_native_injection_cleans_up_and_never_replays_with_xdotool() -> None:
    session = FakeX11InputSession(fail_injection_once=True)
    harness = KeyboardHarness(input_backend="auto", session=session)

    with pytest.raises(X11InputInjectionError, match="partial"):
        anyio.run(harness.controller.hotkey, ["ctrl", "t"])

    assert harness.commands == []
    assert [_event_pairs(events) for events in session.emissions] == [
        [(37, True)],
        [(28, False), (37, False)],
    ]
    assert session.pressed_keycodes() == frozenset()


def test_down_and_up_update_held_state_only_after_native_success() -> None:
    harness = KeyboardHarness()

    anyio.run(harness.controller.down, "shift")
    anyio.run(harness.controller.down, "shift")
    assert harness.held == {"shift"}
    assert len(harness.session.emissions) == 1

    anyio.run(harness.controller.up, "shift")
    assert harness.held == set()
    assert [_event_pairs(events) for events in harness.session.emissions] == [
        [(50, True)],
        [(50, False)],
    ]


def test_shifted_key_down_keeps_owned_modifier_until_key_up() -> None:
    harness = KeyboardHarness()

    anyio.run(harness.controller.down, "A")

    assert _event_pairs(harness.session.emissions[0]) == [
        (50, True),
        (38, True),
    ]
    assert harness.session.pressed_keycodes() == frozenset({38, 50})

    anyio.run(harness.controller.up, "A")

    assert _event_pairs(harness.session.emissions[1]) == [
        (38, False),
        (50, False),
    ]
    assert harness.session.pressed_keycodes() == frozenset()


def test_auto_falls_back_when_xtest_is_unavailable_but_forced_xtest_fails() -> None:
    unavailable = FakeX11InputSession(available=False)
    automatic = KeyboardHarness(input_backend="auto", session=unavailable)

    anyio.run(automatic.controller.press, "a")
    assert automatic.commands == [("xdotool", "key", "a")]

    forced = KeyboardHarness(input_backend="xtest", session=unavailable)
    with pytest.raises(X11InputUnavailableError, match="unavailable"):
        anyio.run(forced.controller.press, "a")
    assert forced.commands == []


def test_auto_press_falls_back_when_native_pre_emission_probe_becomes_unavailable() -> None:
    harness = KeyboardHarness(input_backend="auto")

    def unavailable(_keycodes: Iterable[int] | None = None) -> frozenset[int]:
        raise X11InputUnavailableError("display disconnected")

    harness.session.pressed_keycodes = unavailable  # type: ignore[method-assign]

    anyio.run(harness.controller.press, "a")

    assert harness.commands == [("xdotool", "key", "a")]
    assert harness.session.emissions == []


def test_auto_typing_falls_back_when_first_native_emit_is_unavailable() -> None:
    harness = KeyboardHarness(input_backend="auto")

    def unavailable(
        _events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
    ) -> None:
        del preserve_pressed_keycodes
        raise X11InputUnavailableError("display disconnected")

    harness.session.emit = unavailable  # type: ignore[method-assign]

    result = anyio.run(harness.controller.type_text, "a", 0, "keystrokes")

    assert result.output["method"] == "xdotool"
    assert harness.commands == [("xdotool", "type", "--delay", "0", "a")]


def test_delayed_typing_never_replays_after_native_progress() -> None:
    harness = KeyboardHarness(input_backend="auto")
    original_emit = harness.session.emit
    calls = 0

    def disconnect_after_first(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise X11InputUnavailableError("display disconnected")
        original_emit(events, preserve_pressed_keycodes=preserve_pressed_keycodes)

    harness.session.emit = disconnect_after_first  # type: ignore[method-assign]

    with pytest.raises(X11InputInjectionError, match="partially"):
        anyio.run(harness.controller.type_text, "aa", 1, "keystrokes")

    assert harness.commands == []


def test_legacy_xdotool_typing_method_remains_an_explicit_adapter() -> None:
    harness = KeyboardHarness()

    result = anyio.run(harness.controller.type_text, "legacy", 7, "xdotool")

    assert harness.commands == [("xdotool", "type", "--delay", "7", "legacy")]
    assert result.output["method"] == "xdotool"


def test_clipboard_is_restored_when_native_paste_fails() -> None:
    session = FakeX11InputSession(fail_injection_once=True)
    harness = KeyboardHarness(session=session)

    with pytest.raises(X11InputInjectionError):
        anyio.run(harness.controller.type_text, "secret", 0, "clipboard")

    assert harness.clipboard == "previous clipboard"


def test_backend_release_all_reconciles_keyboard_after_failed_release() -> None:
    backend = X11DesktopBackend(input_backend="xdotool")
    commands: list[tuple[str, ...]] = []
    fail_next_release = True

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal fail_next_release
        commands.append(args)
        if args == ("xdotool", "keyup", "shift") and fail_next_release:
            fail_next_release = False
            raise RuntimeError("transient key release failure")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run  # type: ignore[method-assign]

    anyio.run(backend.key_down, "shift")
    backend._last_input_backend = "xtest"
    released = anyio.run(backend.release_all)
    assert backend.input_backend == "xdotool"

    anyio.run(backend.key_down, "shift")

    assert released.output == {"keys": ["shift"], "buttons": []}
    assert commands.count(("xdotool", "keyup", "shift")) == 2
    assert commands.count(("xdotool", "keydown", "shift")) == 2
    assert backend.held_keys == {"shift"}


def test_release_all_attributes_the_final_mouse_cleanup_adapter() -> None:
    class NativePointerSession:
        failure = None

        def available(self) -> bool:
            return True

        def emit(self, _events, **_kwargs) -> None:
            return None

    backend = X11DesktopBackend(input_backend="xdotool")
    backend._mouse._configured_backend = "auto"
    backend._mouse._xtest = NativePointerSession()  # type: ignore[assignment]

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run  # type: ignore[method-assign]

    anyio.run(backend.key_down, "shift")
    anyio.run(backend.mouse_down, "left")
    released = anyio.run(backend.release_all)

    assert released.output == {"keys": ["shift"], "buttons": ["left"]}
    assert backend.input_backend == "xtest"
