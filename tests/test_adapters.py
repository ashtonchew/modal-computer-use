from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from modal_computer_use.adapters.anthropic import (
    AnthropicAdapter,
    anthropic_screenshot_metadata,
    anthropic_tool_result,
    get_tool_version,
)
from modal_computer_use.adapters.generic import ActionExecutor
from modal_computer_use.adapters.openai import (
    OpenAIAdapter,
    openai_computer_call_output,
    openai_screenshot_metadata,
)
from modal_computer_use.adapters.provenance import PROVIDER_ACTION_METADATA_KEY
from modal_computer_use.errors import ActionValidationError, UnsupportedActionError
from modal_computer_use.models import (
    ActionBatchResult,
    ActionDecision,
    ActionItemResult,
    ActionResult,
    CoordinateSpace,
    Screenshot,
)

FIXTURES = Path(__file__).parent / "fixtures"


class RecordingActions:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []

    def apply(self, action: Any, *, source: str = "sdk") -> ActionResult:
        dumped = action.model_dump(mode="json")
        dumped["source"] = source
        self.applied.append(dumped)
        return ActionResult(ok=True, output={"type": action.type})

    def run(
        self,
        actions: list[Any],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        source: str = "sdk",
    ) -> ActionBatchResult:
        dumped = [action.model_dump(mode="json") for action in actions]
        self.runs.append(
            {
                "actions": dumped,
                "continue_on_error": continue_on_error,
                "screenshot_after": screenshot_after,
                "source": source,
            }
        )
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(index=index, type=action["type"], ok=True)
                for index, action in enumerate(dumped)
            ],
        )


class RecordingComputer:
    def __init__(self) -> None:
        self.actions = RecordingActions()


def _fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text())


def _tiny_screenshot() -> Screenshot:
    return Screenshot(
        format="png",
        width=1,
        height=1,
        size_bytes=68,
        data_base64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC",
        sha256="synthetic",
        artifact_uri="artifact://screenshots/tiny.png",
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=1,
            desktop_height=1,
        ),
    )


def test_openai_adapter_fixture_matrix() -> None:
    computer = RecordingComputer()
    adapter = OpenAIAdapter(computer)

    adapter.apply_many(_fixture("openai_actions.json"))

    run = computer.actions.runs[0]
    assert run["source"] == "openai-adapter"
    assert [action["type"] for action in run["actions"]] == [
        "click",
        "double_click",
        "scroll",
        "type",
        "wait",
        "keypress",
        "keypress",
        "drag",
        "move",
        "screenshot",
    ]
    assert run["actions"][0] == {
        "type": "click",
        "metadata": {
            PROVIDER_ACTION_METADATA_KEY: {
                "type": "click",
                "x": 100,
                "y": 200,
                "button": "left",
                "keys": ["shift"],
            }
        },
        "call_id": None,
        "sequence": None,
        "timeout_ms": None,
        "x": 100,
        "y": 200,
        "button": "left",
        "modifiers": ["shift"],
    }
    assert run["actions"][2]["direction"] == "down"
    assert run["actions"][2]["amount"] == 5
    assert [run["actions"][5]["key"], run["actions"][6]["key"]] == ["ctrl", "c"]
    assert run["actions"][7]["modifiers"] == ["shift"]
    assert run["actions"][7]["path"] == [{"x": 1, "y": 2}, {"x": 3, "y": 4}]


