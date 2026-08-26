"""Run the live, same-topology promotion Benchmark Surface.

This file is application-owned Modal orchestration. Core measurement stays Modal-free in
``modal_computer_use.benchmarks.promotion_measurement``.

Run it from a clean committed source revision:

    modal run --env main scripts/run_optimized_default_promotion.py \
      --source-sha <40-character-git-sha> \
      --output-dir benchmark-results/candidates/optimized-default-<date>
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import modal

from modal_computer_use import AsyncComputerSandbox, ComputerConfig, ComputerSessionHandle
from modal_computer_use._regions import is_verifiable_modal_region_selector
from modal_computer_use.benchmarks.promotion_gate import (
    CANDIDATE_ARM,
    MINIMUM_SAMPLES_PER_ARM,
    PRIOR_PUBLIC_ARM,
    compare_promotion_artifacts,
    sanitize_promotion_artifact,
)
from modal_computer_use.benchmarks.promotion_measurement import (
    measure_interleaved_promotion,
)
from modal_computer_use.latency import SessionStartupTiming

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = os.environ.get(
    "MODAL_COMPUTER_USE_PROMOTION_APP_NAME",
    "modal-computer-use-optimized-default-promotion",
)
OWNER = os.environ.get(
    "MODAL_COMPUTER_USE_PROMOTION_OWNER",
    "modal-computer-use-optimized-default-owner",
)
MODAL_ENVIRONMENT = os.environ.get("MODAL_COMPUTER_USE_PROMOTION_ENVIRONMENT", "main")
CLOUD = os.environ.get("MODAL_COMPUTER_USE_PROMOTION_CLOUD", "aws")
REGION = os.environ.get("MODAL_COMPUTER_USE_PROMOTION_REGION", "us-west")
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
ACTION_BATCH = ({"type": "move", "x": 32, "y": 32},)
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_OBSERVED_CLOUD_LABELS = {
    "CLOUD_PROVIDER_AWS": "aws",
    "CLOUD_PROVIDER_AZURE": "azure",
    "CLOUD_PROVIDER_GCP": "gcp",
    "CLOUD_PROVIDER_OCI": "oci",
}


@dataclass(frozen=True)
class PromotionSettings:
    """Explicit placement, cost, and measurement choices for one live run."""

    app_name: str
    owner: str
    environment: str
    cloud: str
    region: str
    source_sha: str
    sample_count: int = MINIMUM_SAMPLES_PER_ARM
    warmup_iterations: int = 1
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
        if not is_verifiable_modal_region_selector(self.region):
            raise ValueError(
                "region must be one verifiable narrow or granted granular Modal region"
            )
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
        cpu_values: tuple[tuple[str, float], ...] = (
            ("function_cpu", self.function_cpu),
            ("sandbox_cpu", self.sandbox_cpu),
        )
        for name, cpu_value in cpu_values:
            if not math.isfinite(cpu_value) or cpu_value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        memory_values: tuple[tuple[str, int], ...] = (
            ("function_memory_mib", self.function_memory_mib),
            ("sandbox_memory_mib", self.sandbox_memory_mib),
        )
        for name, memory_value in memory_values:
            if memory_value < 128:
                raise ValueError(f"{name} must be at least 128")


class PromotionRuntime(Protocol):
    """Adapter seam for live ownership and placed Function dispatch."""

    def own(
        self,
        settings: PromotionSettings,
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
        settings: PromotionSettings,
    ) -> dict[str, dict[str, Any]]: ...


async def execute_live_promotion(
    runtime: PromotionRuntime,
    *,
    settings: PromotionSettings,
    output_dir: Path,
    clock: Any = time.perf_counter,
) -> dict[str, Any]:
    """Own, dispatch, measure, gate, write, and clean up one promotion run."""
    settings.validate()
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
        configuration = _configuration(
            settings,
            handle=handle,
            target_placement=target_placement,
            function_placement=function_placement,
        )
        artifacts = await runtime.measure(
            handle,
            run_id=f"optimized-default-promotion-{uuid.uuid4().hex}",
            configuration=configuration,
            lifecycle_timings={
                "cold_start_ms": cold_start_ms,
                "startup_ms": startup_ms,
                "dispatch_ms": dispatch_ms,
            },
            settings=settings,
        )

    prior = sanitize_promotion_artifact(artifacts[PRIOR_PUBLIC_ARM])
    candidate = sanitize_promotion_artifact(artifacts[CANDIDATE_ARM])
    decision = compare_promotion_artifacts(prior, candidate)
    _write_new_json(output_paths[PRIOR_PUBLIC_ARM], prior)
    _write_new_json(output_paths[CANDIDATE_ARM], candidate)
    _write_new_json(output_paths["decision"], decision)
    return decision


def _configuration(
    settings: PromotionSettings,
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
        "target_identity": f"config-{config_hash}",
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
        "image_identity": f"inline-source-{settings.source_sha}",
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": "xtest",
        "screenshot": {
            "format": "png",
            "show_cursor": False,
            "processing": "daemon",
            "storage": "inline",
        },
        "action_payload_sha256": "0" * 64,
        "warmup_iterations": settings.warmup_iterations,
        "connection_reuse": "one-pooled-async-client",
        "warm_capacity": {
            "function_min_containers": FUNCTION_MIN_CONTAINERS,
            "sandbox_pool_capacity": SANDBOX_WARM_POOL_CAPACITY,
        },
    }


def _startup_timings(timing: SessionStartupTiming) -> tuple[float, float]:
    stages = timing.as_dict()["stages"]
    create_started = _observed_stage(stages, "sandbox_create_started")
    registered = _observed_stage(stages, "sandbox_registered")
    attested = _observed_stage(stages, "attestation_ready")
    return max(0.0, registered - create_started), max(0.0, attested - registered)


def _observed_stage(stages: Mapping[str, Any], name: str) -> float:
    stage = stages.get(name)
    if not isinstance(stage, Mapping) or stage.get("status") != "observed":
        raise ValueError(f"startup timing stage {name} was not observed")
    value = stage.get("elapsed_ms")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
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
    cloud = (
        _OBSERVED_CLOUD_LABELS.get(raw_cloud, raw_cloud)
        if isinstance(raw_cloud, str)
        else raw_cloud
    )
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
    existing = [path.name for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError("promotion output files already exist")
    return paths


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _function_placement() -> dict[str, str]:
    cloud = os.environ.get("MODAL_CLOUD_PROVIDER")
    region = os.environ.get("MODAL_REGION")
    try:
        return _exact_placement(
            {"cloud": cloud, "region": region},
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
    min_containers=FUNCTION_MIN_CONTAINERS,
    max_containers=FUNCTION_MAX_CONTAINERS,
    timeout=FUNCTION_TIMEOUT_SECONDS,
    restrict_modal_access=False,
)
async def run_promotion_function(
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
        raise ValueError("promotion Function mode is invalid")
    if (
        handle is None
        or run_id is None
        or configuration is None
        or lifecycle_timings is None
        or settings_payload is None
    ):
        raise ValueError("promotion Function measurement inputs are incomplete")
    settings = PromotionSettings(**settings_payload)
    settings.validate()
    observed = dict(configuration.get("observed_placement", {}))
    observed["function"] = placement
    measured_configuration = dict(configuration)
    measured_configuration["observed_placement"] = observed

    def borrow() -> Any:
        return handle.borrow_async(
            run_id=run_id,
            function_region=settings.region,
            readiness_timeout=SANDBOX_READINESS_TIMEOUT_SECONDS,
        )

    return await measure_interleaved_promotion(
        borrow,
        actions=list(ACTION_BATCH),
        configuration=measured_configuration,
        lifecycle_timings=lifecycle_timings,
        sample_count=settings.sample_count,
        warmup_iterations=settings.warmup_iterations,
        schedule_seed=settings.schedule_seed,
        bootstrap_seed=settings.bootstrap_seed,
        bootstrap_resamples=settings.bootstrap_resamples,
    )


class ModalPromotionRuntime:
    """Live Modal Adapter for the promotion orchestration seam."""

    def own(
        self,
        settings: PromotionSettings,
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
            tags={"computer-use.benchmark": "optimized-default-promotion"},
            cloud=settings.cloud,
        )

    async def probe(self) -> dict[str, str]:
        result = await run_promotion_function.remote.aio("probe")
        return _exact_placement(
            result,
            name="Function",
            expected_cloud=CLOUD,
            expected_region=REGION,
        )

    async def measure(
        self,
        handle: object,
        *,
        run_id: str,
        configuration: dict[str, Any],
        lifecycle_timings: dict[str, float],
        settings: PromotionSettings,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(handle, ComputerSessionHandle):
            raise TypeError("promotion owner returned an invalid session handle")
        payload = await run_promotion_function.remote.aio(
            "measure",
            handle,
            run_id,
            configuration,
            lifecycle_timings,
            settings.__dict__,
        )
        return {
            PRIOR_PUBLIC_ARM: dict(payload[PRIOR_PUBLIC_ARM]),
            CANDIDATE_ARM: dict(payload[CANDIDATE_ARM]),
        }


@app.local_entrypoint()
async def main(
    source_sha: str,
    output_dir: str = "benchmark-results/candidates/optimized-default-live",
    sample_count: int = MINIMUM_SAMPLES_PER_ARM,
    warmup_iterations: int = 1,
) -> None:
    settings = PromotionSettings(
        app_name=APP_NAME,
        owner=OWNER,
        environment=MODAL_ENVIRONMENT,
        cloud=CLOUD,
        region=REGION,
        source_sha=source_sha,
        sample_count=sample_count,
        warmup_iterations=warmup_iterations,
    )
    decision = await execute_live_promotion(
        ModalPromotionRuntime(),
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
        raise RuntimeError("the optimized default did not pass the promotion gate")


if __name__ == "__main__":
    asyncio.run(
        execute_live_promotion(
            ModalPromotionRuntime(),
            settings=PromotionSettings(
                app_name=APP_NAME,
                owner=OWNER,
                environment=MODAL_ENVIRONMENT,
                cloud=CLOUD,
                region=REGION,
                source_sha=os.environ["SOURCE_SHA"],
            ),
            output_dir=Path(
                "benchmark-results/candidates/optimized-default-live"
            ),
        )
    )
