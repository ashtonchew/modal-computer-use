from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modal_computer_use.benchmarks.modal_optimized_frontier import (
    ARM_V1_CONNECT,
    ARM_V1_TUNNEL,
    ARM_V2_I6PN,
    COMPARISON,
    DIAGNOSTIC_ARMS,
    PILOT_ARMS,
    PRIMARY_ARMS,
    OptimizedFrontierConfig,
    build_placement_binding,
    build_preregistration,
    build_result_artifact,
    build_trial_schedule,
    classified_raw_artifact_path,
    evaluate_gates,
    lifecycle_gate_failure_reason,
    promotion_gate_failure_reason,
    requested_controls,
    sanitize_result_artifact,
    serialize_json,
    validate_result_artifact,
)
from modal_computer_use.benchmarks.modal_optimized_frontier_execution import (
    FRONTIER_RESULT_END,
    FRONTIER_RESULT_START,
    BenchmarkTerminationSignal,
    exclusive_frontier_execution_lock,
    raise_benchmark_termination_signal,
    run_frontier_trial,
)
from modal_computer_use.benchmarks.modal_v2_placement import (
    serialize_placement_capability,
)

PLACEMENT_PATH = Path("benchmark-data/modal-v2-placement-capability-2026-07-19.json")


def test_configuration_and_schedules_predeclare_primary_and_diagnostics() -> None:
    config = OptimizedFrontierConfig(image_revision="a" * 40)

    assert config.pilot_samples_per_arm == 5
    assert config.full_samples_per_primary_arm == 30
    assert config.expected_placement(ARM_V1_TUNNEL) == (
        "CLOUD_PROVIDER_OCI",
        "us-phoenix-1",
    )
    assert config.expected_placement(ARM_V2_I6PN) == (
        "CLOUD_PROVIDER_AZURE",
        "westus3",
    )
    pilot = build_trial_schedule(phase="pilot", samples_per_arm=5, seed=20260721)
    full = build_trial_schedule(phase="full", samples_per_arm=30, seed=20260722)
    assert len(pilot) == 20
    assert len(full) == 60
    assert {row["arm"] for row in pilot} == set(PILOT_ARMS)
    assert {row["arm"] for row in full} == set(PRIMARY_ARMS)
    assert not ({row["arm"] for row in full} & set(DIAGNOSTIC_ARMS))

    with pytest.raises(ValueError, match="exactly 5"):
        OptimizedFrontierConfig(image_revision="a" * 40, pilot_samples_per_arm=4)
    with pytest.raises(ValueError, match="unconstrained"):
        OptimizedFrontierConfig(image_revision="a" * 40, v2_cloud="aws")
    with pytest.raises(ValueError, match="prewarmed Chromium"):
        OptimizedFrontierConfig(image_revision="a" * 40, browser_prewarm=False)
    with pytest.raises(ValueError, match="1024x768"):
        OptimizedFrontierConfig(image_revision="a" * 40, width=1280)


def test_placement_binding_preserves_descriptive_only_foundation() -> None:
    payload, raw = _placement()
    binding = build_placement_binding(
        payload,
        artifact_path=PLACEMENT_PATH.as_posix(),
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
    )

    assert binding["classification"] == "descriptive-placement-capability-only"
    assert binding["measurement_performed"] is False
    assert binding["backend_causal_comparison_available"] is False
    assert binding["observed_common_stratum"] is False
    assert binding["v1"]["actual_region"] == "us-phoenix-1"
    assert binding["v2"]["actual_region"] == "westus3"

    altered = copy.deepcopy(payload)
    altered["measurement_performed"] = True
    with pytest.raises(ValueError, match="must not contain measurements"):
        build_placement_binding(
            altered,
            artifact_path=PLACEMENT_PATH.as_posix(),
            artifact_sha256="a" * 64,
        )


