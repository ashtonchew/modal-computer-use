from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest

from modal_computer_use.benchmarks.image_lifecycle import (
    INLINE_RECIPE_ARM,
    MANAGED_EXACT_ID_ARM,
    ImageLifecycleBenchmarkSpec,
    ImageLifecycleObservation,
    run_image_lifecycle_benchmark,
    validate_image_lifecycle_artifact,
)
from modal_computer_use.image import ImageCanaryRecord, ImageReleaseRecord


def _release_record() -> ImageReleaseRecord:
    revision = "a" * 40
    return ImageReleaseRecord(
        schema_version=1,
        logical_release="2.0.0",
        source_revision=revision,
        image_variant="standard",
        image_name="modal-computer-use-standard",
        image_tag=revision,
        image_reference=f"modal-computer-use-standard:{revision}",
        workspace_name="test-workspace",
        environment_name="test-environment",
        modal_image_object_id="im-managed",
        pyproject_sha256="b" * 64,
        uv_lock_sha256="c" * 64,
        image_builder_version="2025.06",
        uv_version="0.12.3",
        modal_sdk_version="1.5.3",
        build_app_name="modal-computer-use-image-builds",
        canary=ImageCanaryRecord(
            status="passed",
            checks=(
                "healthz",
                "readyz",
                "version",
                "capabilities",
                "image_object_id",
                "browser",
                "screenshot",
                "cleanup",
            ),
            checked_at="2026-08-08T20:00:00Z",
        ),
        published_at="2026-08-08T20:01:00Z",
    )


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class _FakeTarget:
    arm: str
    clock: _Clock
    events: list[str]
    managed_object_id: str = "im-managed"
    close_failure: Exception | None = None

    def observe_first_frame(self) -> ImageLifecycleObservation:
        self.events.append(f"observe:{self.arm}")
        self.clock.advance(0.020)
        return ImageLifecycleObservation(
            modal_image_object_id=(
                self.managed_object_id
                if self.arm == MANAGED_EXACT_ID_ARM
                else "im-inline"
            ),
            actual_cloud="aws",
            actual_region="us-west-2",
            frame_valid=True,
            startup_stages={
                "sandbox_registered": {"status": "observed", "elapsed_ms": 5.0},
                "authenticated_daemon_ready": {
                    "status": "observed",
                    "elapsed_ms": 15.0,
                },
                "first_valid_frame": {"status": "observed", "elapsed_ms": 30.0},
            },
        )

    def close(self) -> None:
        self.events.append(f"close:{self.arm}")
        if self.close_failure is not None:
            raise self.close_failure
        self.clock.advance(0.005)


@dataclass
class _FakeArm:
    name: str
    clock: _Clock
    events: list[str]
    create_failure: Exception | None = None
    close_failure: Exception | None = None
    close_failure_phase: str | None = None
    managed_object_id: str = "im-managed"

    def create(self, trial: Any) -> _FakeTarget:
        self.events.append(f"create:{self.name}:{trial.phase}:{trial.pair_index}")
        if self.create_failure is not None:
            raise self.create_failure
        self.clock.advance(0.010)
        close_failure = (
            self.close_failure
            if self.close_failure_phase is None
            or self.close_failure_phase == trial.phase
            else None
        )
        return _FakeTarget(
            self.name,
            self.clock,
            self.events,
            managed_object_id=self.managed_object_id,
            close_failure=close_failure,
        )


