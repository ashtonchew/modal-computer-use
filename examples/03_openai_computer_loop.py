"""Canonical OpenAI Responses API computer-use loop.

Install the provider extra with ``uv sync --extra openai`` and set
``OPENAI_API_KEY`` before running this example. Provider calls stay in this
user-owned example; core modules remain provider-neutral.
"""

from __future__ import annotations

import os
from time import monotonic
from typing import Any

from modal_computer_use import ComputerConfig, ComputerSandbox
from modal_computer_use.adapters.openai import (
    OpenAIAdapter,
    openai_computer_call_output,
)

DEFAULT_MODEL = "gpt-5.6"
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_ACTIONS = 200
DEFAULT_MAX_ELAPSED_SECONDS = 300.0
DEFAULT_MAX_ACTION_TIMEOUT_MS = 30_000
COMPUTER_TOOL = {"type": "computer"}


def run_openai_computer_loop(
    *,
    client: Any,
    computer: Any,
    task: str,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_actions: int = DEFAULT_MAX_ACTIONS,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
    max_action_timeout_ms: int = DEFAULT_MAX_ACTION_TIMEOUT_MS,
) -> Any:
    """Run the GA computer tool until the model stops requesting actions."""
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    if max_actions < 1:
        raise ValueError("max_actions must be at least 1")
    if max_elapsed_seconds <= 0:
        raise ValueError("max_elapsed_seconds must be positive")
    if max_action_timeout_ms < 1:
        raise ValueError("max_action_timeout_ms must be at least 1")

    adapter = OpenAIAdapter(computer)
    started_at = monotonic()
    action_count = 0
    response = client.responses.create(
        model=model,
        tools=[COMPUTER_TOOL],
        input=task,
    )

    for _ in range(max_turns):
        _check_deadline(started_at, max_elapsed_seconds)
        calls = [
            item for item in response.output if getattr(item, "type", None) == "computer_call"
        ]
        if not calls:
            return response

        outputs: list[dict[str, Any]] = []
        for call in calls:
            _check_deadline(started_at, max_elapsed_seconds)
            actions = [_provider_dict(action) for action in call.actions]
            action_count += sum(_native_action_count(action) for action in actions)
            if action_count > max_actions:
                raise RuntimeError(f"OpenAI computer loop exceeded {max_actions} actions")
            batch = adapter.apply_many(
                actions,
                screenshot_after=True,
                max_action_timeout_ms=max_action_timeout_ms,
            )
            if not batch.ok:
                raise RuntimeError(f"computer action batch failed: {batch.results}")
            screenshot = batch.screenshot or computer.screenshots.full()
            outputs.append(
                openai_computer_call_output(
                    screenshot,
                    call_id=call.call_id,
                    detail="original",
                )
            )

        response = client.responses.create(
            model=model,
            tools=[COMPUTER_TOOL],
            previous_response_id=response.id,
            input=outputs,
        )

    raise RuntimeError(f"OpenAI computer loop exceeded {max_turns} turns")


def _provider_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"unsupported OpenAI computer action object: {type(value).__name__}")


def _native_action_count(action: dict[str, Any]) -> int:
    if action.get("type") == "keypress" and isinstance(action.get("keys"), list):
        return len(action["keys"])
    if action.get("type") == "scroll":
        return max(
            1,
            int(bool(action.get("scroll_x"))) + int(bool(action.get("scroll_y"))),
        )
    return 1


def _check_deadline(started_at: float, max_elapsed_seconds: float) -> None:
    if monotonic() - started_at > max_elapsed_seconds:
        raise RuntimeError(
            f"OpenAI computer loop exceeded {max_elapsed_seconds:g} seconds"
        )


def main() -> None:
    from openai import OpenAI

    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    config = ComputerConfig(desktop={"resolution": (1440, 900)})
    with ComputerSandbox.create(config=config) as computer:
        computer.wait_until_ready()
        response = run_openai_computer_loop(
            client=OpenAI(),
            computer=computer,
            model=model,
            task=(
                "Open https://example.com in the browser. Verify that the page title is "
                "'Example Domain', then report the title and stop. Use the computer tool "
                "for UI interaction. Do not sign in, submit forms, or change data."
            ),
        )
        print(response.output_text)


if __name__ == "__main__":
    main()
