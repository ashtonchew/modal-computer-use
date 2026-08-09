from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import (
    AsyncQueuedProviderResponses,
    AsyncRecordingComputer,
    RecordingComputer,
    load_example,
    tiny_screenshot,
)

from modal_computer_use.models import (
    ActionBatchResult,
    ActionItemResult,
)


def _client(responses: list[Any]) -> tuple[Any, AsyncQueuedProviderResponses]:
    create = AsyncQueuedProviderResponses(responses)
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create.create)))
    return client, create


class _AsyncActionsBridge:
    def __init__(self, computer: RecordingComputer) -> None:
        self._actions = computer.actions

    async def apply(self, action: Any, **kwargs: Any) -> Any:
        return self._actions.apply(action, **kwargs)

    async def run(self, actions: list[Any], **kwargs: Any) -> Any:
        return self._actions.run(actions, **kwargs)


class _AsyncScreenshotsBridge:
    def __init__(self, computer: RecordingComputer) -> None:
        self._screenshots = computer.screenshots

    async def full(self) -> Any:
        return self._screenshots.full()


def _async_computer(computer: RecordingComputer) -> Any:
    async def step(actions: list[Any], **kwargs: Any) -> Any:
        return computer.step(actions, **kwargs)

    return SimpleNamespace(actions=_AsyncActionsBridge(computer), step=step)


def _response(*tool_uses: Any) -> Any:
    return SimpleNamespace(stop_reason="tool_use", content=list(tool_uses))


def _terminal_response() -> Any:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="done")],
    )


def _tool_use(
    tool_use_id: str,
    *,
    name: str,
    tool_input: dict[str, Any],
) -> Any:
    return SimpleNamespace(
        type="tool_use",
        id=tool_use_id,
        name=name,
        input=tool_input,
    )


@pytest.mark.asyncio
async def test_placed_anthropic_trajectory_borrows_once_for_the_full_model_loop() -> None:
    example = load_example("anthropic_message_server.py")
    events: list[str] = []
    computer = AsyncRecordingComputer()
    screenshot = tiny_screenshot().model_copy(
        update={"bytes": b"png-bytes", "data_base64": None}
    )

    original_step = computer.step

    async def step(actions: list[Any], **kwargs: Any) -> Any:
        assert handle.active
        events.append("step")
        result = await original_step(actions, **kwargs)
        return SimpleNamespace(actions=result.actions, screenshot=screenshot, timing=None)

    computer.step = step
    responses = AsyncQueuedProviderResponses(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={
                        "actions": [
                            {"action": "mouse_move", "coordinate": [10, 20]},
                            {"action": "left_click"},
                        ]
                    },
                )
            ),
            _terminal_response(),
        ]
    )

    async def create(**kwargs: Any) -> Any:
        assert handle.active
        events.append("model")
        return await responses.create(**kwargs)

    client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=create))
    )

    class Handle:
        def __init__(self) -> None:
            self.active = False
            self.borrow_calls: list[tuple[str, str]] = []

        @asynccontextmanager
        async def borrow_async(
            self, *, run_id: str, function_region: str
        ) -> AsyncIterator[Any]:
            self.borrow_calls.append((run_id, function_region))
            self.active = True
            events.append("borrow-enter")
            try:
                yield computer
            finally:
                self.active = False
                events.append("borrow-exit")

    handle = Handle()
    response = await example.run_anthropic_trajectory_body(
        handle,
        "Inspect the page",
        run_id="run-123",
        function_region="us-west-2",
        client=client,
        max_turns=2,
    )

    assert response.stop_reason == "end_turn"
    assert handle.borrow_calls == [("run-123", "us-west-2")]
    assert [[action.type for action in batch] for batch in computer.steps] == [["move", "click"]]
    assert computer.step_kwargs == [
        {
            "continue_on_error": False,
            "call_id": "tool_batch",
            "screenshot_options": None,
            "max_action_timeout_ms": 30_000,
        }
    ]
    assert events == [
        "borrow-enter",
        "model",
        "step",
        "model",
        "borrow-exit",
    ]
    tool_result = responses.calls[1]["messages"][2]["content"][0]
    image = next(block for block in tool_result["content"] if block["type"] == "image")
    assert image["source"]["data"] == screenshot.to_base64()


