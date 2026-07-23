from __future__ import annotations

import importlib.util
from collections import deque
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from modal_computer_use.models import (
    ActionBatchResult,
    ActionItemResult,
    ActionResult,
    CoordinateSpace,
    Screenshot,
)

ROOT = Path(__file__).parents[1]


def _load_example(filename: str) -> ModuleType:
    path = ROOT / "examples" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tiny_screenshot() -> Screenshot:
    return Screenshot(
        format="png",
        width=1,
        height=1,
        size_bytes=68,
        data_base64=(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4"
            "z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        ),
        sha256="synthetic",
        artifact_uri="artifact://screenshots/tiny.png",
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=1,
            desktop_height=1,
        ),
    )


class _Screenshots:
    def full(self) -> Screenshot:
        return _tiny_screenshot()


class _Actions:
    def __init__(self) -> None:
        self.applied: list[Any] = []
        self.batches: list[list[Any]] = []

    def apply(self, action: Any, *, source: str = "sdk") -> ActionResult:
        self.applied.append((action, source))
        return ActionResult(ok=True, output={"type": action.type})

    def run(
        self,
        actions: list[Any],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        source: str = "sdk",
        max_action_timeout_ms: int | None = None,
    ) -> ActionBatchResult:
        del continue_on_error, source, max_action_timeout_ms
        self.batches.append(actions)
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(index=index, type=action.type, ok=True)
                for index, action in enumerate(actions)
            ],
            screenshot=_tiny_screenshot() if screenshot_after else None,
        )


class _Computer:
    def __init__(self) -> None:
        self.actions = _Actions()
        self.screenshots = _Screenshots()


class _CreateQueue:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.popleft()


def test_openai_cookbook_preserves_action_order_and_response_chain() -> None:
    example = _load_example("03_openai_computer_loop.py")
    computer = _Computer()
    create = _CreateQueue(
        [
            SimpleNamespace(
                id="resp_1",
                output=[
                    SimpleNamespace(
                        type="computer_call",
                        call_id="call_1",
                        actions=[
                            {"type": "move", "x": 10, "y": 20},
                            {"type": "click", "x": 10, "y": 20, "button": "left"},
                        ],
                    )
                ],
            ),
            SimpleNamespace(id="resp_2", output=[], output_text="done"),
        ]
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=create.create))

    response = example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
    )

    assert response.output_text == "done"
    assert [action.type for action in computer.actions.batches[0]] == ["move", "click"]
    assert create.calls[0]["tools"] == [{"type": "computer"}]
    assert create.calls[1]["previous_response_id"] == "resp_1"
    output = create.calls[1]["input"][0]
    assert output["type"] == "computer_call_output"
    assert output["call_id"] == "call_1"
    assert output["output"]["detail"] == "original"


def test_anthropic_cookbook_preserves_assistant_content_and_tool_ids() -> None:
    example = _load_example("anthropic_message_server.py")
    computer = _Computer()
    tool_use = SimpleNamespace(
        type="tool_use",
        id="tool_1",
        input={"action": "left_click", "coordinate": [10, 20]},
    )
    create = _CreateQueue(
        [
            SimpleNamespace(content=[tool_use]),
            SimpleNamespace(content=[SimpleNamespace(type="text", text="done")]),
        ]
    )
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create.create)))

    response = example.run_anthropic_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        display_width_px=1280,
        display_height_px=800,
    )

    assert response.content[0].text == "done"
    assert create.calls[0]["betas"] == ["computer-use-2025-11-24"]
    assert create.calls[0]["tools"] == [
        {
            "type": "computer_20251124",
            "name": "computer",
            "display_width_px": 1280,
            "display_height_px": 800,
            "enable_zoom": True,
        }
    ]
    messages = create.calls[1]["messages"]
    assert messages[1] == {"role": "assistant", "content": [tool_use]}
    tool_result = messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "tool_1"
    assert tool_result["content"][0]["type"] == "image"


@pytest.mark.parametrize(
    ("filename", "function_name", "kwargs"),
    [
        (
            "03_openai_computer_loop.py",
            "run_openai_computer_loop",
            {"task": "Inspect the page"},
        ),
        (
            "anthropic_message_server.py",
            "run_anthropic_computer_loop",
            {
                "task": "Inspect the page",
                "display_width_px": 1280,
                "display_height_px": 800,
            },
        ),
    ],
)
def test_provider_cookbooks_reject_unbounded_turn_configuration(
    filename: str,
    function_name: str,
    kwargs: dict[str, Any],
) -> None:
    example = _load_example(filename)

    with pytest.raises(ValueError, match="max_turns must be at least 1"):
        getattr(example, function_name)(
            client=object(),
            computer=object(),
            max_turns=0,
            **kwargs,
        )


@pytest.mark.parametrize(
    ("filename", "function_name", "kwargs"),
    [
        (
            "03_openai_computer_loop.py",
            "run_openai_computer_loop",
            {"task": "Inspect the page"},
        ),
        (
            "anthropic_message_server.py",
            "run_anthropic_computer_loop",
            {
                "task": "Inspect the page",
                "display_width_px": 1280,
                "display_height_px": 800,
            },
        ),
    ],
)
def test_provider_cookbooks_require_action_and_time_budgets(
    filename: str,
    function_name: str,
    kwargs: dict[str, Any],
) -> None:
    example = _load_example(filename)
    run_loop = getattr(example, function_name)

    with pytest.raises(ValueError, match="max_actions must be at least 1"):
        run_loop(client=object(), computer=object(), max_actions=0, **kwargs)
    with pytest.raises(ValueError, match="max_elapsed_seconds must be positive"):
        run_loop(client=object(), computer=object(), max_elapsed_seconds=0, **kwargs)
    with pytest.raises(ValueError, match="max_action_timeout_ms must be at least 1"):
        run_loop(client=object(), computer=object(), max_action_timeout_ms=0, **kwargs)