def test_preregistration_labels_ratio_and_every_unavoidable_asymmetry() -> None:
    preregistration = _preregistration()

    assert preregistration["primary_arms_predeclared"] == list(PRIMARY_ARMS)
    assert preregistration["diagnostic_arms_predeclared"] == list(DIAGNOSTIC_ARMS)
    assert preregistration["classification_policy"] == {
        "comparison": "descriptive-best-system",
        "ratio_name": "optimized-frontier-path-ratio",
        "ratio_direction": "v1_optimized_p50_divided_by_v2_optimized_p50",
        "backend_causal_speedup_allowed": False,
        "winner_label_allowed": False,
        "v2_connect_parity_allowed": False,
    }
    assert preregistration["unavoidable_asymmetries"] == [
        "backend generation",
        "cloud provider",
        "concrete region",
        "runner generation",
        "ingress",
        "transport",
    ]
    assert preregistration["capabilities"]["v2_connect_tokens"] == "unsupported"
    assert preregistration["measurement"]["runner_lifecycle"] == (
        "create-verify-measure-terminate-per-sample"
    )


def test_primary_gate_ignores_diagnostic_failure_but_fails_on_primary_cleanup() -> None:
    preregistration = _preregistration()
    trials = _trials("pilot", 5, PILOT_ARMS)
    diagnostic = next(row for row in trials if row["arm"] == ARM_V1_CONNECT)
    diagnostic["status"] = "failed"

    gates = evaluate_gates(
        trials,
        preregistration=preregistration,
        execution={"pilot": {"run_cleanup": _cleanup()}},
    )

    assert gates["primary_pilot_eligible"] is True
    assert gates["advance_to_full"] == list(PRIMARY_ARMS)
    assert gates["arms"][ARM_V1_CONNECT]["eligible"] is False
    assert gates["comparison"]["backend_causal"] is False

    primary = next(row for row in trials if row["arm"] == ARM_V2_I6PN)
    primary["cleanup"]["run_sweep_succeeded"] = False
    gates = evaluate_gates(
        trials,
        preregistration=preregistration,
        execution={"pilot": {"run_cleanup": _cleanup()}},
    )
    assert gates["primary_pilot_eligible"] is False
    assert gates["advance_to_full"] == []


def test_gate_rejects_placement_drift_retry_and_partial_verification() -> None:
    preregistration = _preregistration()
    trials = _trials("pilot", 5, PILOT_ARMS)
    rows = [row for row in trials if row["arm"] == ARM_V1_TUNNEL]
    rows[0]["actual"]["runner_region"] = "us-west-2"
    rows[1]["retry_count"] = 1
    rows[2]["verification"]["causal_frame"] = False

    gate = evaluate_gates(
        trials,
        preregistration=preregistration,
        execution={"pilot": {"run_cleanup": _cleanup()}},
    )["arms"][ARM_V1_TUNNEL]

    assert gate["eligible"] is False
    assert "runner placement differed from the predeclared frontier" in gate["reasons"]
    assert "retry policy was violated" in gate["reasons"]
    assert "verification was incomplete" in gate["reasons"]


def test_pilot_gate_rejects_phase_cleanup_cost_and_schedule_identity_failures() -> None:
    preregistration = _preregistration()
    trials = _trials("pilot", 5, PILOT_ARMS)

    failed_cleanup = _cleanup()
    failed_cleanup["enumeration"]["after"]["_experimental_list"] = 1
    gates = evaluate_gates(
        trials,
        preregistration=preregistration,
        execution={"pilot": {"run_cleanup": failed_cleanup}},
    )
    assert gates["primary_pilot_eligible"] is False
    assert gates["pilot_run_cleanup"]["eligible"] is False

    preregistration["configuration"]["max_estimated_cost_usd"] = 0.1
    gates = evaluate_gates(
        trials,
        preregistration=preregistration,
        execution={"pilot": {"run_cleanup": _cleanup()}},
    )
    assert gates["primary_pilot_eligible"] is False
    assert gates["pilot_cost"]["eligible"] is False

    preregistration = _preregistration()
    arm_rows = [row for row in trials if row["arm"] == ARM_V1_TUNNEL]
    arm_rows[1]["sequence"] = arm_rows[0]["sequence"]
    arm_rows[1]["lifecycle_index"] = arm_rows[0]["lifecycle_index"]
    gates = evaluate_gates(
        trials,
        preregistration=preregistration,
        execution={"pilot": {"run_cleanup": _cleanup()}},
    )
    assert "attempt identities differed from the preregistered lifecycle schedule" in (
        gates["arms"][ARM_V1_TUNNEL]["reasons"]
    )

    preregistration = _preregistration()
    preregistration["environment"]["placement_capability"]["measurement_performed"] = True
    gates = evaluate_gates(
        _trials("pilot", 5, PILOT_ARMS),
        preregistration=preregistration,
        execution={"pilot": {"run_cleanup": _cleanup()}},
    )
    assert gates["primary_pilot_eligible"] is False
    assert gates["provenance"]["eligible"] is False