@pytest.mark.asyncio
async def test_anthropic_stops_after_step_result_loss() -> None:
    example = load_example("anthropic_message_server.py")
    computer = AsyncRecordingComputer()

    async def fail_step(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("https://private.invalid screenshot-secret")

    computer.step = fail_step
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={
                        "actions": [
                            {"action": "left_click", "coordinate": [10, 20]},
                        ]
                    },
                )
            )
        ]
    )

    with pytest.raises(
        RuntimeError,
        match="Anthropic computer action batch failed: TimeoutError",
    ) as raised:
        await example.run_anthropic_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            display_width_px=1280,
            display_height_px=800,
        )

    assert len(create.calls) == 1
    assert computer.steps == []
    assert "private.invalid" not in str(raised.value)
    assert "screenshot-secret" not in str(raised.value)


def _run(
    example: Any,
    *,
    client: Any,
    computer: RecordingComputer,
    **kwargs: Any,
) -> Any:
    return asyncio.run(
        example.run_anthropic_computer_loop(
            client=client,
            computer=_async_computer(computer),
            task="Inspect the page",
            display_width_px=1280,
            display_height_px=800,
            **kwargs,
        )
    )


def _schema_action_names(tool: dict[str, Any]) -> set[str]:
    action_schema = tool["input_schema"]["$defs"]["computer_action"]
    return {variant["properties"]["action"]["const"] for variant in action_schema["oneOf"]}


def _schema_action_variant(tool: dict[str, Any], action_name: str) -> dict[str, Any]:
    action_schema = tool["input_schema"]["$defs"]["computer_action"]
    return next(
        variant
        for variant in action_schema["oneOf"]
        if variant["properties"]["action"]["const"] == action_name
    )


def _text_blocks(tool_result: dict[str, Any]) -> list[str]:
    return [block["text"] for block in tool_result["content"] if block["type"] == "text"]


BASE_ACTIONS = {
    "mouse_move",
    "left_click_drag",
    "key",
    "type",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "screenshot",
    "cursor_position",
}
ENHANCED_ACTIONS = {
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "scroll",
    "hold_key",
    "wait",
}


@pytest.mark.parametrize(
    ("tool_version", "enable_zoom", "expected_actions"),
    [
        ("computer_20241022", False, BASE_ACTIONS),
        ("computer_20250124", True, BASE_ACTIONS | ENHANCED_ACTIONS),
        (
            "computer_20251124",
            False,
            BASE_ACTIONS | ENHANCED_ACTIONS,
        ),
        (
            "computer_20251124",
            True,
            BASE_ACTIONS | ENHANCED_ACTIONS | {"zoom"},
        ),
    ],
)
def test_anthropic_batch_schema_tracks_tool_version(
    tool_version: str,
    enable_zoom: bool,
    expected_actions: set[str],
) -> None:
    example = load_example("anthropic_message_server.py")

    tool = example._computer_batch_tool_definition(
        tool_version=tool_version,
        enable_zoom=enable_zoom,
        max_batch_actions=17,
    )

    assert tool["name"] == "computer_batch"
    assert _schema_action_names(tool) == expected_actions
    actions_schema = tool["input_schema"]["properties"]["actions"]
    assert actions_schema["minItems"] == 1
    assert actions_schema["maxItems"] == 17
    assert tool["input_schema"]["additionalProperties"] is False
    if "hold_key" in expected_actions:
        action_schema = tool["input_schema"]["$defs"]["computer_action"]
        hold_key = next(
            variant
            for variant in action_schema["oneOf"]
            if variant["properties"]["action"]["const"] == "hold_key"
        )
        assert hold_key["properties"]["actions"] == {
            "type": "array",
            "items": {"$ref": "#/$defs/computer_action"},
            "minItems": 1,
            "maxItems": 17,
        }


