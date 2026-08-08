"""Run the live, same-topology Computer Step promotion benchmark.

This application-owned Modal runner is intentionally separate from the historical
article-parity benchmark. It does not run during tests or release automation.

Run it only with explicit authorization and a clean, committed source revision:

    modal run --env main scripts/run_step_promotion.py \
      --source-sha <40-character-git-sha> \
      --output-dir benchmark-results/candidates/computer-step-<date>
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import modal

from modal_computer_use import AsyncComputerSandbox, ComputerConfig, ComputerSessionHandle
from modal_computer_use.benchmarks.step_promotion_gate import (
    CANDIDATE_ARM,
    MINIMUM_SAMPLES_PER_ARM,
    PRIOR_PUBLIC_ARM,
    compare_step_promotion_artifacts,
)
from modal_computer_use.benchmarks.step_promotion_measurement import (
    measure_interleaved_step_promotion,
)
from modal_computer_use.latency import SessionStartupTiming

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = os.environ.get(
    "MODAL_COMPUTER_USE_STEP_PROMOTION_APP_NAME",
    "modal-computer-use-step-promotion",
)
OWNER = os.environ.get(
    "MODAL_COMPUTER_USE_STEP_PROMOTION_OWNER",
    "modal-computer-use-step-promotion-owner",
)
MODAL_ENVIRONMENT = os.environ.get("MODAL_COMPUTER_USE_STEP_PROMOTION_ENVIRONMENT", "main")
CLOUD = os.environ.get("MODAL_COMPUTER_USE_STEP_PROMOTION_CLOUD", "aws")
REGION = os.environ.get("MODAL_COMPUTER_USE_STEP_PROMOTION_REGION", "us-west-2")
FUNCTION_CPU = 1.0
FUNCTION_MEMORY_MIB = 2048
FUNCTION_MIN_CONTAINERS = 0
FUNCTION_MAX_CONTAINERS = 1
FUNCTION_TIMEOUT_SECONDS = 900
SANDBOX_CPU = 1.0
SANDBOX_MEMORY_MIB = 2048
SANDBOX_TIMEOUT_SECONDS = 900
SANDBOX_READINESS_TIMEOUT_SECONDS = 180
SANDBOX_WARM_POOL_CAPACITY = 0
ACTION_BATCH = ({"type": "click", "x": 512, "y": 384, "button": "left"},)
_EXACT_REGION = re.compile(r"^[a-z][a-z0-9]*-[a-z][a-z0-9]*-[0-9][a-z0-9]*$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_OBSERVED_CLOUD_LABELS = {
    "CLOUD_PROVIDER_AWS": "aws",
    "CLOUD_PROVIDER_AZURE": "azure",
    "CLOUD_PROVIDER_GCP": "gcp",
    "CLOUD_PROVIDER_OCI": "oci",
}


@dataclass(frozen=True)
class StepPromotionSettings:
    """Explicit placement, cost, and measurement choices for one live run."""

    app_name: str
    owner: str
    environment: str
    cloud: str
    region: str
    source_sha: str
    sample_count: int = MINIMUM_SAMPLES_PER_ARM
    warmup_iterations: int = 2
    schedule_seed: int = 20260808
    bootstrap_seed: int = 20260808
    bootstrap_resamples: int = 2_000
    function_cpu: float = FUNCTION_CPU
    function_memory_mib: int = FUNCTION_MEMORY_MIB
    sandbox_cpu: float = SANDBOX_CPU
    sandbox_memory_mib: int = SANDBOX_MEMORY_MIB

    def validate(self) -> None:
        for name, value in (
            ("app_name", self.app_name),
            ("owner", self.owner),
            ("environment", self.environment),
            ("cloud", self.cloud),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if _EXACT_REGION.fullmatch(self.region) is None:
            raise ValueError("region must be one exact Modal region")
        if _SOURCE_SHA.fullmatch(self.source_sha) is None:
            raise ValueError("source_sha must be one full Git SHA")
        if self.sample_count < MINIMUM_SAMPLES_PER_ARM:
            raise ValueError(
                f"sample_count must be at least {MINIMUM_SAMPLES_PER_ARM}"
            )
        if self.warmup_iterations < 0:
            raise ValueError("warmup_iterations must be nonnegative")
        if self.schedule_seed < 1 or self.bootstrap_seed < 1:
            raise ValueError("benchmark seeds must be positive")
        if self.bootstrap_resamples < 100:
            raise ValueError("bootstrap_resamples must be at least 100")
        for name, value in (
            ("function_cpu", self.function_cpu),
            ("sandbox_cpu", self.sandbox_cpu),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name, value in (
            ("function_memory_mib", self.function_memory_mib),
            ("sandbox_memory_mib", self.sandbox_memory_mib),
        ):
            if value < 128:
                raise ValueError(f"{name} must be at least 128")


class StepPromotionRuntime(Protocol):
    """Adapter seam for ownership and placed Function dispatch."""

    def own(
        self,
        settings: StepPromotionSettings,
        timing: SessionStartupTiming,
    ) -> Any: ...

    async def probe(self) -> dict[str, str]: ...

    async def measure(
        self,
        handle: object,
        *,
        run_id: str,
        configuration: dict[str, Any],
        lifecycle_timings: dict[str, float],
        settings: StepPromotionSettings,
    ) -> dict[str, dict[str, Any]]: ...


async def execute_live_step_promotion(
    runtime: StepPromotionRuntime,
    *,
    settings: StepPromotionSettings,
    output_dir: Path,
    clock: Any = time.perf_counter,
    source_verifier: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Own, probe placement, measure once, gate, write, and clean up one run."""
    settings.validate()
    (source_verifier or verify_clean_source_revision)(settings.source_sha)
    output_paths = _output_paths(output_dir)
    timing = SessionStartupTiming(clock=clock)

    async with runtime.own(settings, timing) as owner:
        target_placement = _exact_placement(
            await owner.runtime_placement(),
            name="target",
            expected_cloud=settings.cloud,
            expected_region=settings.region,
        )
        handle = owner.session_handle()
        cold_start_ms, startup_ms = _startup_timings(timing)
        dispatch_started = clock()
        function_placement = _exact_placement(
            await runtime.probe(),
            name="Function",
            expected_cloud=settings.cloud,
            expected_region=settings.region,
        )
        dispatch_ms = max(0.0, (clock() - dispatch_started) * 1000.0)
        artifacts = await runtime.measure(
            handle,
            run_id=f"computer-step-promotion-{uuid.uuid4().hex}",
            configuration=_configuration(
                settings,
                handle=handle,
                target_placement=target_placement,
                function_placement=function_placement,
            ),
            lifecycle_timings={
                "cold_start_ms": cold_start_ms,
                "startup_ms": startup_ms,
                "dispatch_ms": dispatch_ms,
            },
            settings=settings,
        )

    prior = artifacts[PRIOR_PUBLIC_ARM]
    candidate = artifacts[CANDIDATE_ARM]
    decision = compare_step_promotion_artifacts(prior, candidate)
    _write_new_json(output_paths[PRIOR_PUBLIC_ARM], prior)
    _write_new_json(output_paths[CANDIDATE_ARM], candidate)
    _write_new_json(output_paths["decision"], decision)
    return decision