def test_complete_artifact_emits_only_descriptive_optimized_frontier_ratio() -> None:
    preregistration = _preregistration()
    trials = [
        *_trials("pilot", 5, PILOT_ARMS),
        *_trials("full", 30, PRIMARY_ARMS),
    ]
    result = build_result_artifact(
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration=preregistration,
        trials=trials,
        throughput=_throughput(),
        execution_status="complete",
        status_reason="all gates passed",
        execution=_execution(),
    )

    comparison = result["comparisons"][COMPARISON]
    assert comparison["phase"] == "full"
    assert comparison["classification"] == "descriptive-best-system"
    assert comparison["ratio_label"] == "optimized-frontier-path-ratio"
    assert comparison["backend_causal"] is False
    assert set(comparison["ratios"]) == {
        "allocation_ms",
        "daemon_ready_ms",
        "browser_ready_ms",
        "first_valid_frame_ms",
        "warm_action_to_frame_ms",
    }
    assert result["claims"]["backend_causal_speedup"] is False
    assert lifecycle_gate_failure_reason(result, preregistration=preregistration) is None
    assert promotion_gate_failure_reason(result, preregistration=preregistration) is None
    validate_result_artifact(result, preregistration=preregistration)


def test_promotion_rejects_missing_dual_list_inventory_and_incomplete_throughput() -> None:
    preregistration = _preregistration()
    result = build_result_artifact(
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration=preregistration,
        trials=[*_trials("pilot", 5, PILOT_ARMS), *_trials("full", 30, PRIMARY_ARMS)],
        throughput=_throughput(),
        execution_status="complete",
        status_reason="all gates passed",
        execution=_execution(),
    )
    result["execution"]["full"]["run_cleanup"]["enumeration"] = None
    assert "both V1 and V2 listings" in str(
        lifecycle_gate_failure_reason(result, preregistration=preregistration)
    )

    result = build_result_artifact(
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration=preregistration,
        trials=[*_trials("pilot", 5, PILOT_ARMS), *_trials("full", 30, PRIMARY_ARMS)],
        throughput=_throughput()[:-1],
        execution_status="rejected",
        status_reason="throughput incomplete",
        execution=_execution(),
    )
    assert "1, 5, and 20" in str(
        promotion_gate_failure_reason(result, preregistration=preregistration)
    )


def test_sanitizer_rejects_candidate_and_tracks_raw_hash_for_complete_result() -> None:
    preregistration = _preregistration()
    candidate = build_result_artifact(
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration=preregistration,
        trials=_trials("pilot", 5, PILOT_ARMS),
        throughput=[],
        execution_status="candidate",
        status_reason="pilot passed",
        execution={"pilot": {"run_cleanup": _cleanup()}},
    )
    with pytest.raises(ValueError, match="only complete"):
        sanitize_result_artifact(
            candidate,
            raw_bytes=serialize_json(candidate).encode(),
            raw_artifact_path="benchmark-results/frontier/candidates/pilot.json",
            preregistration=preregistration,
            normalizer_sha="a" * 40,
        )

    complete = build_result_artifact(
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration=preregistration,
        trials=[*_trials("pilot", 5, PILOT_ARMS), *_trials("full", 30, PRIMARY_ARMS)],
        throughput=_throughput(),
        execution_status="complete",
        status_reason="all gates passed",
        execution=_execution(),
    )
    raw = serialize_json(complete).encode()
    promoted = sanitize_result_artifact(
        complete,
        raw_bytes=raw,
        raw_artifact_path="benchmark-results/frontier/candidates/full.json",
        preregistration=preregistration,
        normalizer_sha="a" * 40,
    )
    assert promoted["artifact_status"] == "current_reference"
    assert promoted["provenance"]["raw_artifact_sha256"] == hashlib.sha256(raw).hexdigest()


