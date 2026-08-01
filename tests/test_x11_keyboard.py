from __future__ import annotations

import asyncio
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable, Sequence

import anyio
import pytest

from modal_computer_use.daemon.desktop import keyboard as keyboard_module
from modal_computer_use.daemon.desktop.keyboard import (
    KeyboardReleaseOutcome,
    KeyReleaseFailure,
    X11KeyboardController,
    XkbKeymapResolver,
)
from modal_computer_use.daemon.desktop.x11 import X11DesktopBackend
from modal_computer_use.daemon.desktop.xtest import (
    KeyEvent,
    X11EmissionResult,
    X11InputInjectionError,
    X11InputReleaseError,
    X11InputStateConflictError,
    X11InputUnavailableError,
    X11KeyboardState,
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
        self.keyboard_state_queries = 0
        self.mapping_queries = 0
        self.pressed_queries = 0
        self.emission_queries = 0
        self.keysym_queries: Counter[str] = Counter()
        self.before_emit: Callable[[], None] | None = None
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
        self.keysym_queries[name] += 1
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
        self.mapping_queries += 1
        return tuple(
            (keycode, tuple(keysyms[:levels])) for keycode, keysyms in sorted(self._levels.items())
        )

    def keyboard_group(self) -> int:
        return self.group

    def modifier_state(self) -> int:
        return self.modifiers

    def keyboard_state(self) -> X11KeyboardState:
        self.keyboard_state_queries += 1
        return X11KeyboardState(group=self.group, modifiers=self.modifiers)

    def pressed_keycodes(self, keycodes: Iterable[int] | None = None) -> frozenset[int]:
        self.pressed_queries += 1
        if keycodes is None:
            return frozenset(self._pressed)
        return frozenset(self._pressed.intersection(keycodes))

    def emit(
        self,
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        if self.before_emit is not None:
            before_emit, self.before_emit = self.before_emit, None
            before_emit()
        preserved = frozenset(preserve_pressed_keycodes)
        rejected = frozenset(reject_pressed_keycodes)
        initially_pressed = frozenset(self._pressed)
        if preserved or rejected:
            self.emission_queries += 1
        if initially_pressed.intersection(rejected):
            raise X11InputStateConflictError("keyboard target key is already held")
        already_pressed = initially_pressed.intersection(preserved)
        filtered = [event for event in events if event.keycode not in already_pressed]
        result = X11EmissionResult(initially_pressed_keycodes=initially_pressed)
        if self._fail_injection_once:
            self._fail_injection_once = False
            if filtered:
                event = filtered[0]
                self.emissions.append([event])
                self._update_pressed(event)
            error = X11InputInjectionError("partial injection")
            error.emission_result = result
            raise error
        self.emissions.append(filtered)
        for event in filtered:
            self._update_pressed(event)
        return result

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
        self.command_inputs: list[str | None] = []
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

    async def run(
        self,
        *args: str,
        input_text: str | None = None,
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        self.command_inputs.append(input_text)
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


def _baseline_lookup_work(
    mapping: Sequence[tuple[int, Sequence[int]]],
    queries: Sequence[int],
) -> tuple[list[tuple[int, int] | None], int]:
    matches: list[tuple[int, int] | None] = []
    work = 0
    for query in queries:
        match = None
        for keycode, keysyms in mapping:
            for level, keysym in enumerate(keysyms):
                work += 1
                if keysym == query:
                    match = (keycode, level)
                    break
            if match is not None:
                break
        matches.append(match)
    return matches, work


def _indexed_lookup_work(
    mapping: Sequence[tuple[int, Sequence[int]]],
    queries: Sequence[int],
) -> tuple[list[tuple[int, int] | None], int]:
    index: dict[int, tuple[int, int]] = {}
    work = 0
    for keycode, keysyms in mapping:
        for level, keysym in enumerate(keysyms):
            work += 1
            index.setdefault(keysym, (keycode, level))
    matches = []
    for query in queries:
        work += 1
        matches.append(index.get(query))
    return matches, work


@pytest.mark.parametrize(
    "operation",
    [
        lambda controller: controller.press("A", ["control", "alt"]),
        lambda controller: controller.hotkey(["control", "shift", "t"]),
        lambda controller: controller.type_text("AaéÉ", 0, "keystrokes"),
    ],
)
def test_native_operation_resolves_from_one_xkb_snapshot(operation) -> None:
    session = FakeX11InputSession()
    harness = KeyboardHarness(session=session)

    anyio.run(_call_keyboard, lambda: operation(harness.controller))

    assert session.keyboard_state_queries == 1
    assert session.mapping_queries == 1


def test_keysym_resolution_memoizes_only_successes() -> None:
    session = FakeX11InputSession()
    resolver = XkbKeymapResolver(session)
    original_resolve = session.resolve_keysym
    startup_failures = 1

    def flaky_resolve(name: str) -> int:
        nonlocal startup_failures
        if name == "A" and startup_failures:
            startup_failures -= 1
            session.keysym_queries[name] += 1
            raise X11InputUnavailableError("display is still starting")
        return original_resolve(name)

    session.resolve_keysym = flaky_resolve  # type: ignore[method-assign]

    with pytest.raises(X11InputUnavailableError, match="still starting"):
        resolver.character("A")

    assert resolver.character("A") is not None
    assert resolver.character("A") is not None
    assert session.keysym_queries["A"] == 2
    assert session.keysym_queries["Shift_L"] == 1


def test_unmapped_keysym_is_retried_after_the_mapping_can_change() -> None:
    session = FakeX11InputSession()
    resolver = XkbKeymapResolver(session)

    assert resolver.character("z") is None
    session._keysyms["z"] = ord("a")
    assert resolver.character("z") is not None
    assert session.keysym_queries["z"] == 2


@pytest.mark.parametrize("size", [100, 1000])
@pytest.mark.parametrize("workload", ["repeated", "varied"])
def test_indexed_resolver_microbenchmark_reduces_deterministic_lookup_work(
    size: int,
    workload: str,
) -> None:
    mapping = tuple(
        (
            keycode,
            tuple(keycode * 4 + level for level in range(4)),
        )
        for keycode in range(8, 256)
    )
    flattened = [keysym for _keycode, keysyms in mapping for keysym in keysyms]
    queries = (
        [flattened[-1]] * size
        if workload == "repeated"
        else [flattened[-1 - (index % len(flattened))] for index in range(size)]
    )

    baseline_matches, baseline_work = _baseline_lookup_work(mapping, queries)
    indexed_matches, indexed_work = _indexed_lookup_work(mapping, queries)

    assert indexed_matches == baseline_matches
    assert baseline_work > indexed_work
    assert indexed_work == len(flattened) + size


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


def test_unknown_semantic_key_never_matches_empty_active_group_slots() -> None:
    harness = KeyboardHarness(input_backend="xtest")

    with pytest.raises(X11InputUnavailableError, match="not mapped"):
        anyio.run(harness.controller.press, "not-a-real-key")

    assert harness.commands == []
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


def test_long_auto_typing_selects_clipboard_before_building_native_strokes(
    monkeypatch,
) -> None:
    harness = KeyboardHarness()

    def reject_amplification(_text: str):
        raise AssertionError("long text must not build a per-character stroke list")

    monkeypatch.setattr(harness.controller._keymap, "text", reject_amplification)

    result = anyio.run(harness.controller.type_text, "a" * 100_000, 0, "auto")

    assert result.output["method"] == "clipboard"
    assert harness.clipboard == "previous clipboard"


def test_long_explicit_native_typing_emits_bounded_batches() -> None:
    harness = KeyboardHarness()
    text = "a" * ((keyboard_module._NATIVE_TYPE_CHUNK_SIZE * 2) + 1)

    result = anyio.run(harness.controller.type_text, text, 0, "keystrokes")

    assert result.output["method"] == "keystrokes"
    assert len(harness.session.emissions) == 3
    assert max(map(len, harness.session.emissions)) <= (
        keyboard_module._NATIVE_TYPE_CHUNK_SIZE * 2
    )


def test_long_explicit_native_typing_preflights_before_emission() -> None:
    harness = KeyboardHarness()
    text = ("a" * keyboard_module._NATIVE_TYPE_CHUNK_SIZE) + "🙂"

    with pytest.raises(ValueError, match="not mapped"):
        anyio.run(harness.controller.type_text, text, 0, "keystrokes")

    assert harness.session.emissions == []


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


def test_hotkey_preserves_modifier_pressed_at_locked_emit() -> None:
    session = FakeX11InputSession()
    session.before_emit = lambda: session._pressed.add(37)
    harness = KeyboardHarness(session=session)

    anyio.run(harness.controller.hotkey, ["control", "t"])

    assert [_event_pairs(events) for events in session.emissions] == [[(28, True), (28, False)]]
    assert session._pressed == {37}
    assert session.pressed_queries == 0
    assert session.emission_queries == 1


@pytest.mark.parametrize(
    ("operation", "pressed"),
    [
        (lambda controller: controller.press("a"), {38}),
        (lambda controller: controller.hotkey(["ctrl", "t"]), {28}),
    ],
)
def test_press_and_hotkey_reject_preheld_target_without_fallback(
    operation,
    pressed: set[int],
) -> None:
    harness = KeyboardHarness(
        input_backend="auto",
        session=FakeX11InputSession(pressed=pressed),
    )

    with pytest.raises(X11InputStateConflictError, match="target key is already held"):
        anyio.run(_call_keyboard, lambda: operation(harness.controller))

    assert harness.commands == []
    assert harness.session.emissions == []


@pytest.mark.parametrize(
    "operation",
    [
        lambda controller: controller.press("a"),
        lambda controller: controller.hotkey(["ctrl", "a"]),
    ],
)
def test_press_and_hotkey_atomically_reject_target_held_at_locked_emit(
    operation,
) -> None:
    session = FakeX11InputSession()
    session.before_emit = lambda: session._pressed.add(38)
    harness = KeyboardHarness(input_backend="auto", session=session)

    with pytest.raises(RuntimeError, match="target key is already held"):
        anyio.run(_call_keyboard, lambda: operation(harness.controller))

    assert harness.commands == []
    assert harness.session.emissions == []
    assert session.pressed_queries == 0
    assert session.emission_queries == 1


def test_positive_duration_press_never_replays_after_native_release_failure() -> None:
    harness = KeyboardHarness(input_backend="auto")
    original_emit = harness.session.emit
    calls = 0

    def disconnect_during_release(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise X11InputUnavailableError("display disconnected during release")
        return original_emit(
            events,
            preserve_pressed_keycodes=preserve_pressed_keycodes,
            reject_pressed_keycodes=reject_pressed_keycodes,
        )

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
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise X11InputUnavailableError("display disconnected during release")
        return original_emit(
            events,
            preserve_pressed_keycodes=preserve_pressed_keycodes,
            reject_pressed_keycodes=reject_pressed_keycodes,
        )

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


def test_partial_injection_never_releases_modifier_held_at_locked_emit() -> None:
    session = FakeX11InputSession(fail_injection_once=True)
    session.before_emit = lambda: session._pressed.add(37)
    harness = KeyboardHarness(input_backend="auto", session=session)

    with pytest.raises(X11InputInjectionError, match="partial"):
        anyio.run(harness.controller.hotkey, ["ctrl", "t"])

    assert [_event_pairs(events) for events in session.emissions] == [
        [(28, True)],
        [(28, False)],
    ]
    assert session._pressed == {37}
    assert harness.commands == []


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


def test_auto_release_falls_back_for_owned_modifier_with_preheld_target() -> None:
    session = FakeX11InputSession()
    session.before_emit = lambda: session._pressed.add(38)
    harness = KeyboardHarness(input_backend="auto", session=session)

    anyio.run(harness.controller.acquire, "A")

    tracked = harness.controller._held_keys["A"]
    assert tracked.owned is False
    assert tracked.owned_modifiers == (50,)
    assert tracked.owned_modifier_names == ("shift",)

    def unavailable(*_args, **_kwargs):
        raise X11InputUnavailableError("display disconnected")

    session.emit = unavailable  # type: ignore[method-assign]
    anyio.run(harness.controller.up, "A")

    assert harness.commands == [("xdotool", "keyup", "shift")]
    assert harness.controller._held_keys == {}


def test_auto_falls_back_when_xtest_is_unavailable_but_forced_xtest_fails() -> None:
    unavailable = FakeX11InputSession(available=False)
    automatic = KeyboardHarness(input_backend="auto", session=unavailable)

    anyio.run(automatic.controller.press, "a")
    assert automatic.commands == [("xdotool", "key", "a")]

    forced = KeyboardHarness(input_backend="xtest", session=unavailable)
    with pytest.raises(X11InputUnavailableError, match="unavailable"):
        anyio.run(forced.controller.press, "a")
    assert forced.commands == []


def test_xdotool_key_acquire_cancellation_directly_releases_untracked_key() -> None:
    harness = KeyboardHarness(input_backend="xdotool")
    keydown_started = asyncio.Event()

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        harness.commands.append(args)
        if args == ("xdotool", "keydown", "shift"):
            keydown_started.set()
            await asyncio.Future()
        return subprocess.CompletedProcess(args, 0, "", "")

    harness.controller._run = run

    async def scenario() -> None:
        task = asyncio.create_task(harness.controller.acquire("shift"))
        await keydown_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert harness.commands == [
        ("xdotool", "keydown", "shift"),
        ("xdotool", "keyup", "shift"),
    ]
    assert harness.controller._held_keys == {}
    assert harness.held == set()


def test_native_key_acquire_cancellation_reconciles_emitted_and_tracked_state() -> None:
    harness = KeyboardHarness(input_backend="xtest")
    state_started = asyncio.Event()

    async def cancelling_state(key: str) -> None:
        harness.held.add(key)
        state_started.set()
        await asyncio.Future()

    harness.controller._key_down_state = cancelling_state

    async def scenario() -> None:
        task = asyncio.create_task(harness.controller.acquire("shift"))
        await state_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert [_event_pairs(events) for events in harness.session.emissions] == [
        [(50, True)],
        [(50, False)],
    ]
    assert harness.controller._held_keys == {}
    assert harness.held == set()


def test_xdotool_key_acquire_retains_possible_hold_when_cancel_cleanup_fails() -> None:
    harness = KeyboardHarness(input_backend="xdotool")
    keydown_started = asyncio.Event()
    cleanup_failed = False

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal cleanup_failed
        harness.commands.append(args)
        if args == ("xdotool", "keydown", "shift"):
            keydown_started.set()
            await asyncio.Future()
        if args == ("xdotool", "keyup", "shift") and not cleanup_failed:
            cleanup_failed = True
            raise RuntimeError("cleanup failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    harness.controller._run = run

    async def scenario() -> None:
        task = asyncio.create_task(harness.controller.acquire("shift"))
        await keydown_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert set(harness.controller._held_keys) == {"shift"}
    assert harness.held == set()

    outcome = anyio.run(harness.controller.release_all)

    assert outcome == KeyboardReleaseOutcome(released=("shift",), failures=())
    assert harness.controller._held_keys == {}
    assert harness.commands == [
        ("xdotool", "keydown", "shift"),
        ("xdotool", "keyup", "shift"),
        ("xdotool", "keyup", "shift"),
    ]


def test_native_key_acquire_retains_tracking_when_cancel_cleanup_fails() -> None:
    harness = KeyboardHarness(input_backend="xtest")
    state_started = asyncio.Event()
    original_emit = harness.session.emit
    calls = 0

    def fail_compensating_release(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("cleanup failed")
        return original_emit(
            events,
            preserve_pressed_keycodes=preserve_pressed_keycodes,
            reject_pressed_keycodes=reject_pressed_keycodes,
        )

    async def cancelling_state(key: str) -> None:
        harness.held.add(key)
        state_started.set()
        await asyncio.Future()

    harness.session.emit = fail_compensating_release  # type: ignore[method-assign]
    harness.controller._key_down_state = cancelling_state

    async def scenario() -> None:
        task = asyncio.create_task(harness.controller.acquire("shift"))
        await state_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert set(harness.controller._held_keys) == {"shift"}
    assert harness.held == {"shift"}
    assert harness.session.pressed_keycodes() == frozenset({50})

    outcome = anyio.run(harness.controller.release_all)

    assert outcome == KeyboardReleaseOutcome(released=("shift",), failures=())
    assert harness.controller._held_keys == {}
    assert harness.held == set()
    assert harness.session.pressed_keycodes() == frozenset()


def test_native_key_acquire_retains_possible_hold_when_partial_cleanup_fails() -> None:
    session = FakeX11InputSession(fail_injection_once=True)
    harness = KeyboardHarness(input_backend="xtest", session=session)
    original_emit = session.emit
    calls = 0

    def fail_compensating_release(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("cleanup failed")
        return original_emit(
            events,
            preserve_pressed_keycodes=preserve_pressed_keycodes,
            reject_pressed_keycodes=reject_pressed_keycodes,
        )

    session.emit = fail_compensating_release  # type: ignore[method-assign]

    with pytest.raises(X11InputInjectionError, match="partial"):
        anyio.run(harness.controller.acquire, "shift")

    assert set(harness.controller._held_keys) == {"shift"}
    assert harness.held == set()
    assert session.pressed_keycodes() == frozenset({50})

    outcome = anyio.run(harness.controller.release_all)

    assert outcome == KeyboardReleaseOutcome(released=("shift",), failures=())
    assert harness.controller._held_keys == {}
    assert harness.held == set()
    assert session.pressed_keycodes() == frozenset()


def test_native_partial_key_acquire_never_claims_locked_preheld_target() -> None:
    session = FakeX11InputSession(fail_injection_once=True)
    session.before_emit = lambda: session._pressed.add(38)
    harness = KeyboardHarness(input_backend="xtest", session=session)
    original_emit = session.emit
    calls = 0

    def fail_compensating_release(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("cleanup failed")
        return original_emit(
            events,
            preserve_pressed_keycodes=preserve_pressed_keycodes,
            reject_pressed_keycodes=reject_pressed_keycodes,
        )

    session.emit = fail_compensating_release  # type: ignore[method-assign]

    with pytest.raises(X11InputInjectionError, match="partial"):
        anyio.run(harness.controller.acquire, "A")

    tracked = harness.controller._held_keys["A"]
    assert tracked.owned is False
    assert tracked.owned_modifiers == (50,)
    assert session.pressed_keycodes() == frozenset({38, 50})

    outcome = anyio.run(harness.controller.release_all)

    assert outcome == KeyboardReleaseOutcome(released=("A",), failures=())
    assert [_event_pairs(events) for events in session.emissions] == [
        [(50, True)],
        [(50, False)],
    ]
    assert session.pressed_keycodes() == frozenset({38})


def test_xdotool_key_acquire_normalizes_post_start_process_failure() -> None:
    harness = KeyboardHarness(input_backend="xdotool")

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        harness.commands.append(args)
        if args == ("xdotool", "keydown", "shift"):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    harness.controller._run = run

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(harness.controller.acquire, "shift")

    assert raised.value.input_backend == "xdotool"
    assert isinstance(raised.value.__cause__, subprocess.CalledProcessError)
    assert harness.commands == [
        ("xdotool", "keydown", "shift"),
        ("xdotool", "keyup", "shift"),
    ]
    assert harness.controller._held_keys == {}


def test_xdotool_key_acquire_does_not_release_known_pre_emission_failure() -> None:
    harness = KeyboardHarness(input_backend="xdotool")

    async def unavailable(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        harness.commands.append(args)
        raise X11InputUnavailableError("not started")

    harness.controller._run = unavailable

    with pytest.raises(X11InputUnavailableError, match="xdotool is unavailable") as raised:
        anyio.run(harness.controller.acquire, "shift")

    assert raised.value.input_backend == "xdotool"
    assert harness.commands == [("xdotool", "keydown", "shift")]
    assert harness.controller._held_keys == {}
    assert harness.held == set()


def test_xdotool_key_acquire_does_not_track_process_spawn_failure() -> None:
    harness = KeyboardHarness(input_backend="xdotool")

    async def missing_process(*_args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("xdotool")

    harness.controller._run = missing_process

    with pytest.raises(X11InputUnavailableError) as raised:
        anyio.run(harness.controller.acquire, "shift")

    assert raised.value.input_backend == "xdotool"
    assert harness.controller._held_keys == {}
    assert harness.held == set()


def test_xdotool_press_cancellation_cleans_failing_and_prior_modifiers() -> None:
    harness = KeyboardHarness(input_backend="xdotool")
    shift_started = asyncio.Event()

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        harness.commands.append(args)
        if args == ("xdotool", "keydown", "shift"):
            shift_started.set()
            await asyncio.Future()
        return subprocess.CompletedProcess(args, 0, "", "")

    harness.controller._run = run

    async def scenario() -> None:
        task = asyncio.create_task(harness.controller.press("a", modifiers=("ctrl", "shift")))
        await shift_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)

    assert harness.commands == [
        ("xdotool", "keydown", "ctrl"),
        ("xdotool", "keydown", "shift"),
        ("xdotool", "keyup", "shift"),
        ("xdotool", "keyup", "ctrl"),
    ]


def test_xdotool_atomic_chord_failure_cleans_every_candidate() -> None:
    harness = KeyboardHarness(input_backend="xdotool")

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        harness.commands.append(args)
        if args == ("xdotool", "key", "ctrl+a"):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    harness.controller._run = run

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(harness.controller.hotkey, ["ctrl", "a"])

    assert raised.value.input_backend == "xdotool"
    assert harness.commands == [
        ("xdotool", "key", "ctrl+a"),
        ("xdotool", "keyup", "a"),
        ("xdotool", "keyup", "ctrl"),
    ]


def test_xdotool_chord_failure_preserves_explicitly_held_key() -> None:
    harness = KeyboardHarness(input_backend="xdotool")
    anyio.run(harness.controller.down, "ctrl")

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        harness.commands.append(args)
        if args == ("xdotool", "keydown", "a"):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    harness.controller._run = run

    with pytest.raises(X11InputInjectionError):
        anyio.run(harness.controller.hotkey, ["ctrl", "a"], 1)

    assert harness.commands == [
        ("xdotool", "keydown", "ctrl"),
        ("xdotool", "keydown", "a"),
        ("xdotool", "keyup", "a"),
    ]
    assert set(harness.controller._held_keys) == {"ctrl"}
    assert harness.held == {"ctrl"}


def test_auto_press_falls_back_when_native_emit_probe_becomes_unavailable() -> None:
    harness = KeyboardHarness(input_backend="auto")

    def unavailable(
        _events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        del preserve_pressed_keycodes, reject_pressed_keycodes
        raise X11InputUnavailableError("display disconnected")

    harness.session.emit = unavailable  # type: ignore[method-assign]

    anyio.run(harness.controller.press, "a")

    assert harness.commands == [("xdotool", "key", "a")]
    assert harness.session.emissions == []


def test_auto_typing_falls_back_when_first_native_emit_is_unavailable() -> None:
    harness = KeyboardHarness(input_backend="auto")

    def unavailable(
        _events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> None:
        del preserve_pressed_keycodes, reject_pressed_keycodes
        raise X11InputUnavailableError("display disconnected")

    harness.session.emit = unavailable  # type: ignore[method-assign]

    result = anyio.run(harness.controller.type_text, "a", 0, "keystrokes")

    assert result.output["method"] == "xdotool"
    assert harness.commands == [("xdotool", "type", "--delay", "0", "--file", "-")]
    assert harness.command_inputs == ["a"]


def test_typing_never_silently_drops_an_already_held_target_key() -> None:
    automatic = KeyboardHarness(
        input_backend="auto",
        session=FakeX11InputSession(pressed={38}),
    )

    with pytest.raises(X11InputStateConflictError, match="already held"):
        anyio.run(automatic.controller.type_text, "a", 0, "keystrokes")
    assert automatic.commands == []
    assert automatic.command_inputs == []
    assert automatic.session.emissions == []

    forced = KeyboardHarness(
        input_backend="xtest",
        session=FakeX11InputSession(pressed={38}),
    )
    with pytest.raises(X11InputStateConflictError, match="already held"):
        anyio.run(forced.controller.type_text, "a", 0, "keystrokes")
    assert forced.commands == []
    assert forced.session.emissions == []


def test_typing_rejects_target_pressed_at_locked_emit() -> None:
    session = FakeX11InputSession()
    session.before_emit = lambda: session._pressed.add(38)
    harness = KeyboardHarness(input_backend="auto", session=session)

    with pytest.raises(X11InputStateConflictError, match="already held"):
        anyio.run(harness.controller.type_text, "a", 0, "keystrokes")
    assert harness.commands == []
    assert harness.command_inputs == []
    assert harness.session.emissions == []
    assert session.pressed_queries == 0
    assert session.emission_queries == 1


def test_delayed_typing_never_replays_after_native_progress() -> None:
    harness = KeyboardHarness(input_backend="auto")
    original_emit = harness.session.emit
    calls = 0

    def disconnect_after_first(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise X11InputUnavailableError("display disconnected")
        return original_emit(
            events,
            preserve_pressed_keycodes=preserve_pressed_keycodes,
            reject_pressed_keycodes=reject_pressed_keycodes,
        )

    harness.session.emit = disconnect_after_first  # type: ignore[method-assign]

    with pytest.raises(X11InputInjectionError, match="partially"):
        anyio.run(harness.controller.type_text, "aa", 1, "keystrokes")

    assert harness.commands == []


def test_delayed_typing_reclassifies_state_conflict_after_native_progress() -> None:
    harness = KeyboardHarness(input_backend="auto")
    original_emit = harness.session.emit
    conflict = X11InputStateConflictError("target became held")
    calls = 0

    def conflict_after_first(
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise conflict
        return original_emit(
            events,
            preserve_pressed_keycodes=preserve_pressed_keycodes,
            reject_pressed_keycodes=reject_pressed_keycodes,
        )

    harness.session.emit = conflict_after_first  # type: ignore[method-assign]

    with pytest.raises(X11InputInjectionError, match="partially") as raised:
        anyio.run(harness.controller.type_text, "aa", 1, "keystrokes")

    assert raised.value.__cause__ is conflict
    assert len(harness.session.emissions) == 1
    assert harness.commands == []


def test_delayed_typing_preserves_pre_emission_state_conflict() -> None:
    session = FakeX11InputSession(pressed={38})
    harness = KeyboardHarness(input_backend="auto", session=session)

    with pytest.raises(X11InputStateConflictError, match="already held"):
        anyio.run(harness.controller.type_text, "a", 1, "keystrokes")

    assert harness.session.emissions == []
    assert harness.commands == []


def test_legacy_xdotool_typing_method_remains_an_explicit_adapter() -> None:
    harness = KeyboardHarness()

    result = anyio.run(harness.controller.type_text, "legacy", 7, "xdotool")

    assert harness.commands == [("xdotool", "type", "--delay", "7", "--file", "-")]
    assert harness.command_inputs == ["legacy"]
    assert result.output["method"] == "xdotool"


def test_xdotool_typing_process_failure_is_partial_and_preserves_backend() -> None:
    harness = KeyboardHarness(input_backend="xdotool")

    async def fail(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, args)

    harness.controller._run = fail

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(harness.controller.type_text, "possibly-partial", 0, "xdotool")

    assert raised.value.input_backend == "xdotool"


def test_xdotool_typing_timeout_is_partial_not_unavailable() -> None:
    harness = KeyboardHarness(input_backend="xdotool")

    async def timeout(*_args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise TimeoutError

    harness.controller._run = timeout

    with pytest.raises(X11InputInjectionError) as raised:
        anyio.run(harness.controller.type_text, "possibly-partial", 0, "xdotool")

    assert not isinstance(raised.value, X11InputUnavailableError)
    assert raised.value.input_backend == "xdotool"


def test_xdotool_press_rejects_an_internally_held_target() -> None:
    harness = KeyboardHarness(input_backend="xdotool")
    anyio.run(harness.controller.acquire, "a")

    with pytest.raises(X11InputStateConflictError, match="already held"):
        anyio.run(harness.controller.press, "a")

    assert harness.commands == [("xdotool", "keydown", "a")]


def test_xdotool_key_release_normalizes_post_start_process_failure() -> None:
    harness = KeyboardHarness(input_backend="xdotool")
    anyio.run(harness.controller.down, "shift")

    async def fail(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, args)

    harness.controller._run = fail

    with pytest.raises(X11InputReleaseError) as raised:
        anyio.run(harness.controller.up, "shift")

    assert raised.value.input_backend == "xdotool"
    assert isinstance(raised.value.__cause__, subprocess.CalledProcessError)
    assert set(harness.controller._held_keys) == {"shift"}


def test_xdotool_key_release_preserves_pre_spawn_unavailable_error() -> None:
    harness = KeyboardHarness(input_backend="xdotool")
    anyio.run(harness.controller.down, "shift")

    async def missing(*_args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("xdotool")

    harness.controller._run = missing

    with pytest.raises(X11InputUnavailableError) as raised:
        anyio.run(harness.controller.up, "shift")

    assert raised.value.input_backend == "xdotool"
    assert set(harness.controller._held_keys) == {"shift"}


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

    assert released.ok is True
    assert released.output == {
        "keys": ["shift"],
        "buttons": [],
    }
    assert commands.count(("xdotool", "keyup", "shift")) == 2
    assert commands.count(("xdotool", "keydown", "shift")) == 2
    assert backend.held_keys == {"shift"}


def test_keyboard_release_outcome_retains_the_failed_attempt_backend() -> None:
    harness = KeyboardHarness(input_backend="xtest")
    anyio.run(harness.controller.down, "shift")

    def fail_release(
        _events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult:
        del preserve_pressed_keycodes, reject_pressed_keycodes
        raise X11InputInjectionError("sensitive release failure")

    harness.session.emit = fail_release  # type: ignore[method-assign]

    outcome = anyio.run(harness.controller.release_all)

    assert outcome == KeyboardReleaseOutcome(
        released=(),
        failures=(KeyReleaseFailure(key="shift", input_backend="xtest"),),
    )
    assert harness.held == {"shift"}


def test_backend_release_all_uses_each_failed_keys_attempt_backend() -> None:
    backend = X11DesktopBackend(input_backend="auto")
    backend.held_keys.update(("a", "shift"))

    async def release_keys(_keys) -> KeyboardReleaseOutcome:
        return KeyboardReleaseOutcome(
            released=(),
            failures=(
                KeyReleaseFailure(key="a", input_backend="xdotool"),
                KeyReleaseFailure(key="shift", input_backend="xtest"),
            ),
        )

    backend._keyboard.release_all = release_keys  # type: ignore[method-assign]

    released = anyio.run(backend.release_all)

    assert released.output["failures"] == [
        {
            "kind": "key",
            "value": "a",
            "input_backend": "xdotool",
            "code": "key_release_failed",
        },
        {
            "kind": "key",
            "value": "shift",
            "input_backend": "xtest",
            "code": "key_release_failed",
        },
    ]


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

    assert released.ok is True
    assert released.output == {
        "keys": ["shift"],
        "buttons": ["left"],
    }
    assert backend.input_backend == "xtest"


def test_release_all_attributes_the_final_failed_mouse_release_attempt() -> None:
    class PartialThenUnavailablePointerSession:
        failure = "XTest unavailable"

        def __init__(self) -> None:
            self.native_attempted = False

        def available(self) -> bool:
            return not self.native_attempted

        def emit(self, _events, **_kwargs) -> None:
            self.native_attempted = True
            raise X11InputInjectionError("partial button release")

    backend = X11DesktopBackend(input_backend="auto")
    backend.held_buttons.add("left")
    backend._mouse._active_backend = "xtest"
    backend._mouse._xtest = PartialThenUnavailablePointerSession()  # type: ignore[assignment]

    async def fail_run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        if args == ("xdotool", "mouseup", "1"):
            raise RuntimeError("xdotool release failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = fail_run  # type: ignore[method-assign]

    released = anyio.run(backend.release_all)

    assert released.ok is False
    assert released.output["failures"] == [
        {
            "kind": "button",
            "value": "left",
            "input_backend": "xdotool",
            "code": "button_release_failed",
        }
    ]
    assert backend._mouse.release_attempt_backend == "xdotool"
    assert backend.input_backend == "xdotool"


def test_release_all_retains_keys_and_buttons_that_cannot_be_released() -> None:
    backend = X11DesktopBackend(input_backend="xdotool")
    commands: list[tuple[str, ...]] = []

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args in {
            ("xdotool", "keyup", "shift"),
            ("xdotool", "mouseup", "1"),
        }:
            raise RuntimeError("release failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run  # type: ignore[method-assign]
    anyio.run(backend.key_down, "shift")
    anyio.run(backend.mouse_down, "left")

    released = anyio.run(backend.release_all)

    assert released.ok is False
    assert released.message == "failed to release all held input"
    assert released.output == {
        "code": "release_all_incomplete",
        "keys": [],
        "buttons": [],
        "remaining": {"keys": ["shift"], "buttons": ["left"]},
        "failures": [
            {
                "kind": "key",
                "value": "shift",
                "input_backend": "xdotool",
                "code": "key_release_failed",
            },
            {
                "kind": "button",
                "value": "left",
                "input_backend": "xdotool",
                "code": "button_release_failed",
            },
        ],
    }
    assert backend.held_keys == {"shift"}
    assert backend.held_buttons == {"left"}
    assert commands.count(("xdotool", "keyup", "shift")) == 2
    assert commands.count(("xdotool", "mouseup", "1")) == 2


def test_release_all_reconciles_private_keyboard_ownership() -> None:
    backend = X11DesktopBackend(input_backend="xdotool")
    calls: list[tuple[str, ...]] = []

    async def release(keys: Iterable[str] = ()) -> KeyboardReleaseOutcome:
        calls.append(tuple(keys))
        return KeyboardReleaseOutcome(
            released=(),
            failures=(KeyReleaseFailure(key="shift", input_backend="xdotool"),),
        )

    backend._keyboard.release_all = release  # type: ignore[method-assign]

    released = anyio.run(backend.release_all)

    assert calls == [()]
    assert released.ok is False
    assert released.output["remaining"]["keys"] == ["shift"]
    assert released.output["failures"] == [
        {
            "kind": "key",
            "value": "shift",
            "input_backend": "xdotool",
            "code": "key_release_failed",
        }
    ]


def test_release_all_bounds_failure_metadata_without_hiding_remaining_keys() -> None:
    backend = X11DesktopBackend(input_backend="xdotool")
    fail_releases = False

    async def run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        if fail_releases and args[:2] == ("xdotool", "keyup"):
            raise RuntimeError("sensitive adapter failure")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run  # type: ignore[method-assign]
    keys = [
        *(chr(ord("a") + index) for index in range(26)),
        *(f"f{index}" for index in range(1, 13)),
    ]
    for key in keys:
        anyio.run(backend.key_down, key)
    fail_releases = True

    released = anyio.run(backend.release_all)

    assert released.ok is False
    assert set(released.output["remaining"]["keys"]) == backend.held_keys
    assert len(released.output["remaining"]["keys"]) == len(keys)
    assert len(released.output["failures"]) == 32
    assert "sensitive adapter failure" not in str(released.output)
    assert backend.held_keys == set(released.output["remaining"]["keys"])


async def _call_keyboard(operation):
    return await operation()
