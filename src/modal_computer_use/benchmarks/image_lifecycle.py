"""Paired Sandbox lifecycle evidence for inline and managed Modal Images.

This Benchmark Surface owns its experimental schedule, timing boundary, cleanup
policy, safe artifact, and comparison. It does not measure Image build time and it
does not belong to the public ``benchmark sdk`` command.
"""

from __future__ import annotations

import math
import random
import re
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..image import ImageReleaseRecord
from .costs import PRICING_SOURCES, PUBLIC_RATE_CATALOG

INLINE_RECIPE_ARM: Literal["inline-recipe"] = "inline-recipe"
MANAGED_EXACT_ID_ARM: Literal["managed-exact-id"] = "managed-exact-id"
IMAGE_LIFECYCLE_BENCHMARK = "modal-image-lifecycle"
IMAGE_LIFECYCLE_SCHEMA_VERSION = 1
PRIMARY_SAMPLES_PER_ARM = 30
DEFAULT_WARMUP_PAIRS = 1
BOOTSTRAP_RESAMPLES = 2_000
BOOTSTRAP_SEED = 20260808
MODAL_NARROW_REGION_MULTIPLIER = 1.75
IMAGE_LIFECYCLE_PRICING_RETRIEVED_DATE = "2026-08-08"
MODAL_REGION_PRICING_SOURCE = "https://modal.com/docs/guide/region-selection"
IMAGE_LIFECYCLE_CALLER_TOPOLOGY = "one-external-sdk-process"

ImageLifecycleArmName = Literal["inline-recipe", "managed-exact-id"]
ImageLifecycleRunKind = Literal["pilot", "primary"]

_ARMS: tuple[ImageLifecycleArmName, ...] = (
    INLINE_RECIPE_ARM,
    MANAGED_EXACT_ID_ARM,
)
_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
_EXACT_REGION = re.compile(r"^[a-z][a-z0-9]*-[a-z][a-z0-9]*-[0-9][a-z0-9]*$")
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "base_url",
    "bearer",
    "clipboard",
    "password",
    "private_key",
    "secret",
    "token",
)


@dataclass(frozen=True, slots=True)
class ImageLifecycleBenchmarkSpec:
    """Fixed inputs for one paired Image Lifecycle Benchmark run."""

    source_revision: str
    release_record: ImageReleaseRecord
    run_kind: ImageLifecycleRunKind
    samples_per_arm: int
    warmup_pairs: int
    schedule_seed: int
    requested_region: str
    cpu: float
    memory_mib: int
    sandbox_timeout_seconds: int
    max_estimated_cost_usd: float
    caller_label: str
    benchmark_run_id: str = "image-lifecycle"
    app_name: str = "modal-computer-use-image-lifecycle"

    def __post_init__(self) -> None:
        if _FULL_REVISION.fullmatch(self.source_revision) is None:
            raise ValueError("source_revision must be a full lowercase Git revision")
        if self.release_record.source_revision != self.source_revision:
            raise ValueError("managed Image release revision must match source_revision")
        if self.run_kind not in {"pilot", "primary"}:
            raise ValueError("run_kind must be pilot or primary")
        if isinstance(self.samples_per_arm, bool) or self.samples_per_arm < 1:
            raise ValueError("samples_per_arm must be positive")
        if self.run_kind == "pilot" and self.samples_per_arm > 5:
            raise ValueError("pilot samples_per_arm must not exceed 5")
        if self.run_kind == "primary" and self.samples_per_arm != PRIMARY_SAMPLES_PER_ARM:
            raise ValueError(
                f"primary samples_per_arm must be {PRIMARY_SAMPLES_PER_ARM}"
            )
        if self.warmup_pairs != DEFAULT_WARMUP_PAIRS:
            raise ValueError(f"warmup_pairs must be {DEFAULT_WARMUP_PAIRS}")
        if isinstance(self.schedule_seed, bool) or self.schedule_seed < 1:
            raise ValueError("schedule_seed must be positive")
        if _EXACT_REGION.fullmatch(self.requested_region) is None:
            raise ValueError("requested_region must be an exact Modal region")
        if isinstance(self.cpu, bool) or self.cpu <= 0:
            raise ValueError("cpu must be positive")
        if isinstance(self.memory_mib, bool) or self.memory_mib < 128:
            raise ValueError("memory_mib must be at least 128")
        if (
            isinstance(self.sandbox_timeout_seconds, bool)
            or self.sandbox_timeout_seconds < 1
            or self.sandbox_timeout_seconds > 900
        ):
            raise ValueError("sandbox_timeout_seconds must be between 1 and 900")
        if (
            isinstance(self.max_estimated_cost_usd, bool)
            or self.max_estimated_cost_usd <= 0
        ):
            raise ValueError("max_estimated_cost_usd must be positive")
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.caller_label) is None:
            raise ValueError("caller_label must be a safe non-empty label")
        for field_name in ("benchmark_run_id", "app_name"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9._-]{1,63}", value) is None
            ):
                raise ValueError(f"{field_name} must be a safe Modal name")
        maximum = maximum_image_lifecycle_cost_usd(self)
        if maximum > self.max_estimated_cost_usd:
            raise ValueError("Image lifecycle cost ceiling exceeds the configured budget")


