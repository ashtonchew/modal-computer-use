from __future__ import annotations

import copy
import json

import pytest

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
    sanitize_result_artifact,
    summarize_distribution,
    validate_result_artifact,
)
from modal_computer_use.benchmarks.modal_v2_candidate_execution import (
    CANDIDATE_RESULT_END,
    CANDIDATE_RESULT_START,
    extract_candidate_runner_result,
)


def test_config_freezes_sample_concurrency_and_supported_cloud_policy() -> None:
    config = ModalV2CandidateConfig(image_revision="a" * 40)

    assert config.pilot_samples_per_arm == 5
    assert config.full_samples_per_arm == 30
    assert config.throughput_concurrency == (1, 5, 20)
    assert config.cloud == "aws"

    with pytest.raises(ValueError, match="azure is not an officially supported"):
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


def test_rejected_raw_output_is_classified_and_never_targets_repository_root() -> None:
    assert classified_raw_artifact_path(
        "benchmark-results/modal-v2/candidates/pilot.json", status="rejected"
    ) == "benchmark-results/modal-v2/rejected/pilot.json"
    with pytest.raises(ValueError, match="under benchmark-results"):
        classified_raw_artifact_path("pilot.json", status="candidate")


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
            "attempts": [
                {
                    "status": "valid",
                    "cleanup_succeeded": True,
                    "actual_cloud": "aws",
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
    assert promoted["provenance"]["raw_artifact_tracked"] is False
    assert len(promoted["provenance"]["raw_artifact_sha256"]) == 64

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


def test_runner_result_parser_requires_bounded_safe_json() -> None:
    stdout = (
        f"noise\n{CANDIDATE_RESULT_START}\n"
        '{"status":"valid","warm_action_to_frame_ms":12.5}\n'
        f"{CANDIDATE_RESULT_END}\n"
    )

    assert extract_candidate_runner_result(stdout)["warm_action_to_frame_ms"] == 12.5
    with pytest.raises(ValueError, match="bounded result"):
        extract_candidate_runner_result('{"status":"valid"}')


def _preregistration() -> dict:
    return build_preregistration(
        ModalV2CandidateConfig(
            image_revision="a" * 40,
            bootstrap_resamples=100,
        ),
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        sdk_versions={"modal": "1.5.2"},
        package_versions={"modal-computer-use": "1.1.0"},
        runner_identity={"kind": "test"},
        commands={
            "preregister": "preregister command",
            "pilot": "pilot command",
            "full": "full command",
            "sanitize": "sanitize command",
            "check": "check command",
        },
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
                        "target_cloud": "aws",
                        "target_region": "us-west-2",
                        "runner_cloud": "aws",
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
                    },
                }
            )
            sequence += 1
    return trials
