"""Run one bounded deployed-Function session handoff smoke.

This test App is deployed only by the protected manual release-validation job.
It never returns or logs screenshots, credentials, endpoints, provider object
identifiers, call identifiers, prompts, or typed content.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import modal

from modal_computer_use import ComputerSessionHandle

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = os.environ.get(
    "MODAL_COMPUTER_USE_HANDOFF_APP_NAME",
    "modal-computer-use-handoff-smoke-local",
)
FUNCTION_REGION = os.environ.get("MODAL_COMPUTER_USE_HANDOFF_REGION", "us-west-2")
if re.fullmatch(r"[a-z][a-z0-9]*-[a-z][a-z0-9]*-[0-9][a-z0-9]*", FUNCTION_REGION) is None:
    raise ValueError("the handoff smoke requires one exact Modal region")
FUNCTION_TIMEOUT_SECONDS = 300
BORROW_READINESS_TIMEOUT_SECONDS = 180
SAFE_RESULT_FIELDS = frozenset(
    {
        "borrow_succeeded",
        "screenshot_succeeded",
        "action_succeeded",
        "width",
        "height",
        "function_cloud",
        "function_region",
    }
)

app = modal.App(APP_NAME)
function_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_pyproject(
        PROJECT_ROOT / "pyproject.toml",
        optional_dependencies=["modal"],
    )
    .add_local_python_source("modal_computer_use", copy=True)
)


def _required_function_placement() -> tuple[str, str]:
    cloud = os.environ.get("MODAL_CLOUD_PROVIDER")
    region = os.environ.get("MODAL_REGION")
    if not cloud or not region:
        raise RuntimeError("Modal Function placement is unavailable")
    return cloud, region


async def run_handoff_smoke_body(
    handle: ComputerSessionHandle,
    run_id: str,
) -> dict[str, object]:
    """Borrow once, observe once, and apply one harmless sequenced action."""
    function_cloud, function_region = _required_function_placement()
    async with handle.borrow_async(
        run_id=run_id,
        function_region=FUNCTION_REGION,
        readiness_timeout=BORROW_READINESS_TIMEOUT_SECONDS,
    ) as computer:
        screenshot = await computer.screenshots.full(
            format="png",
            processing="daemon",
            storage="inline",
        )
        action = await computer.actions.run(
            [{"type": "wait", "duration_ms": 50}],
            continue_on_error=False,
        )

    screenshot_succeeded = (
        screenshot.width > 0
        and screenshot.height > 0
        and screenshot.size_bytes > 0
        and (screenshot.bytes is not None or screenshot.data_base64 is not None)
        and screenshot.artifact_uri is None
    )
    return {
        "borrow_succeeded": True,
        "screenshot_succeeded": screenshot_succeeded,
        "action_succeeded": action.ok,
        "width": screenshot.width,
        "height": screenshot.height,
        "function_cloud": function_cloud,
        "function_region": function_region,
    }


@app.function(
    image=function_image,
    region=FUNCTION_REGION,
    retries=0,
    min_containers=0,
    max_containers=1,
    timeout=FUNCTION_TIMEOUT_SECONDS,
    restrict_modal_access=False,
)
async def run_handoff_smoke(
    handle: ComputerSessionHandle,
    run_id: str,
) -> dict[str, object]:
    return await run_handoff_smoke_body(handle, run_id)
