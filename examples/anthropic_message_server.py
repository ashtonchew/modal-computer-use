"""Canonical Anthropic Messages API computer-use loop.

Install the provider extra with ``uv sync --extra anthropic`` and set
``ANTHROPIC_API_KEY`` before running this example. The loop preserves complete
assistant content and returns one matching ``tool_result`` for every tool use.
"""

from __future__ import annotations

import os
from time import monotonic
from typing import Any

from modal_computer_use import ComputerConfig, ComputerSandbox
from modal_computer_use.adapters.anthropic import (
    AnthropicAdapter,
    anthropic_tool_result,
    get_tool_version,
)
from modal_computer_use.models import Screenshot

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TOOL_VERSION = "computer_20251124"
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_ACTIONS = 200
DEFAULT_MAX_ELAPSED_SECONDS = 300.0
DEFAULT_MAX_ACTION_TIMEOUT_MS = 30_000


def run_anthropic_computer_loop(
    *,
    client: Any,
    computer: Any,
    task: str,
    display_width_px: int,
    display_height_px: int,
    model: str = DEFAULT_MODEL,
    tool_version: str = DEFAULT_TOOL_VERSION,
    enable_zoom: bool = True,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_actions: int = DEFAULT_MAX_ACTIONS,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
    max_action_timeout_ms: int = DEFAULT_MAX_ACTION_TIMEOUT_MS,
) -> Any:
    """Run a bounded Claude computer-use sampling loop."""
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    if max_actions < 1:
        raise ValueError("max_actions must be at least 1")
    if max_elapsed_seconds <= 0:
        raise ValueError("max_elapsed_seconds must be positive")
    if max_action_timeout_ms < 1:
        raise ValueError("max_action_timeout_ms must be at least 1")

    version = get_tool_version(tool_version)
    adapter = AnthropicAdapter(
        computer,
        tool_version=tool_version,
        enable_zoom=enable_zoom,
    )
    tool: dict[str, Any] = {
        "type": tool_version,
        "name": "computer",
        "display_width_px": display_width_px,
        "display_height_px": display_height_px,
    }
    if enable_zoom:
        tool["enable_zoom"] = True

    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    started_at = monotonic()
    action_count = 0
    for _ in range(max_turns):
        _check_deadline(started_at, max_elapsed_seconds)
        response = client.beta.messages.create(
            model=model,
            max_tokens=4096,
            betas=[version.beta_header],
            tools=[tool],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [
            block for block in response.content if getattr(block, "type", None) == "tool_use"
        ]
        if not tool_uses:
            return response

        results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            _check_deadline(started_at, max_elapsed_seconds)
            action_count += 1
            if action_count > max_actions:
                raise RuntimeError(
                    f"Anthropic computer loop exceeded {max_actions} actions"
                )
            action = _provider_dict(tool_use.input)
            action.setdefault("timeout_ms", max_action_timeout_ms)
            result = adapter.apply(action)
            if not result.ok:
                results.append(
                    anthropic_tool_result(tool_use_id=tool_use.id, result=result)
                )
                continue
            screenshot = _result_screenshot(action, result.output)
            if screenshot is None:
                screenshot = computer.screenshots.full()
            results.append(
                anthropic_tool_result(tool_use_id=tool_use.id, result=screenshot)
            )
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"Anthropic computer loop exceeded {max_turns} turns")


def _provider_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"unsupported Anthropic computer action object: {type(value).__name__}")


def _result_screenshot(action: dict[str, Any], output: dict[str, Any]) -> Screenshot | None:
    if action.get("action") not in {"screenshot", "zoom"}:
        return None
    try:
        return Screenshot.model_validate(output)
    except Exception:
        return None


def _check_deadline(started_at: float, max_elapsed_seconds: float) -> None:
    if monotonic() - started_at > max_elapsed_seconds:
        raise RuntimeError(
            f"Anthropic computer loop exceeded {max_elapsed_seconds:g} seconds"
        )


def main() -> None:
    from anthropic import Anthropic

    width, height = 1280, 800
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    with ComputerSandbox.create(
        config=ComputerConfig(desktop={"resolution": (width, height)})
    ) as computer:
        computer.wait_until_ready()
        response = run_anthropic_computer_loop(
            client=Anthropic(),
            computer=computer,
            model=model,
            display_width_px=width,
            display_height_px=height,
            task=(
                "Open the browser and verify the example page is reachable. "
                "Stop after reporting the page title; do not sign in, submit "
                "forms, or change data."
            ),
        )
        print(response.content)


if __name__ == "__main__":
    main()
