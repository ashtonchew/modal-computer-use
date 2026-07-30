"""Borrow one Modal desktop for a complete stateful deployed Function trajectory.

Replace ``choose_action_with_model`` with an application-owned model call. The
SDK core remains provider-neutral, and this example never logs task text,
typed content, screenshots, endpoints, credentials, or resource identifiers.

Deploy this module once before calling ``run_example``:

``uv run modal deploy examples/modal_function_session_handoff.py``
"""

from __future__ import annotations

from typing import Any

from modal_computer_use import ComputerConfig, ComputerSandbox, ComputerSessionHandle

FUNCTION_REGION = "us-west"  # Replace with one measured Modal region selector.
APP_NAME = "computer-use-function-handoff"


def choose_action_with_model(*, task: str, screenshot: object, turn: int) -> dict[str, object]:
    """Placeholder for the application's provider-specific model call."""
    _ = (task, screenshot, turn)
    return {"type": "wait", "duration_ms": 50}


def run_trajectory_body(
    handle: ComputerSessionHandle,
    task: str,
    *,
    function_region: str = FUNCTION_REGION,
    max_turns: int = 3,
) -> dict[str, object]:
    """Hold one borrowed connection across the repeated observe/decide/act loop."""
    with handle.borrow(function_region=function_region) as computer:
        for turn in range(max_turns):
            screenshot = computer.screenshots.full(format="png", processing="daemon")
            action = choose_action_with_model(task=task, screenshot=screenshot, turn=turn)
            computer.actions.run([action])
    return {"completed": True, "turns": max_turns}


try:
    import modal
except ImportError:
    modal = None
    app = None
    run_trajectory = None
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
    def run_trajectory(
        handle: ComputerSessionHandle,
        task: str,
    ) -> dict[str, object]:
        return run_trajectory_body(handle, task)


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
        runtime={"modal_region": FUNCTION_REGION},
    )
    with ComputerSandbox.create(config=config, app_name=APP_NAME) as owner:
        handle = owner.session_handle()
        if not spawn:
            return {"mode": "remote", "result": deployed.remote(handle, task)}
        call = deployed.spawn(handle, task)
        if cancel_spawned:
            call.cancel()
            return {"mode": "spawn", "cancel_requested": True}
        return {"mode": "spawn", "result": call.get()}


if __name__ == "__main__":
    run_example(task="Replace this placeholder task before running")