def test_image_lifecycle_surface_runs_one_paired_interleaved_schedule() -> None:
    clock = _Clock()
    events: list[str] = []
    spec = ImageLifecycleBenchmarkSpec(
        source_revision="a" * 40,
        release_record=_release_record(),
        run_kind="pilot",
        samples_per_arm=2,
        warmup_pairs=1,
        schedule_seed=7,
        requested_region="us-west-2",
        cpu=1.0,
        memory_mib=2048,
        sandbox_timeout_seconds=180,
        max_estimated_cost_usd=20.0,
        caller_label="test-external-caller",
    )
    arms = {
        INLINE_RECIPE_ARM: _FakeArm(INLINE_RECIPE_ARM, clock, events),
        MANAGED_EXACT_ID_ARM: _FakeArm(MANAGED_EXACT_ID_ARM, clock, events),
    }

    artifact = run_image_lifecycle_benchmark(
        spec,
        arms=arms,
        clock=clock,
        generated_at=lambda: "2026-08-08T21:00:00Z",
    )

    assert artifact["status"] == "complete"
    assert artifact["benchmark"] == "modal-image-lifecycle"
    assert len(artifact["schedule"]) == 6
    assert len(artifact["observations"]) == 4
    assert {row["arm"] for row in artifact["observations"]} == {
        INLINE_RECIPE_ARM,
        MANAGED_EXACT_ID_ARM,
    }
    assert all(
        row["timings_ms"]["create_to_first_valid_frame"] == 30.0
        for row in artifact["observations"]
    )
    assert all(
        row["resource_lifetime_ms"] == 35.0 for row in artifact["observations"]
    )
    assert all(
        row["cleanup"] == {"attempted": True, "succeeded": True}
        for row in artifact["observations"]
    )
    assert len([event for event in events if event.startswith("create:")]) == 6
    assert len([event for event in events if event.startswith("close:")]) == 6
    assert artifact["cost"]["maximum_estimate_usd"] < 20.0
    assert artifact["cost"]["lifecycle_wall_time_seconds"] == 0.21
    assert artifact["cost"]["duration_policy"] == "create_start_through_cleanup_wall_time"
    assert artifact["comparison"]["paired_delta_ms"]["bootstrap_95_ci"] == {
        "median": [0.0, 0.0],
        "mean": [0.0, 0.0],
        "resamples": 2_000,
        "seed": 20260808,
    }
    validate_image_lifecycle_artifact(artifact)

    wrong_identity = deepcopy(artifact)
    managed = next(
        row
        for row in wrong_identity["observations"]
        if row["arm"] == MANAGED_EXACT_ID_ARM
    )
    managed["modal_image_object_id"] = "im-other"
    with pytest.raises(ValueError, match=r"managed.*object ID"):
        validate_image_lifecycle_artifact(wrong_identity)

    replacement = deepcopy(artifact)
    replacement["replacement_samples"] = 1
    with pytest.raises(ValueError, match="replacement"):
        validate_image_lifecycle_artifact(replacement)

    placement_drift = deepcopy(artifact)
    placement_drift["observations"][0]["actual_placement"]["cloud"] = "gcp"
    with pytest.raises(ValueError, match="one observed placement"):
        validate_image_lifecycle_artifact(placement_drift)

    understated_cost = deepcopy(artifact)
    understated_cost["cost"]["maximum_estimate_usd"] = 0.01
    with pytest.raises(ValueError, match="maximum estimate is invalid"):
        validate_image_lifecycle_artifact(understated_cost)


def test_image_lifecycle_surface_stops_without_retry_and_redacts_failures() -> None:
    clock = _Clock()
    events: list[str] = []
    spec = ImageLifecycleBenchmarkSpec(
        source_revision="a" * 40,
        release_record=_release_record(),
        run_kind="pilot",
        samples_per_arm=2,
        warmup_pairs=1,
        schedule_seed=7,
        requested_region="us-west-2",
        cpu=1.0,
        memory_mib=2048,
        sandbox_timeout_seconds=180,
        max_estimated_cost_usd=20.0,
        caller_label="test-external-caller",
    )
    arms = {
        INLINE_RECIPE_ARM: _FakeArm(
            INLINE_RECIPE_ARM,
            clock,
            events,
            create_failure=RuntimeError("secret endpoint and bearer"),
        ),
        MANAGED_EXACT_ID_ARM: _FakeArm(MANAGED_EXACT_ID_ARM, clock, events),
    }

    artifact = run_image_lifecycle_benchmark(
        spec,
        arms=arms,
        clock=clock,
        generated_at=lambda: "2026-08-08T21:00:00Z",
    )

    assert artifact["status"] == "rejected"
    assert artifact["retries"] == 0
    assert artifact["replacement_samples"] == 0
    assert len(events) == 1
    assert artifact["failures"][0]["error_type"] == "RuntimeError"
    assert "secret" not in str(artifact)