def test_openai_adapter_accepts_explicit_compatibility_shapes() -> None:
    adapter = OpenAIAdapter(RecordingComputer())

    drag = adapter.normalize(
        {
            "type": "drag",
            "start_x": 1,
            "start_y": 2,
            "end_x": 3,
            "end_y": 4,
        }
    )
    tuple_path = adapter.normalize({"type": "drag", "path": [[1, 2], [3, 4]]})
    wait = adapter.normalize({"type": "wait", "duration_ms": 100})

    assert drag["start_x"] == 1
    assert drag["end_y"] == 4
    assert tuple_path["path"] == [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
    assert wait["duration_ms"] == 100


def test_provider_apply_many_forwards_batch_options() -> None:
    openai_computer = RecordingComputer()
    OpenAIAdapter(openai_computer).apply_many(
        [{"type": "move", "x": 1, "y": 2}],
        continue_on_error=True,
        screenshot_after=True,
    )

    anthropic_computer = RecordingComputer()
    AnthropicAdapter(anthropic_computer, tool_version="computer_20241022").apply_many(
        [{"action": "mouse_move", "coordinate": [3, 4]}],
        continue_on_error=True,
        screenshot_after=True,
    )

    assert openai_computer.actions.runs[0]["continue_on_error"] is True
    assert openai_computer.actions.runs[0]["screenshot_after"] is True
    assert anthropic_computer.actions.runs[0]["continue_on_error"] is True
    assert anthropic_computer.actions.runs[0]["screenshot_after"] is True


def test_provider_apply_forwards_source_to_single_action_calls() -> None:
    openai_computer = RecordingComputer()
    OpenAIAdapter(openai_computer).apply({"type": "move", "x": 1, "y": 2})

    anthropic_computer = RecordingComputer()
    AnthropicAdapter(anthropic_computer, tool_version="computer_20241022").apply(
        {"action": "mouse_move", "coordinate": [3, 4]}
    )

    assert openai_computer.actions.applied[0]["source"] == "openai-adapter"
    assert anthropic_computer.actions.applied[0]["source"] == "anthropic-adapter"


def test_openai_unknown_action_fails_closed() -> None:
    adapter = OpenAIAdapter(RecordingComputer())
    with pytest.raises(UnsupportedActionError):
        adapter.normalize({"type": "future"})
    with pytest.raises(UnsupportedActionError):
        adapter.normalize({"type": "future", "new_provider_field": True})


def test_openai_ga_scroll_buttons_and_modifier_fields() -> None:
    computer = RecordingComputer()
    adapter = OpenAIAdapter(computer)

    adapter.apply_many(
        [
            {"type": "click", "button": "wheel", "x": 1, "y": 2, "keys": ["CTRL"]},
            {"type": "click", "button": "back", "x": 3, "y": 4},
            {
                "type": "scroll",
                "x": 5,
                "y": 6,
                "scroll_x": -250,
                "scroll_y": 600,
                "keys": ["SHIFT"],
            },
        ]
    )

    actions = computer.actions.runs[0]["actions"]
    assert actions[0]["button"] == "middle"
    assert actions[0]["modifiers"] == ["CTRL"]
    assert actions[1]["button"] == "back"
    assert actions[2]["type"] == "hold_key"
    assert actions[2]["actions"][0]["direction"] == "down"
    assert actions[2]["actions"][0]["amount"] == 6
    assert actions[3]["actions"][0]["direction"] == "left"
    assert actions[3]["actions"][0]["amount"] == 2


def test_openai_multi_native_actions_require_batch_path() -> None:
    adapter = OpenAIAdapter(RecordingComputer())

    with pytest.raises(ActionValidationError, match="require apply_many"):
        adapter.normalize({"type": "keypress", "keys": ["CTRL", "C"]})
    with pytest.raises(ActionValidationError, match="require apply_many"):
        adapter.normalize({"type": "scroll", "scroll_x": 100, "scroll_y": 100})


@pytest.mark.parametrize(
    "action",
    [
        {"type": "keypress", "keys": ["CTRL", "C"], "future_field": True},
        {
            "type": "scroll",
            "x": 1,
            "y": 2,
            "scroll_y": 100,
            "future_field": True,
        },
    ],
)
def test_openai_expanded_actions_reject_unknown_fields(
    action: dict[str, Any],
) -> None:
    adapter = OpenAIAdapter(RecordingComputer())

    with pytest.raises(ActionValidationError, match="unknown fields"):
        adapter.apply_many([action])


def test_openai_allow_unknown_is_explicit_safe_noop() -> None:
    adapter = OpenAIAdapter(RecordingComputer(), allow_unknown=True)
    normalized = adapter.normalize({"type": "future", "raw": "ignored"})
    assert normalized == {
        "type": "wait",
        "duration_ms": 0,
        "metadata": {PROVIDER_ACTION_METADATA_KEY: {"type": "future", "raw": "ignored"}},
    }


def test_openai_allow_unknown_does_not_bypass_known_action_validation() -> None:
    adapter = OpenAIAdapter(RecordingComputer(), allow_unknown=True)
    with pytest.raises(ActionValidationError):
        adapter.normalize({"type": "click", "x": 1, "y": 2, "future_field": True})


def test_openai_provider_provenance_redacts_sensitive_text() -> None:
    adapter = OpenAIAdapter(RecordingComputer())
    normalized = adapter.normalize({"type": "type", "text": "secret typed value"})
    assert normalized["metadata"][PROVIDER_ACTION_METADATA_KEY] == {
        "type": "type",
        "text": {"redacted": True, "length": 18},
    }


def test_openai_provider_provenance_redacts_sensitive_metadata() -> None:
    adapter = OpenAIAdapter(RecordingComputer())
    normalized = adapter.normalize(
        {
            "type": "click",
            "x": 1,
            "y": 2,
            "metadata": {"secret": "provider-secret"},
        }
    )

    provider_action = normalized["metadata"][PROVIDER_ACTION_METADATA_KEY]
    assert provider_action["metadata"]["secret"] == {"redacted": True, "length": 15}
    assert "provider-secret" not in json.dumps(normalized)


def test_provider_provenance_redacts_secret_patterns_in_safe_named_fields() -> None:
    adapter = OpenAIAdapter(RecordingComputer(), allow_unknown=True)

    normalized = adapter.normalize(
        {
            "type": "future",
            "note": "Bearer provider-secret artifact://screenshots/private.png",
        }
    )

    serialized = json.dumps(normalized)
    provider_action = normalized["metadata"][PROVIDER_ACTION_METADATA_KEY]
    assert provider_action["note"] == "Bearer [redacted] [redacted]"
    assert "provider-secret" not in serialized
    assert "artifact://screenshots/private.png" not in serialized


def test_openai_computer_call_output_uses_native_screenshot_without_metadata_loss() -> None:
    screenshot = _tiny_screenshot()

    output = openai_computer_call_output(
        screenshot,
        call_id="call_123",
    )
    metadata = openai_screenshot_metadata(screenshot)

    assert output == {
        "type": "computer_call_output",
        "call_id": "call_123",
        "output": {
            "type": "computer_screenshot",
            "image_url": f"data:image/png;base64,{screenshot.data_base64}",
            "detail": "original",
        },
    }
    assert metadata["width"] == 1
    assert metadata["coordinate_space"]["desktop_width"] == 1
    assert metadata["artifact_uri"] == "artifact://screenshots/tiny.png"
    assert "data_base64" not in metadata
    assert "bytes" not in metadata


def test_openai_preserves_native_metadata_and_rejects_unknown_fields() -> None:
    adapter = OpenAIAdapter(RecordingComputer())
    normalized = adapter.normalize(
        {
            "type": "click",
            "x": 1,
            "y": 2,
            "call_id": "call_1",
            "sequence": 7,
            "metadata": {"provider": "openai"},
            "timeout_ms": 1000,
        }
    )
    assert normalized["call_id"] == "call_1"
    assert normalized["sequence"] == 7
    assert normalized["metadata"]["provider"] == "openai"
    assert normalized["metadata"][PROVIDER_ACTION_METADATA_KEY]["type"] == "click"
    assert normalized["timeout_ms"] == 1000
    with pytest.raises(ActionValidationError):
        adapter.normalize({"type": "click", "x": 1, "y": 2, "future_field": True})


def test_anthropic_versions() -> None:
    assert get_tool_version("computer_20241022").supports_enhanced_actions is False
    assert get_tool_version("computer_20251124").supports_zoom is True


def test_anthropic_defaults_to_current_tool_with_zoom_opt_in() -> None:
    adapter = AnthropicAdapter(RecordingComputer())

    assert adapter.version.name == "computer_20251124"
    assert adapter.beta_header == "computer-use-2025-11-24"
    assert adapter.enable_zoom is False
    with pytest.raises(UnsupportedActionError, match="not enabled"):
        adapter.normalize({"action": "zoom", "region": [0, 0, 10, 10]})


def test_anthropic_20241022_fixture_matrix() -> None:
    computer = RecordingComputer()
    adapter = AnthropicAdapter(computer, tool_version="computer_20241022")

    adapter.apply_many(_fixture("anthropic_20241022.json"))

    run = computer.actions.runs[0]
    assert run["source"] == "anthropic-adapter"
    assert [action["type"] for action in run["actions"]] == [
        "move",
        "drag",
        "drag",
        "hotkey",
        "type",
        "click",
        "click",
        "click",
        "double_click",
        "click",
        "screenshot",
        "cursor_position",
    ]
    assert run["actions"][1]["start_x"] is None
    assert run["actions"][1]["end_x"] == 300
    assert run["actions"][2]["start_x"] == 10
    assert run["actions"][2]["end_y"] == 40
    assert run["actions"][5]["x"] is None
    assert run["actions"][5]["button"] == "left"
    assert run["actions"][6]["x"] == 50
    assert run["actions"][6]["y"] == 60
    assert run["actions"][7]["button"] == "right"
    assert run["actions"][9]["button"] == "middle"


def test_anthropic_20250124_enhanced_fixture_matrix() -> None:
    computer = RecordingComputer()
    adapter = AnthropicAdapter(computer, tool_version="computer_20250124")

    adapter.apply_many(_fixture("anthropic_20250124.json"))

    actions = computer.actions.runs[0]["actions"]
    assert [action["type"] for action in actions] == [
        "triple_click",
        "mouse_down",
        "mouse_up",
        "scroll",
        "hold_key",
        "wait",
    ]
    assert actions[3]["direction"] == "down"
    assert actions[3]["amount"] == 3
    assert actions[4]["key"] == "shift"
    assert actions[4]["duration_ms"] == 100


def test_anthropic_current_modifiers_and_duration_validation() -> None:
    adapter = AnthropicAdapter(RecordingComputer())

    click = adapter.normalize({"action": "left_click", "key": "shift"})
    scroll = adapter.normalize(
        {
            "action": "scroll",
            "scroll_direction": "down",
            "scroll_amount": 3,
            "text": "shift",
        }
    )

    assert click["modifiers"] == ["shift"]
    assert scroll["type"] == "hold_key"
    assert scroll["actions"][0]["direction"] == "down"
    with pytest.raises(ActionValidationError, match="between 0 and 100"):
        adapter.normalize({"action": "wait", "duration": 101})


@pytest.mark.parametrize("duration", [60, 61, 100])
def test_anthropic_hold_key_accepts_current_duration_boundary(
    duration: int,
) -> None:
    computer = RecordingComputer()

    AnthropicAdapter(computer).apply(
        {"action": "hold_key", "text": "shift", "duration": duration}
    )

    assert computer.actions.applied[0]["duration_ms"] == duration * 1000


def test_anthropic_hold_key_normalizes_nested_actions() -> None:
    adapter = AnthropicAdapter(RecordingComputer(), tool_version="computer_20250124")

    action = adapter.normalize(
        {
            "action": "hold_key",
            "key": "shift",
            "duration_ms": 100,
            "actions": [
                {"action": "mouse_move", "coordinate": [10, 20]},
                {"action": "left_click"},
            ],
        }
    )

    assert action["type"] == "hold_key"
    assert action["key"] == "shift"
    assert [nested["type"] for nested in action["actions"]] == ["move", "click"]
    assert action["actions"][0]["x"] == 10
    assert action["actions"][0]["y"] == 20


def test_anthropic_hold_key_provider_provenance_redacts_nested_typed_text() -> None:
    adapter = AnthropicAdapter(RecordingComputer(), tool_version="computer_20250124")

    action = adapter.normalize(
        {
            "action": "hold_key",
            "key": "shift",
            "duration_ms": 100,
            "actions": [{"action": "type", "text": "nested secret"}],
        }
    )

    provider_action = action["metadata"][PROVIDER_ACTION_METADATA_KEY]
    assert provider_action["actions"][0]["text"] == {
        "redacted": True,
        "length": 13,
    }


def test_anthropic_20251124_zoom_fixture_matrix() -> None:
    computer = RecordingComputer()
    adapter = AnthropicAdapter(
        computer,
        tool_version="computer_20251124",
        enable_zoom=True,
    )

    adapter.apply_many(_fixture("anthropic_20251124.json"))

    action = computer.actions.runs[0]["actions"][0]
    assert action["type"] == "zoom"
    assert action["region"] == {"x": 0, "y": 0, "width": 100, "height": 100}
    assert action["scale"] == 1.0


def test_anthropic_enhanced_actions_are_version_gated() -> None:
    adapter = AnthropicAdapter(RecordingComputer(), tool_version="computer_20241022")
    for action in _fixture("anthropic_20250124.json"):
        with pytest.raises(UnsupportedActionError):
            adapter.normalize(action)


def test_anthropic_zoom_requires_tool_support_even_if_enabled() -> None:
    adapter = AnthropicAdapter(
        RecordingComputer(),
        tool_version="computer_20250124",
        enable_zoom=True,
    )
    with pytest.raises(UnsupportedActionError):
        adapter.normalize({"action": "zoom", "region": {"x": 0, "y": 0, "width": 1, "height": 1}})


def test_anthropic_zoom_requires_region_as_structured_validation_error() -> None:
    adapter = AnthropicAdapter(
        RecordingComputer(),
        tool_version="computer_20251124",
        enable_zoom=True,
    )

    with pytest.raises(ActionValidationError, match="zoom action requires region"):
        adapter.normalize({"action": "zoom"})


def test_anthropic_unknown_action_fails_closed() -> None:
    adapter = AnthropicAdapter(RecordingComputer(), tool_version="computer_20251124")
    with pytest.raises(UnsupportedActionError):
        adapter.normalize({"action": "future_action"})
    with pytest.raises(UnsupportedActionError):
        adapter.normalize({"action": "future_action", "new_provider_field": True})


def test_anthropic_allow_unknown_is_explicit_safe_noop() -> None:
    adapter = AnthropicAdapter(
        RecordingComputer(), tool_version="computer_20251124", allow_unknown=True
    )

    normalized = adapter.normalize(
        {"action": "future_action", "stderr": "Bearer provider-secret"}
    )

    assert normalized == {
        "type": "wait",
        "duration_ms": 0,
        "metadata": {
            PROVIDER_ACTION_METADATA_KEY: {
                "action": "future_action",
                "stderr": {"redacted": True, "length": 22},
            },
            "provider_action_redactions": ["stderr"],
        },
    }


def test_anthropic_allow_unknown_does_not_bypass_known_action_validation() -> None:
    adapter = AnthropicAdapter(
        RecordingComputer(), tool_version="computer_20241022", allow_unknown=True
    )
    with pytest.raises(ActionValidationError):
        adapter.normalize(
            {"action": "mouse_move", "coordinate": [1, 2], "future_field": True}
        )


def test_anthropic_preserves_native_metadata_and_rejects_unknown_fields() -> None:
    adapter = AnthropicAdapter(RecordingComputer(), tool_version="computer_20241022")
    normalized = adapter.normalize(
        {
            "action": "mouse_move",
            "coordinate": [1, 2],
            "call_id": "call_1",
            "sequence": 7,
            "metadata": {"provider": "anthropic"},
            "timeout_ms": 1000,
        }
    )
    assert normalized["call_id"] == "call_1"
    assert normalized["sequence"] == 7
    assert normalized["metadata"]["provider"] == "anthropic"
    assert normalized["metadata"][PROVIDER_ACTION_METADATA_KEY]["action"] == "mouse_move"
    assert normalized["timeout_ms"] == 1000
    with pytest.raises(ActionValidationError):
        adapter.normalize({"action": "mouse_move", "coordinate": [1, 2], "future_field": True})


def test_anthropic_provider_provenance_redacts_sensitive_text() -> None:
    adapter = AnthropicAdapter(RecordingComputer(), tool_version="computer_20241022")
    normalized = adapter.normalize({"action": "type", "text": "secret typed value"})
    assert normalized["metadata"][PROVIDER_ACTION_METADATA_KEY] == {
        "action": "type",
        "text": {"redacted": True, "length": 18},
    }

    key_normalized = adapter.normalize({"action": "key", "text": "ctrl+c"})
    assert key_normalized["metadata"][PROVIDER_ACTION_METADATA_KEY] == {
        "action": "key",
        "text": "ctrl+c",
    }


def test_anthropic_provider_provenance_redacts_sensitive_metadata() -> None:
    adapter = AnthropicAdapter(RecordingComputer(), tool_version="computer_20241022")
    normalized = adapter.normalize(
        {
            "action": "mouse_move",
            "coordinate": [1, 2],
            "metadata": {"stderr": "Bearer provider-secret"},
        }
    )

    provider_action = normalized["metadata"][PROVIDER_ACTION_METADATA_KEY]
    assert provider_action["metadata"]["stderr"] == {"redacted": True, "length": 22}
    assert "provider-secret" not in json.dumps(normalized)


def test_generic_executor_rejects_unknown_native_actions_even_when_compat_flag_is_set() -> None:
    computer = RecordingComputer()
    with pytest.raises(ActionValidationError):
        ActionExecutor(computer, allow_unknown=True).apply(
            {"type": "future_action", "payload": {"secret": "provider-secret"}}
        )

    assert computer.actions.applied == []


def test_anthropic_tool_result_builds_image_block_and_safe_metadata() -> None:
    screenshot = _tiny_screenshot()

    result = anthropic_tool_result(tool_use_id="toolu_123", result=screenshot)
    metadata = anthropic_screenshot_metadata(screenshot)

    assert result == {
        "type": "tool_result",
        "tool_use_id": "toolu_123",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot.data_base64,
                },
            }
        ],
    }
    assert metadata["sha256"] == "synthetic"
    assert metadata["coordinate_space"]["image_height"] == 1
    assert "data_base64" not in metadata
    assert "bytes" not in metadata