def test_anthropic_batch_schema_exposes_only_canonical_provider_fields() -> None:
    example = load_example("anthropic_message_server.py")
    tool = example._computer_batch_tool_definition(
        tool_version="computer_20251124",
        enable_zoom=True,
        max_batch_actions=50,
    )

    action_schema = tool["input_schema"]["$defs"]["computer_action"]
    properties = {
        property_name
        for variant in action_schema["oneOf"]
        for property_name in variant["properties"]
    }

    assert {"scroll_direction", "scroll_amount", "duration", "text", "key", "region"} <= properties
    assert {
        "direction",
        "amount",
        "duration_ms",
        "scale",
        "timeout_ms",
        "metadata",
        "call_id",
        "sequence",
    }.isdisjoint(properties)


@pytest.mark.parametrize("tool_version", ["computer_20250124", "computer_20251124"])
def test_anthropic_batch_scroll_amount_requires_positive_integer(
    tool_version: str,
) -> None:
    example = load_example("anthropic_message_server.py")
    tool = example._computer_batch_tool_definition(
        tool_version=tool_version,
        enable_zoom=True,
        max_batch_actions=50,
    )

    scroll = _schema_action_variant(tool, "scroll")

    assert scroll["properties"]["scroll_amount"] == {
        "type": "integer",
        "minimum": 1,
    }
    assert {"scroll_direction", "scroll_amount"} <= set(scroll["required"])


@pytest.mark.parametrize(
    ("tool_version", "click_properties"),
    [
        ("computer_20241022", {"action"}),
        ("computer_20250124", {"action", "coordinate", "key"}),
        ("computer_20251124", {"action", "coordinate", "key"}),
    ],
)
def test_anthropic_batch_schema_gates_version_specific_fields(
    tool_version: str,
    click_properties: set[str],
) -> None:
    example = load_example("anthropic_message_server.py")
    tool = example._computer_batch_tool_definition(
        tool_version=tool_version,
        enable_zoom=True,
        max_batch_actions=50,
    )

    click = _schema_action_variant(tool, "left_click")
    assert set(click["properties"]) == click_properties
    assert click["required"] == ["action"]

    drag = _schema_action_variant(tool, "left_click_drag")
    assert set(drag["properties"]) == {
        "action",
        "coordinate",
        "start_coordinate",
    }
    assert drag["required"] == ["action", "coordinate", "start_coordinate"]

    if tool_version != "computer_20241022":
        for action_name in ("left_mouse_down", "left_mouse_up"):
            mouse_button = _schema_action_variant(tool, action_name)
            assert set(mouse_button["properties"]) == {"action"}
            assert mouse_button["required"] == ["action"]


def test_anthropic_batch_schema_fails_closed_for_new_registry_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = load_example("anthropic_message_server.py")
    assert set(example.ANTHROPIC_TOOL_VERSIONS) == set(example._REVIEWED_BATCH_TOOL_VERSIONS)
    monkeypatch.setitem(
        example.ANTHROPIC_TOOL_VERSIONS,
        "computer_20990101",
        object(),
    )

    with pytest.raises(RuntimeError, match="requires review"):
        example._computer_batch_tool_definition(
            tool_version="computer_20251124",
            enable_zoom=True,
            max_batch_actions=50,
        )


def test_anthropic_request_keeps_hosted_and_custom_tools_distinct() -> None:
    example = load_example("anthropic_message_server.py")
    computer = RecordingComputer()
    client, create = _client([_terminal_response()])

    _run(example, client=client, computer=computer)

    hosted_tool, batch_tool = create.calls[0]["tools"]
    assert hosted_tool == {
        "type": "computer_20251124",
        "name": "computer",
        "display_width_px": 1280,
        "display_height_px": 800,
        "enable_zoom": True,
    }
    assert "input_schema" not in hosted_tool
    assert batch_tool["name"] == "computer_batch"
    assert "type" not in batch_tool
    assert "All coordinates refer to the screenshot observed before" in batch_tool["description"]
    assert "intermediate visual replanning" in batch_tool["description"]


