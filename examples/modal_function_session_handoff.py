"""Borrow one Modal desktop for a complete stateful deployed Function trajectory.

Replace ``choose_action_with_model`` with an application-owned model call. The
SDK core remains provider-neutral, and this example never logs task text,
typed content, screenshots, endpoints, credentials, or resource identifiers.

Deploy this module once before calling ``run_example``:

``uv run modal deploy examples/modal_function_session_handoff.py``
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import Any

from modal_computer_use import ComputerConfig, ComputerSandbox, ComputerSessionHandle

FUNCTION_REGION = "us-west"  # Replace with one measured Modal region selector.
APP_NAME = "computer-use-function-handoff"


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
) -> dict[str, object]:
    """Hold one borrowed connection across the repeated observe/decide/act loop."""
    async with handle.borrow_async(
        run_id=run_id, function_region=function_region
    ) as computer:
        for turn in range(max_turns):
            screenshot = await computer.screenshots.full(
                format="png", processing="daemon"
            )
            action = await choose_action_with_model(
                task=task, screenshot=screenshot, turn=turn
            )
            await computer.actions.run([action])
    return {"completed": True, "turns": max_turns}


async def run_distinct_trajectories_body(
    handles: list[ComputerSessionHandle],
    tasks: list[str],
    run_ids: list[str],
    *,
    function_region: str = FUNCTION_REGION,
) -> list[dict[str, object]]:
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
    function_image = modal.Image.debian_slim().pip_install("modal-computer-use[modal]")

    @app.function(
        image=function_image,
        region=FUNCTION_REGION,
        retries=0,
        min_containers=0,
        max_containers=4,
        timeout=900,
    )
    async def run_trajectory(
        handle: ComputerSessionHandle,
        task: str,
        run_id: str,
    ) -> dict[str, object]:
        return await run_trajectory_body(handle, task, run_id=run_id)

    @app.function(
        image=function_image,
        region=FUNCTION_REGION,
        retries=0,
        min_containers=0,
        max_containers=4,
        timeout=900,
    )
    async def run_distinct_trajectories(
        handles: list[ComputerSessionHandle],
        tasks: list[str],
        run_ids: list[str],
    ) -> list[dict[str, object]]:
        return await run_distinct_trajectories_body(handles, tasks, run_ids)


def run_example(
    *,
    task: str,
    spawn: bool = False,
    cancel_spawned: bool = False,
) -> dict[str, Any]:
    """Own the desktop through one native deployed Function invocation."""
    if modal is None or app is None or run_trajectory is None:
        raise ImportError("Modal is required to run this example")
    if cancel_spawned and not spawn:
        raise ValueError("cancel_spawned requires spawn=True")
    deployed = modal.Function.from_name(APP_NAME, "run_trajectory")
    config = ComputerConfig(
        ingress="attested-tunnel",
        runtime={"modal_environment": "main", "modal_region": FUNCTION_REGION},
    )
    with ComputerSandbox.create(config=config, app_name=APP_NAME) as owner:
        handle = owner.session_handle()
        run_id = f"trajectory_{uuid.uuid4().hex}"
        if not spawn:
            return {"mode": "remote", "result": deployed.remote(handle, task, run_id)}
        call = deployed.spawn(handle, task, run_id)
        if cancel_spawned:
            call.cancel()
            with suppress(Exception):
                call.get()
            return {"mode": "spawn", "cancel_requested": True}
        return {"mode": "spawn", "result": call.get()}


if __name__ == "__main__":
    run_example(task="Replace this placeholder task before running")
