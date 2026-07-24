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
    previous_response_id: str | None = None
    next_input: str | list[dict[str, Any]] = task

    for turn in range(max_turns):
        _check_deadline(started_at, max_elapsed_seconds)
        request: dict[str, Any] = {
            "model": model,
            "tools": [COMPUTER_TOOL],
            "input": next_input,
            "timeout": _remaining_request_timeout_seconds(
                started_at, max_elapsed_seconds
            ),
        }
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        response = client.responses.create(**request)
        _check_deadline(started_at, max_elapsed_seconds)
        calls = [
            item for item in response.output if getattr(item, "type", None) == "computer_call"
        ]
        if not calls:
            return response
        if turn == max_turns - 1:
            raise RuntimeError(f"OpenAI computer loop exceeded {max_turns} turns")

        outputs: list[dict[str, Any]] = []
        for call in calls:
            _check_deadline(started_at, max_elapsed_seconds)
            actions = [_provider_dict(action) for action in call.actions]
            action_count += sum(_native_action_count(action) for action in actions)
            if action_count > max_actions:
                raise RuntimeError(f"OpenAI computer loop exceeded {max_actions} actions")
            for action in actions:
                native_count = _native_action_count(action)
                timeout_ms = _remaining_action_timeout_ms(
                    started_at=started_at,
                    max_elapsed_seconds=max_elapsed_seconds,
                    max_action_timeout_ms=max_action_timeout_ms,
                    native_action_count=native_count,
                )
                bounded_action = {**action, "timeout_ms": timeout_ms}
                batch = adapter.apply_many(
                    [bounded_action],
                    max_action_timeout_ms=timeout_ms,
                )
                if not batch.ok:
                    raise RuntimeError(f"computer action batch failed: {batch.results}")
                _check_deadline(started_at, max_elapsed_seconds)
            screenshot = computer.screenshots.full()
            _check_deadline(started_at, max_elapsed_seconds)
            outputs.append(
                openai_computer_call_output(
                    screenshot,
                    call_id=call.call_id,
                    detail="original",
                )
            )

        previous_response_id = response.id
        next_input = outputs

    raise RuntimeError(f"OpenAI computer loop exceeded {max_turns} turns")


def _provider_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"unsupported OpenAI computer action object: {type(value).__name__}")


def _native_action_count(action: dict[str, Any]) -> int:
    if action.get("type") == "keypress" and isinstance(action.get("keys"), list):
        return max(1, len(action["keys"]))
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


def _remaining_action_timeout_ms(
    *,
    started_at: float,
    max_elapsed_seconds: float,
    max_action_timeout_ms: int,
    native_action_count: int,
) -> int:
    remaining_ms = int(
        (max_elapsed_seconds - (monotonic() - started_at)) * 1000
    )
    per_action_ms = remaining_ms // native_action_count
    if per_action_ms < 1:
        raise RuntimeError(
            f"OpenAI computer loop exceeded {max_elapsed_seconds:g} seconds"
        )
    return min(max_action_timeout_ms, per_action_ms)


def _remaining_request_timeout_seconds(
    started_at: float,
    max_elapsed_seconds: float,
) -> float:
    remaining = max_elapsed_seconds - (monotonic() - started_at)
    if remaining <= 0:
        raise RuntimeError(
            f"OpenAI computer loop exceeded {max_elapsed_seconds:g} seconds"
        )
    return remaining


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
