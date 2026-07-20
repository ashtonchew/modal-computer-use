from __future__ import annotations

import copy
import json

import pytest

import modal_computer_use.benchmarks.modal_v2_candidate_execution as candidate_execution
from modal_computer_use.benchmarks.modal_v2_candidate import (
    ARM_V1_CONNECT,
    ARM_V1_TUNNEL,
    ARM_V2_I6PN,
    ARM_V2_TUNNEL,
    BACKEND_COMPARISON,
    CANONICAL_ARMS,
    ModalV2CandidateConfig,
    build_preregistration,
    build_result_artifact,
    build_trial_schedule,
    classified_raw_artifact_path,
    evaluate_pilot_gates,
    preregistration_sha256,
    sanitize_result_artifact,
    summarize_distribution,
    validate_phase_checkpoint,
    validate_result_artifact,
)
from modal_computer_use.benchmarks.modal_v2_candidate_execution import (
    CANDIDATE_RESULT_END,
    CANDIDATE_RESULT_START,
    CandidatePlacementMismatchError,
    extract_modal_direct_runner_result,
    modal_direct_runner_code,
    modal_direct_runner_error_code,
    modal_direct_runner_error_detail,
    modal_direct_runner_error_stage,
    modal_direct_runner_error_type,
    run_candidate_phase,
    run_candidate_throughput,
)
from modal_computer_use.benchmarks.modal_v2_placement import (
    build_placement_capability_binding,
    run_placement_capability_matrix,
)
from modal_computer_use.sandbox import ModalCandidatePlacementProbe


