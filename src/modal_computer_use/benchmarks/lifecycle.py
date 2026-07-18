from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .safety import _failure

CleanupError = tuple[str, Exception]


@dataclass
class LifecycleMeasurement[ResourceT]:
    samples_ms: list[float]
    observations: list[Any]
    failures: list[dict[str, Any]]
    cleanup_errors: list[CleanupError]
    completed_runtime_seconds: float
    retained_resource: ResourceT | None = None
    retained_observation: Any = None
    retained_started_at: float | None = None


def measure_create_to_first_observation[ResourceT](
    *,
    name: str,
    iterations: int,
    warmup_iterations: int,
    create: Callable[[], ResourceT],
    observe: Callable[[ResourceT], Any],
    cleanup: Callable[[ResourceT], list[CleanupError] | None],
    retain_final_measured_resource: bool = False,
    redacted_text: str | None = None,
) -> LifecycleMeasurement[ResourceT]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if warmup_iterations < 0:
        raise ValueError("warmup_iterations must be >= 0")

    measurement = LifecycleMeasurement[ResourceT](
        samples_ms=[],
        observations=[],
        failures=[],
        cleanup_errors=[],
        completed_runtime_seconds=0.0,
    )
    if not _run_lifecycle_phase(
        measurement,
        name=name,
        phase="warmup",
        count=warmup_iterations,
        create=create,
        observe=observe,
        cleanup=cleanup,
        retain_final=False,
        redacted_text=redacted_text,
    ):
        return measurement
    _run_lifecycle_phase(
        measurement,
        name=name,
        phase="measure",
        count=iterations,
        create=create,
        observe=observe,
        cleanup=cleanup,
        retain_final=retain_final_measured_resource,
        redacted_text=redacted_text,
    )
    return measurement


def _run_lifecycle_phase[ResourceT](
    measurement: LifecycleMeasurement[ResourceT],
    *,
    name: str,
    phase: str,
    count: int,
    create: Callable[[], ResourceT],
    observe: Callable[[ResourceT], Any],
    cleanup: Callable[[ResourceT], list[CleanupError] | None],
    retain_final: bool,
    redacted_text: str | None,
) -> bool:
    for iteration in range(count):
        started = time.perf_counter()
        resource: ResourceT | None = None
        retained = False
        try:
            resource = create()
            observation = observe(resource)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            measurement.failures.append(
                _failure(
                    name,
                    phase=phase,
                    iteration=iteration,
                    exc=exc,
                    elapsed_ms=elapsed_ms,
                    redacted_text=redacted_text,
                )
            )
        else:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if phase == "measure":
                measurement.samples_ms.append(elapsed_ms)
                measurement.observations.append(observation)
            if retain_final and iteration == count - 1:
                measurement.retained_resource = resource
                measurement.retained_observation = observation
                measurement.retained_started_at = started
                retained = True
        finally:
            if resource is not None and not retained:
                measurement.cleanup_errors.extend(cleanup_lifecycle_resource(cleanup, resource))
                measurement.completed_runtime_seconds += time.perf_counter() - started
        if phase == "warmup" and measurement.failures:
            return False
    return True


def cleanup_lifecycle_resource[ResourceT](
    cleanup: Callable[[ResourceT], list[CleanupError] | None], resource: ResourceT
) -> list[CleanupError]:
    try:
        return cleanup(resource) or []
    except Exception as exc:
        return [("cleanup", exc)]
