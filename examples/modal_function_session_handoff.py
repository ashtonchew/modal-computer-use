"""Borrow one Modal desktop for a complete stateful deployed Function trajectory.

Replace ``choose_action_with_model`` with an application-owned model call. The
SDK core remains provider-neutral, and this example never logs task text,
typed content, screenshots, endpoints, credentials, or resource identifiers.

The constants below are application choices, not SDK defaults. Select the
environment, region, CPU, memory, and container limits for your workload.
Warm capacity is disabled in this example.

Deploy this module once in the same explicit environment used by the owner:

``uv run modal deploy --env main examples/modal_function_session_handoff.py``
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from modal_computer_use import (
    AsyncComputerSandbox,
    ComputerConfig,
    ComputerSessionHandle,
    OperationResultUnavailableError,
)

_examples_dir = str(Path(__file__).resolve().parent)
if _examples_dir not in sys.path:
    sys.path.insert(0, _examples_dir)

from run_gateway.domain import TrajectoryOutcome, TrajectoryStatus  # noqa: E402

APP_NAME = "computer-use-function-handoff"
MODAL_ENVIRONMENT = "main"
FUNCTION_REGION = "us-west-2"  # Replace with one measured exact Modal region selector.
FUNCTION_CPU = 1.0
FUNCTION_MEMORY_MIB = 2048
FUNCTION_PYTHON_VERSION = "3.12"
FUNCTION_PACKAGE_SPEC = "modal-computer-use[modal]"
FUNCTION_MIN_CONTAINERS = 0
FUNCTION_MAX_CONTAINERS = 4
FUNCTION_TIMEOUT_SECONDS = 900
FUNCTION_RETRIES = 0
SANDBOX_CPU = 1.0
SANDBOX_MEMORY_MIB = 2048
SANDBOX_RESOURCE_PROFILE = "browser"
SANDBOX_IMAGE_SOURCE = "inline"
SANDBOX_BROWSER_KIND = "chromium"
SANDBOX_BROWSER_PREWARM = False
SANDBOX_BROWSER_GPU_MODE = "off"
SANDBOX_TIMEOUT_SECONDS = 900
SANDBOX_IDLE_TIMEOUT_SECONDS = None
SANDBOX_READINESS_TIMEOUT_SECONDS = 120
SANDBOX_WARM_POOL_CAPACITY = 0


def sandbox_configuration() -> ComputerConfig:
    """Build the explicit Sandbox half of the placed trajectory configuration."""
    if SANDBOX_WARM_POOL_CAPACITY != 0:
        raise ValueError(
            "use ComputerSandboxManager and WarmPoolPolicy for explicit Sandbox warm capacity"
        )
    return ComputerConfig(
        ingress="attested-tunnel",
        runtime={
            "modal_environment": MODAL_ENVIRONMENT,
            "modal_region": FUNCTION_REGION,
            "timeout_seconds": SANDBOX_TIMEOUT_SECONDS,
            "idle_timeout_seconds": SANDBOX_IDLE_TIMEOUT_SECONDS,
            "readiness_timeout_seconds": SANDBOX_READINESS_TIMEOUT_SECONDS,
        },
        resources={
            "profile": SANDBOX_RESOURCE_PROFILE,
            "cpu": SANDBOX_CPU,
            "memory_mib": SANDBOX_MEMORY_MIB,
        },
        image={"source": SANDBOX_IMAGE_SOURCE},
        browser={
            "kind": SANDBOX_BROWSER_KIND,
            "prewarm": SANDBOX_BROWSER_PREWARM,
            "gpu_mode": SANDBOX_BROWSER_GPU_MODE,
        },
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


async def choose_action_with_model(
    *, task: str, screenshot: object, turn: int
) -> dict[str, object]:
    """Placeholder for the application's provider-specific model call."""
    _ = (task, screenshot, turn)
    return {"type": "wait", "duration_ms": 50}