def test_anthropic_tool_result_summarizes_action_result_without_raw_output() -> None:
    result = anthropic_tool_result(
        tool_use_id="toolu_123",
        result=ActionResult(
            ok=False,
            message="denied",
            elapsed_ms=12.5,
            output={"text": "sensitive typed payload"},
        ),
    )

    assert result == {
        "type": "tool_result",
        "tool_use_id": "toolu_123",
        "content": [
            {
                "type": "text",
                "text": '{"elapsed_ms":12.5,"message":"denied","ok":false}',
            }
        ],
        "is_error": True,
    }


def test_action_executor_applies_coordinate_space_to_all_point_shapes() -> None:
    computer = RecordingComputer()
    executor = ActionExecutor(
        computer,
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=200,
            desktop_height=100,
            image_width=100,
            image_height=50,
        ),
    )

    executor.apply_many(
        [
            {"type": "click", "x": 10, "y": 11},
            {"type": "move", "x": 12, "y": 13},
            {"type": "scroll", "x": 14, "y": 15, "direction": "down", "amount": 1},
            {"type": "drag", "start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4},
            {"type": "drag", "path": [{"x": 5, "y": 6}, {"x": 7, "y": 8}]},
            {"type": "zoom", "region": {"x": 10, "y": 10, "width": 20, "height": 20}},
        ]
    )

    actions = computer.actions.runs[0]["actions"]
    assert (actions[0]["x"], actions[0]["y"]) == (20, 22)
    assert (actions[1]["x"], actions[1]["y"]) == (24, 26)
    assert (actions[2]["x"], actions[2]["y"]) == (28, 30)
    assert (actions[3]["start_x"], actions[3]["start_y"]) == (2, 4)
    assert (actions[3]["end_x"], actions[3]["end_y"]) == (6, 8)
    assert actions[4]["path"] == [{"x": 10, "y": 12}, {"x": 14, "y": 16}]
    assert actions[5]["region"] == {"x": 20, "y": 20, "width": 40, "height": 40}


def test_action_executor_applies_coordinate_space_to_nested_hold_actions() -> None:
    computer = RecordingComputer()
    executor = ActionExecutor(
        computer,
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=200,
            desktop_height=100,
            image_width=100,
            image_height=50,
        ),
    )

    executor.apply_many(
        [
            {
                "type": "hold_key",
                "key": "shift",
                "actions": [
                    {"type": "move", "x": 10, "y": 11},
                    {"type": "drag", "path": [{"x": 5, "y": 6}, {"x": 7, "y": 8}]},
                    {"type": "zoom", "region": {"x": 10, "y": 10, "width": 20, "height": 20}},
                ],
            }
        ]
    )

    nested = computer.actions.runs[0]["actions"][0]["actions"]
    assert (nested[0]["x"], nested[0]["y"]) == (20, 22)
    assert nested[1]["path"] == [{"x": 10, "y": 12}, {"x": 14, "y": 16}]
    assert nested[2]["region"] == {"x": 20, "y": 20, "width": 40, "height": 40}