def verify_clean_source_revision(source_sha: str) -> None:
    """Fail before Modal I/O unless the local source is the requested clean commit."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to verify the promotion source")
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
        raise ValueError("worktree must be clean before a live promotion run")


def _configuration(
    settings: StepPromotionSettings,
    *,
    handle: object,
    target_placement: dict[str, str],
    function_placement: dict[str, str],
) -> dict[str, Any]:
    config_hash = getattr(handle, "config_hash", None)
    if not isinstance(config_hash, str) or not config_hash.strip():
        raise ValueError("session handle lacks a stable configuration identity")
    requested = {"cloud": settings.cloud, "region": settings.region}
    return {
        "caller_topology": "one-application-owned-modal-function",
        "target_identity": "cursor-metadata-causality-target-v1",
        "requested_placement": requested,
        "observed_placement": {
            "function": function_placement,
            "target": target_placement,
        },
        "resources": {
            "function": {
                "cpu": settings.function_cpu,
                "memory_mib": settings.function_memory_mib,
            },
            "sandbox": {
                "cpu": settings.sandbox_cpu,
                "memory_mib": settings.sandbox_memory_mib,
            },
        },
        "image_identity": f"inline-source-{settings.source_sha}-config-{config_hash}",
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": "xtest",
        "screenshot": {
            "format": "png",
            "quality": 90,
            "scale": 1.0,
            "show_cursor": False,
            "processing": "daemon",
            "storage": "inline",
            "transport": "raw-binary",
        },
        "action_scenario": "reset-pointer-then-click-unique-coordinate-v1",
        "connection_reuse": "one-pooled-async-client",
        "warm_capacity": {
            "function_min_containers": FUNCTION_MIN_CONTAINERS,
            "sandbox_pool_capacity": SANDBOX_WARM_POOL_CAPACITY,
        },
    }


def _startup_timings(timing: SessionStartupTiming) -> tuple[float, float]:
    stages = timing.as_dict()["stages"]
    created = _observed_stage(stages, "sandbox_create_started")
    registered = _observed_stage(stages, "sandbox_registered")
    attested = _observed_stage(stages, "attestation_ready")
    return max(0.0, registered - created), max(0.0, attested - registered)


def _observed_stage(stages: Mapping[str, Any], name: str) -> float:
    stage = stages.get(name)
    if not isinstance(stage, Mapping) or stage.get("status") != "observed":
        raise ValueError(f"startup timing stage {name} was not observed")
    value = stage.get("elapsed_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"startup timing stage {name} is invalid")
    return float(value)


def _exact_placement(
    value: Mapping[str, Any],
    *,
    name: str,
    expected_cloud: str,
    expected_region: str,
) -> dict[str, str]:
    raw_cloud = value.get("cloud")
    cloud = _OBSERVED_CLOUD_LABELS.get(raw_cloud, raw_cloud) if isinstance(raw_cloud, str) else None
    region = value.get("region")
    if cloud != expected_cloud or region != expected_region:
        raise ValueError(f"{name} placement does not match the requested placement")
    if not isinstance(cloud, str) or not isinstance(region, str):
        raise ValueError(f"{name} placement is unavailable")
    return {"cloud": cloud, "region": region}


def _output_paths(output_dir: Path) -> dict[str, Path]:
    paths = {
        PRIOR_PUBLIC_ARM: output_dir / "prior-public.json",
        CANDIDATE_ARM: output_dir / "candidate-default.json",
        "decision": output_dir / "promotion-decision.json",
    }
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("promotion output files already exist")
    return paths


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _function_placement() -> dict[str, str]:
    try:
        return _exact_placement(
            {
                "cloud": os.environ.get("MODAL_CLOUD_PROVIDER"),
                "region": os.environ.get("MODAL_REGION"),
            },
            name="Function",
            expected_cloud=CLOUD,
            expected_region=REGION,
        )
    except ValueError as exc:
        raise RuntimeError("Function placement does not match the requested placement") from exc


app = modal.App(APP_NAME)
function_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_pyproject(
        PROJECT_ROOT / "pyproject.toml",
        optional_dependencies=["modal"],
    )
    .add_local_python_source("modal_computer_use", copy=True)
)


@app.function(
    image=function_image,
    cloud=CLOUD,
    region=REGION,
    cpu=FUNCTION_CPU,
    memory=FUNCTION_MEMORY_MIB,
    retries=0,
    min_containers=0,
    max_containers=1,
    timeout=FUNCTION_TIMEOUT_SECONDS,
    restrict_modal_access=False,
)
async def run_step_promotion_function(
    mode: str,
    handle: ComputerSessionHandle | None = None,
    run_id: str | None = None,
    configuration: dict[str, Any] | None = None,
    lifecycle_timings: dict[str, float] | None = None,
    settings_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    placement = _function_placement()
    if mode == "probe":
        return placement
    if mode != "measure":
        raise ValueError("step promotion Function mode is invalid")
    if any(
        value is None
        for value in (handle, run_id, configuration, lifecycle_timings, settings_payload)
    ):
        raise ValueError("step promotion Function inputs are incomplete")
    if not isinstance(handle, ComputerSessionHandle):
        raise TypeError("step promotion handle is invalid")
    settings = StepPromotionSettings(**settings_payload)
    settings.validate()
    measured_configuration = dict(configuration)
    observed = dict(measured_configuration["observed_placement"])
    observed["function"] = placement
    measured_configuration["observed_placement"] = observed
    def borrow() -> Any:
        return handle.borrow_async(
            run_id=run_id,
            function_region=settings.region,
            readiness_timeout=SANDBOX_READINESS_TIMEOUT_SECONDS,
        )

    async def prepare_target(computer: Any, _pair_index: int, _arm: str) -> dict[str, Any]:
        reset = await computer.actions.run(
            [{"type": "move", "x": 16, "y": 16}],
            continue_on_error=False,
            screenshot_after=False,
            source="step-promotion-preparation",
        )
        if not reset.ok:
            raise RuntimeError("pointer reset failed")
        baseline = await computer.screenshots.full(
            format="png",
            quality=90,
            scale=1.0,
            show_cursor=False,
            processing="daemon",
            storage="inline",
        )
        cursor = baseline.cursor_position
        if cursor is None or cursor.x != 16 or cursor.y != 16:
            raise RuntimeError("pointer baseline is invalid")
        return {
            "x": 512,
            "y": 384,
            "baseline_captured_at": baseline.captured_at,
        }

    def verify_fresh_frame(
        screenshot: Any,
        token: object,
    ) -> bool:
        if not isinstance(token, Mapping):
            return False
        cursor = screenshot.cursor_position
        baseline_captured_at = token.get("baseline_captured_at")
        return (
            cursor is not None
            and cursor.x == token.get("x")
            and cursor.y == token.get("y")
            and screenshot.cursor_visible is False
            and isinstance(baseline_captured_at, datetime)
            and screenshot.captured_at > token.get("baseline_captured_at")
        )

    return await measure_interleaved_step_promotion(
        borrow,
        actions=list(ACTION_BATCH),
        prepare=prepare_target,
        verify_frame=verify_fresh_frame,
        configuration=measured_configuration,
        lifecycle_timings=lifecycle_timings,
        sample_count=settings.sample_count,
        warmup_iterations=settings.warmup_iterations,
        schedule_seed=settings.schedule_seed,
        bootstrap_seed=settings.bootstrap_seed,
        bootstrap_resamples=settings.bootstrap_resamples,
    )


class ModalStepPromotionRuntime:
    """Live Modal Adapter for the Computer Step promotion seam."""

    def own(
        self,
        settings: StepPromotionSettings,
        timing: SessionStartupTiming,
    ) -> Any:
        if settings.app_name != APP_NAME:
            raise ValueError("settings app_name differs from the running Modal App")
        config = ComputerConfig(
            ingress="attested-tunnel",
            expose_vnc="off",
            runtime={
                "modal_environment": settings.environment,
                "modal_region": settings.region,
                "timeout_seconds": SANDBOX_TIMEOUT_SECONDS,
                "readiness_timeout_seconds": SANDBOX_READINESS_TIMEOUT_SECONDS,
            },
            resources={
                "profile": "standard",
                "cpu": settings.sandbox_cpu,
                "memory_mib": settings.sandbox_memory_mib,
            },
            image={"source": "inline"},
            actions={"input_backend": "xtest"},
        )
        return AsyncComputerSandbox.create(
            config=config,
            app_name=settings.app_name,
            owner=settings.owner,
            timing=timing,
            tags={"computer-use.benchmark": "computer-step-promotion"},
            cloud=settings.cloud,
        )

    async def probe(self) -> dict[str, str]:
        result = await run_step_promotion_function.remote.aio("probe")
        return dict(result)

    async def measure(
        self,
        handle: object,
        *,
        run_id: str,
        configuration: dict[str, Any],
        lifecycle_timings: dict[str, float],
        settings: StepPromotionSettings,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(handle, ComputerSessionHandle):
            raise TypeError("promotion owner returned an invalid session handle")
        payload = await run_step_promotion_function.remote.aio(
            "measure",
            handle,
            run_id,
            configuration,
            lifecycle_timings,
            asdict(settings),
        )
        return {
            PRIOR_PUBLIC_ARM: dict(payload[PRIOR_PUBLIC_ARM]),
            CANDIDATE_ARM: dict(payload[CANDIDATE_ARM]),
        }


@app.local_entrypoint()
async def main(
    source_sha: str,
    output_dir: str = "benchmark-results/candidates/computer-step-live",
    sample_count: int = MINIMUM_SAMPLES_PER_ARM,
    warmup_iterations: int = 2,
) -> None:
    settings = StepPromotionSettings(
        app_name=APP_NAME,
        owner=OWNER,
        environment=MODAL_ENVIRONMENT,
        cloud=CLOUD,
        region=REGION,
        source_sha=source_sha,
        sample_count=sample_count,
        warmup_iterations=warmup_iterations,
    )
    decision = await execute_live_step_promotion(
        ModalStepPromotionRuntime(),
        settings=settings,
        output_dir=Path(output_dir),
    )
    print(
        json.dumps(
            {
                "decision": decision.get("decision"),
                "eligible": decision.get("eligible"),
                "paired_samples": decision.get("paired_samples"),
                "output_dir": output_dir,
            },
            sort_keys=True,
        )
    )
    if decision.get("decision") != "promote":
        raise RuntimeError("Computer Step did not pass the promotion gate")
