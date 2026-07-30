"""Canonical Anthropic Messages API computer-use loop.

Install the provider extras with ``uv sync --extra modal --extra anthropic``
and set ``ANTHROPIC_API_KEY`` before running this example. The loop keeps the
provider-owned hosted computer tool and adds an application-owned batch tool
for predictable action sequences.
"""

from __future__ import annotations

import json
import os
from time import monotonic
from typing import Any

from modal_computer_use import (
    BrowserConfig,
    ComputerConfig,
    ComputerSandbox,
    ResourceConfig,
)
from modal_computer_use.adapters.anthropic import (
    ANTHROPIC_TOOL_VERSIONS,
    AnthropicAdapter,
    anthropic_screenshot_content,
    anthropic_tool_result,
    get_tool_version,
)
from modal_computer_use.models import (
    ActionBatchResult,
    ActionItemResult,
    ActionResult,
    Screenshot,
)

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TOOL_VERSION = "computer_20251124"
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_TRAJECTORY_ACTIONS = 200
DEFAULT_MAX_BATCH_ACTIONS = 50
DEFAULT_MAX_ELAPSED_SECONDS = 300.0
DEFAULT_MAX_ACTION_TIMEOUT_MS = 30_000

_REVIEWED_BATCH_TOOL_VERSIONS = frozenset(
    {
        "computer_20241022",
        "computer_20250124",
        "computer_20251124",
    }
)
_BASE_ACTIONS = (
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
)
_ENHANCED_ACTIONS = (
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "scroll",
    "hold_key",
    "wait",
)


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
    max_trajectory_actions: int = DEFAULT_MAX_TRAJECTORY_ACTIONS,
    max_batch_actions: int = DEFAULT_MAX_BATCH_ACTIONS,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
    max_action_timeout_ms: int = DEFAULT_MAX_ACTION_TIMEOUT_MS,
) -> Any:
    """Run a bounded Claude computer-use sampling loop."""
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    if max_trajectory_actions < 1:
        raise ValueError("max_trajectory_actions must be at least 1")
    if max_batch_actions < 1:
        raise ValueError("max_batch_actions must be at least 1")
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
    hosted_computer_tool: dict[str, Any] = {
        "type": tool_version,
        "name": "computer",
        "display_width_px": display_width_px,
        "display_height_px": display_height_px,
    }
    if zoom_enabled:
        hosted_computer_tool["enable_zoom"] = True
    computer_batch_tool = _computer_batch_tool_definition(
        tool_version=tool_version,
        enable_zoom=zoom_enabled,
        max_batch_actions=max_batch_actions,
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    started_at = monotonic()
    trajectory_action_count = 0
    for turn in range(max_turns):
        _check_deadline(started_at, max_elapsed_seconds)
        response = client.beta.messages.create(
            model=model,
            max_tokens=4096,
            betas=[version.beta_header],
            tools=[hosted_computer_tool, computer_batch_tool],
            messages=messages,
            timeout=_remaining_request_timeout_seconds(started_at, max_elapsed_seconds),
        )
        _check_deadline(started_at, max_elapsed_seconds)
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
            raise RuntimeError("Anthropic returned stop_reason='tool_use' without a tool_use block")
        if turn == max_turns - 1:
            raise RuntimeError(f"Anthropic computer loop exceeded {max_turns} turns")

        requested_actions = sum(_requested_action_count(tool_use) for tool_use in tool_uses)
        if trajectory_action_count + requested_actions > max_trajectory_actions:
            raise RuntimeError(
                f"Anthropic computer loop exceeded {max_trajectory_actions} trajectory actions"
            )
        trajectory_action_count += requested_actions

        tool_results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            try:
                _check_deadline(started_at, max_elapsed_seconds)
                tool_input = _provider_dict(tool_use.input)
                if _tool_name(tool_use) == "computer_batch":
                    actions = tool_input.get("actions")
                    if not isinstance(actions, list):
                        actions = []
                        batch_result = _batch_preflight_failure(actions, failed_index=0)
                    else:
                        batch_result = _execute_computer_batch(
                            adapter=adapter,
                            actions=actions,
                            max_batch_actions=max_batch_actions,
                            started_at=started_at,
                            max_elapsed_seconds=max_elapsed_seconds,
                            max_action_timeout_ms=max_action_timeout_ms,
                        )
                    tool_result = _computer_batch_tool_result(
                        tool_use_id=tool_use.id,
                        actions=actions,
                        batch_result=batch_result,
                    )
                elif _tool_name(tool_use) == "computer":
                    action = dict(tool_input)
                    action["timeout_ms"] = _remaining_action_timeout_ms(
                        started_at=started_at,
                        max_elapsed_seconds=max_elapsed_seconds,
                        max_action_timeout_ms=max_action_timeout_ms,
                    )
                    action_result = adapter.apply(action)
                    tool_result = _hosted_computer_tool_result(
                        computer=computer,
                        tool_use_id=tool_use.id,
                        action=action,
                        action_result=action_result,
                    )
                else:
                    raise ValueError("unsupported Anthropic client tool")
            except Exception as exc:
                tool_result = anthropic_tool_result(
                    tool_use_id=tool_use.id,
                    result=ActionResult(
                        ok=False,
                        message=f"computer tool failed: {type(exc).__name__}",
                    ),
                )
            tool_results.append(tool_result)
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(f"Anthropic computer loop exceeded {max_turns} turns")


def _computer_action_schema(
    *,
    tool_version: str,
    enable_zoom: bool,
    max_batch_actions: int,
) -> dict[str, Any]:
    _assert_reviewed_batch_tool_versions()
    version = get_tool_version(tool_version)
    coordinate = {
        "type": "array",
        "items": {"type": "integer", "minimum": 0},
        "minItems": 2,
        "maxItems": 2,
    }
    text = {"type": "string"}
    key_modifier = {"type": "string", "minLength": 1}
    schemas = [
        _action_variant("mouse_move", {"coordinate": coordinate}, ("coordinate",)),
        _action_variant(
            "left_click_drag",
            {"coordinate": coordinate, "start_coordinate": coordinate},
            ("coordinate", "start_coordinate"),
        ),
        _action_variant("key", {"text": key_modifier}, ("text",)),
        _action_variant("type", {"text": text}, ("text",)),
        *[
            _action_variant(
                action,
                {"coordinate": coordinate, "key": key_modifier}
                if version.supports_enhanced_actions
                else None,
            )
            for action in ("left_click", "right_click", "middle_click", "double_click")
        ],
        _action_variant("screenshot"),
        _action_variant("cursor_position"),
    ]
    if version.supports_enhanced_actions:
        schemas.extend(
            [
                _action_variant("triple_click", {"coordinate": coordinate, "key": key_modifier}),
                _action_variant("left_mouse_down"),
                _action_variant("left_mouse_up"),
                _action_variant(
                    "scroll",
                    {
                        "coordinate": coordinate,
                        "scroll_direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                        },
                        "scroll_amount": {"type": "integer", "minimum": 1},
                        "text": key_modifier,
                    },
                    ("scroll_direction", "scroll_amount"),
                ),
                _action_variant(
                    "hold_key",
                    {
                        "text": key_modifier,
                        "duration": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 100,
                        },
                        "actions": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/computer_action"},
                            "minItems": 1,
                            "maxItems": max_batch_actions,
                        },
                    },
                    ("text", "duration"),
                ),
                _action_variant(
                    "wait",
                    {
                        "duration": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 100,
                        }
                    },
                    ("duration",),
                ),
            ]
        )
    if enable_zoom and version.supports_zoom:
        schemas.append(
            _action_variant(
                "zoom",
                {
                    "region": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                        "minItems": 4,
                        "maxItems": 4,
                    }
                },
                ("region",),
            )
        )
    return {"oneOf": schemas}


