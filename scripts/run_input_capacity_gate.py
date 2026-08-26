"""Run the authorized same-runtime normalized-input capacity gate.

This command is intentionally fail-closed.  It requires ``--authorize`` and
the standard Modal token environment variables before it can allocate a
billable Sandbox.  The default local-entrypoint invocation is therefore a
configuration check, not a live run.

Example (explicit authorization required)::

    modal run --env main scripts/run_input_capacity_gate.py \
      --source-sha <40-character-git-sha> --authorize
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import modal

from modal_computer_use import AsyncComputerSandbox, ComputerConfig, ComputerSessionHandle
from modal_computer_use.benchmarks.input_capacity_gate import (
    _RESOURCE_SAMPLE_SCRIPT,
    CAPACITY_BENCHMARK,
    INPUT_RATE_LIMIT_POLICY,
    InputCapacitySettings,
    execute_input_capacity_gate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = os.environ.get(
    "MODAL_COMPUTER_USE_INPUT_CAPACITY_APP_NAME",
    "modal-computer-use-input-capacity",
)
OWNER = os.environ.get(
    "MODAL_COMPUTER_USE_INPUT_CAPACITY_OWNER",
    "modal-computer-use-input-capacity-owner",
)
MODAL_ENVIRONMENT = os.environ.get("MODAL_COMPUTER_USE_INPUT_CAPACITY_ENVIRONMENT", "main")
CLOUD = os.environ.get("MODAL_COMPUTER_USE_INPUT_CAPACITY_CLOUD", "aws")
REGION = os.environ.get("MODAL_COMPUTER_USE_INPUT_CAPACITY_REGION", "us-west")
FUNCTION_CPU = 1.0
FUNCTION_MEMORY_MIB = 2_048
FUNCTION_MIN_CONTAINERS = 0
FUNCTION_MAX_CONTAINERS = 1
FUNCTION_TIMEOUT_SECONDS = 900
SANDBOX_CPU = 1.0
SANDBOX_MEMORY_MIB = 2_048
SANDBOX_TIMEOUT_SECONDS = 900
SANDBOX_READINESS_TIMEOUT_SECONDS = 180
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_OBSERVED_CLOUD_LABELS = {
    "CLOUD_PROVIDER_AWS": "aws",
    "CLOUD_PROVIDER_AZURE": "azure",
    "CLOUD_PROVIDER_GCP": "gcp",
    "CLOUD_PROVIDER_OCI": "oci",
}

app = modal.App(APP_NAME)
function_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_pyproject(
        PROJECT_ROOT / "pyproject.toml",
        optional_dependencies=["modal"],
    )
    .add_local_python_source("modal_computer_use", copy=True)
)


def require_live_authorization(
    authorized: bool,
    *,
    credential_probe: Callable[[], bool] | None = None,
) -> None:
    """Reject accidental live or billable execution before Modal I/O."""

    if not authorized:
        raise PermissionError(
            "input capacity gate is live and billable; pass --authorize explicitly"
        )
    has_environment_credentials = all(
        os.environ.get(name) for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")
    )
    if not has_environment_credentials and not (
        credential_probe or _modal_profile_authenticated
    )():
        raise PermissionError(
            "input capacity gate requires Modal credentials or an authenticated Modal profile"
        )


def _modal_profile_authenticated() -> bool:
    modal_cli = shutil.which("modal")
    if modal_cli is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
            [modal_cli, "token", "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def verify_clean_source_revision(source_sha: str) -> None:
    """Fail before Sandbox allocation unless the source revision is exact and clean."""

    if _SOURCE_SHA.fullmatch(source_sha) is None:
        raise ValueError("source_sha must be one full lowercase Git SHA")
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to verify the capacity-gate source")
    head = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [git, "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != source_sha:
        raise ValueError("source_sha does not match git HEAD")
    status = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [git, "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ValueError("worktree must be clean before a live capacity-gate run")


def _function_placement() -> dict[str, str]:
    observed_cloud = _OBSERVED_CLOUD_LABELS.get(os.environ.get("MODAL_CLOUD_PROVIDER", ""))
    observed_region = os.environ.get("MODAL_REGION")
    if observed_cloud != CLOUD or observed_region != REGION:
        raise RuntimeError(
            "Function placement does not match the requested capacity-gate placement"
        )
    return {"cloud": observed_cloud, "region": observed_region}


def _exact_placement(value: Mapping[str, Any], *, name: str) -> dict[str, str]:
    raw_cloud = value.get("cloud")
    cloud = _OBSERVED_CLOUD_LABELS.get(raw_cloud, raw_cloud) if isinstance(raw_cloud, str) else None
    region = value.get("region")
    if cloud != CLOUD or region != REGION:
        raise RuntimeError(f"{name} placement does not match the requested placement")
    if not isinstance(cloud, str) or not isinstance(region, str):
        raise RuntimeError(f"{name} placement is unavailable")
    return {"cloud": cloud, "region": region}


@app.function(
    image=function_image,
    cloud=CLOUD,
    region=REGION,
    cpu=FUNCTION_CPU,
    memory=FUNCTION_MEMORY_MIB,
    retries=0,
    min_containers=FUNCTION_MIN_CONTAINERS,
    max_containers=FUNCTION_MAX_CONTAINERS,
    timeout=FUNCTION_TIMEOUT_SECONDS,
    restrict_modal_access=False,
)
async def run_input_capacity_function(
    mode: str,
    handle: ComputerSessionHandle | None = None,
    settings_payload: dict[str, Any] | None = None,
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    placement = _function_placement()
    if mode == "probe":
        return placement
    if mode != "measure" or handle is None or settings_payload is None or configuration is None:
        raise ValueError("input capacity Function inputs are incomplete")
    settings = InputCapacitySettings(**settings_payload)
    measured_configuration = dict(configuration)
    observed = dict(measured_configuration.get("observed_placement", {}))
    observed["function"] = placement
    measured_configuration["observed_placement"] = observed
    borrow = cast(
        Callable[[], AbstractAsyncContextManager[Any]],
        lambda: handle.borrow_async(
            run_id=f"input-capacity-{settings.source_sha[:12]}",
            function_region=settings.requested_region,
            readiness_timeout=SANDBOX_READINESS_TIMEOUT_SECONDS,
        ),
    )
    target = await modal.Sandbox.from_id.aio(handle.sandbox_id)

    async def resource_sampler() -> Mapping[str, Any]:
        process = await target.exec.aio(
            "python",
            "-c",
            _RESOURCE_SAMPLE_SCRIPT,
            timeout=10,
        )
        raw = (await process.stdout.read.aio()).strip()
        decoded = json.loads(raw)
        if not isinstance(decoded, Mapping):
            raise RuntimeError("Sandbox resource sample was malformed")
        return decoded

    return await execute_input_capacity_gate(
        borrow,
        settings=settings,
        configuration=measured_configuration,
        resource_sampler=resource_sampler,
    )


def _configuration(
    settings: InputCapacitySettings,
    *,
    target_placement: dict[str, str],
    function_placement: dict[str, str],
) -> dict[str, Any]:
    return {
        "caller_topology": "one-application-owned-modal-function",
        "requested_placement": {
            "cloud": settings.requested_cloud,
            "region": settings.requested_region,
        },
        "observed_placement": {
            "target": target_placement,
            "function": function_placement,
        },
        "resources": {
            "function": {"cpu": FUNCTION_CPU, "memory_mib": FUNCTION_MEMORY_MIB},
            "sandbox": {"cpu": settings.cpu, "memory_mib": settings.memory_mib},
        },
        "image_identity": f"inline-source-{settings.source_sha}",
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": settings.input_backend,
        "input_rate_limit_policy": INPUT_RATE_LIMIT_POLICY,
        "input_rate_limit_per_sec": settings.input_rate_limit_per_sec,
        "input_rate_limit_burst": settings.input_rate_limit_burst,
        "warm_capacity": {
            "function_min_containers": FUNCTION_MIN_CONTAINERS,
            "sandbox_pool_capacity": 0,
        },
    }


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"capacity-gate output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@app.local_entrypoint()
async def main(
    source_sha: str,
    authorize: bool = False,
    output: str = "benchmark-results/candidates/input-capacity-live.json",
    input_rate_limit_per_sec: int = 2_000,
    input_rate_limit_burst: int = 4_000,
    batches: int = 80,
    warmup_batches: int = 4,
) -> None:
    require_live_authorization(authorize)
    verify_clean_source_revision(source_sha)
    settings = InputCapacitySettings(
        requested_cloud=CLOUD,
        requested_region=REGION,
        source_sha=source_sha,
        input_rate_limit_per_sec=input_rate_limit_per_sec,
        input_rate_limit_burst=input_rate_limit_burst,
        batches=batches,
        warmup_batches=warmup_batches,
    )
    settings.validate()
    async with AsyncComputerSandbox.create(
        config=ComputerConfig(
            ingress="attested-tunnel",
            expose_vnc="off",
            runtime={
                "modal_environment": MODAL_ENVIRONMENT,
                "modal_region": REGION,
                "timeout_seconds": SANDBOX_TIMEOUT_SECONDS,
                "readiness_timeout_seconds": SANDBOX_READINESS_TIMEOUT_SECONDS,
            },
            resources={
                "profile": "standard",
                "cpu": settings.cpu,
                "memory_mib": settings.memory_mib,
            },
            image={"source": "inline"},
            actions={
                "input_backend": settings.input_backend,
                "input_rate_limit_per_sec": settings.input_rate_limit_per_sec,
                "input_rate_limit_burst": settings.input_rate_limit_burst,
            },
        ),
        app_name=APP_NAME,
        owner=OWNER,
        cloud=CLOUD,
        tags={"computer-use.benchmark": CAPACITY_BENCHMARK},
    ) as owner:
        target_placement = _exact_placement(await owner.runtime_placement(), name="target")
        handle = owner.session_handle()
        function_placement = _exact_placement(
            await run_input_capacity_function.remote.aio("probe"),
            name="Function",
        )
        payload = await run_input_capacity_function.remote.aio(
            "measure",
            handle,
            asdict(settings),
            _configuration(
                settings,
                target_placement=target_placement,
                function_placement=function_placement,
            ),
        )
    output_path = Path(output)
    _write_new(output_path, payload)
    print(
        json.dumps(
            {
                "benchmark": payload.get("benchmark"),
                "status": payload.get("status"),
                "weighted_tokens_per_sec": payload.get("summary", {}).get(
                    "weighted_tokens_per_sec"
                ),
                "output": output,
            },
            sort_keys=True,
        )
    )
    if payload.get("status") != "complete":
        raise RuntimeError("input capacity gate did not pass")