class _FakeRunner:
    def __init__(self) -> None:
        self.placement = {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"}
        self.terminated = False

    def terminate(self) -> bool:
        self.terminated = True
        return True


def test_config_freezes_sample_concurrency_and_common_cloud_policy() -> None:
    config = ModalV2CandidateConfig(image_revision="a" * 40)

    assert config.pilot_samples_per_arm == 5
    assert config.full_samples_per_arm == 30
    assert config.throughput_concurrency == (1, 5, 20)
    assert config.cloud is None
    assert config.max_estimated_cost_usd == 20.0

    with pytest.raises(ValueError, match="unsupported by the required Modal V1"):
        ModalV2CandidateConfig(image_revision="a" * 40, cloud="azure")
    with pytest.raises(ValueError, match="pilot requires exactly 5"):
        ModalV2CandidateConfig(image_revision="a" * 40, pilot_samples_per_arm=4)
    with pytest.raises(ValueError, match="throughput concurrency"):
        ModalV2CandidateConfig(image_revision="a" * 40, throughput_concurrency=(1, 5))


def test_trial_schedule_is_deterministic_randomized_and_balanced() -> None:
    first = build_trial_schedule(phase="pilot", samples_per_arm=5, seed=20260719)
    second = build_trial_schedule(phase="pilot", samples_per_arm=5, seed=20260719)

    assert first == second
    assert len(first) == 20
    assert [row["arm"] for row in first[:4]] != list(CANONICAL_ARMS)
    assert {arm: sum(row["arm"] == arm for row in first) for arm in CANONICAL_ARMS} == {
        arm: 5 for arm in CANONICAL_ARMS
    }
    assert all(
        {row["arm"] for row in first[offset : offset + len(CANONICAL_ARMS)]}
        == set(CANONICAL_ARMS)
        for offset in range(0, len(first), len(CANONICAL_ARMS))
    )
    assert {row["sequence"] for row in first} == set(range(20))


def test_preregistration_labels_product_matched_and_asymmetric_arms() -> None:
    preregistration = _preregistration()

    assert set(preregistration["arms"]) == set(CANONICAL_ARMS)
    assert preregistration["arms"][ARM_V1_CONNECT]["classification"] == "public-product-path"
    assert preregistration["arms"][ARM_V1_TUNNEL]["neutral_backend_comparison_arm"] is True
    assert preregistration["arms"][ARM_V2_TUNNEL]["neutral_backend_comparison_arm"] is True
    assert preregistration["arms"][ARM_V2_I6PN]["classification"] == (
        "asymmetric-optimized-candidate"
    )
    assert preregistration["classification_policy"]["target-loopback"] == (
        "same-container-diagnostic-only"
    )
    assert preregistration["capabilities"]["v2_connect_tokens"] == "unsupported"
    assert preregistration["capabilities"]["v2_memory_snapshots"] == "unsupported"


def test_distribution_reports_raw_samples_ecdf_and_deterministic_uncertainty() -> None:
    first = summarize_distribution(
        [10.0, 20.0, 30.0, 40.0, 50.0],
        bootstrap_seed=7,
        bootstrap_resamples=200,
    )
    second = summarize_distribution(
        [50.0, 40.0, 30.0, 20.0, 10.0],
        bootstrap_seed=7,
        bootstrap_resamples=200,
    )

    assert first == second
    assert first["p50_ms"] == 30.0
    assert first["raw_samples_ms"] == [10.0, 20.0, 30.0, 40.0, 50.0]
    assert first["ecdf"][-1]["probability"] == 1.0
    assert len(first["bootstrap_95_ci"]["p50_ms"]) == 2


def test_pilot_gate_allows_only_transport_matched_backend_ratio() -> None:
    preregistration = _preregistration()
    trials = _trials("pilot", 5)

    gates = evaluate_pilot_gates(trials, preregistration=preregistration)

    assert gates["advance_to_full"] == list(CANONICAL_ARMS)
    assert gates["comparisons"][BACKEND_COMPARISON]["eligible"] is True
    assert gates["comparisons"][ARM_V1_CONNECT]["backend_causal_ratio_eligible"] is False
    assert gates["comparisons"][ARM_V2_I6PN]["backend_causal_ratio_eligible"] is False


def test_pilot_gate_fails_closed_on_placement_verification_and_cleanup() -> None:
    preregistration = _preregistration()
    trials = _trials("pilot", 5)
    trials[0]["actual"]["target_region"] = "us-east-1"
    trials[1]["verification"]["causal_frame"] = False
    trials[2]["cleanup"]["target_terminated"] = False

    gates = evaluate_pilot_gates(trials, preregistration=preregistration)

    assert gates["arms"][trials[1]["arm"]]["eligible"] is False
    assert gates["arms"][trials[2]["arm"]]["eligible"] is False
    assert gates["comparisons"][BACKEND_COMPARISON]["eligible"] is False
    assert gates["comparisons"][BACKEND_COMPARISON]["ratio_metrics"] == []
    assert all(not gate["eligible"] for gate in gates["arms"].values())


def test_pilot_gate_rejects_runner_target_cross_cloud_path() -> None:
    preregistration = _preregistration()
    trials = _trials("pilot", 5)
    trials[0]["actual"]["runner_cloud"] = "CLOUD_PROVIDER_AZURE"
    trials[0]["actual"]["runner_region"] = "westus3"

    gates = evaluate_pilot_gates(trials, preregistration=preregistration)

    gate = gates["arms"][trials[0]["arm"]]
    assert gate["eligible"] is False
    assert "target and runner were not colocated on one exact provider region" in gate["reasons"]


def test_pilot_gate_rejects_drift_from_unconstrained_capability_placement() -> None:
    preregistration = _preregistration(cloud=None)
    trials = _trials("pilot", 5)
    for trial in trials:
        trial["requested"]["cloud"] = None
        trial["actual"].update(
            {
                "target_cloud": "CLOUD_PROVIDER_GCP",
                "target_region": "us-west1-a",
                "runner_cloud": "CLOUD_PROVIDER_GCP",
                "runner_region": "us-west1-a",
            },
        )

    gates = evaluate_pilot_gates(trials, preregistration=preregistration)

    assert all(not gate["eligible"] for gate in gates["arms"].values())
    assert all(
        "actual placement differed from the capability-bound placement" in gate["reasons"]
        for gate in gates["arms"].values()
    )


def test_rejected_raw_output_is_classified_and_never_targets_repository_root() -> None:
    assert classified_raw_artifact_path(
        "benchmark-results/modal-v2/candidates/pilot.json", status="rejected"
    ) == "benchmark-results/modal-v2/rejected/pilot.json"
    with pytest.raises(ValueError, match="under benchmark-results"):
        classified_raw_artifact_path("pilot.json", status="candidate")


def test_checkpoint_validator_binds_partial_trials_to_preregistration() -> None:
    preregistration = _preregistration()
    trials = _trials("pilot", 5)[:3]
    checkpoint = {
        "schema_version": 1,
        "benchmark": "modal-v2-candidate-checkpoint",
        "generated_at": "2026-07-19T00:00:00Z",
        "source_sha": "a" * 40,
        "preregistration_sha256": preregistration_sha256(preregistration),
        "phase": "pilot",
        "state": "running",
        "schedule_total": 20,
        "completed_attempts": 3,
        "trials": trials,
        "execution": {"state": "running"},
    }

    validate_phase_checkpoint(checkpoint, preregistration=preregistration)

    invalid = copy.deepcopy(checkpoint)
    invalid["completed_attempts"] = 4
    with pytest.raises(ValueError, match="completed count"):
        validate_phase_checkpoint(invalid, preregistration=preregistration)


def test_candidate_phase_checkpoints_after_cleanup_on_interrupt(monkeypatch) -> None:
    config = ModalV2CandidateConfig(
        image_revision="a" * 40,
        cloud="aws",
        bootstrap_resamples=100,
    )
    runner = _FakeRunner()
    calls = 0

    def fake_trial(*_args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        item = kwargs["schedule_item"]
        return {
            "phase": item["phase"],
            "arm": item["arm"],
            "status": "failed",
            "failure": {"error_type": "RuntimeError"},
            "cleanup": {
                "target_terminated": True,
                "target_detached": True,
                "runner_terminated": None,
            },
        }

    monkeypatch.setattr(candidate_execution, "run_candidate_trial", fake_trial)
    checkpoints: list[tuple[list[dict], dict]] = []
    progress: list[tuple] = []
    schedule = build_trial_schedule(phase="pilot", samples_per_arm=5, seed=20260719)[:2]

    with pytest.raises(KeyboardInterrupt):
        run_candidate_phase(
            config,
            schedule=schedule,
            runner_factory=lambda **_kwargs: runner,
            checkpoint=lambda trials, execution: checkpoints.append(
                (copy.deepcopy(trials), copy.deepcopy(execution))
            ),
            progress=lambda *args: progress.append(args),
            cleanup_sweep=lambda **_kwargs: {
                "cleanup_succeeded": True,
                "remaining_sandboxes": 0,
            },
        )

    assert progress[0][-2:] == ("failed", "RuntimeError")
    assert checkpoints[-1][1]["state"] == "interrupted"
    assert checkpoints[-1][1]["error_type"] == "KeyboardInterrupt"
    assert checkpoints[-1][0][0]["cleanup"]["runner_terminated"] is True
    assert runner.terminated is True


def test_candidate_phase_rejects_runner_cloud_mismatch_before_trials(monkeypatch) -> None:
    config = ModalV2CandidateConfig(
        image_revision="a" * 40,
        cloud="aws",
        bootstrap_resamples=100,
    )
    runner = _FakeRunner()
    runner.placement = {"cloud": "CLOUD_PROVIDER_AZURE", "region": "westus3"}
    monkeypatch.setattr(
        candidate_execution,
        "run_candidate_trial",
        lambda *_args, **_kwargs: pytest.fail("placement mismatch must fail before trials"),
    )
    checkpoints: list[tuple[list[dict], dict]] = []

    with pytest.raises(CandidatePlacementMismatchError, match="requested runner cloud aws"):
        run_candidate_phase(
            config,
            schedule=build_trial_schedule(
                phase="pilot", samples_per_arm=5, seed=20260719
            ),
            runner_factory=lambda **_kwargs: runner,
            checkpoint=lambda trials, execution: checkpoints.append(
                (copy.deepcopy(trials), copy.deepcopy(execution))
            ),
            cleanup_sweep=lambda **_kwargs: {
                "cleanup_succeeded": True,
                "remaining_sandboxes": 0,
            },
        )

    assert checkpoints[-1][0] == []
    assert checkpoints[-1][1]["state"] == "failed"
    assert checkpoints[-1][1]["placement_preflight"] == {
        "requested_cloud": "aws",
        "requested_region": "us-west",
        "expected_actual_cloud": None,
        "expected_actual_region": None,
        "actual_cloud": "CLOUD_PROVIDER_AZURE",
            "actual_region": "westus3",
            "eligible": False,
            "reason": "requested runner cloud aws; observed CLOUD_PROVIDER_AZURE/westus3",
        }
    assert checkpoints[-1][1]["runner_cleanup_succeeded"] is True
    assert runner.terminated is True


def test_result_validator_rejects_aliases_winner_claims_and_ineligible_ratios() -> None:
    preregistration = _preregistration()
    payload = build_result_artifact(
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration=preregistration,
        trials=_trials("pilot", 5),
        throughput=[],
        execution_status="candidate",
        status_reason="pilot complete",
    )
    assert payload["comparisons"][BACKEND_COMPARISON]["ratios"]

    winner = copy.deepcopy(payload)
    winner["claims"]["winner"] = "v2"
    with pytest.raises(ValueError, match="default or winner"):
        validate_result_artifact(winner, preregistration=preregistration)

    alias = copy.deepcopy(payload)
    alias["trials"][0]["arm"] = "modal-v2-ab"
    with pytest.raises(ValueError, match="arm label"):
        validate_result_artifact(alias, preregistration=preregistration)

    ineligible = copy.deepcopy(payload)
    ineligible["eligibility"]["comparisons"][BACKEND_COMPARISON]["eligible"] = False
    ineligible["eligibility"]["comparisons"][BACKEND_COMPARISON]["ratio_metrics"] = []
    ineligible["claims"]["backend_causal_ratios_emitted"] = False
    with pytest.raises(ValueError, match="ineligible backend"):
        validate_result_artifact(ineligible, preregistration=preregistration)

    interrupted_full = build_result_artifact(
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration=preregistration,
        trials=[*_trials("pilot", 5), _trials("full", 1)[0]],
        throughput=[],
        execution_status="rejected",
        status_reason="full interrupted after one retained attempt",
    )
    assert interrupted_full["comparisons"][BACKEND_COMPARISON]["phase"] == "pilot"


def test_promotion_requires_complete_full_samples_and_throughput() -> None:
    preregistration = _preregistration()
    pilot = _trials("pilot", 5)
    full = _trials("full", 30)
    throughput = [
        {
            "backend": backend,
            "concurrency": concurrency,
            "status": "valid",
            "cleanup_succeeded": True,
            "requested_cloud": "aws",
            "requested_region": "us-west",
            "attempts": [
                {
                    "status": "valid",
                    "cleanup_succeeded": True,
                    "actual_cloud": "CLOUD_PROVIDER_AWS",
                    "actual_region": "us-west-2",
                }
                for _ in range(concurrency)
            ],
        }
        for backend in ("v1", "v2")
        for concurrency in (1, 5, 20)
    ]
    raw = build_result_artifact(
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        preregistration=preregistration,
        trials=[*pilot, *full],
        throughput=throughput,
        execution_status="complete",
        status_reason="all gates passed",
        execution={
            "pilot": {
                "run_cleanup": {
                    "cleanup_succeeded": True,
                    "remaining_sandboxes": 0,
                    "termination_failures": 0,
                }
            },
            "full": {
                "run_cleanup": {
                    "cleanup_succeeded": True,
                    "remaining_sandboxes": 0,
                    "termination_failures": 0,
                }
            },
            "throughput_cleanup": {
                "cleanup_succeeded": True,
                "remaining_sandboxes": 0,
            },
        },
    )
    raw_bytes = json.dumps(raw, sort_keys=True).encode()

    promoted = sanitize_result_artifact(
        raw,
        raw_bytes=raw_bytes,
        raw_artifact_path="benchmark-results/modal-v2-candidate/candidates/full.json",
        preregistration=preregistration,
        normalizer_sha="b" * 40,
    )

    assert promoted["artifact_status"] == "current_reference"
    assert promoted["comparisons"][BACKEND_COMPARISON]["phase"] == "full"
    assert promoted["provenance"]["raw_artifact_tracked"] is False
    assert len(promoted["provenance"]["raw_artifact_sha256"]) == 64

    with pytest.raises(ValueError, match="under benchmark-results"):
        sanitize_result_artifact(
            raw,
            raw_bytes=raw_bytes,
            raw_artifact_path="benchmark-data/full.json",
            preregistration=preregistration,
            normalizer_sha="b" * 40,
        )

    missing_sweep = copy.deepcopy(raw)
    missing_sweep["execution"].pop("throughput_cleanup")
    with pytest.raises(ValueError, match="throughput cleanup"):
        sanitize_result_artifact(
            missing_sweep,
            raw_bytes=json.dumps(missing_sweep).encode(),
            raw_artifact_path="benchmark-results/modal-v2-candidate/candidates/full.json",
            preregistration=preregistration,
            normalizer_sha="b" * 40,
        )

    missing_lifecycle_sweep = copy.deepcopy(raw)
    missing_lifecycle_sweep["execution"]["pilot"].pop("run_cleanup")
    with pytest.raises(ValueError, match="pilot run cleanup"):
        sanitize_result_artifact(
            missing_lifecycle_sweep,
            raw_bytes=json.dumps(missing_lifecycle_sweep).encode(),
            raw_artifact_path="benchmark-results/modal-v2-candidate/candidates/full.json",
            preregistration=preregistration,
            normalizer_sha="b" * 40,
        )

    incomplete = copy.deepcopy(raw)
    incomplete["trials"] = incomplete["trials"][:-1]
    with pytest.raises(ValueError, match="30 valid full trials"):
        sanitize_result_artifact(
            incomplete,
            raw_bytes=json.dumps(incomplete).encode(),
            raw_artifact_path="benchmark-results/modal-v2-candidate/candidates/full.json",
            preregistration=preregistration,
            normalizer_sha="b" * 40,
        )

    mismatched_throughput = copy.deepcopy(raw)
    for row in mismatched_throughput["throughput"]:
        for attempt in row["attempts"]:
            attempt["actual_region"] = "us-east-1"
    with pytest.raises(ValueError, match="capability-bound throughput placement"):
        sanitize_result_artifact(
            mismatched_throughput,
            raw_bytes=json.dumps(mismatched_throughput).encode(),
            raw_artifact_path="benchmark-results/modal-v2-candidate/candidates/full.json",
            preregistration=preregistration,
            normalizer_sha="b" * 40,
        )


def test_throughput_uses_exact_run_id_and_finishes_with_cleanup_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_kwargs: dict[str, object] = {}
    batch_calls: list[tuple[str, int, int]] = []
    cleanup_calls: list[tuple[str, str]] = []

    class FakeContext:
        async def run_batch(
            self,
            *,
            backend: str,
            concurrency: int,
            timeout_seconds: int,
        ) -> dict[str, object]:
            batch_calls.append((backend, concurrency, timeout_seconds))
            return {
                "backend": backend,
                "concurrency": concurrency,
                "status": "valid",
                "attempts": [],
                "cleanup_succeeded": True,
            }

    def context_factory(**kwargs: object) -> FakeContext:
        factory_kwargs.update(kwargs)
        return FakeContext()

    def cleanup(*, app_name: str, run_id: str) -> dict[str, object]:
        cleanup_calls.append((app_name, run_id))
        return {"cleanup_succeeded": True, "remaining_sandboxes": 0}

    monkeypatch.setattr(
        candidate_execution,
        "create_modal_benchmark_allocation_context",
        context_factory,
    )
    monkeypatch.setattr(candidate_execution, "cleanup_modal_benchmark_run", cleanup)

    rows, sweep = run_candidate_throughput(
        ModalV2CandidateConfig(image_revision="a" * 40),
        app_name="candidate-throughput",
        run_id="run-123-throughput",
    )

    assert factory_kwargs["run_id"] == "run-123-throughput"
    assert factory_kwargs["benchmark_tag"] == "modal-v2-candidate-throughput"
    assert batch_calls == [
        (backend, concurrency, 900)
        for concurrency in (1, 5, 20)
        for backend in ("v1", "v2")
    ]
    assert len(rows) == 6
    assert cleanup_calls == [("candidate-throughput", "run-123-throughput")]
    assert sweep == {"cleanup_succeeded": True, "remaining_sandboxes": 0}


def test_throughput_interrupt_sweeps_exact_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_calls: list[tuple[str, str]] = []

    class InterruptingContext:
        async def run_batch(self, **_kwargs: object) -> dict[str, object]:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        candidate_execution,
        "create_modal_benchmark_allocation_context",
        lambda **_kwargs: InterruptingContext(),
    )
    monkeypatch.setattr(
        candidate_execution,
        "cleanup_modal_benchmark_run",
        lambda *, app_name, run_id: cleanup_calls.append((app_name, run_id)),
    )

    with pytest.raises(KeyboardInterrupt):
        run_candidate_throughput(
            ModalV2CandidateConfig(image_revision="a" * 40),
            app_name="candidate-throughput",
            run_id="run-123-throughput",
        )

    assert cleanup_calls == [("candidate-throughput", "run-123-throughput")]


def test_runner_result_parser_requires_bounded_safe_json() -> None:
    stdout = (
        f"noise\n{CANDIDATE_RESULT_START}\n"
        '{"status":"valid","warm_action_to_frame_ms":12.5}\n'
        f"{CANDIDATE_RESULT_END}\n"
    )

    assert (
        extract_modal_direct_runner_result(
            stdout,
            result_start=CANDIDATE_RESULT_START,
            result_end=CANDIDATE_RESULT_END,
        )["warm_action_to_frame_ms"]
        == 12.5
    )
    with pytest.raises(ValueError, match="bounded result"):
        extract_modal_direct_runner_result(
            '{"status":"valid"}',
            result_start=CANDIDATE_RESULT_START,
            result_end=CANDIDATE_RESULT_END,
        )


def test_direct_runner_uses_version_contract_and_safe_remote_error_types() -> None:
    source = modal_direct_runner_code(
        result_start=CANDIDATE_RESULT_START,
        result_end=CANDIDATE_RESULT_END,
        source_prefix="modal-v2-candidate",
    )

    assert 'get("api_version") == "v1"' in source
    assert 'get("daemon_version"), str' in source
    assert "computer.wait_until_ready(timeout=180.0)" in source
    assert "initial_frame = stream.start(drain_initial_frame=True)" in source
    assert "previous_payload = initial_frame.compose()" in source
    assert source.count("previous_payload=previous_payload") == 2
    assert '"status": "valid" if all(verification.values()) else "failed"' in source
    assert modal_direct_runner_error_type({"error_type": "ConnectError"}) == "ConnectError"
    assert modal_direct_runner_error_type({"error_type": "unsafe detail"}) == (
        "DirectRunnerFailure"
    )
    assert modal_direct_runner_error_stage({"error_stage": "measured_action_frame"}) == (
        "measured_action_frame"
    )
    assert modal_direct_runner_error_stage({"error_stage": "unsafe detail"}) is None
    assert modal_direct_runner_error_code({"error_code": "observation_stream_error"}) == (
        "observation_stream_error"
    )
    assert modal_direct_runner_error_code({"error_code": "unsafe detail"}) is None
    assert modal_direct_runner_error_detail({"error_detail": "unexpected_frame"}) == (
        "unexpected_frame"
    )
    assert modal_direct_runner_error_detail({"error_detail": "unsafe detail"}) is None


def _preregistration(*, cloud: str | None = "aws") -> dict:
    return build_preregistration(
        ModalV2CandidateConfig(
            image_revision="a" * 40,
            cloud=cloud,
            expected_actual_cloud="CLOUD_PROVIDER_AWS",
            expected_actual_region="us-west-2",
            bootstrap_resamples=100,
        ),
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        sdk_versions={"modal": "1.5.2"},
        package_versions={"modal-computer-use": "1.1.0"},
        runner_identity={"kind": "test"},
        placement_capability=_placement_capability_binding(cloud=cloud),
        commands={
            "placement_probe": "placement probe command",
            "preregister": "preregister command",
            "pilot": "pilot command",
            "full": "full command",
            "sanitize": "sanitize command",
            "check": "check command",
        },
    )


def _placement_capability_binding(*, cloud: str | None) -> dict:
    def probe(**kwargs):
        return ModalCandidatePlacementProbe(
            run_id=kwargs["run_id"],
            backend=kwargs["backend"],
            requested_cloud=kwargs["cloud"],
            requested_region=kwargs["region"],
            actual_cloud="CLOUD_PROVIDER_AWS",
            actual_region="us-west-2",
            i6pn_enabled=kwargs["i6pn"],
            i6pn_verified=kwargs["i6pn"],
            sandbox_created=True,
            cleanup_succeeded=True,
            status="valid",
        )

    payload = run_placement_capability_matrix(
        run_id="placement-test",
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        image_revision="a" * 40,
        region="us-west",
        target_cpu=4.0,
        target_memory_mib=8192,
        cloud_requests=(cloud,),
        probe=probe,
    )
    return build_placement_capability_binding(
        payload,
        artifact_path=(
            "benchmark-results/modal-v2-candidate-2026-07-19/"
            "diagnostics/placement-capability.json"
        ),
    )


def _trials(phase: str, count: int) -> list[dict]:
    trials: list[dict] = []
    sequence = 0
    for lifecycle_index in range(count):
        for arm in CANONICAL_ARMS:
            backend = "v1" if arm in {ARM_V1_CONNECT, ARM_V1_TUNNEL} else "v2"
            ingress = {
                ARM_V1_CONNECT: "connect-endpoint",
                ARM_V1_TUNNEL: "encrypted-tunnel",
                ARM_V2_TUNNEL: "encrypted-tunnel",
                ARM_V2_I6PN: "workspace-private-i6pn",
            }[arm]
            trials.append(
                {
                    "sequence": sequence,
                    "phase": phase,
                    "arm": arm,
                    "lifecycle_index": lifecycle_index,
                    "status": "valid",
                    "metrics": {
                        "allocation_ms": 100.0 + lifecycle_index + (10 if backend == "v1" else 0),
                        "daemon_ready_ms": 200.0 + lifecycle_index,
                        "browser_ready_ms": 300.0 + lifecycle_index,
                        "first_valid_frame_ms": 400.0 + lifecycle_index,
                        "warm_action_to_frame_ms": 20.0 + lifecycle_index,
                    },
                    "requested": {
                        "backend": backend,
                        "caller_path": "same-region-separate-v2-runner",
                        "ingress": ingress,
                        "action_transport": "persistent-hot-session",
                        "observation_transport": "binary-envelope",
                        "cloud": "aws",
                        "region": "us-west",
                        "expected_actual_cloud": "CLOUD_PROVIDER_AWS",
                        "expected_actual_region": "us-west-2",
                        "cpu": 4.0,
                        "memory_mib": 8192,
                        "image_identity": "modal-computer-use-chromium:" + "a" * 40,
                        "browser": "chromium",
                        "browser_prewarm": True,
                        "width": 1024,
                        "height": 768,
                        "readiness_boundary": "runner-direct-authenticated-daemon-browser-frame",
                        "action_semantics": "click-512-512-left",
                        "observation_semantics": "changed-causal-png-binary-envelope",
                        "cleanup_policy": "terminate-target-runner-and-detach-target",
                    },
                    "actual": {
                        "target_cloud": "CLOUD_PROVIDER_AWS",
                        "target_region": "us-west-2",
                        "runner_cloud": "CLOUD_PROVIDER_AWS",
                        "runner_region": "us-west-2",
                    },
                    "verification": {
                        "healthz": True,
                        "readyz": True,
                        "version": True,
                        "capabilities": True,
                        "browser": True,
                        "frame": True,
                        "action": True,
                        "causal_frame": True,
                        "changed_frame": True,
                        "binary_envelope": True,
                    },
                    "retry_count": 0,
                    "failure": None,
                    "cleanup": {
                        "target_terminated": True,
                        "target_detached": True,
                        "runner_terminated": True,
                        "run_sweep_succeeded": True,
                    },
                }
            )
            sequence += 1
    return trials