def _computer_batch_tool_definition(
    *,
    tool_version: str,
    enable_zoom: bool,
    max_batch_actions: int,
) -> dict[str, Any]:
    action_schema = _computer_action_schema(
        tool_version=tool_version,
        enable_zoom=enable_zoom,
        max_batch_actions=max_batch_actions,
    )
    return {
        "name": "computer_batch",
        "description": (
            "Execute a predictable sequence of computer actions in order and stop on "
            "the first error. All coordinates refer to the screenshot observed before "
            "the batch. Use this only when no action needs intermediate visual "
            "replanning. Use the hosted computer tool for exploration, sensitive "
            "steps, recovery, or decisions that depend on an intermediate screen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/computer_action"},
                    "minItems": 1,
                    "maxItems": max_batch_actions,
                }
            },
            "required": ["actions"],
            "additionalProperties": False,
            "$defs": {"computer_action": action_schema},
        },
    }


def _execute_computer_batch(
    *,
    adapter: AnthropicAdapter,
    actions: list[Any],
    max_batch_actions: int,
    started_at: float,
    max_elapsed_seconds: float,
    max_action_timeout_ms: int,
) -> ActionBatchResult:
    if not actions:
        return _batch_preflight_failure(actions, failed_index=0)
    if len(actions) > max_batch_actions:
        return _batch_preflight_failure(actions, failed_index=max_batch_actions)
    provider_actions: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            return _batch_preflight_failure(actions, failed_index=index)
        try:
            adapter.normalize(action)
        except Exception:
            return _batch_preflight_failure(actions, failed_index=index)
        provider_actions.append(action)
    expanded_action_count = sum(_anthropic_action_count(action) for action in provider_actions)
    if expanded_action_count > max_batch_actions:
        return _batch_preflight_failure(actions, failed_index=0)
    try:
        remaining_batch_timeout_ms = _remaining_batch_action_timeout_ms(
            started_at=started_at,
            max_elapsed_seconds=max_elapsed_seconds,
            max_action_timeout_ms=max_action_timeout_ms,
            expanded_action_count=expanded_action_count,
        )
        return adapter.apply_many(
            provider_actions,
            continue_on_error=False,
            screenshot_after=True,
            max_action_timeout_ms=remaining_batch_timeout_ms,
        )
    except Exception:
        return _batch_preflight_failure(actions, failed_index=0)