def test_action_executor_policy_sees_transformed_action_and_denies_before_execution() -> None:
    computer = RecordingComputer()
    seen: list[dict[str, Any]] = []

    def deny_large_x(action: Any, _: dict[str, Any]) -> ActionDecision:
        seen.append(action.model_dump(mode="json"))
        if getattr(action, "x", 0) >= 20:
            return ActionDecision(decision="deny", reason="x too large")
        return ActionDecision(decision="allow")

    executor = ActionExecutor(
        computer,
        before_action=deny_large_x,
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=200,
            desktop_height=100,
            image_width=100,
            image_height=50,
        ),
    )

    with pytest.raises(UnsupportedActionError, match="x too large"):
        executor.apply_many(
            [
                {"type": "move", "x": 5, "y": 5},
                {"type": "click", "x": 10, "y": 10},
            ]
        )

    assert seen[-1]["x"] == 20
    assert computer.actions.runs == []


def test_action_executor_policy_sees_nested_hold_actions_before_execution() -> None:
    computer = RecordingComputer()
    seen: list[dict[str, Any]] = []

    def deny_nested_move(action: Any, _: dict[str, Any]) -> ActionDecision:
        seen.append(action.model_dump(mode="json"))
        if action.type == "move":
            return ActionDecision(decision="deny", reason="nested move denied")
        return ActionDecision(decision="allow")

    executor = ActionExecutor(computer, before_action=deny_nested_move)

    with pytest.raises(UnsupportedActionError, match="nested move denied"):
        executor.apply_many(
            [
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "move", "x": 10, "y": 20}],
                }
            ]
        )

    assert computer.actions.runs == []
    assert [action["type"] for action in seen] == ["hold_key", "move"]
