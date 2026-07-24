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
from modal_computer_use.models import ActionResult, Screenshot

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
    zoom_enabled = enable_zoom and version.supports_zoom
    adapter = AnthropicAdapter(
        computer,
        tool_version=tool_version,
        enable_zoom=zoom_enabled,
    )
    tool: dict[str, Any] = {
        "type": tool_version,
        "name": "computer",
        "display_width_px": display_width_px,
        "display_height_px": display_height_px,
    }
    if zoom_enabled:
        tool["enable_zoom"] = True

    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    started_at = monotonic()
    action_count = 0
    for turn in range(max_turns):
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
        if response.stop_reason != "tool_use":
            if tool_uses:
                raise RuntimeError(
                    "Anthropic returned tool_use content without stop_reason='tool_use'"
                )
            return response
        if not tool_uses:
            raise RuntimeError(
                "Anthropic returned stop_reason='tool_use' without a tool_use block"
            )
        if turn == max_turns - 1:
            raise RuntimeError(f"Anthropic computer loop exceeded {max_turns} turns")
        requested_actions = sum(_requested_action_count(tool_use) for tool_use in tool_uses)
        if action_count + requested_actions > max_actions:
            raise RuntimeError(f"Anthropic computer loop exceeded {max_actions} actions")

        results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            action_count += 1
            try:
                _check_deadline(started_at, max_elapsed_seconds)
                action = _provider_dict(tool_use.input)
                action_count += _anthropic_action_count(action) - 1
                action["timeout_ms"] = _remaining_action_timeout_ms(
                    started_at=started_at,
                    max_elapsed_seconds=max_elapsed_seconds,
                    max_action_timeout_ms=max_action_timeout_ms,
                )
                result = adapter.apply(action)
                results.append(
                    _tool_result(
                        computer=computer,
                        tool_use_id=tool_use.id,
                        action=action,
                        result=result,
                    )
                )
            except Exception as exc:
                results.append(
                    anthropic_tool_result(
                        tool_use_id=tool_use.id,
                        result=ActionResult(
                            ok=False,
                            message=f"computer action failed: {type(exc).__name__}",
                        ),
                    )
                )
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"Anthropic computer loop exceeded {max_turns} turns")


def _provider_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"unsupported Anthropic computer action object: {type(value).__name__}")


def _requested_action_count(tool_use: Any) -> int:
    try:
        return _anthropic_action_count(_provider_dict(tool_use.input))
    except Exception:
        return 0


def _anthropic_action_count(action: dict[str, Any]) -> int:
    if (action.get("action") or action.get("type")) != "hold_key":
        return 1
    nested = action.get("actions")
    if not isinstance(nested, list):
        return 1
    return 1 + sum(
        _anthropic_action_count(item) if isinstance(item, dict) else 1
        for item in nested
    )


def _remaining_action_timeout_ms(
    *,
    started_at: float,
    max_elapsed_seconds: float,
    max_action_timeout_ms: int,
) -> int:
    remaining_ms = int(
        (max_elapsed_seconds - (monotonic() - started_at)) * 1000
    )
    if remaining_ms < 1:
        raise RuntimeError(
            f"Anthropic computer loop exceeded {max_elapsed_seconds:g} seconds"
        )
    return min(max_action_timeout_ms, remaining_ms)


def _tool_result(
    *,
    computer: Any,
    tool_use_id: str,
    action: dict[str, Any],
    result: ActionResult,
) -> dict[str, Any]:
    if not result.ok:
        return anthropic_tool_result(tool_use_id=tool_use_id, result=result)
    if action.get("action") == "cursor_position":
        x = int(result.output["x"])
        y = int(result.output["y"])
        return anthropic_tool_result(
            tool_use_id=tool_use_id,
            result=ActionResult(ok=True, message=f"X={x},Y={y}"),
        )
    if action.get("action") in {"screenshot", "zoom"}:
        screenshot = Screenshot.model_validate(result.output)
    else:
        screenshot = computer.screenshots.full()
    return anthropic_tool_result(tool_use_id=tool_use_id, result=screenshot)


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
                "Open https://example.com in the browser. Verify that the page title is "
                "'Example Domain', then report the title and stop. Do not sign in, "
                "submit forms, or change data."
            ),
        )
        print(response.content)


if __name__ == "__main__":
    main()