def _computer_batch_tool_result(
    *,
    tool_use_id: str,
    actions: list[Any],
    batch_result: ActionBatchResult,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    native_image_indexes: set[int] = set()
    failed_item: ActionItemResult | None = None
    screenshot_failure: ActionItemResult | None = None
    for item in batch_result.results:
        if item.type == "screenshot_after":
            if not item.ok and screenshot_failure is None:
                screenshot_failure = item
            continue
        action_name = _safe_action_name(actions, item.index)
        if item.ok:
            text = _batch_item_text(item, action_name)
        else:
            text = f"[actions[{item.index}]:{action_name}] computer action failed"
            if failed_item is None:
                failed_item = item
        content.append({"type": "text", "text": text})
        action = actions[item.index] if 0 <= item.index < len(actions) else {}
        native_screenshots, ends_with_native_screenshot = _native_screenshot_outputs(
            action, item.output
        )
        content.extend(
            anthropic_screenshot_content(screenshot) for screenshot in native_screenshots
        )
        if ends_with_native_screenshot:
            native_image_indexes.add(item.index)

    last_action_index = len(actions) - 1
    if batch_result.screenshot is not None and last_action_index not in native_image_indexes:
        content.append({"type": "text", "text": "[post_batch_screenshot]"})
        content.append(anthropic_screenshot_content(batch_result.screenshot))

    payload: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content or [{"type": "text", "text": "[computer_batch] no actions completed"}],
    }
    if screenshot_failure is not None:
        completed_count = sum(
            item.ok and 0 <= item.index < len(actions)
            for item in batch_result.results
            if item.type != "screenshot_after"
        )
        skipped_count = max(0, len(actions) - completed_count)
        payload["content"].append(
            {
                "type": "text",
                "text": (
                    "[post_batch_screenshot] capture failed "
                    f"({completed_count} completed, {skipped_count} skipped)"
                ),
            }
        )
        payload["is_error"] = True
    elif failed_item is not None or not batch_result.ok:
        failed_index = failed_item.index if failed_item is not None else 0
        if failed_item is not None and failed_item.error_code == "batch_preflight_failed":
            completed_count = 0
            skipped_count = max(0, len(actions) - 1)
        else:
            completed_count = sum(
                item.ok and item.index < failed_index for item in batch_result.results
            )
            skipped_count = max(0, len(actions) - failed_index - 1)
        payload["content"].append(
            {
                "type": "text",
                "text": (
                    f"[computer_batch] stopped at actions[{failed_index}] "
                    f"({completed_count} completed, {skipped_count} skipped)"
                ),
            }
        )
        payload["is_error"] = True
    return payload


