"""Canonical OpenAI Responses API computer-use loop.

Install the runtime extras with ``uv sync --extra modal --extra openai`` and
set ``OPENAI_API_KEY`` before running this example. Provider calls stay in
this user-owned example; core modules remain provider-neutral.
"""

from __future__ import annotations

import os
from time import monotonic
from typing import Any

from modal_computer_use import (
    BrowserConfig,
    ComputerConfig,
    ComputerSandbox,
    ResourceConfig,
)
from modal_computer_use.adapters.openai import (
    OpenAIAdapter,
    openai_computer_call_output,
)
from modal_computer_use.errors import ActionValidationError, UnsupportedActionError
from modal_computer_use.models import ComputerAction, Screenshot, parse_action

DEFAULT_MODEL = "gpt-5.6"
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_TRAJECTORY_ACTIONS = 200
DEFAULT_MAX_BATCH_ACTIONS = 50
DEFAULT_MAX_ELAPSED_SECONDS = 300.0
DEFAULT_MAX_ACTION_TIMEOUT_MS = 30_000
DEFAULT_MAX_BATCH_DURATION_MS = 30_000
COMPUTER_TOOL = {"type": "computer"}


class _OpenAILimitError(RuntimeError):
    pass


def run_openai_computer_loop(
    *,
    client: Any,
    computer: Any,
    task: str,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_trajectory_actions: int = DEFAULT_MAX_TRAJECTORY_ACTIONS,
    max_batch_actions: int = DEFAULT_MAX_BATCH_ACTIONS,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
    max_action_timeout_ms: int = DEFAULT_MAX_ACTION_TIMEOUT_MS,
    max_batch_duration_ms: int = DEFAULT_MAX_BATCH_DURATION_MS,
    before_action: Any | None = None,
) -> Any:
    """Run the GA computer tool until the model stops requesting actions."""
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
    if max_batch_duration_ms < 1:
        raise ValueError("max_batch_duration_ms must be at least 1")

    adapter = OpenAIAdapter(computer)
    started_at = monotonic()
    trajectory_action_count = 0
    previous_response_id: str | None = None
    next_input: str | list[dict[str, Any]] = task

    for turn in range(max_turns):
        _check_deadline(started_at, max_elapsed_seconds)
        request: dict[str, Any] = {
            "model": model,
            "tools": [COMPUTER_TOOL],
            "input": next_input,
            "timeout": _remaining_request_timeout_seconds(started_at, max_elapsed_seconds),
        }
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        response = client.responses.create(**request)
        _check_deadline(started_at, max_elapsed_seconds)
        calls = [item for item in response.output if getattr(item, "type", None) == "computer_call"]
        if not calls:
            return response
        if turn == max_turns - 1:
            raise RuntimeError(f"OpenAI computer loop exceeded {max_turns} turns")

        preflighted_calls: list[tuple[Any, list[dict[str, Any]], int, int]] = []
        response_action_count = 0
        try:
            for call in calls:
                actions = [_provider_dict(action) for action in call.actions]
                if not actions:
                    raise ActionValidationError("OpenAI computer_call actions must not be empty")
                normalized_actions = _normalize_for_preflight(adapter, actions)
                expanded_action_count = sum(
                    _count_expanded_action_tree(action) for action in normalized_actions
                )
                if expanded_action_count > max_batch_actions:
                    raise _OpenAILimitError(
                        f"OpenAI computer call exceeded {max_batch_actions} expanded batch actions"
                    )
                _preflight_policy(normalized_actions, before_action)
                response_action_count += expanded_action_count
                preflighted_calls.append(
                    (call, actions, len(normalized_actions), expanded_action_count)
                )
            if trajectory_action_count + response_action_count > max_trajectory_actions:
                raise _OpenAILimitError(
                    f"OpenAI computer loop exceeded {max_trajectory_actions} trajectory actions"
                )
        except _OpenAILimitError:
            raise
        except Exception as exc:
            raise RuntimeError(f"OpenAI computer preflight failed: {type(exc).__name__}") from None

        trajectory_action_count += response_action_count
        outputs: list[dict[str, Any]] = []
        for call, actions, normalized_action_count, expanded_action_count in preflighted_calls:
            remaining_batch_timeout_ms = _remaining_batch_timeout_ms(
                started_at=started_at,
                max_elapsed_seconds=max_elapsed_seconds,
                max_batch_duration_ms=max_batch_duration_ms,
            )
            timeout_slots = expanded_action_count + 1
            allocated_action_timeout_ms = min(
                max_action_timeout_ms,
                remaining_batch_timeout_ms // timeout_slots,
            )
            if allocated_action_timeout_ms < 1:
                _raise_deadline(max_elapsed_seconds)
            bounded_actions = [
                {**action, "timeout_ms": allocated_action_timeout_ms} for action in actions
            ]
            try:
                batch_result = adapter.apply_many(
                    bounded_actions,
                    continue_on_error=False,
                    screenshot_after=True,
                    max_action_timeout_ms=allocated_action_timeout_ms,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"OpenAI computer action batch failed: {type(exc).__name__}"
                ) from None
            native_screenshot = _native_final_screenshot(
                batch_result,
                actions=actions,
                normalized_action_count=normalized_action_count,
            )
            if native_screenshot is not None:
                post_batch_screenshot = native_screenshot
            else:
                if not batch_result.ok:
                    raise RuntimeError(_sanitized_batch_failure(batch_result))
                post_batch_screenshot = batch_result.screenshot
                if post_batch_screenshot is None:
                    raise RuntimeError("OpenAI computer action batch returned no screenshot")
            _check_deadline(started_at, max_elapsed_seconds)
            outputs.append(
                openai_computer_call_output(
                    post_batch_screenshot,
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


def _normalize_for_preflight(
    adapter: OpenAIAdapter,
    actions: list[dict[str, Any]],
) -> list[ComputerAction]:
    normalized: list[ComputerAction] = []
    for action in actions:
        kind = action.get("type") or action.get("action")
        if kind == "keypress" and isinstance(action.get("keys"), list):
            keys = action["keys"]
            if not keys:
                raise ActionValidationError("OpenAI keypress action requires keys")
            for key in keys:
                single_key_action = {
                    key_name: value for key_name, value in action.items() if key_name != "keys"
                }
                single_key_action["key"] = key
                normalized.append(parse_action(adapter.normalize(single_key_action)))
            continue
        if kind == "scroll":
            for axis_action in _split_scroll_axes(action):
                normalized.append(parse_action(adapter.normalize(axis_action)))
            continue
        normalized.append(parse_action(adapter.normalize(action)))
    return normalized


def _split_scroll_axes(action: dict[str, Any]) -> list[dict[str, Any]]:
    scroll_x = int(action.get("scroll_x", action.get("dx", 0)) or 0)
    scroll_y = int(action.get("scroll_y", action.get("dy", 0)) or 0)
    if not scroll_x and not scroll_y and "amount" in action:
        scroll_y = int(action["amount"])
    if not scroll_x and not scroll_y:
        raise ActionValidationError("OpenAI scroll action requires a non-zero delta")
    common = {
        key: value
        for key, value in action.items()
        if key not in {"scroll_x", "scroll_y", "dx", "dy", "amount"}
    }
    split: list[dict[str, Any]] = []
    if scroll_y:
        split.append({**common, "scroll_y": scroll_y})
    if scroll_x:
        split.append({**common, "scroll_x": scroll_x})
    return split


def _count_expanded_action_tree(action: ComputerAction) -> int:
    count = 1
    if action.type != "hold_key" or not action.actions:
        return count
    return count + sum(
        _count_expanded_action_tree(parse_action(nested_action)) for nested_action in action.actions
    )


def _preflight_policy(
    actions: list[ComputerAction],
    before_action: Any | None,
) -> None:
    if before_action is None:
        return
    for action in actions:
        _preflight_policy_tree(action, before_action)


def _preflight_policy_tree(action: ComputerAction, before_action: Any) -> None:
    decision = before_action(
        action,
        {
            "source": "openai-adapter",
            "coordinate_space": None,
            "coordinates_transformed": False,
        },
    )
    if decision and decision.decision != "allow":
        raise UnsupportedActionError(f"action denied: {action.type}")
    if action.type == "hold_key" and action.actions:
        for nested_action in action.actions:
            _preflight_policy_tree(parse_action(nested_action), before_action)


def _native_final_screenshot(
    batch_result: Any,
    *,
    actions: list[dict[str, Any]],
    normalized_action_count: int,
) -> Screenshot | None:
    final_action = actions[-1]
    if (final_action.get("type") or final_action.get("action")) != "screenshot":
        return None
    if len(batch_result.results) < normalized_action_count:
        return None
    action_results = batch_result.results[:normalized_action_count]
    if not all(item.ok for item in action_results):
        return None
    try:
        return Screenshot.model_validate(action_results[-1].output)
    except Exception:
        return None


def _sanitized_batch_failure(batch_result: Any) -> str:
    failed = next((item for item in batch_result.results if not item.ok), None)
    if failed is None:
        return "OpenAI computer action batch failed"
    return f"OpenAI computer action batch failed at index {failed.index}"


def _check_deadline(started_at: float, max_elapsed_seconds: float) -> None:
    if monotonic() - started_at > max_elapsed_seconds:
        _raise_deadline(max_elapsed_seconds)


def _raise_deadline(max_elapsed_seconds: float) -> None:
    raise RuntimeError(f"OpenAI computer loop exceeded {max_elapsed_seconds:g} seconds")


def _remaining_batch_timeout_ms(
    *,
    started_at: float,
    max_elapsed_seconds: float,
    max_batch_duration_ms: int,
) -> int:
    remaining_ms = int((max_elapsed_seconds - (monotonic() - started_at)) * 1000)
    if remaining_ms < 1:
        _raise_deadline(max_elapsed_seconds)
    return min(max_batch_duration_ms, remaining_ms)


def _remaining_request_timeout_seconds(
    started_at: float,
    max_elapsed_seconds: float,
) -> float:
    remaining = max_elapsed_seconds - (monotonic() - started_at)
    if remaining <= 0:
        _raise_deadline(max_elapsed_seconds)
    return remaining


def main() -> None:
    from openai import OpenAI

    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    config = ComputerConfig(
        desktop={"resolution": (1440, 900)},
        resources=ResourceConfig(profile="browser"),
        browser=BrowserConfig(kind="chromium", prewarm=True),
    )
    with ComputerSandbox.create(config=config, wait=True) as computer:
        computer.ensure_browser_ready(config)
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
