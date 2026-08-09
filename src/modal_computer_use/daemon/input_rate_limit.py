from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from modal_computer_use.models import (
    ClickAction,
    ComputerAction,
    CursorPositionAction,
    DoubleClickAction,
    DragAction,
    HoldKeyAction,
    HotkeyAction,
    ScreenshotAction,
    ScrollAction,
    TripleClickAction,
    TypeAction,
    WaitAction,
    ZoomAction,
    parse_action,
)

_WORK_UNIT = 32
INPUT_RATE_LIMIT_POLICY = "normalized-input-work-v1"


@dataclass(frozen=True, slots=True)
class InputRateLimitWait:
    retry_after_ms: int
    available_tokens: float


@dataclass(slots=True)
class InputTokenBucket:
    """One daemon's continuously refilled input-work admission budget."""

    refill_rate: int
    capacity: int
    clock: Callable[[], float] = time.monotonic
    _available: float = field(init=False, repr=False)
    _updated_at: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.refill_rate < 0:
            raise ValueError("refill_rate must be non-negative")
        if self.capacity < 1:
            raise ValueError("capacity must be positive")
        self._available = float(self.capacity)
        self._updated_at = self.clock()

    @property
    def available(self) -> float:
        self._refill()
        return self._available

    def reservation_error(self, cost: int) -> InputRateLimitWait | None:
        if cost < 0:
            raise ValueError("cost must be non-negative")
        if cost == 0 or self.refill_rate == 0:
            return None
        if cost > self.capacity:
            raise ValueError("cost exceeds bucket capacity")
        self._refill()
        deficit = cost - self._available
        if deficit <= 0:
            return None
        return InputRateLimitWait(
            retry_after_ms=max(1, math.ceil((deficit / self.refill_rate) * 1_000)),
            available_tokens=self._available,
        )

    def reserve(self, cost: int) -> InputRateLimitWait | None:
        error = self.reservation_error(cost)
        if error is not None:
            return error
        if cost > 0 and self.refill_rate > 0:
            self._available -= cost
        return None

    def refund(self, cost: int) -> None:
        if cost < 0:
            raise ValueError("cost must be non-negative")
        self._refill()
        if cost > 0 and self.refill_rate > 0:
            self._available = min(float(self.capacity), self._available + cost)

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        if self.refill_rate > 0 and elapsed > 0:
            self._available = min(
                float(self.capacity),
                self._available + (elapsed * self.refill_rate),
            )


def input_token_cost(action: ComputerAction) -> int:
    """Return normalized input-work tokens, not native X11 event counts."""
    if isinstance(action, WaitAction | ScreenshotAction | ZoomAction | CursorPositionAction):
        return 0
    if isinstance(action, TripleClickAction):
        return 3
    if isinstance(action, DoubleClickAction):
        return 2
    if isinstance(action, ClickAction):
        return 1
    if isinstance(action, TypeAction):
        return 1 + math.ceil(len(action.text) / _WORK_UNIT)
    if isinstance(action, ScrollAction):
        return 1 + math.ceil(action.amount / _WORK_UNIT)
    if isinstance(action, DragAction):
        return 1 + math.ceil(len(action.path or []) / _WORK_UNIT)
    if isinstance(action, HotkeyAction):
        return hotkey_input_token_cost(len(action.keys))
    if isinstance(action, HoldKeyAction):
        nested = sum(input_token_cost(parse_action(item)) for item in action.actions or [])
        return 1 + nested
    return 1


def batch_input_token_cost(actions: list[ComputerAction]) -> int:
    return sum(input_token_cost(action) for action in actions)


def typing_input_token_cost(text: str) -> int:
    return 1 + math.ceil(len(text) / _WORK_UNIT)


def scroll_input_token_cost(amount: int) -> int:
    return 1 + math.ceil(amount / _WORK_UNIT)


def drag_input_token_cost(path_points: int) -> int:
    return 1 + math.ceil(path_points / _WORK_UNIT)


def repeated_click_input_token_cost(*, count: int) -> int:
    return max(1, count)


def hotkey_input_token_cost(key_count: int) -> int:
    return max(1, math.ceil(key_count / 4))
