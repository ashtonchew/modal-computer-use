"""Run an OpenAI computer-use loop in a placed Modal Function.

Install the runtime extras with ``uv sync --extra modal --extra openai`` and
create the ``openai-api-key`` Modal Secret before you deploy this file. The
async owner creates one desktop. It sends a versioned handle to a Function in
the same exact region. That Function borrows the desktop once for the full
model loop. Provider calls stay in this application-owned example.

The constants below are application choices, not SDK defaults. Review the
region, environment, resources, image, browser, timeouts, and capacity before
you deploy. Warm capacity is off.
"""

from __future__ import annotations

import asyncio
import uuid
from time import monotonic
from typing import Any

from modal_computer_use import (
    AsyncComputerSandbox,
    BrowserConfig,
    ComputerConfig,
    ComputerSessionHandle,
    ResourceConfig,
)
from modal_computer_use.adapters.openai import (
    OpenAIAdapter,
    openai_computer_call_output,
)
from modal_computer_use.errors import ActionValidationError, UnsupportedActionError
from modal_computer_use.models import ComputerAction, parse_action

DEFAULT_MODEL = "gpt-5.6"
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_TRAJECTORY_ACTIONS = 200
DEFAULT_MAX_BATCH_ACTIONS = 50
DEFAULT_MAX_ELAPSED_SECONDS = 300.0
DEFAULT_MAX_ACTION_TIMEOUT_MS = 30_000
DEFAULT_MAX_BATCH_DURATION_MS = 30_000
COMPUTER_TOOL = {"type": "computer"}
APP_NAME = "openai-computer-use"
MODAL_ENVIRONMENT = "main"
MODAL_REGION = "us-west-2"  # Replace with one measured exact Modal region selector.
OPENAI_CREDENTIAL_REFERENCE = "openai-api-key"
FUNCTION_CPU = 1.0
FUNCTION_MEMORY_MIB = 2048
FUNCTION_PYTHON_VERSION = "3.12"
FUNCTION_PACKAGE_SPEC = "modal-computer-use[modal,openai]"
FUNCTION_TIMEOUT_SECONDS = 900
FUNCTION_RETRIES = 0
FUNCTION_MIN_CONTAINERS = 0
FUNCTION_MAX_CONTAINERS = 4
SANDBOX_CPU = 1.0
SANDBOX_MEMORY_MIB = 2048
SANDBOX_RESOURCE_PROFILE = "browser"
SANDBOX_IMAGE_SOURCE = "inline"
SANDBOX_BROWSER_KIND = "chromium"
SANDBOX_BROWSER_PREWARM = True
SANDBOX_BROWSER_GPU_MODE = "off"
SANDBOX_RESOLUTION = (1440, 900)
SANDBOX_TIMEOUT_SECONDS = 900
SANDBOX_IDLE_TIMEOUT_SECONDS = None
SANDBOX_READINESS_TIMEOUT_SECONDS = 120
SANDBOX_WARM_POOL_CAPACITY = 0


class _OpenAILimitError(RuntimeError):
    pass