@dataclass(frozen=True, slots=True)
class ImageLifecycleTrial:
    """One preregistered Image lifecycle attempt."""

    sequence: int
    phase: Literal["warmup", "measure"]
    pair_index: int
    sample_index: int | None
    position: int
    arm: ImageLifecycleArmName


@dataclass(frozen=True, slots=True)
class ImageLifecycleObservation:
    """Safe evidence returned by one live Image lifecycle target."""

    modal_image_object_id: str
    actual_cloud: str
    actual_region: str
    frame_valid: bool
    startup_stages: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        if not self.modal_image_object_id.startswith("im-"):
            raise ValueError("Image lifecycle observation requires a Modal Image object ID")
        if not self.actual_cloud.strip() or not self.actual_region.strip():
            raise ValueError("Image lifecycle observation requires an observed placement")
        if self.frame_valid is not True:
            raise ValueError("Image lifecycle observation requires a valid first frame")


class ImageLifecycleTarget(Protocol):
    """A created target measured by the Image Lifecycle Benchmark Surface."""

    def observe_first_frame(self) -> ImageLifecycleObservation: ...

    def close(self) -> None: ...


class ImageLifecycleArm(Protocol):
    """An Image selection policy that can create one lifecycle target."""

    name: str

    def create(self, trial: ImageLifecycleTrial) -> ImageLifecycleTarget: ...