def _provider_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"unsupported Anthropic computer action object: {type(value).__name__}")


def _tool_name(tool_use: Any) -> str:
    name = getattr(tool_use, "name", None)
    return "computer" if name is None else str(name)


def _requested_action_count(tool_use: Any) -> int:
    try:
        tool_input = _provider_dict(tool_use.input)
        if _tool_name(tool_use) == "computer_batch":
            actions = tool_input.get("actions")
            if not isinstance(actions, list):
                return 0
            return sum(
                _anthropic_action_count(action) for action in actions if isinstance(action, dict)
            )
        if _tool_name(tool_use) == "computer":
            return _anthropic_action_count(tool_input)
    except Exception:
        return 0
    return 0


def _anthropic_action_count(action: dict[str, Any]) -> int:
    if (action.get("action") or action.get("type")) != "hold_key":
        return 1
    nested = action.get("actions")
    if not isinstance(nested, list):
        return 1
    return 1 + sum(
        _anthropic_action_count(item) if isinstance(item, dict) else 1 for item in nested
    )


def _remaining_action_timeout_ms(
    *,
    started_at: float,
    max_elapsed_seconds: float,
    max_action_timeout_ms: int,
) -> int:
    remaining_ms = int((max_elapsed_seconds - (monotonic() - started_at)) * 1000)
    if remaining_ms < 1:
        raise RuntimeError(f"Anthropic computer loop exceeded {max_elapsed_seconds:g} seconds")
    return min(max_action_timeout_ms, remaining_ms)


def _remaining_batch_action_timeout_ms(
    *,
    started_at: float,
    max_elapsed_seconds: float,
    max_action_timeout_ms: int,
    expanded_action_count: int,
) -> int:
    remaining_ms = int((max_elapsed_seconds - (monotonic() - started_at)) * 1000)
    slots = expanded_action_count + 1
    if remaining_ms < slots:
        raise RuntimeError(f"Anthropic computer loop exceeded {max_elapsed_seconds:g} seconds")
    return min(max_action_timeout_ms, remaining_ms // slots)


def _remaining_request_timeout_seconds(
    started_at: float,
    max_elapsed_seconds: float,
) -> float:
    remaining = max_elapsed_seconds - (monotonic() - started_at)
    if remaining <= 0:
        raise RuntimeError(f"Anthropic computer loop exceeded {max_elapsed_seconds:g} seconds")
    return remaining


def _hosted_computer_tool_result(
    *,
    computer: Any,
    tool_use_id: str,
    action: dict[str, Any],
    action_result: ActionResult,
) -> dict[str, Any]:
    if not action_result.ok:
        return anthropic_tool_result(
            tool_use_id=tool_use_id,
            result=ActionResult(ok=False, message="computer action failed"),
        )
    if action.get("action") == "cursor_position":
        x = int(action_result.output["x"])
        y = int(action_result.output["y"])
        return anthropic_tool_result(
            tool_use_id=tool_use_id,
            result=ActionResult(ok=True, message=f"X={x},Y={y}"),
        )
    if action.get("action") in {"screenshot", "zoom"}:
        screenshot = Screenshot.model_validate(action_result.output)
    else:
        screenshot = computer.screenshots.full()
    return anthropic_tool_result(tool_use_id=tool_use_id, result=screenshot)


def _action_variant(
    action: str,
    properties: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"action": {"const": action}, **(properties or {})},
        "required": ["action", *required],
        "additionalProperties": False,
    }