async def run_trajectory_body(
    handle: ComputerSessionHandle,
    task: str,
    *,
    run_id: str,
    function_region: str = FUNCTION_REGION,
    max_turns: int = 3,
) -> dict[str, str]:
    """Hold one borrowed connection across the repeated observe/decide/act loop."""
    async with handle.borrow_async(run_id=run_id, function_region=function_region) as computer:
        try:
            for turn in range(max_turns):
                screenshot = await computer.screenshots.full(format="png", processing="daemon")
                action = await choose_action_with_model(task=task, screenshot=screenshot, turn=turn)
                await computer.actions.run([action])
        except OperationResultUnavailableError:
            await computer.observe_after_result_loss()
            return TrajectoryOutcome(TrajectoryStatus.INDETERMINATE).as_dict()
    return TrajectoryOutcome(TrajectoryStatus.SUCCEEDED).as_dict()


async def run_distinct_trajectories_body(
    handles: list[ComputerSessionHandle],
    tasks: list[str],
    run_ids: list[str],
    *,
    function_region: str = FUNCTION_REGION,
) -> list[dict[str, str]]:
    """Run independent trajectories concurrently, one lease per desktop."""
    if not (len(handles) == len(tasks) == len(run_ids)):
        raise ValueError("handles, tasks, and run_ids must have equal lengths")
    if len({handle.sandbox_id for handle in handles}) != len(handles):
        raise ValueError("each concurrent trajectory requires a distinct desktop handle")
    return list(
        await asyncio.gather(
            *(
                run_trajectory_body(
                    handle,
                    task,
                    run_id=run_id,
                    function_region=function_region,
                )
                for handle, task, run_id in zip(handles, tasks, run_ids, strict=True)
            )
        )
    )


try:
    import modal
except ImportError:
    modal = None
    app = None
    run_trajectory = None
    run_distinct_trajectories = None
else:
    app = modal.App(APP_NAME)
    function_image = modal.Image.debian_slim(
        python_version=FUNCTION_PYTHON_VERSION
    ).pip_install(FUNCTION_PACKAGE_SPEC)

    @app.function(
        image=function_image,
        region=FUNCTION_REGION,
        cpu=FUNCTION_CPU,
        memory=FUNCTION_MEMORY_MIB,
        retries=FUNCTION_RETRIES,
        min_containers=FUNCTION_MIN_CONTAINERS,
        max_containers=FUNCTION_MAX_CONTAINERS,
        timeout=FUNCTION_TIMEOUT_SECONDS,
    )
    async def run_trajectory(
        handle: ComputerSessionHandle,
        task: str,
        run_id: str,
    ) -> dict[str, str]:
        return await run_trajectory_body(handle, task, run_id=run_id)

    @app.function(
        image=function_image,
        region=FUNCTION_REGION,
        cpu=FUNCTION_CPU,
        memory=FUNCTION_MEMORY_MIB,
        retries=FUNCTION_RETRIES,
        min_containers=FUNCTION_MIN_CONTAINERS,
        max_containers=FUNCTION_MAX_CONTAINERS,
        timeout=FUNCTION_TIMEOUT_SECONDS,
    )
    async def run_distinct_trajectories(
        handles: list[ComputerSessionHandle],
        tasks: list[str],
        run_ids: list[str],
    ) -> list[dict[str, str]]:
        return await run_distinct_trajectories_body(handles, tasks, run_ids)


async def run_example(
    *,
    task: str,
    spawn: bool = False,
    cancel_spawned: bool = False,
) -> dict[str, Any]:
    """Own the desktop through one native async deployed Function invocation."""
    if modal is None or app is None or run_trajectory is None:
        raise ImportError("Modal is required to run this example")
    if cancel_spawned and not spawn:
        raise ValueError("cancel_spawned requires spawn=True")
    deployed = modal.Function.from_name(
        APP_NAME,
        "run_trajectory",
        environment_name=MODAL_ENVIRONMENT,
    )
    config = sandbox_configuration()
    async with AsyncComputerSandbox.create(config=config, app_name=APP_NAME) as owner:
        handle = owner.session_handle()
        run_id = f"trajectory_{uuid.uuid4().hex}"
        if not spawn:
            return {
                "mode": "remote",
                "result": await deployed.remote.aio(handle, task, run_id),
            }
        call = await deployed.spawn.aio(handle, task, run_id)
        if cancel_spawned:
            await call.cancel.aio()
            with suppress(Exception):
                await call.get.aio()
            return {"mode": "spawn", "cancel_requested": True}
        return {"mode": "spawn", "result": await call.get.aio()}


if __name__ == "__main__":
    asyncio.run(run_example(task="Replace this placeholder task before running"))