def run_image_lifecycle_benchmark(
    spec: ImageLifecycleBenchmarkSpec,
    *,
    arms: Mapping[str, ImageLifecycleArm],
    clock: Callable[[], float],
    generated_at: Callable[[], str],
) -> dict[str, Any]:
    """Run one zero-retry paired lifecycle schedule and return safe evidence."""

    if set(arms) != set(_ARMS):
        raise ValueError("Image lifecycle benchmark requires both fixed arms")
    for name, arm in arms.items():
        if arm.name != name:
            raise ValueError("Image lifecycle arm name does not match its mapping key")

    schedule = _build_schedule(spec)
    observations: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    lifecycle_wall_time_ms = 0.0
    observed_placement: tuple[str, str] | None = None
    stopped = False
    for trial in schedule:
        if stopped:
            break
        started = clock()
        target: ImageLifecycleTarget | None = None
        measured: dict[str, Any] | None = None
        try:
            target = arms[trial.arm].create(trial)
            create_return_ms = _elapsed_ms(clock, started)
            observation = target.observe_first_frame()
            _verify_observation(spec, trial, observation)
            placement = (observation.actual_cloud, observation.actual_region)
            if observed_placement is None:
                observed_placement = placement
            elif placement != observed_placement:
                raise ValueError(
                    "Image lifecycle arms did not use one observed placement"
                )
            create_to_first_frame_ms = _observed_stage_ms(
                observation.startup_stages,
                "first_valid_frame",
            )
            if trial.phase == "measure":
                measured = {
                    "sequence": trial.sequence,
                    "pair_index": trial.pair_index,
                    "sample_index": trial.sample_index,
                    "position": trial.position,
                    "arm": trial.arm,
                    "status": "ok",
                    "modal_image_object_id": observation.modal_image_object_id,
                    "actual_placement": {
                        "cloud": observation.actual_cloud,
                        "region": observation.actual_region,
                    },
                    "frame_valid": True,
                    "startup_stages": {
                        key: dict(value)
                        for key, value in observation.startup_stages.items()
                    },
                    "timings_ms": {
                        "create_call_return": create_return_ms,
                        "create_to_first_valid_frame": create_to_first_frame_ms,
                    },
                }
        except Exception as exc:
            failures.append(_safe_failure(trial, exc, phase="lifecycle"))
            stopped = True
        finally:
            cleanup_succeeded = target is not None
            if target is not None:
                try:
                    target.close()
                except Exception as exc:
                    cleanup_succeeded = False
                    failures.append(_safe_failure(trial, exc, phase="cleanup"))
                    stopped = True
            if measured is not None:
                if not cleanup_succeeded:
                    measured["status"] = "failed"
                measured["resource_lifetime_ms"] = _elapsed_ms(clock, started)
                measured["cleanup"] = {
                    "attempted": target is not None,
                    "succeeded": cleanup_succeeded,
                }
                observations.append(measured)
            if target is not None:
                lifecycle_wall_time_ms += _elapsed_ms(clock, started)

    complete = (
        not failures
        and len(observations) == spec.samples_per_arm * len(_ARMS)
        and not stopped
    )
    artifact = {
        "schema_version": IMAGE_LIFECYCLE_SCHEMA_VERSION,
        "benchmark": IMAGE_LIFECYCLE_BENCHMARK,
        "status": "complete" if complete else "rejected",
        "generated_at": generated_at(),
        "run_kind": spec.run_kind,
        "configuration": _configuration(spec),
        "schedule": [_trial_dict(trial) for trial in schedule],
        "observations": observations,
        "failures": failures,
        "retries": 0,
        "replacement_samples": 0,
        "comparison": _comparison(observations),
        "cost": _cost(spec, lifecycle_wall_time_ms),
    }
    validate_image_lifecycle_artifact(artifact)
    return artifact