def test_rejected_artifacts_stay_under_ignored_results() -> None:
    assert (
        classified_raw_artifact_path(
            "benchmark-results/frontier/candidates/pilot.json", status="rejected"
        )
        == "benchmark-results/frontier/rejected/pilot.json"
    )
    with pytest.raises(ValueError, match="under benchmark-results"):
        classified_raw_artifact_path("pilot.json", status="rejected")


def test_termination_signal_uses_interrupt_cleanup_path() -> None:
    with pytest.raises(BenchmarkTerminationSignal):
        raise_benchmark_termination_signal(15, None)


def test_execution_lock_rejects_overlapping_pilots(tmp_path: Path) -> None:
    lock_path = tmp_path / "frontier.lock"

    with (
        exclusive_frontier_execution_lock(lock_path),
        pytest.raises(RuntimeError, match="already active"),
        exclusive_frontier_execution_lock(lock_path),
    ):
        pytest.fail("overlapping execution must not acquire the lock")

    with exclusive_frontier_execution_lock(lock_path):
        pass


@pytest.mark.parametrize(
    ("arm", "backend", "i6pn"),
    ((ARM_V1_TUNNEL, "v1", False), (ARM_V2_I6PN, "v2", True)),
)
def test_lifecycle_creates_generation_matched_runner_and_cleans_every_resource(
    arm: str,
    backend: str,
    i6pn: bool,
) -> None:
    config = OptimizedFrontierConfig(image_revision="a" * 40)
    expected_cloud, expected_region = config.expected_placement(arm)
    runner_calls: list[dict] = []
    target_calls: list[dict] = []
    cleanup_calls: list[tuple[str, str, bool]] = []

    class Runner:
        def __init__(self) -> None:
            self.placement = {"cloud": expected_cloud, "region": expected_region}

        def execute(self, *_args, **_kwargs):
            payload = {
                "status": "valid",
                "stages_ms": {
                    "daemon_ready": 10.0,
                    "browser_ready": 20.0,
                    "first_valid_frame": 30.0,
                },
                "warm_action_to_frame_ms": 4.0,
                "placement": {"cloud": expected_cloud, "region": expected_region},
                "verification": {
                    key: True
                    for key in (
                        "healthz",
                        "readyz",
                        "version",
                        "capabilities",
                        "browser",
                        "frame",
                        "action",
                        "causal_frame",
                        "changed_frame",
                        "binary_envelope",
                    )
                },
            }
            return SimpleNamespace(
                stdout=(f"{FRONTIER_RESULT_START}\n{json.dumps(payload)}\n{FRONTIER_RESULT_END}\n")
            )

        def terminate(self) -> bool:
            return True

    class Target:
        def runtime_placement(self):
            return {"cloud": expected_cloud, "region": expected_region}

        def terminate(self, *, wait: bool) -> None:
            assert wait is True

        def detach(self) -> None:
            return None

    def runner_factory(**kwargs):
        runner_calls.append(kwargs)
        return Runner()

    def target_factory(**kwargs):
        target_calls.append(kwargs)
        kwargs["timing"].mark("sandbox_registered")
        return Target()

    def cleanup_sweep(*, app_name: str, run_id: str, include_inventory: bool):
        cleanup_calls.append((app_name, run_id, include_inventory))
        return _cleanup()

    trial = run_frontier_trial(
        config,
        schedule_item={
            "sequence": 0,
            "phase": "pilot",
            "arm": arm,
            "lifecycle_index": 0,
        },
        app_name="frontier-app",
        phase_run_id="phase-run",
        runner_factory=runner_factory,
        target_factory=target_factory,
        cleanup_sweep=cleanup_sweep,
    )

    assert trial["status"] == "valid"
    assert runner_calls[0]["backend"] == backend
    assert runner_calls[0]["i6pn"] is i6pn
    assert runner_calls[0]["cpu"] == config.runner_cpu
    assert runner_calls[0]["memory_mib"] == config.runner_memory_mib
    assert runner_calls[0]["runner_label"] == "modal-optimized-frontier"
    assert target_calls[0]["backend"] == backend
    assert cleanup_calls == [("frontier-app", "phase-run-pilot-000", True)]
    assert trial["cleanup"]["target_terminated"] is True
    assert trial["cleanup"]["target_detached"] is True
    assert trial["cleanup"]["runner_terminated"] is True
    assert trial["cleanup"]["run_sweep_succeeded"] is True