@pytest.mark.parametrize(
    ("tool_version", "beta_header", "hosted_zoom"),
    [
        ("computer_20241022", "computer-use-2024-10-22", False),
        ("computer_20250124", "computer-use-2025-01-24", False),
        ("computer_20251124", "computer-use-2025-11-24", True),
    ],
)
def test_anthropic_request_preserves_version_header(
    tool_version: str,
    beta_header: str,
    hosted_zoom: bool,
) -> None:
    example = load_example("anthropic_message_server.py")
    client, create = _client([_terminal_response()])

    _run(
        example,
        client=client,
        computer=RecordingComputer(),
        tool_version=tool_version,
    )

    assert create.calls[0]["betas"] == [beta_header]
    hosted_tool = create.calls[0]["tools"][0]
    assert ("enable_zoom" in hosted_tool) is hosted_zoom


def test_anthropic_computer_batch_executes_one_ordered_batch() -> None:
    example = load_example("anthropic_message_server.py")
    computer = RecordingComputer()
    actions = [
        {"action": "mouse_move", "coordinate": [10, 20]},
        {"action": "left_click"},
        {"action": "type", "text": "hello"},
    ]
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={"actions": actions},
                )
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    assert len(computer.steps) == 1
    assert [action.type for action in computer.steps[0]] == [
        "move",
        "click",
        "type",
    ]
    assert computer.step_kwargs == [
        {
            "continue_on_error": False,
            "call_id": "tool_batch",
            "screenshot_options": None,
            "max_action_timeout_ms": 30_000,
        }
    ]
    tool_result = create.calls[1]["messages"][2]["content"][0]
    assert tool_result["tool_use_id"] == "tool_batch"
    assert _text_blocks(tool_result)[:3] == [
        '[actions[0]:mouse_move] {"ok":true}',
        '[actions[1]:left_click] {"ok":true}',
        '[actions[2]:type] {"ok":true}',
    ]
    assert computer.screenshots.full_calls == 0


def test_anthropic_computer_batch_rejects_zero_scroll_before_dispatch() -> None:
    example = load_example("anthropic_message_server.py")
    computer = RecordingComputer()
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={
                        "actions": [
                            {"action": "mouse_move", "coordinate": [10, 20]},
                            {
                                "action": "scroll",
                                "scroll_direction": "down",
                                "scroll_amount": 0,
                            }
                        ]
                    },
                )
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    assert computer.steps == []
    tool_result = create.calls[1]["messages"][2]["content"][0]
    assert tool_result["is_error"] is True
    assert "[computer_batch] stopped at actions[1] (0 completed, 1 skipped)" in _text_blocks(
        tool_result
    )


def test_anthropic_computer_batch_reports_skipped_actions_without_secrets() -> None:
    example = load_example("anthropic_message_server.py")
    batch_result = ActionBatchResult(
        ok=False,
        results=[
            ActionItemResult(index=0, type="move", ok=True),
            ActionItemResult(
                index=1,
                type="click",
                ok=False,
                error_code="backend_error",
                error="Bearer daemon-secret artifact://screenshots/private.png",
            ),
        ],
        screenshot=tiny_screenshot(),
    )
    computer = RecordingComputer(batch_results=[batch_result])
    actions = [
        {"action": "mouse_move", "coordinate": [10, 20]},
        {"action": "left_click"},
        {"action": "type", "text": "must not run"},
    ]
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={"actions": actions},
                )
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    tool_result = create.calls[1]["messages"][2]["content"][0]
    serialized = repr(tool_result)
    assert tool_result["is_error"] is True
    assert "[computer_batch] stopped at actions[1] (1 completed, 1 skipped)" in _text_blocks(
        tool_result
    )
    assert "daemon-secret" not in serialized
    assert "artifact://screenshots/private.png" not in serialized


def test_anthropic_computer_batch_reports_post_batch_screenshot_failure() -> None:
    example = load_example("anthropic_message_server.py")
    batch_result = ActionBatchResult(
        ok=False,
        results=[
            ActionItemResult(index=0, type="move", ok=True),
            ActionItemResult(
                index=1,
                type="screenshot_after",
                ok=False,
                error_code="capture_failed",
                error="Bearer daemon-secret",
            ),
        ],
    )
    computer = RecordingComputer(batch_results=[batch_result])
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={"actions": [{"action": "mouse_move", "coordinate": [10, 20]}]},
                )
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    tool_result = create.calls[1]["messages"][2]["content"][0]
    serialized = repr(tool_result)
    assert tool_result["is_error"] is True
    assert "[post_batch_screenshot] capture failed (1 completed, 0 skipped)" in _text_blocks(
        tool_result
    )
    assert "actions[1]" not in serialized
    assert "daemon-secret" not in serialized