def _assert_reviewed_batch_tool_versions() -> None:
    if frozenset(ANTHROPIC_TOOL_VERSIONS) != _REVIEWED_BATCH_TOOL_VERSIONS:
        raise RuntimeError(
            "Anthropic computer_batch schema requires review for the tool-version registry"
        )


def _batch_preflight_failure(
    actions: list[Any],
    *,
    failed_index: int,
) -> ActionBatchResult:
    safe_index = min(max(failed_index, 0), max(0, len(actions) - 1))
    return ActionBatchResult(
        ok=False,
        results=[
            ActionItemResult(
                index=safe_index,
                type=_safe_action_name(actions, safe_index),
                ok=False,
                error_code="batch_preflight_failed",
            )
        ],
    )


def _batch_item_text(item: ActionItemResult, action_name: str) -> str:
    if action_name == "cursor_position":
        try:
            return (
                f"[actions[{item.index}]:cursor_position] "
                f"X={int(item.output['x'])},Y={int(item.output['y'])}"
            )
        except (KeyError, TypeError, ValueError):
            return f"[actions[{item.index}]:cursor_position] completed"
    summary = {"ok": True}
    if item.elapsed_ms is not None:
        summary["elapsed_ms"] = item.elapsed_ms
    return (
        f"[actions[{item.index}]:{action_name}] "
        f"{json.dumps(summary, sort_keys=True, separators=(',', ':'))}"
    )


def _safe_action_name(actions: list[Any], index: int) -> str:
    if index < 0 or index >= len(actions) or not isinstance(actions[index], dict):
        return "unknown"
    value = actions[index].get("action")
    known_actions = {*_BASE_ACTIONS, *_ENHANCED_ACTIONS, "zoom"}
    return value if isinstance(value, str) and value in known_actions else "unknown"


def _screenshot_from_output(output: dict[str, Any]) -> Screenshot | None:
    try:
        return Screenshot.model_validate(output)
    except Exception:
        return None


def _native_screenshot_outputs(
    action: Any,
    output: dict[str, Any],
) -> tuple[list[Screenshot], bool]:
    if not isinstance(action, dict):
        return [], False
    action_name = action.get("action")
    if action_name in {"screenshot", "zoom"}:
        screenshot = _screenshot_from_output(output)
        return ([screenshot], True) if screenshot is not None else ([], False)
    if action_name != "hold_key":
        return [], False
    nested_actions = action.get("actions")
    nested_results = output.get("actions")
    if not isinstance(nested_actions, list) or not isinstance(nested_results, list):
        return [], False
    results_by_index = {
        result.get("index"): result
        for result in nested_results
        if isinstance(result, dict) and isinstance(result.get("index"), int)
    }
    screenshots: list[Screenshot] = []
    ends_with_native_screenshot = False
    for index, nested_action in enumerate(nested_actions):
        nested_result = results_by_index.get(index)
        if not isinstance(nested_result, dict) or nested_result.get("ok") is not True:
            continue
        nested_output = nested_result.get("output")
        if not isinstance(nested_output, dict):
            nested_output = {}
        nested_screenshots, nested_ends_with_screenshot = _native_screenshot_outputs(
            nested_action,
            nested_output,
        )
        screenshots.extend(nested_screenshots)
        if index == len(nested_actions) - 1:
            ends_with_native_screenshot = nested_ends_with_screenshot
    return screenshots, ends_with_native_screenshot


def _check_deadline(started_at: float, max_elapsed_seconds: float) -> None:
    if monotonic() - started_at > max_elapsed_seconds:
        raise RuntimeError(f"Anthropic computer loop exceeded {max_elapsed_seconds:g} seconds")


def main() -> None:
    from anthropic import Anthropic

    width, height = 1280, 800
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    config = ComputerConfig(
        desktop={"resolution": (width, height)},
        resources=ResourceConfig(profile="browser"),
        browser=BrowserConfig(kind="chromium", prewarm=True),
    )
    with ComputerSandbox.create(config=config, wait=True) as computer:
        computer.ensure_browser_ready(config)
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
