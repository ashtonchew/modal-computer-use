from __future__ import annotations

from typing import Any, TypedDict


class AnthropicComputerAction(TypedDict, total=False):
    action: str
    coordinate: list[int]
    start_coordinate: list[int]
    text: str
    key: str
    direction: str
    amount: int
    duration_ms: int
    region: dict[str, Any]