async def run_openai_computer_loop(
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
        response = await client.responses.create(**request)
        _check_deadline(started_at, max_elapsed_seconds)
        calls = [item for item in response.output if getattr(item, "type", None) == "computer_call"]
        if not calls:
            return response
        if turn == max_turns - 1:
            raise RuntimeError(f"OpenAI computer loop exceeded {max_turns} turns")

        preflighted_calls: list[tuple[Any, list[ComputerAction], int]] = []
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
                preflighted_calls.append((call, normalized_actions, expanded_action_count))
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
        for call, normalized_actions, expanded_action_count in preflighted_calls:
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
                action.model_copy(update={"timeout_ms": allocated_action_timeout_ms})
                for action in normalized_actions
            ]
            try:
                step_result = await computer.step(
                    bounded_actions,
                    continue_on_error=False,
                    call_id=call.call_id,
                    max_action_timeout_ms=allocated_action_timeout_ms,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"OpenAI computer action batch failed: {type(exc).__name__}"
                ) from None
            batch_result = step_result.actions
            if not batch_result.ok:
                raise RuntimeError(_sanitized_batch_failure(batch_result))
            _check_deadline(started_at, max_elapsed_seconds)
            outputs.append(
                openai_computer_call_output(
                    step_result.screenshot,
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


def sandbox_configuration() -> ComputerConfig:
    """Build the explicit Sandbox configuration for this example."""
    return ComputerConfig(
        ingress="attested-tunnel",
        desktop={"resolution": SANDBOX_RESOLUTION},
        runtime={
            "modal_environment": MODAL_ENVIRONMENT,
            "modal_region": MODAL_REGION,
            "timeout_seconds": SANDBOX_TIMEOUT_SECONDS,
            "idle_timeout_seconds": SANDBOX_IDLE_TIMEOUT_SECONDS,
            "readiness_timeout_seconds": SANDBOX_READINESS_TIMEOUT_SECONDS,
        },
        resources=ResourceConfig(
            profile=SANDBOX_RESOURCE_PROFILE,
            cpu=SANDBOX_CPU,
            memory_mib=SANDBOX_MEMORY_MIB,
        ),
        image={"source": SANDBOX_IMAGE_SOURCE},
        browser=BrowserConfig(
            kind=SANDBOX_BROWSER_KIND,
            prewarm=SANDBOX_BROWSER_PREWARM,
            gpu_mode=SANDBOX_BROWSER_GPU_MODE,
        ),
    )


def resolved_trajectory_configuration() -> dict[str, Any]:
    """Return the cost and placement choices that this example will request."""
    resolved = sandbox_configuration().resolved_cost_and_placement()
    resolved["function"] = {
        "cpu": FUNCTION_CPU,
        "memory_mib": FUNCTION_MEMORY_MIB,
        "image": {
            "base": "debian_slim",
            "python_version": FUNCTION_PYTHON_VERSION,
            "package": FUNCTION_PACKAGE_SPEC,
        },
        "timeout_seconds": FUNCTION_TIMEOUT_SECONDS,
        "retries": FUNCTION_RETRIES,
        "min_containers": FUNCTION_MIN_CONTAINERS,
        "max_containers": FUNCTION_MAX_CONTAINERS,
    }
    resolved["warm_capacity"] = {
        "function_min_containers": FUNCTION_MIN_CONTAINERS,
        "sandbox_pool_capacity": SANDBOX_WARM_POOL_CAPACITY,
    }
    return resolved


async def run_openai_trajectory_body(
    handle: ComputerSessionHandle,
    task: str,
    *,
    run_id: str,
    client: Any,
    function_region: str = MODAL_REGION,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> Any:
    """Borrow one desktop once, then run the complete application model loop."""
    async with handle.borrow_async(run_id=run_id, function_region=function_region) as computer:
        return await run_openai_computer_loop(
            client=client,
            computer=computer,
            task=task,
            model=model,
            max_turns=max_turns,
        )


try:
    import modal
except ImportError:
    modal = None
    app = None
    run_openai_trajectory = None
else:
    app = modal.App(APP_NAME)
    function_image = modal.Image.debian_slim(
        python_version=FUNCTION_PYTHON_VERSION
    ).pip_install(FUNCTION_PACKAGE_SPEC)

    @app.function(
        image=function_image,
        region=MODAL_REGION,
        cpu=FUNCTION_CPU,
        memory=FUNCTION_MEMORY_MIB,
        timeout=FUNCTION_TIMEOUT_SECONDS,
        retries=FUNCTION_RETRIES,
        min_containers=FUNCTION_MIN_CONTAINERS,
        max_containers=FUNCTION_MAX_CONTAINERS,
        secrets=[
            modal.Secret.from_name(
                OPENAI_CREDENTIAL_REFERENCE,
                required_keys=["OPENAI_API_KEY"],
            )
        ],
    )
    async def run_openai_trajectory(
        handle: ComputerSessionHandle,
        task: str,
        model: str,
        run_id: str,
    ) -> str:
        from openai import AsyncOpenAI

        async with AsyncOpenAI() as client:
            response = await run_openai_trajectory_body(
                handle,
                task,
                model=model,
                run_id=run_id,
                client=client,
            )
        return response.output_text


async def run_example(*, task: str, model: str = DEFAULT_MODEL) -> str:
    """Create one desktop and pass its handle to the placed Function."""
    if modal is None or app is None or run_openai_trajectory is None:
        raise ImportError("Modal is required to run this example")
    deployed = modal.Function.from_name(
        APP_NAME,
        "run_openai_trajectory",
        environment_name=MODAL_ENVIRONMENT,
    )
    config = sandbox_configuration()
    async with AsyncComputerSandbox.create(config=config, app_name=APP_NAME) as owner:
        handle = owner.session_handle()
        run_id = f"openai_trajectory_{uuid.uuid4().hex}"
        return await deployed.remote.aio(handle, task, model, run_id)


def main() -> None:
    output_text = asyncio.run(
        run_example(
            task=(
                "Open https://example.com in the browser. Verify that the page title is "
                "'Example Domain', then report the title and stop. Use the computer tool "
                "for UI interaction. Do not sign in, submit forms, or change data."
            )
        )
    )
    print(output_text)


if __name__ == "__main__":
    main()