def test_image_lifecycle_surface_retains_cleanup_failure_without_replacement() -> None:
    clock = _Clock()
    events: list[str] = []
    spec = ImageLifecycleBenchmarkSpec(
        source_revision="a" * 40,
        release_record=_release_record(),
        run_kind="pilot",
        samples_per_arm=1,
        warmup_pairs=1,
        schedule_seed=7,
        requested_region="us-west-2",
        cpu=1.0,
        memory_mib=2048,
        sandbox_timeout_seconds=180,
        max_estimated_cost_usd=20.0,
        caller_label="test-external-caller",
    )
    arms = {
        arm: _FakeArm(
            arm,
            clock,
            events,
            close_failure=RuntimeError("secret cleanup detail"),
        )
        for arm in (INLINE_RECIPE_ARM, MANAGED_EXACT_ID_ARM)
    }

    artifact = run_image_lifecycle_benchmark(
        spec,
        arms=arms,
        clock=clock,
        generated_at=lambda: "2026-08-08T21:00:00Z",
    )

    assert artifact["status"] == "rejected"
    assert artifact["failures"] == [
        {
            "sequence": 0,
            "phase": "cleanup",
            "schedule_phase": "warmup",
            "pair_index": 0,
            "sample_index": None,
            "arm": INLINE_RECIPE_ARM,
            "error_type": "RuntimeError",
        }
    ]
    assert artifact["observations"] == []
    assert artifact["replacement_samples"] == 0
    assert "secret" not in str(artifact)


def test_image_lifecycle_surface_marks_a_measured_cleanup_failure() -> None:
    clock = _Clock()
    events: list[str] = []
    spec = ImageLifecycleBenchmarkSpec(
        source_revision="a" * 40,
        release_record=_release_record(),
        run_kind="pilot",
        samples_per_arm=1,
        warmup_pairs=1,
        schedule_seed=7,
        requested_region="us-west-2",
        cpu=1.0,
        memory_mib=2048,
        sandbox_timeout_seconds=180,
        max_estimated_cost_usd=20.0,
        caller_label="test-external-caller",
    )
    arms = {
        arm: _FakeArm(
            arm,
            clock,
            events,
            close_failure=RuntimeError("secret cleanup detail"),
            close_failure_phase="measure",
        )
        for arm in (INLINE_RECIPE_ARM, MANAGED_EXACT_ID_ARM)
    }

    artifact = run_image_lifecycle_benchmark(
        spec,
        arms=arms,
        clock=clock,
        generated_at=lambda: "2026-08-08T21:00:00Z",
    )

    assert artifact["status"] == "rejected"
    assert len(artifact["observations"]) == 1
    assert artifact["observations"][0]["status"] == "failed"
    assert artifact["observations"][0]["cleanup"] == {
        "attempted": True,
        "succeeded": False,
    }


def test_image_lifecycle_spec_enforces_primary_count_identity_and_budget() -> None:
    common = {
        "source_revision": "a" * 40,
        "release_record": _release_record(),
        "warmup_pairs": 1,
        "schedule_seed": 7,
        "requested_region": "us-west-2",
        "cpu": 1.0,
        "memory_mib": 2048,
        "sandbox_timeout_seconds": 180,
        "max_estimated_cost_usd": 20.0,
        "caller_label": "test-external-caller",
    }

    with pytest.raises(ValueError, match="primary samples_per_arm"):
        ImageLifecycleBenchmarkSpec(
            **common,
            run_kind="primary",
            samples_per_arm=29,
        )
    with pytest.raises(ValueError, match="budget"):
        ImageLifecycleBenchmarkSpec(
            **{**common, "max_estimated_cost_usd": 0.01},
            run_kind="primary",
            samples_per_arm=30,
        )
    with pytest.raises(ValueError, match="release revision"):
        ImageLifecycleBenchmarkSpec(
            **{**common, "source_revision": "d" * 40},
            run_kind="pilot",
            samples_per_arm=2,
        )