@pytest.mark.parametrize("action_name", ["screenshot", "zoom"])
def test_anthropic_batch_uses_one_semantic_post_batch_screenshot(
    action_name: str,
) -> None:
    example = load_example("anthropic_message_server.py")
    shot = tiny_screenshot()
    batch_result = ActionBatchResult(
        ok=True,
        results=[
            ActionItemResult(
                index=0,
                type=action_name,
                ok=True,
                output=shot.model_dump(mode="json"),
            )
        ],
        screenshot=shot,
    )
    computer = RecordingComputer(batch_results=[batch_result])
    action = (
        {"action": "screenshot"}
        if action_name == "screenshot"
        else {"action": "zoom", "region": [0, 0, 1, 1]}
    )
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={"actions": [action]},
                )
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    tool_result = create.calls[1]["messages"][2]["content"][0]
    assert sum(block["type"] == "image" for block in tool_result["content"]) == 1
    assert "[post_batch_screenshot]" not in _text_blocks(tool_result)
    assert computer.screenshots.full_calls == 0


def test_anthropic_batch_preserves_nested_native_image() -> None:
    example = load_example("anthropic_message_server.py")
    shot = tiny_screenshot()
    batch_result = ActionBatchResult(
        ok=True,
        results=[
            ActionItemResult(
                index=0,
                type="hold_key",
                ok=True,
                output={
                    "actions": [
                        {
                            "index": 0,
                            "type": "screenshot",
                            "ok": True,
                            "output": shot.model_dump(mode="json"),
                        }
                    ]
                },
            )
        ],
        screenshot=shot,
    )
    computer = RecordingComputer(batch_results=[batch_result])
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={
                        "actions": [
                            {
                                "action": "hold_key",
                                "text": "shift",
                                "duration": 0.1,
                                "actions": [{"action": "screenshot"}],
                            }
                        ]
                    },
                )
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    tool_result = create.calls[1]["messages"][2]["content"][0]
    assert sum(block["type"] == "image" for block in tool_result["content"]) == 1
    assert "[post_batch_screenshot]" not in _text_blocks(tool_result)


def test_anthropic_batch_cursor_position_is_textual() -> None:
    example = load_example("anthropic_message_server.py")
    batch_result = ActionBatchResult(
        ok=True,
        results=[
            ActionItemResult(
                index=0,
                type="cursor_position",
                ok=True,
                output={"x": 12, "y": 34},
            )
        ],
        screenshot=tiny_screenshot(),
    )
    computer = RecordingComputer(batch_results=[batch_result])
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_cursor",
                    name="computer_batch",
                    tool_input={"actions": [{"action": "cursor_position"}]},
                )
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    tool_result = create.calls[1]["messages"][2]["content"][0]
    assert "[actions[0]:cursor_position] X=12,Y=34" in _text_blocks(tool_result)


@pytest.mark.parametrize("action_name", ["screenshot", "zoom"])
def test_anthropic_hosted_tool_uses_the_semantic_screenshot_interface(
    action_name: str,
) -> None:
    example = load_example("anthropic_message_server.py")
    shot = tiny_screenshot().model_copy(update={"data_base64": "bmF0aXZl", "bytes": None})
    computer = RecordingComputer(
        batch_results=[
            ActionBatchResult(
                ok=True,
                results=[
                    ActionItemResult(
                        index=0,
                        type=action_name,
                        ok=True,
                        output=shot.model_dump(mode="json"),
                    )
                ],
            )
        ]
    )
    action = (
        {"action": "screenshot"}
        if action_name == "screenshot"
        else {"action": "zoom", "region": [0, 0, 1, 1]}
    )
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_native",
                    name="computer",
                    tool_input=action,
                )
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    tool_result = create.calls[1]["messages"][2]["content"][0]
    assert tool_result["content"][0]["type"] == "image"
    assert tool_result["content"][0]["source"]["data"] == "bmF0aXZl"
    assert computer.step_kwargs[0]["call_id"] == "tool_native"
    assert computer.screenshots.full_calls == 0


