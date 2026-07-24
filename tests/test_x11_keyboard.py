from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence

import anyio
import pytest

from modal_computer_use.daemon.desktop import keyboard as keyboard_module
from modal_computer_use.daemon.desktop.keyboard import X11KeyboardController
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
        }
        self._levels = {
            38: (ord("a"), ord("A"), 0, 0),
            26: (ord("e"), ord("E"), ord("é"), ord("É")),
            28: (ord("t"), ord("T"), 0, 0),
            55: (ord("v"), ord("V"), 0, 0),
            10: (ord("1"), ord("!"), 0, 0),
            20: (ord("-"), ord("_"), 0, 0),
        }

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


def test_auto_falls_back_when_xtest_is_unavailable_but_forced_xtest_fails() -> None:
    unavailable = FakeX11InputSession(available=False)
    automatic = KeyboardHarness(input_backend="auto", session=unavailable)

    anyio.run(automatic.controller.press, "a")
    assert automatic.commands == [("xdotool", "key", "a")]

    forced = KeyboardHarness(input_backend="xtest", session=unavailable)
    with pytest.raises(X11InputUnavailableError, match="unavailable"):
        anyio.run(forced.controller.press, "a")
    assert forced.commands == []


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