def validate_image_lifecycle_artifact(payload: Mapping[str, Any]) -> None:
    """Reject incomplete, unsafe, or non-preregistered lifecycle evidence."""

    if not isinstance(payload, Mapping):
        raise ValueError("Image lifecycle artifact must be an object")
    _validate_safe_payload(payload)
    if payload.get("schema_version") != IMAGE_LIFECYCLE_SCHEMA_VERSION:
        raise ValueError("Image lifecycle artifact schema is unsupported")
    if payload.get("benchmark") != IMAGE_LIFECYCLE_BENCHMARK:
        raise ValueError("Image lifecycle benchmark name is invalid")
    status = payload.get("status")
    if status not in {"complete", "rejected"}:
        raise ValueError("Image lifecycle status is invalid")
    run_kind = payload.get("run_kind")
    if run_kind not in {"pilot", "primary"}:
        raise ValueError("Image lifecycle run kind is invalid")
    if payload.get("retries") != 0:
        raise ValueError("Image lifecycle retries must be zero")
    if payload.get("replacement_samples") != 0:
        raise ValueError("Image lifecycle replacement samples must be zero")

    configuration = _require_mapping(payload.get("configuration"), "configuration")
    source_revision = configuration.get("source_revision")
    if not isinstance(source_revision, str) or _FULL_REVISION.fullmatch(source_revision) is None:
        raise ValueError("Image lifecycle source revision is invalid")
    samples_per_arm = _positive_int(
        configuration.get("samples_per_arm"), "samples_per_arm"
    )
    if run_kind == "primary" and samples_per_arm != PRIMARY_SAMPLES_PER_ARM:
        raise ValueError("primary Image lifecycle evidence requires 30 samples per arm")
    if run_kind == "pilot" and samples_per_arm > 5:
        raise ValueError("pilot Image lifecycle evidence exceeds five samples per arm")
    warmup_pairs = _positive_int(configuration.get("warmup_pairs"), "warmup_pairs")
    if warmup_pairs != DEFAULT_WARMUP_PAIRS:
        raise ValueError("Image lifecycle evidence requires one warmup pair")
    schedule_seed = _positive_int(configuration.get("schedule_seed"), "schedule_seed")
    requested_region = configuration.get("requested_region")
    if (
        not isinstance(requested_region, str)
        or _EXACT_REGION.fullmatch(requested_region) is None
    ):
        raise ValueError("Image lifecycle requested region is invalid")
    resources = _require_mapping(configuration.get("resources"), "resources")
    resource_limits = _require_mapping(
        configuration.get("resource_limits"), "resource_limits"
    )
    if resources != resource_limits:
        raise ValueError("Image lifecycle resource limits must equal resource requests")
    managed_release = _require_mapping(
        configuration.get("managed_release"), "managed_release"
    )
    managed_object_id = managed_release.get("modal_image_object_id")
    if not isinstance(managed_object_id, str) or not managed_object_id.startswith("im-"):
        raise ValueError("managed Image lifecycle object ID is invalid")
    if configuration.get("image_build_duration_included") is not False:
        raise ValueError("Image build duration must stay outside lifecycle evidence")
    if configuration.get("caller_topology") != IMAGE_LIFECYCLE_CALLER_TOPOLOGY:
        raise ValueError("Image lifecycle caller topology is invalid")
    caller_label = configuration.get("caller_label")
    if not isinstance(caller_label, str) or re.fullmatch(
        r"[A-Za-z0-9._-]{1,128}", caller_label
    ) is None:
        raise ValueError("Image lifecycle caller label is invalid")

    raw_schedule = payload.get("schedule")
    if not isinstance(raw_schedule, list):
        raise ValueError("Image lifecycle schedule must be a list")
    expected_schedule = _build_schedule_rows(
        samples_per_arm=samples_per_arm,
        warmup_pairs=warmup_pairs,
        seed=schedule_seed,
    )
    if raw_schedule != expected_schedule:
        raise ValueError("Image lifecycle schedule differs from its fixed inputs")

    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("Image lifecycle observations must be a list")
    seen: dict[str, set[int]] = {arm: set() for arm in _ARMS}
    observed_placements: set[tuple[Any, Any]] = set()
    for raw in observations:
        row = _require_mapping(raw, "observation")
        arm = row.get("arm")
        if arm not in _ARMS:
            raise ValueError("Image lifecycle observation arm is invalid")
        sample_index = row.get("sample_index")
        if isinstance(sample_index, bool) or not isinstance(sample_index, int):
            raise ValueError("Image lifecycle sample index is invalid")
        if sample_index < 0 or sample_index >= samples_per_arm:
            raise ValueError("Image lifecycle sample index is outside the protocol")
        if sample_index in seen[arm]:
            raise ValueError("Image lifecycle sample index is duplicated")
        seen[arm].add(sample_index)
        observation_status = row.get("status")
        if observation_status not in {"ok", "failed"}:
            raise ValueError("Image lifecycle observation status is invalid")
        if row.get("frame_valid") is not True:
            raise ValueError("Image lifecycle observation has no valid frame")
        cleanup = _require_mapping(row.get("cleanup"), "cleanup")
        cleanup_attempted = cleanup.get("attempted")
        cleanup_succeeded = cleanup.get("succeeded")
        if cleanup_attempted is not True or not isinstance(cleanup_succeeded, bool):
            raise ValueError("Image lifecycle cleanup status is invalid")
        if observation_status == "ok" and not cleanup_succeeded:
            raise ValueError("successful Image lifecycle observation has failed cleanup")
        if observation_status == "failed" and cleanup_succeeded:
            raise ValueError("failed Image lifecycle observation has successful cleanup")
        placement = _require_mapping(row.get("actual_placement"), "actual_placement")
        if placement.get("region") != requested_region or not placement.get("cloud"):
            raise ValueError("Image lifecycle placement differs from the request")
        observed_placements.add((placement.get("cloud"), placement.get("region")))
        object_id = row.get("modal_image_object_id")
        if not isinstance(object_id, str) or not object_id.startswith("im-"):
            raise ValueError("Image lifecycle observation object ID is invalid")
        if arm == MANAGED_EXACT_ID_ARM and object_id != managed_object_id:
            raise ValueError("managed Image lifecycle object ID does not match the release")
        _positive_number(row.get("resource_lifetime_ms"), "resource_lifetime_ms")
        timings = _require_mapping(row.get("timings_ms"), "timings_ms")
        _positive_number(
            timings.get("create_to_first_valid_frame"),
            "create_to_first_valid_frame",
        )
    failures = payload.get("failures")
    if not isinstance(failures, list):
        raise ValueError("Image lifecycle failures must be a list")
    if status == "complete":
        expected_indexes = set(range(samples_per_arm))
        if (
            failures
            or any(indexes != expected_indexes for indexes in seen.values())
            or any(row.get("status") != "ok" for row in observations)
        ):
            raise ValueError("complete Image lifecycle evidence is incomplete")
        if len(observed_placements) != 1:
            raise ValueError("complete Image lifecycle evidence requires one observed placement")

    cost = _require_mapping(payload.get("cost"), "cost")
    maximum = _positive_number(cost.get("maximum_estimate_usd"), "maximum cost")
    expected_maximum = _maximum_cost_from_values(
        samples_per_arm=samples_per_arm,
        warmup_pairs=warmup_pairs,
        sandbox_timeout_seconds=_positive_int(
            configuration.get("sandbox_timeout_seconds"),
            "sandbox_timeout_seconds",
        ),
        cpu=_positive_number(resources.get("cpu"), "resource cpu"),
        memory_mib=_positive_int(resources.get("memory_mib"), "resource memory_mib"),
    )
    if not math.isclose(maximum, expected_maximum, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("Image lifecycle maximum estimate is invalid")
    budget = _positive_number(
        configuration.get("max_estimated_cost_usd"), "cost budget"
    )
    if maximum > budget:
        raise ValueError("Image lifecycle maximum estimate exceeds its budget")


def maximum_image_lifecycle_cost_usd(spec: ImageLifecycleBenchmarkSpec) -> float:
    """Return the target ceiling for the preregistered schedule."""

    return _maximum_cost_from_values(
        samples_per_arm=spec.samples_per_arm,
        warmup_pairs=spec.warmup_pairs,
        sandbox_timeout_seconds=spec.sandbox_timeout_seconds,
        cpu=spec.cpu,
        memory_mib=spec.memory_mib,
    )


def _maximum_cost_from_values(
    *,
    samples_per_arm: int,
    warmup_pairs: int,
    sandbox_timeout_seconds: int,
    cpu: float,
    memory_mib: int,
) -> float:
    lifecycle_count = (warmup_pairs + samples_per_arm) * len(_ARMS)
    target_cost = lifecycle_count * sandbox_timeout_seconds * _resource_rate_per_second(
        cpu=cpu,
        memory_mib=memory_mib,
    )
    return target_cost


def _build_schedule(spec: ImageLifecycleBenchmarkSpec) -> list[ImageLifecycleTrial]:
    return [
        ImageLifecycleTrial(**row)
        for row in _build_schedule_rows(
            samples_per_arm=spec.samples_per_arm,
            warmup_pairs=spec.warmup_pairs,
            seed=spec.schedule_seed,
        )
    ]


def _build_schedule_rows(
    *, samples_per_arm: int, warmup_pairs: int, seed: int
) -> list[dict[str, Any]]:
    rng = random.Random(seed)  # noqa: S311 - deterministic scheduling.
    schedule: list[dict[str, Any]] = []
    sequence = 0
    for phase, count in (
        ("warmup", warmup_pairs),
        ("measure", samples_per_arm),
    ):
        for pair_index in range(count):
            pair_arms = list(_ARMS)
            rng.shuffle(pair_arms)
            for position, arm in enumerate(pair_arms):
                schedule.append(
                    {
                        "sequence": sequence,
                        "phase": phase,
                        "pair_index": pair_index,
                        "sample_index": pair_index if phase == "measure" else None,
                        "position": position,
                        "arm": arm,
                    }
                )
                sequence += 1
    return schedule


def _verify_observation(
    spec: ImageLifecycleBenchmarkSpec,
    trial: ImageLifecycleTrial,
    observation: ImageLifecycleObservation,
) -> None:
    if observation.actual_region != spec.requested_region:
        raise ValueError("Image lifecycle target placement differs from the request")
    if (
        trial.arm == MANAGED_EXACT_ID_ARM
        and observation.modal_image_object_id
        != spec.release_record.modal_image_object_id
    ):
        raise ValueError("managed lifecycle target does not use the release object ID")


def _configuration(spec: ImageLifecycleBenchmarkSpec) -> dict[str, Any]:
    record = spec.release_record
    return {
        "source_revision": spec.source_revision,
        "benchmark_run_id": spec.benchmark_run_id,
        "app_name": spec.app_name,
        "samples_per_arm": spec.samples_per_arm,
        "warmup_pairs": spec.warmup_pairs,
        "schedule_seed": spec.schedule_seed,
        "requested_region": spec.requested_region,
        "resources": {"cpu": spec.cpu, "memory_mib": spec.memory_mib},
        "resource_limits": {"cpu": spec.cpu, "memory_mib": spec.memory_mib},
        "sandbox_timeout_seconds": spec.sandbox_timeout_seconds,
        "max_estimated_cost_usd": spec.max_estimated_cost_usd,
        "managed_release": {
            "logical_release": record.logical_release,
            "image_variant": record.image_variant,
            "image_reference": record.image_reference,
            "modal_image_object_id": record.modal_image_object_id,
            "workspace_name": record.workspace_name,
            "environment_name": record.environment_name,
            "pyproject_sha256": record.pyproject_sha256,
            "uv_lock_sha256": record.uv_lock_sha256,
            "image_builder_version": record.image_builder_version,
            "uv_version": record.uv_version,
            "modal_sdk_version": record.modal_sdk_version,
        },
        "measurement_scope": "sandbox-create-through-first-valid-frame",
        "image_build_duration_included": False,
        "caller_topology": IMAGE_LIFECYCLE_CALLER_TOPOLOGY,
        "caller_label": spec.caller_label,
        "bootstrap": {
            "resamples": BOOTSTRAP_RESAMPLES,
            "seed": BOOTSTRAP_SEED,
        },
    }


def _comparison(observations: list[dict[str, Any]]) -> dict[str, Any]:
    samples = {
        arm: [
            float(row["timings_ms"]["create_to_first_valid_frame"])
            for row in observations
            if row["arm"] == arm and row["status"] == "ok"
        ]
        for arm in _ARMS
    }
    if any(not values for values in samples.values()):
        return {"status": "not_available", "arms": {}}
    summaries = {arm: _summary(values) for arm, values in samples.items()}
    paired = {
        int(row["sample_index"]): float(
            row["timings_ms"]["create_to_first_valid_frame"]
        )
        for row in observations
        if row["arm"] == MANAGED_EXACT_ID_ARM and row["status"] == "ok"
    }
    inline = {
        int(row["sample_index"]): float(
            row["timings_ms"]["create_to_first_valid_frame"]
        )
        for row in observations
        if row["arm"] == INLINE_RECIPE_ARM and row["status"] == "ok"
    }
    common = sorted(set(paired) & set(inline))
    deltas = [paired[index] - inline[index] for index in common]
    bootstrap = _bootstrap_interval(deltas)
    return {
        "status": "measured" if len(common) == len(inline) == len(paired) else "partial",
        "arms": summaries,
        "managed_vs_inline": {
            "mean_ratio": (
                summaries[MANAGED_EXACT_ID_ARM]["mean"]
                / summaries[INLINE_RECIPE_ARM]["mean"]
            ),
            "p50_delta_ms": (
                summaries[MANAGED_EXACT_ID_ARM]["p50"]
                - summaries[INLINE_RECIPE_ARM]["p50"]
            ),
        },
        "paired_delta_ms": {
            "direction": "managed-minus-inline",
            **_summary(deltas),
            "bootstrap_95_ci": bootstrap,
        },
    }


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50": statistics.median(ordered),
        "p95": _percentile(ordered, 95),
        "mean": statistics.fmean(samples),
        "min": min(samples),
        "max": max(samples),
    }