def test_anthropic_hosted_tool_results_preserve_id_order() -> None:
    example = load_example("anthropic_message_server.py")
    computer = RecordingComputer()
    tool_uses = [
        _tool_use(
            "tool_click",
            name="computer",
            tool_input={"action": "left_click", "coordinate": [10, 20]},
        ),
        _tool_use(
            "tool_cursor",
            name="computer",
            tool_input={"action": "cursor_position"},
        ),
    ]
    client, create = _client([_response(*tool_uses), _terminal_response()])

    _run(example, client=client, computer=computer)

    assert [[action.type for action in batch] for batch in computer.steps] == [["click"]]
    assert [action.type for action, _source in computer.actions.applied] == ["cursor_position"]
    results = create.calls[1]["messages"][2]["content"]
    assert create.calls[1]["messages"][1] == {
        "role": "assistant",
        "content": tool_uses,
    }
    assert [result["tool_use_id"] for result in results] == [
        "tool_click",
        "tool_cursor",
    ]
    assert results[1]["content"] == [{"type": "text", "text": '{"message":"X=12,Y=34","ok":true}'}]
    assert len(computer.steps) == 1


def test_anthropic_batch_counts_nested_actions_before_dispatch() -> None:
    example = load_example("anthropic_message_server.py")
    computer = RecordingComputer()
    nested_batch = [
        {
            "action": "hold_key",
            "text": "shift",
            "duration": 0.1,
            "actions": [
                {"action": "mouse_move", "coordinate": [10, 20]},
                {"action": "left_click"},
            ],
        }
    ]
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={"actions": nested_batch},
                )
            ),
            _terminal_response(),
        ]
    )

    _run(
        example,
        client=client,
        computer=computer,
        max_batch_actions=2,
    )

    assert computer.steps == []
    tool_result = create.calls[1]["messages"][2]["content"][0]
    assert tool_result["is_error"] is True
    assert "[computer_batch] stopped at actions[0] (0 completed, 0 skipped)" in _text_blocks(
        tool_result
    )


def test_anthropic_trajectory_limit_counts_nested_actions_before_dispatch() -> None:
    example = load_example("anthropic_message_server.py")
    computer = RecordingComputer()
    client, _create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={
                        "actions": [
                            {
                                "action": "hold_key",
                                "text": "shift",
                                "duration": 0.1,
                                "actions": [
                                    {
                                        "action": "mouse_move",
                                        "coordinate": [10, 20],
                                    },
                                    {"action": "left_click"},
                                ],
                            }
                        ]
                    },
                )
            )
        ]
    )

    with pytest.raises(RuntimeError, match="exceeded 2 trajectory actions"):
        _run(
            example,
            client=client,
            computer=computer,
            max_trajectory_actions=2,
        )

    assert computer.steps == []


def test_anthropic_batch_allocates_remaining_deadline_across_actions_and_frame() -> None:
    example = load_example("anthropic_message_server.py")
    example.monotonic = lambda: 0.0
    computer = RecordingComputer()
    client, _create = _client(
        [
            _response(
                _tool_use(
                    "tool_batch",
                    name="computer_batch",
                    tool_input={
                        "actions": [
                            {"action": "mouse_move", "coordinate": [10, 20]},
                            {"action": "left_click"},
                        ]
                    },
                )
            ),
            _terminal_response(),
        ]
    )

    _run(
        example,
        client=client,
        computer=computer,
        max_elapsed_seconds=1.0,
    )

    assert computer.step_kwargs[0]["max_action_timeout_ms"] == 333


def test_anthropic_request_and_terminal_response_obey_elapsed_deadline() -> None:
    example = load_example("anthropic_message_server.py")
    clock = [0.0]
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        clock[0] = 1.1
        return _terminal_response()

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    example.monotonic = lambda: clock[0]

    with pytest.raises(RuntimeError, match="exceeded 1 seconds"):
        _run(
            example,
            client=client,
            computer=RecordingComputer(),
            max_elapsed_seconds=1.0,
        )

    assert calls[0]["timeout"] == 1.0


