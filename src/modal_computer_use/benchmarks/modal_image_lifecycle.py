"""Modal adapters for the Image Lifecycle Benchmark Surface."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config import BrowserConfig, ComputerConfig, ResourceConfig, RuntimeConfig
from ..image import (
    ImageReleaseRecord,
    _resolve_release_image_object_id,
    resolve_release_image,
)
from ..latency import SessionStartupTiming
from ..sandbox import ComputerSandbox, run_modal_benchmark_function_with_image_once
from .image_lifecycle import (
    IMAGE_LIFECYCLE_BENCHMARK,
    INLINE_RECIPE_ARM,
    MANAGED_EXACT_ID_ARM,
    ImageLifecycleArmName,
    ImageLifecycleBenchmarkSpec,
    ImageLifecycleObservation,
    ImageLifecycleTrial,
    image_lifecycle_runner_timeout_seconds,
    run_image_lifecycle_benchmark,
)

ComputerFactory = Callable[..., ComputerSandbox]
ImageResolver = Callable[[ImageReleaseRecord], object]
ExactImageResolver = Callable[[str], object]
FunctionLauncher = Callable[..., dict[str, Any]]


@dataclass(slots=True)
class _ModalImageLifecycleTarget:
    computer: ComputerSandbox
    config: ComputerConfig
    timing: SessionStartupTiming

    def observe_first_frame(self) -> ImageLifecycleObservation:
        self.computer.ensure_browser_ready(self.config, timing=self.timing)
        self.computer.first_valid_frame(self.config, timing=self.timing)
        placement = self.computer.runtime_placement()
        return ImageLifecycleObservation(
            modal_image_object_id=self.computer.modal_image_object_id(),
            actual_cloud=_normalize_cloud(placement.get("cloud")),
            actual_region=placement.get("region") or "",
            frame_valid=True,
            startup_stages=self.timing.as_dict()["stages"],
        )

    def close(self) -> None:
        self.computer.__exit__(None, None, None)


@dataclass(frozen=True, slots=True)
class _ModalImageLifecycleArm:
    name: ImageLifecycleArmName
    spec: ImageLifecycleBenchmarkSpec
    image: object | None
    create_computer: ComputerFactory
    clock: Callable[[], float]

    def create(self, trial: ImageLifecycleTrial) -> _ModalImageLifecycleTarget:
        config = _trial_config(self.spec, trial)
        timing = SessionStartupTiming(clock=self.clock)
        computer = self.create_computer(
            config=config,
            app_name=self.spec.app_name,
            image=self.image,
            tags={
                "benchmark": IMAGE_LIFECYCLE_BENCHMARK,
                "benchmark_arm": trial.arm,
            },
            wait=True,
            timing=timing,
            cpu=(self.spec.cpu, self.spec.cpu),
            memory=(self.spec.memory_mib, self.spec.memory_mib),
        )
        computer.__enter__()
        return _ModalImageLifecycleTarget(
            computer=computer,
            config=config,
            timing=timing,
        )


def run_modal_image_lifecycle(
    spec: ImageLifecycleBenchmarkSpec,
    *,
    function_launcher: FunctionLauncher = run_modal_benchmark_function_with_image_once,
    resolve_image: ImageResolver = resolve_release_image,
) -> dict[str, Any]:
    """Run the lifecycle schedule in one exact, same-region Modal Function."""

    runner_image = resolve_image(spec.release_record)
    return function_launcher(
        run_modal_image_lifecycle_in_runner,
        config=spec,
        run_tag=spec.benchmark_run_id,
        app_name=f"{spec.app_name}-runner",
        region=spec.requested_region,
        environment_name=spec.release_record.environment_name,
        image=runner_image,
        cpu=1.0,
        memory_mib=1024,
        timeout_seconds=image_lifecycle_runner_timeout_seconds(spec),
        cpu_limit=1.0,
        memory_limit_mib=1024,
        retries=0,
    )


def run_modal_image_lifecycle_in_runner(
    spec: ImageLifecycleBenchmarkSpec,
    *,
    run_tag: str,
    runner_placement: dict[str, str | None] | None = None,
    create_computer: ComputerFactory = ComputerSandbox.create,
    resolve_exact_image: ExactImageResolver = _resolve_release_image_object_id,
    clock: Callable[[], float] = time.perf_counter,
    generated_at: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Create both target arms from the single application-owned runner."""

    if run_tag != spec.benchmark_run_id:
        raise ValueError("Image lifecycle runner tag differs from the specification")
    placement = runner_placement or {
        "cloud": os.environ.get("MODAL_CLOUD_PROVIDER"),
        "region": os.environ.get("MODAL_REGION"),
    }
    caller_placement = {
        "cloud": _normalize_cloud(placement.get("cloud")),
        "region": placement.get("region") or "",
    }
    managed_image = resolve_exact_image(spec.release_record.modal_image_object_id)
    arms = {
        INLINE_RECIPE_ARM: _ModalImageLifecycleArm(
            name=INLINE_RECIPE_ARM,
            spec=spec,
            image=None,
            create_computer=create_computer,
            clock=clock,
        ),
        MANAGED_EXACT_ID_ARM: _ModalImageLifecycleArm(
            name=MANAGED_EXACT_ID_ARM,
            spec=spec,
            image=managed_image,
            create_computer=create_computer,
            clock=clock,
        ),
    }
    return run_image_lifecycle_benchmark(
        spec,
        arms=arms,
        caller_placement=caller_placement,
        clock=clock,
        generated_at=generated_at or _utc_now,
    )


def _trial_config(
    spec: ImageLifecycleBenchmarkSpec, trial: ImageLifecycleTrial
) -> ComputerConfig:
    browser_kind = (
        spec.release_record.image_variant
        if spec.release_record.image_variant in {"firefox", "chromium"}
        else None
    )
    return ComputerConfig(
        run_id=f"{spec.benchmark_run_id}-{trial.sequence:03d}",
        runtime=RuntimeConfig(
            timeout_seconds=spec.sandbox_timeout_seconds,
            readiness_timeout_seconds=spec.sandbox_timeout_seconds,
            modal_environment=spec.release_record.environment_name,
            modal_region=spec.requested_region,
        ),
        resources=ResourceConfig(
            profile="browser" if browser_kind is not None else "standard",
            cpu=spec.cpu,
            memory_mib=spec.memory_mib,
        ),
        browser=(
            BrowserConfig(kind=browser_kind, prewarm=True)
            if browser_kind is not None
            else None
        ),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_cloud(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return {
        "cloud_provider_aws": "aws",
        "cloud_provider_gcp": "gcp",
        "cloud_provider_oci": "oci",
        "cloud_provider_azure": "azure",
    }.get(normalized, normalized)