def _percentile(ordered: list[float], percentile: int) -> float:
    if len(ordered) == 1:
        return ordered[0]
    rank = (percentile / 100) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _bootstrap_interval(deltas: list[float]) -> dict[str, Any]:
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - deterministic evidence.
    medians: list[float] = []
    means: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        draw = [deltas[rng.randrange(len(deltas))] for _ in deltas]
        medians.append(statistics.median(draw))
        means.append(statistics.fmean(draw))
    return {
        "median": [_percentile(sorted(medians), 2.5), _percentile(sorted(medians), 97.5)],
        "mean": [_percentile(sorted(means), 2.5), _percentile(sorted(means), 97.5)],
        "resamples": BOOTSTRAP_RESAMPLES,
        "seed": BOOTSTRAP_SEED,
    }


def _cost(spec: ImageLifecycleBenchmarkSpec, lifecycle_wall_time_ms: float) -> dict[str, Any]:
    measured_seconds = lifecycle_wall_time_ms / 1000.0
    return {
        "status": "estimated",
        "currency": "USD",
        "maximum_estimate_usd": maximum_image_lifecycle_cost_usd(spec),
        "target_cost_estimate_usd": measured_seconds * _modal_rate_per_second(spec),
        "lifecycle_wall_time_seconds": measured_seconds,
        "duration_policy": "create_start_through_cleanup_wall_time",
        "exclusions": [
            "Image build",
            "canary Sandbox",
            "control plane",
            "billing adjustments",
        ],
        "pricing": {
            "retrieved_date": IMAGE_LIFECYCLE_PRICING_RETRIEVED_DATE,
            "source_url": PRICING_SOURCES["modal"],
            "region_multiplier": MODAL_NARROW_REGION_MULTIPLIER,
            "region_source_url": MODAL_REGION_PRICING_SOURCE,
        },
    }