def _placement() -> tuple[dict, bytes]:
    raw = PLACEMENT_PATH.read_bytes()
    payload = json.loads(raw)
    assert raw == serialize_placement_capability(payload)
    return payload, raw


def _preregistration() -> dict:
    payload, raw = _placement()
    config = OptimizedFrontierConfig(image_revision="a" * 40, bootstrap_resamples=100)
    binding = build_placement_binding(
        payload,
        artifact_path=PLACEMENT_PATH.as_posix(),
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
    )
    return build_preregistration(
        config,
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        placement_binding=binding,
        sdk_versions={"modal": "1.5.2"},
        package_versions={"modal-computer-use": "0.1.0"},
        commands={key: key for key in ("preregister", "pilot", "full", "sanitize", "check")},
    )


def _trials(phase: str, count: int, arms: tuple[str, ...]) -> list[dict]:
    config = OptimizedFrontierConfig(image_revision="a" * 40, bootstrap_resamples=100)
    rows: list[dict] = []
    seed = config.order_seed if phase == "pilot" else config.order_seed + 1
    schedule = build_trial_schedule(phase=phase, samples_per_arm=count, seed=seed)
    assert {item["arm"] for item in schedule} == set(arms)
    for item in schedule:
        arm = item["arm"]
        index = item["lifecycle_index"]
        cloud, region = config.expected_placement(arm)
        rows.append(
            {
                "sequence": item["sequence"],
                "phase": item["phase"],
                "arm": arm,
                "lifecycle_index": item["lifecycle_index"],
                "status": "valid",
                "metrics": {
                    "allocation_ms": 120.0 + index + (20 if arm == ARM_V1_TUNNEL else 0),
                    "daemon_ready_ms": 220.0 + index,
                    "browser_ready_ms": 320.0 + index,
                    "first_valid_frame_ms": 420.0 + index,
                    "warm_action_to_frame_ms": 20.0 + index,
                },
                "requested": requested_controls(config, arm),
                "actual": {
                    "target_cloud": cloud,
                    "target_region": region,
                    "runner_cloud": cloud,
                    "runner_region": region,
                    "i6pn_reachability": (
                        "verified-workspace-private-direct"
                        if arm == ARM_V2_I6PN
                        else "not-applicable"
                    ),
                },
                "verification": {
                    key: True
                    for key in (
                        "runner_placement",
                        "healthz",
                        "readyz",
                        "version",
                        "capabilities",
                        "browser",
                        "frame",
                        "action",
                        "causal_frame",
                        "changed_frame",
                        "binary_envelope",
                    )
                },
                "retry_count": 0,
                "failure": None,
                "cleanup": {
                    "target_terminated": True,
                    "target_detached": True,
                    "runner_terminated": True,
                    "run_sweep_succeeded": True,
                    "enumeration": _cleanup()["enumeration"],
                },
                "estimated_billed_cost": {
                    "status": "resource-time-proxy",
                    "estimated_usd": 0.01,
                },
            }
        )
    return rows


def _throughput() -> list[dict]:
    config = OptimizedFrontierConfig(image_revision="a" * 40)
    return [
        {
            "arm": arm,
            "concurrency": concurrency,
            "status": "valid",
            "cleanup_succeeded": True,
            "attempts": [
                {
                    "status": "valid",
                    "cleanup_succeeded": True,
                    "actual_cloud": config.expected_placement(arm)[0],
                    "actual_region": config.expected_placement(arm)[1],
                }
                for _ in range(concurrency)
            ],
        }
        for concurrency in (1, 5, 20)
        for arm in PRIMARY_ARMS
    ]


def _cleanup() -> dict:
    return {
        "matched_sandboxes": 0,
        "terminated_sandboxes": 0,
        "termination_failures": 0,
        "remaining_sandboxes": 0,
        "cleanup_succeeded": True,
        "enumeration": {
            "before": {"list": 0, "_experimental_list": 0},
            "after": {"list": 0, "_experimental_list": 0},
            "apis": ["Sandbox.list", "Sandbox._experimental_list"],
        },
    }


def _execution() -> dict:
    return {
        "pilot": {"run_cleanup": _cleanup()},
        "full": {"run_cleanup": _cleanup()},
        "throughput_cleanup": _cleanup(),
    }