def test_anthropic_hosted_failure_is_sanitized_and_later_result_is_returned() -> None:
    example = load_example("anthropic_message_server.py")
    computer = RecordingComputer(
        batch_results=[
            ActionBatchResult(
                ok=False,
                results=[
                    ActionItemResult(
                        index=0,
                        type="click",
                        ok=False,
                        error="Bearer daemon-secret artifact://screenshots/private.png",
                    )
                ],
            )
        ]
    )
    client, create = _client(
        [
            _response(
                _tool_use(
                    "tool_bad",
                    name="computer",
                    tool_input={"action": "left_click", "coordinate": [10, 20]},
                ),
                _tool_use(
                    "tool_cursor",
                    name="computer",
                    tool_input={"action": "cursor_position"},
                ),
            ),
            _terminal_response(),
        ]
    )

    _run(example, client=client, computer=computer)

    results = create.calls[1]["messages"][2]["content"]
    assert [result["tool_use_id"] for result in results] == [
        "tool_bad",
        "tool_cursor",
    ]
    assert results[0]["is_error"] is True
    assert "computer action failed" in results[0]["content"][0]["text"]
    assert "daemon-secret" not in repr(results[0])


def test_anthropic_rejects_invalid_limit_configuration() -> None:
    example = load_example("anthropic_message_server.py")
    common = {
        "client": object(),
        "computer": object(),
        "task": "Inspect the page",
        "display_width_px": 1280,
        "display_height_px": 800,
    }

    with pytest.raises(ValueError, match="max_turns must be at least 1"):
        asyncio.run(example.run_anthropic_computer_loop(**common, max_turns=0))
    with pytest.raises(ValueError, match="max_trajectory_actions must be at least 1"):
        asyncio.run(example.run_anthropic_computer_loop(**common, max_trajectory_actions=0))
    with pytest.raises(ValueError, match="max_batch_actions must be at least 1"):
        asyncio.run(example.run_anthropic_computer_loop(**common, max_batch_actions=0))
    with pytest.raises(ValueError, match="max_elapsed_seconds must be positive"):
        asyncio.run(example.run_anthropic_computer_loop(**common, max_elapsed_seconds=0))
    with pytest.raises(ValueError, match="max_action_timeout_ms must be at least 1"):
        asyncio.run(example.run_anthropic_computer_loop(**common, max_action_timeout_ms=0))


def test_anthropic_stops_before_tools_on_final_allowed_turn() -> None:
    example = load_example("anthropic_message_server.py")
    computer = RecordingComputer()
    client, _create = _client(
        [
            _response(
                _tool_use(
                    "tool_click",
                    name="computer",
                    tool_input={"action": "left_click", "coordinate": [10, 20]},
                )
            )
        ]
    )

    with pytest.raises(RuntimeError, match="exceeded 1 turns"):
        _run(
            example,
            client=client,
            computer=computer,
            max_turns=1,
        )

    assert computer.actions.applied == []
    assert computer.steps == []


@pytest.mark.asyncio
async def test_async_owner_hands_one_versioned_handle_to_the_anthropic_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = load_example("anthropic_message_server.py")
    events: list[str] = []
    owner_create_kwargs: list[dict[str, Any]] = []
    remote_arguments: list[tuple[Any, ...]] = []
    handle = SimpleNamespace(schema_version=2)

    class Owner:
        async def __aenter__(self) -> Any:
            events.append("owner-enter")
            return self

        async def __aexit__(self, *_args: Any) -> None:
            events.append("owner-exit")

        def session_handle(self) -> Any:
            events.append("handle")
            return handle

    class Deployed:
        def __init__(self) -> None:
            self.remote = SimpleNamespace(aio=self.remote_async)

        async def remote_async(self, *args: Any) -> str:
            events.append("remote")
            remote_arguments.append(args)
            return "done"

    def create_owner(**kwargs: Any) -> Owner:
        owner_create_kwargs.append(kwargs)
        return Owner()

    environments: list[str | None] = []

    def from_name(*_args: Any, environment_name: str | None = None) -> Deployed:
        environments.append(environment_name)
        return Deployed()

    monkeypatch.setattr(
        example,
        "modal",
        SimpleNamespace(Function=SimpleNamespace(from_name=from_name)),
    )
    monkeypatch.setattr(example, "app", object())
    monkeypatch.setattr(example, "run_anthropic_trajectory", object())
    monkeypatch.setattr(example.AsyncComputerSandbox, "create", create_owner)

    result = await example.run_example(task="Inspect the page", model="claude-test")

    assert result == "done"
    assert environments == [example.MODAL_ENVIRONMENT]
    assert events == ["owner-enter", "handle", "remote", "owner-exit"]
    assert remote_arguments[0][:3] == (handle, "Inspect the page", "claude-test")
    assert str(remote_arguments[0][3]).startswith("anthropic_trajectory_")
    config = owner_create_kwargs[0]["config"]
    assert config.runtime.modal_environment == example.MODAL_ENVIRONMENT
    assert config.runtime.modal_region == example.MODAL_REGION
    assert owner_create_kwargs[0]["app_name"] == example.APP_NAME