def _modal_rate_per_second(spec: ImageLifecycleBenchmarkSpec) -> float:
    return _resource_rate_per_second(cpu=spec.cpu, memory_mib=spec.memory_mib)


def _resource_rate_per_second(*, cpu: float, memory_mib: int) -> float:
    rates = PUBLIC_RATE_CATALOG["modal"]
    memory_gib = memory_mib / 1024.0
    base_rate = cpu * float(rates["cpu"]["rate"]) + memory_gib * float(
        rates["memory"]["rate"]
    )
    return base_rate * MODAL_NARROW_REGION_MULTIPLIER




def _elapsed_ms(clock: Callable[[], float], started: float) -> float:
    return round(max(0.0, (clock() - started) * 1000.0), 6)


def _observed_stage_ms(
    stages: Mapping[str, Mapping[str, Any]], stage_name: str
) -> float:
    stage = stages.get(stage_name)
    value = None if stage is None else stage.get("elapsed_ms")
    if (
        stage is None
        or stage.get("status") != "observed"
        or isinstance(value, bool)
        or not isinstance(value, int | float)
        or value < 0
    ):
        raise ValueError(f"Image lifecycle observation requires {stage_name}")
    return float(value)


def _trial_dict(trial: ImageLifecycleTrial) -> dict[str, Any]:
    return {
        "sequence": trial.sequence,
        "phase": trial.phase,
        "pair_index": trial.pair_index,
        "sample_index": trial.sample_index,
        "position": trial.position,
        "arm": trial.arm,
    }


def _safe_failure(
    trial: ImageLifecycleTrial, exc: Exception, *, phase: str
) -> dict[str, Any]:
    return {
        "sequence": trial.sequence,
        "phase": phase,
        "schedule_phase": trial.phase,
        "pair_index": trial.pair_index,
        "sample_index": trial.sample_index,
        "arm": trial.arm,
        "error_type": type(exc).__name__,
    }


def _validate_safe_payload(value: Any, *, key: str | None = None) -> None:
    if key is not None:
        normalized = key.lower().replace("-", "_")
        if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
            raise ValueError("Image lifecycle artifact contains a secret-bearing field")
    if isinstance(value, Mapping):
        for item_key, item in value.items():
            _validate_safe_payload(item, key=str(item_key))
    elif isinstance(value, list):
        for item in value:
            _validate_safe_payload(item)


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Image lifecycle {name} must be an object")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"Image lifecycle {name} must be positive")
    return value


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"Image lifecycle {name} must be positive")
    return float(value)
