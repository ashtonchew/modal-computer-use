from __future__ import annotations

import pytest

from modal_computer_use.adapters.anthropic import AnthropicAdapter, get_tool_version
from modal_computer_use.adapters.openai import OpenAIAdapter
from modal_computer_use.errors import UnsupportedActionError


def test_openai_adapter_mapping(computer) -> None:
    adapter = OpenAIAdapter(computer)
    assert adapter.normalize({"type": "click", "x": 1, "y": 2})["type"] == "click"
    assert adapter.normalize({"type": "double_click", "x": 1, "y": 2})["type"] == "double_click"
    assert adapter.normalize({"type": "scroll", "dy": -3})["direction"] == "up"
    assert adapter.normalize({"type": "drag", "path": [[1, 2], [3, 4]]})["path"][1]["x"] == 3
    with pytest.raises(UnsupportedActionError):
        adapter.normalize({"type": "future"})


def test_anthropic_versions() -> None:
    assert get_tool_version("computer_20241022").supports_enhanced_actions is False
    assert get_tool_version("computer_20251124").supports_zoom is True


def test_anthropic_adapter_mapping(computer) -> None:
    adapter = AnthropicAdapter(computer, tool_version="computer_20251124")
    assert adapter.normalize({"action": "mouse_move", "coordinate": [5, 6]}) == {
        "type": "move",
        "x": 5,
        "y": 6,
    }
    assert adapter.normalize({"action": "left_click"}) == {"type": "click", "button": "left"}
    assert adapter.normalize({"action": "left_click_drag", "coordinate": [9, 10]}) == {
        "type": "drag",
        "end_x": 9,
        "end_y": 10,
    }
    assert adapter.normalize({"action": "key", "text": "ctrl+c"}) == {
        "type": "hotkey",
        "keys": ["ctrl", "c"],
    }
    assert (
        adapter.normalize(
            {"action": "zoom", "region": {"x": 0, "y": 0, "width": 10, "height": 10}}
        )["type"]
        == "zoom"
    )
    with pytest.raises(UnsupportedActionError):
        adapter.normalize({"action": "future_action"})