def test_anthropic_example_makes_cost_and_placement_choices_explicit() -> None:
    example = load_example("anthropic_message_server.py")
    config = example.sandbox_configuration()
    resolved = example.resolved_trajectory_configuration()
    assert example.__spec__.origin is not None
    source = Path(example.__spec__.origin).read_text()

    assert config.ingress == "attested-tunnel"
    assert config.desktop.resolution == example.SANDBOX_RESOLUTION
    assert config.resources.profile == example.SANDBOX_RESOURCE_PROFILE
    assert config.resources.cpu == example.SANDBOX_CPU
    assert config.resources.memory_mib == example.SANDBOX_MEMORY_MIB
    assert config.image.source == example.SANDBOX_IMAGE_SOURCE
    assert config.browser.kind == example.SANDBOX_BROWSER_KIND
    assert config.browser.prewarm is example.SANDBOX_BROWSER_PREWARM
    assert config.browser.gpu_mode == example.SANDBOX_BROWSER_GPU_MODE
    assert config.runtime.timeout_seconds == example.SANDBOX_TIMEOUT_SECONDS
    assert config.runtime.idle_timeout_seconds == example.SANDBOX_IDLE_TIMEOUT_SECONDS
    assert config.runtime.readiness_timeout_seconds == example.SANDBOX_READINESS_TIMEOUT_SECONDS
    assert example.FUNCTION_MIN_CONTAINERS == 0
    assert example.FUNCTION_RETRIES == 0
    assert example.SANDBOX_WARM_POOL_CAPACITY == 0
    assert resolved["modal_environment"] == example.MODAL_ENVIRONMENT
    assert resolved["modal_region"] == example.MODAL_REGION
    assert resolved["function"] == {
        "cpu": example.FUNCTION_CPU,
        "memory_mib": example.FUNCTION_MEMORY_MIB,
        "image": {
            "base": "debian_slim",
            "python_version": example.FUNCTION_PYTHON_VERSION,
            "package": example.FUNCTION_PACKAGE_SPEC,
        },
        "timeout_seconds": example.FUNCTION_TIMEOUT_SECONDS,
        "retries": example.FUNCTION_RETRIES,
        "min_containers": example.FUNCTION_MIN_CONTAINERS,
        "max_containers": example.FUNCTION_MAX_CONTAINERS,
    }
    assert resolved["warm_capacity"] == {
        "function_min_containers": 0,
        "sandbox_pool_capacity": 0,
    }
    assert "region=MODAL_REGION" in source
    assert "min_containers=FUNCTION_MIN_CONTAINERS" in source
    assert "retries=FUNCTION_RETRIES" in source
    assert "async with handle.borrow_async" in source
    assert "await computer.step(" in source
    assert "await computer.screenshots.full()" not in source
    assert "full_bytes(" not in source
    assert "optimized=" not in source


def test_anthropic_main_prints_only_the_placed_trajectory_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = load_example("anthropic_message_server.py")

    async def run_example(**_kwargs: Any) -> str:
        return "done"

    monkeypatch.setattr(example, "run_example", run_example)

    example.main()

    assert capsys.readouterr().out == "done\n"
