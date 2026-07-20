from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import statistics
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from .artifacts import _validate_safe_value
from .measurement import _percentile
from .modal_v2_placement import (
    placement_capability_sha256,
    validate_placement_capability_matrix,
)

ARM_V1_CONNECT = "v1-connect-product"
ARM_V1_TUNNEL = "v1-encrypted-tunnel"
ARM_V2_TUNNEL = "v2-encrypted-tunnel"
ARM_V2_I6PN = "v2-i6pn-direct-optimized"
CANONICAL_ARMS = (
    ARM_V1_CONNECT,
    ARM_V1_TUNNEL,
    ARM_V2_TUNNEL,
    ARM_V2_I6PN,
)
BACKEND_COMPARISON = "v1-v2-encrypted-tunnel-backend-generation"
PRODUCT_PATH_CLASSIFICATION = "public-product-path"
ASYMMETRIC_CLASSIFICATION = "asymmetric-optimized-candidate"
TRIAL_METRICS = (
    "allocation_ms",
    "daemon_ready_ms",
    "browser_ready_ms",
    "first_valid_frame_ms",
    "warm_action_to_frame_ms",
)
OFFICIAL_SOURCE_URLS = (
    "https://modal.com/docs/guide/sandbox-v2",
    "https://modal.com/docs/guide/private-networking",
    "https://modal.com/docs/guide/sandbox-networking",
    "https://modal.com/docs/guide/region-selection",
    "https://modal.com/docs/guide/environment_variables",
    "https://modal.com/docs/guide/sandbox-resources",
)
_COMMIT_LENGTH = 40
_DIGEST_LENGTH = 64
_ALLOWED_PHASES = {"pilot", "full", "throughput"}
_ALLOWED_STATUSES = {"valid", "failed", "timeout", "skipped"}
_FORBIDDEN_KEYS = {
    "authorization",
    "base_url",
    "bearer",
    "clipboard",
    "endpoint",
    "frame_bytes",
    "private_ip",
    "screenshot",
    "stderr",
    "stdout",
    "token",
    "typed_text",
}


@dataclass(frozen=True, slots=True)
class ModalV2CandidateConfig:
    image_revision: str
    cloud: str | None = None
    region: str = "us-west"
    expected_actual_cloud: str | None = None
    expected_actual_region: str | None = None
    cpu: float = 4.0
    memory_mib: int = 8192
    browser: str = "chromium"
    width: int = 1024
    height: int = 768
    browser_prewarm: bool = True
    pilot_samples_per_arm: int = 5
    full_samples_per_arm: int = 30
    order_seed: int = 20260719
    bootstrap_seed: int = 20260720
    bootstrap_resamples: int = 2_000
    throughput_concurrency: tuple[int, ...] = (1, 5, 20)
    enable_concurrency_50: bool = False
    max_estimated_cost_usd: float = 20.0
    sandbox_timeout_seconds: int = 900
    readiness_timeout_seconds: int = 180

    def __post_init__(self) -> None:
        if not _is_hex(self.image_revision, _COMMIT_LENGTH):
            raise ValueError("image_revision must be a full Git SHA")
        if not isinstance(self.region, str) or not self.region.strip():
            raise ValueError("region must be explicit")
        if self.cloud is not None and (
            not isinstance(self.cloud, str) or not self.cloud.strip()
        ):
            raise ValueError("cloud must be non-empty when provided")
        if self.cloud == "azure":
            raise ValueError("azure is unsupported by the required Modal V1 benchmark arms")
        if (self.expected_actual_cloud is None) != (self.expected_actual_region is None):
            raise ValueError("expected actual cloud and region must be provided together")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.expected_actual_cloud, self.expected_actual_region)
            if value is not None
        ):
            raise ValueError("expected actual placement values must be non-empty")
        if not _positive_number(self.cpu):
            raise ValueError("cpu must be positive")
        positive_integers = (
            self.memory_mib,
            self.width,
            self.height,
            self.pilot_samples_per_arm,
            self.full_samples_per_arm,
            self.bootstrap_resamples,
            self.sandbox_timeout_seconds,
            self.readiness_timeout_seconds,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in positive_integers
        ):
            raise ValueError(
                "sample, resource, dimension, and timeout values must be positive integers"
            )
        if self.pilot_samples_per_arm != 5:
            raise ValueError("the pilot requires exactly 5 samples per arm")
        if self.full_samples_per_arm != 30:
            raise ValueError("the full phase requires exactly 30 samples per eligible arm")
        if self.throughput_concurrency != (1, 5, 20):
            raise ValueError("throughput concurrency must be exactly 1, 5, and 20")
        if not _positive_number(self.max_estimated_cost_usd):
            raise ValueError("max_estimated_cost_usd must be positive")


def arm_definitions() -> dict[str, dict[str, Any]]:
    return {
        ARM_V1_CONNECT: {
            "backend": "v1",
            "classification": PRODUCT_PATH_CLASSIFICATION,
            "caller_path": "same-region-separate-v2-runner",
            "ingress": "connect-endpoint",
            "action_transport": "persistent-hot-session-over-connect",
            "observation_transport": "binary-envelope-over-connect",
            "workspace_private": False,
            "neutral_backend_comparison_arm": False,
            "optimizations": [],
        },
        ARM_V1_TUNNEL: {
            "backend": "v1",
            "classification": "transport-matched-backend-arm",
            "caller_path": "same-region-separate-v2-runner",
            "ingress": "encrypted-tunnel",
            "action_transport": "persistent-hot-session-over-encrypted-tunnel",
            "observation_transport": "binary-envelope-over-encrypted-tunnel",
            "workspace_private": False,
            "neutral_backend_comparison_arm": True,
            "optimizations": [],
        },
        ARM_V2_TUNNEL: {
            "backend": "v2",
            "classification": "transport-matched-backend-arm",
            "caller_path": "same-region-separate-v2-runner",
            "ingress": "encrypted-tunnel",
            "action_transport": "persistent-hot-session-over-encrypted-tunnel",
            "observation_transport": "binary-envelope-over-encrypted-tunnel",
            "workspace_private": False,
            "neutral_backend_comparison_arm": True,
            "optimizations": ["modal-v2-sandbox-backend"],
        },
        ARM_V2_I6PN: {
            "backend": "v2",
            "classification": ASYMMETRIC_CLASSIFICATION,
            "caller_path": "same-region-separate-v2-runner",
            "ingress": "workspace-private-i6pn",
            "action_transport": "persistent-hot-session-over-workspace-private-http",
            "observation_transport": "binary-envelope-over-workspace-private-http",
            "workspace_private": True,
            "neutral_backend_comparison_arm": False,
            "optimizations": [
                "modal-v2-sandbox-backend",
                "workspace-private-i6pn",
                "direct-runner-to-target-data-path",
                "application-bearer-authentication",
            ],
        },
    }


def build_trial_schedule(
    *,
    phase: Literal["pilot", "full"],
    samples_per_arm: int,
    seed: int,
) -> list[dict[str, Any]]:
    expected = 5 if phase == "pilot" else 30
    if samples_per_arm != expected:
        raise ValueError(f"{phase} schedule requires exactly {expected} samples per arm")
    rng = random.Random(seed)  # noqa: S311 - preregistered trial randomization.
    rows: list[dict[str, Any]] = []
    for lifecycle_index in range(samples_per_arm):
        block = list(CANONICAL_ARMS)
        rng.shuffle(block)
        rows.extend({"arm": arm, "lifecycle_index": lifecycle_index} for arm in block)
    return [{"sequence": sequence, "phase": phase, **row} for sequence, row in enumerate(rows)]


def build_preregistration(
    config: ModalV2CandidateConfig,
    *,
    source_sha: str,
    generated_at: str,
    sdk_versions: dict[str, str],
    package_versions: dict[str, str],
    runner_identity: dict[str, Any],
    placement_capability: dict[str, Any],
    commands: dict[str, str],
) -> dict[str, Any]:
    if not _is_hex(source_sha, _COMMIT_LENGTH):
        raise ValueError("source_sha must be a full Git SHA")
    _validate_placement_capability_binding(
        placement_capability,
        config=config,
        source_sha=source_sha,
    )
    required_commands = {
        "placement_probe",
        "preregister",
        "pilot",
        "full",
        "sanitize",
        "check",
    }
    if set(commands) != required_commands or any(not value.strip() for value in commands.values()):
        raise ValueError("commands must contain the six exact candidate benchmark commands")
    full_seed = config.order_seed + 1
    return {
        "schema_version": 1,
        "benchmark": "modal-v2-candidate-preregistration",
        "generated_at": generated_at,
        "source_sha": source_sha,
        "configuration": asdict(config),
        "arms": arm_definitions(),
        "pilot_schedule": build_trial_schedule(
            phase="pilot",
            samples_per_arm=config.pilot_samples_per_arm,
            seed=config.order_seed,
        ),
        "full_schedule": build_trial_schedule(
            phase="full",
            samples_per_arm=config.full_samples_per_arm,
            seed=full_seed,
        ),
        "measurement": {
            "readiness_boundary": (
                "request start through daemon readiness, browser prewarm verification, and first "
                "authenticated 1024x768 PNG frame"
            ),
            "allocation_boundary": (
                "immediately before Sandbox.create or Sandbox._experimental_create until the SDK "
                "returns the registered sandbox handle"
            ),
            "daemon_ready_boundary": "request start until authenticated /readyz succeeds",
            "browser_ready_boundary": (
                "request start until configured prewarmed browser is verified"
            ),
            "warm_action_boundary": (
                "immediately before one correlated click dispatch on a persistent observation "
                "session until a matching changed causal binary-envelope frame is reconstructed"
            ),
            "action": {"type": "click", "x": 512, "y": 512, "button": "left"},
            "observation": {
                "format": "png",
                "frame_encoding": "binary-envelope",
                "require_causal_frame": True,
                "require_changed_frame": True,
            },
            "retries": {"harness_retries": 0, "replacement_samples": False},
            "cleanup": {"target": "terminate-and-detach", "runner": "terminate"},
        },
        "decision_gates": {
            "pilot": [
                "exactly five retained lifecycle attempts per arm",
                (
                    "all route, browser, frame, action, causal, and binary-envelope "
                    "verification passes"
                ),
                "all target and runner cleanup succeeds",
                (
                    "requested image, CPU, memory, cloud, region, browser, dimensions, "
                    "and prewarm match"
                ),
                (
                    "actual target and runner cloud and region are observed and identical "
                    "across matched arms"
                ),
                "no retries, replacements, or hidden failures",
            ],
            "backend_causal_ratio": [
                "pilot gate passes for V1 and V2 encrypted-tunnel arms",
                "image, target resources, target placement, runner placement, readiness, action, "
                "observation, and encrypted-tunnel controls are exact matches",
            ],
            "full": (
                "advance an arm to 30 independent measured lifecycles only after its pilot and "
                "common comparability gates pass"
            ),
            "throughput": {
                "concurrency": list(config.throughput_concurrency),
                "concurrency_50_enabled": config.enable_concurrency_50,
                "requires_full_gate": True,
                "max_estimated_cost_usd": config.max_estimated_cost_usd,
            },
        },
        "classification_policy": {
            ARM_V1_CONNECT: PRODUCT_PATH_CLASSIFICATION,
            ARM_V1_TUNNEL: "transport-matched-backend-arm",
            ARM_V2_TUNNEL: "transport-matched-backend-arm",
            ARM_V2_I6PN: ASYMMETRIC_CLASSIFICATION,
            "target-loopback": "same-container-diagnostic-only",
            "v2_is_default": False,
            "winner_label_allowed": False,
        },
        "environment": {
            "runner_identity": copy.deepcopy(runner_identity),
            "placement_capability": copy.deepcopy(placement_capability),
            "sdk_versions": dict(sorted(sdk_versions.items())),
            "package_versions": dict(sorted(package_versions.items())),
            "image_identity": f"modal-computer-use-{config.browser}:{config.image_revision}",
            "requested_cloud": config.cloud,
            "requested_region": config.region,
            "clean_source_verified": True,
            "azure_support_decision": (
                "not selected: a live Modal 1.5.2 V2 probe accepted azure/us-west and reported "
                "CLOUD_PROVIDER_AZURE/westus3, but V1 rejected azure as an unknown cloud provider"
            ),
            "v2_aws_placement_probe": (
                "a prior V2 runner requested as aws/us-west reported "
                "CLOUD_PROVIDER_AZURE/westus3; live preflight must reject that mismatch"
            ),
            "placement_match_policy": (
                "cloud may be unconstrained during discovery; every actual target and runner cloud "
                "and concrete region must still be observed and identical before causal ratios"
            ),
        },
        "capabilities": {
            "modal_v2": "beta",
            "v2_connect_tokens": "unsupported",
            "v2_encrypted_tunnels": "supported",
            "v2_i6pn": "supported-region-scoped-workspace-private",
            "v2_memory_snapshots": "unsupported",
            "filesystem_snapshot_experiment": "not_enabled",
            "warm_capacity": "not_enabled",
        },
        "official_source_urls": list(OFFICIAL_SOURCE_URLS),
        "commands": dict(commands),
    }


def _validate_placement_capability_binding(
    binding: dict[str, Any],
    *,
    config: ModalV2CandidateConfig,
    source_sha: str,
) -> None:
    required = {
        "artifact_path",
        "sha256",
        "run_id",
        "source_sha",
        "image_identity",
        "requested_region",
        "target_resources",
        "runner_resources",
        "selected_request",
        "classification",
        "measurement_performed",
        "evidence",
    }
    if set(binding) != required:
        raise ValueError("placement capability binding fields are invalid")
    path = PurePosixPath(str(binding["artifact_path"]))
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "benchmark-results"
        or ".." in path.parts
    ):
        raise ValueError("placement capability artifact must be under benchmark-results")
    if not _is_hex(binding["sha256"], _DIGEST_LENGTH):
        raise ValueError("placement capability digest must be SHA-256")
    if not isinstance(binding["run_id"], str) or not binding["run_id"]:
        raise ValueError("placement capability run ID is invalid")
    if binding["source_sha"] != source_sha:
        raise ValueError("placement capability source SHA differs from preregistration")
    expected_image = f"modal-computer-use-{config.browser}:{config.image_revision}"
    if binding["image_identity"] != expected_image:
        raise ValueError("placement capability image differs from preregistration")
    if binding["requested_region"] != config.region:
        raise ValueError("placement capability region differs from preregistration")
    if binding["target_resources"] != {"cpu": config.cpu, "memory_mib": config.memory_mib}:
        raise ValueError("placement capability target resources differ from preregistration")
    if binding["runner_resources"] != {"cpu": 1.0, "memory_mib": 1024}:
        raise ValueError("placement capability runner resources are invalid")
    selected = binding["selected_request"]
    if not isinstance(selected, dict) or selected.get("cloud") != config.cloud:
        raise ValueError("placement capability cloud differs from preregistration")
    if selected.get("region") != config.region:
        raise ValueError("placement capability selected region differs from preregistration")
    actual = selected.get("actual_placement")
    if not isinstance(actual, dict) or not all(
        isinstance(actual.get(key), str) and actual[key].strip() for key in ("cloud", "region")
    ):
        raise ValueError("placement capability lacks an exact observed placement")
    if (
        config.expected_actual_cloud != actual["cloud"]
        or config.expected_actual_region != actual["region"]
    ):
        raise ValueError("expected actual placement differs from placement capability")
    if binding["classification"] != "exact-common-placement-available":
        raise ValueError("placement capability classification is not comparable")
    if binding["measurement_performed"] is not False:
        raise ValueError("placement capability must be unmeasured")
    evidence = binding["evidence"]
    if not isinstance(evidence, dict):
        raise ValueError("placement capability evidence is invalid")
    validate_placement_capability_matrix(evidence)
    if placement_capability_sha256(evidence) != binding["sha256"]:
        raise ValueError("placement capability evidence digest differs from its binding")
    evidence_fields = {
        "run_id": "run_id",
        "source_sha": "source_sha",
        "image_identity": "image_identity",
        "requested_region": "requested_region",
        "target_resources": "target_resources",
        "runner_resources": "runner_resources",
        "selected_request": "selected_request",
        "classification": "classification",
        "measurement_performed": "measurement_performed",
    }
    if any(binding[key] != evidence[value] for key, value in evidence_fields.items()):
        raise ValueError("placement capability evidence differs from its binding")


def preregistration_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(serialize_json(payload).encode()).hexdigest()


def summarize_trials(
    trials: list[dict[str, Any]],
    *,
    bootstrap_seed: int,
    bootstrap_resamples: int,
) -> dict[str, Any]:
    by_arm: dict[str, dict[str, Any]] = {}
    for arm_index, arm in enumerate(CANONICAL_ARMS):
        arm_trials = [trial for trial in trials if trial.get("arm") == arm]
        by_arm[arm] = {
            "attempted": len(arm_trials),
            "valid": sum(trial.get("status") == "valid" for trial in arm_trials),
            "failed": sum(trial.get("status") == "failed" for trial in arm_trials),
            "timeout": sum(trial.get("status") == "timeout" for trial in arm_trials),
            "skipped": sum(trial.get("status") == "skipped" for trial in arm_trials),
            "retry_count": sum(_nonnegative_int(trial.get("retry_count")) for trial in arm_trials),
            "metrics": {
                metric: summarize_distribution(
                    [
                        float(trial["metrics"][metric])
                        for trial in arm_trials
                        if trial.get("status") == "valid"
                        and isinstance(trial.get("metrics"), dict)
                        and _nonnegative_finite(trial["metrics"].get(metric))
                    ],
                    bootstrap_seed=bootstrap_seed + arm_index * 100 + metric_index,
                    bootstrap_resamples=bootstrap_resamples,
                )
                for metric_index, metric in enumerate(TRIAL_METRICS)
            },
        }
    return by_arm


def summarize_distribution(
    samples: list[float],
    *,
    bootstrap_seed: int,
    bootstrap_resamples: int,
) -> dict[str, Any]:
    if bootstrap_resamples < 1:
        raise ValueError("bootstrap_resamples must be positive")
    if any(not _nonnegative_finite(sample) for sample in samples):
        raise ValueError("distribution samples must be finite and nonnegative")
    ordered = sorted(float(sample) for sample in samples)
    if not ordered:
        return {
            "count": 0,
            "p50_ms": None,
            "p95_ms": None,
            "bootstrap_95_ci": {"p50_ms": None, "p95_ms": None},
            "raw_samples_ms": [],
            "ecdf": [],
        }
    rng = random.Random(bootstrap_seed)  # noqa: S311 - deterministic bootstrap sampling.
    bootstrap_p50: list[float] = []
    bootstrap_p95: list[float] = []
    for _ in range(bootstrap_resamples):
        resample = sorted(rng.choice(ordered) for _ in ordered)
        bootstrap_p50.append(statistics.median(resample))
        bootstrap_p95.append(_percentile(resample, 95))
    return {
        "count": len(ordered),
        "p50_ms": statistics.median(ordered),
        "p95_ms": _percentile(ordered, 95),
        "bootstrap_95_ci": {
            "p50_ms": [
                _percentile(sorted(bootstrap_p50), 2.5),
                _percentile(sorted(bootstrap_p50), 97.5),
            ],
            "p95_ms": [
                _percentile(sorted(bootstrap_p95), 2.5),
                _percentile(sorted(bootstrap_p95), 97.5),
            ],
        },
        "raw_samples_ms": ordered,
        "ecdf": [
            {"value_ms": value, "rank": index + 1, "probability": (index + 1) / len(ordered)}
            for index, value in enumerate(ordered)
        ],
    }


def evaluate_pilot_gates(
    trials: list[dict[str, Any]],
    *,
    preregistration: dict[str, Any],
) -> dict[str, Any]:
    configuration = _mapping(preregistration.get("configuration"), "configuration")
    expected_count = configuration.get("pilot_samples_per_arm")
    image_identity = _mapping(preregistration.get("environment"), "environment").get(
        "image_identity"
    )
    arm_results: dict[str, dict[str, Any]] = {}
    for arm in CANONICAL_ARMS:
        rows = [
            trial for trial in trials if trial.get("phase") == "pilot" and trial.get("arm") == arm
        ]
        reasons: list[str] = []
        if len(rows) != expected_count:
            reasons.append(f"expected {expected_count} pilot attempts; observed {len(rows)}")
        if any(trial.get("status") != "valid" for trial in rows):
            reasons.append("one or more pilot attempts were not valid")
        if any(trial.get("retry_count") != 0 for trial in rows):
            reasons.append("pilot attempts contained retries")
        if any(not _verification_passed(trial.get("verification")) for trial in rows):
            reasons.append("route, frame, action, causal, or binary-envelope verification failed")
        if any(not _cleanup_passed(trial.get("cleanup")) for trial in rows):
            reasons.append("target or runner cleanup was incomplete")
        if any(_trial_control_mismatches(trial, configuration, image_identity) for trial in rows):
            reasons.append("requested workload, image, resource, or placement controls differed")
        if any(not _actual_placement_observed(trial) for trial in rows):
            reasons.append("actual target or runner cloud/region was not observed")
        if any(not _actual_cloud_matches_requested(trial) for trial in rows):
            reasons.append("actual target or runner cloud did not match the requested provider")
        if any(not _actual_placement_matches_capability(trial) for trial in rows):
            reasons.append("actual placement differed from the capability-bound placement")
        if any(not _target_runner_colocated(trial) for trial in rows):
            reasons.append("target and runner were not colocated on one exact provider region")
        if len({_placement_signature(trial) for trial in rows}) != 1:
            reasons.append("actual target or runner placement varied within the arm")
        arm_results[arm] = {"eligible": not reasons, "reasons": reasons}

    all_rows = [trial for trial in trials if trial.get("phase") == "pilot"]
    if len({_placement_signature(trial) for trial in all_rows}) != 1:
        reason = "actual target and runner placement was not identical across all four arms"
        for result in arm_results.values():
            result["eligible"] = False
            if reason not in result["reasons"]:
                result["reasons"].append(reason)

    backend_reasons: list[str] = []
    for arm in (ARM_V1_TUNNEL, ARM_V2_TUNNEL):
        if not arm_results[arm]["eligible"]:
            backend_reasons.append(f"{arm} pilot gate failed")
    matched_rows = [
        trial
        for trial in trials
        if trial.get("phase") == "pilot" and trial.get("arm") in {ARM_V1_TUNNEL, ARM_V2_TUNNEL}
    ]
    placement_signatures = {_placement_signature(trial) for trial in matched_rows}
    if len(placement_signatures) != 1:
        backend_reasons.append("actual target/runner cloud and region were not identical")
    control_signatures = {_backend_control_signature(trial) for trial in matched_rows}
    if len(control_signatures) != 1:
        backend_reasons.append("backend-generation controls were not identical")
    return {
        "arms": arm_results,
        "comparisons": {
            BACKEND_COMPARISON: {
                "eligible": not backend_reasons,
                "reasons": backend_reasons,
                "ratio_metrics": (
                    ["allocation_ms", "daemon_ready_ms", "browser_ready_ms", "first_valid_frame_ms"]
                    if not backend_reasons
                    else []
                ),
            },
            ARM_V1_CONNECT: {
                "eligible": arm_results[ARM_V1_CONNECT]["eligible"],
                "classification": PRODUCT_PATH_CLASSIFICATION,
                "backend_causal_ratio_eligible": False,
            },
            ARM_V2_I6PN: {
                "eligible": arm_results[ARM_V2_I6PN]["eligible"],
                "classification": ASYMMETRIC_CLASSIFICATION,
                "backend_causal_ratio_eligible": False,
            },
        },
        "advance_to_full": [arm for arm in CANONICAL_ARMS if arm_results[arm]["eligible"]],
    }


def build_result_artifact(
    *,
    source_sha: str,
    generated_at: str,
    preregistration: dict[str, Any],
    trials: list[dict[str, Any]],
    throughput: list[dict[str, Any]],
    execution_status: Literal["candidate", "rejected", "complete"],
    status_reason: str,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if execution_status != "complete" and not status_reason.strip():
        raise ValueError("candidate and rejected results require an exact status reason")
    configuration = _mapping(preregistration.get("configuration"), "configuration")
    pilot_gates = evaluate_pilot_gates(trials, preregistration=preregistration)
    summaries = {
        phase: summarize_trials(
            [trial for trial in trials if trial.get("phase") == phase],
            bootstrap_seed=int(configuration["bootstrap_seed"]) + phase_index * 10_000,
            bootstrap_resamples=int(configuration["bootstrap_resamples"]),
        )
        for phase_index, phase in enumerate(("pilot", "full"))
    }
    comparison_phase = (
        "full"
        if _full_backend_comparison_eligible(trials, preregistration=preregistration)
        else "pilot"
    )
    comparisons = _build_comparisons(
        summaries[comparison_phase],
        pilot_gates=pilot_gates,
        phase=comparison_phase,
    )
    result = {
        "schema_version": 1,
        "benchmark": "modal-v2-candidate-results",
        "generated_at": generated_at,
        "status": execution_status,
        "status_reason": status_reason,
        "source_sha": source_sha,
        "preregistration_sha256": preregistration_sha256(preregistration),
        "configuration": copy.deepcopy(configuration),
        "arms": arm_definitions(),
        "trials": copy.deepcopy(trials),
        "summaries": summaries,
        "comparisons": comparisons,
        "throughput": copy.deepcopy(throughput),
        "cost": _cost_summary(trials),
        "execution": copy.deepcopy(execution or {}),
        "eligibility": pilot_gates,
        "claims": {
            "v2_is_default": False,
            "winner": None,
            "connect_parity": False,
            "backend_causal_ratios_emitted": pilot_gates["comparisons"][BACKEND_COMPARISON][
                "eligible"
            ],
            "i6pn_is_asymmetric": True,
            "target_loopback_is_product_arm": False,
        },
        "provenance": {
            "source_sha": source_sha,
            "image_identity": _mapping(preregistration.get("environment"), "environment").get(
                "image_identity"
            ),
            "sdk_versions": copy.deepcopy(
                _mapping(preregistration.get("environment"), "environment").get("sdk_versions")
            ),
            "official_source_urls": list(OFFICIAL_SOURCE_URLS),
            "placement_capability": copy.deepcopy(
                _mapping(preregistration.get("environment"), "environment").get(
                    "placement_capability"
                )
            ),
            "raw_artifact_tracked": False,
        },
    }
    validate_result_artifact(result, preregistration=preregistration)
    return result


def validate_result_artifact(
    payload: dict[str, Any],
    *,
    preregistration: dict[str, Any] | None = None,
) -> None:
    if (
        payload.get("schema_version") != 1
        or payload.get("benchmark") != "modal-v2-candidate-results"
    ):
        raise ValueError("candidate result schema or benchmark name is invalid")
    if set(_mapping(payload.get("arms"), "arms")) != set(CANONICAL_ARMS):
        raise ValueError("candidate results require the four canonical arms")
    _reject_forbidden_fields(payload)
    _validate_safe_value(payload)
    trials = _list_of_mappings(payload.get("trials"), "trials")
    _validate_trials(trials)
    claims = _mapping(payload.get("claims"), "claims")
    if claims.get("v2_is_default") is not False or claims.get("winner") is not None:
        raise ValueError("candidate results cannot label V2 as default or winner")
    if claims.get("connect_parity") is not False:
        raise ValueError("V2 Connect parity is unsupported")
    eligibility = _mapping(payload.get("eligibility"), "eligibility")
    comparison = _mapping(
        _mapping(eligibility.get("comparisons"), "comparisons").get(BACKEND_COMPARISON),
        BACKEND_COMPARISON,
    )
    if claims.get("backend_causal_ratios_emitted") is not comparison.get("eligible"):
        raise ValueError("backend causal claim differs from the eligibility gate")
    if not comparison.get("eligible") and comparison.get("ratio_metrics"):
        raise ValueError("ineligible backend comparisons cannot emit causal ratio metrics")
    emitted = _mapping(payload.get("comparisons"), "result comparisons").get(BACKEND_COMPARISON)
    if comparison.get("eligible"):
        if not isinstance(emitted, dict) or not emitted.get("ratios"):
            raise ValueError("eligible backend comparisons require explicit causal ratios")
    elif emitted is not None:
        raise ValueError("ineligible backend comparisons cannot emit a comparison object")
    if preregistration is not None:
        expected_digest = preregistration_sha256(preregistration)
        if payload.get("preregistration_sha256") != expected_digest:
            raise ValueError("result preregistration digest does not match")
        if payload.get("source_sha") != preregistration.get("source_sha"):
            raise ValueError("result source SHA differs from preregistration")
        expected_placement = _mapping(
            _mapping(preregistration.get("environment"), "environment").get(
                "placement_capability"
            ),
            "placement capability",
        )
        if _mapping(payload.get("provenance"), "provenance").get(
            "placement_capability"
        ) != expected_placement:
            raise ValueError("result placement capability differs from preregistration")


def validate_phase_checkpoint(
    payload: dict[str, Any],
    *,
    preregistration: dict[str, Any],
) -> None:
    if (
        payload.get("schema_version") != 1
        or payload.get("benchmark") != "modal-v2-candidate-checkpoint"
    ):
        raise ValueError("candidate checkpoint schema or benchmark name is invalid")
    _reject_forbidden_fields(payload)
    _validate_safe_value(payload)
    if payload.get("source_sha") != preregistration.get("source_sha"):
        raise ValueError("checkpoint source SHA differs from preregistration")
    if payload.get("preregistration_sha256") != preregistration_sha256(preregistration):
        raise ValueError("checkpoint preregistration digest does not match")
    phase = payload.get("phase")
    if phase not in {"pilot", "full"}:
        raise ValueError("checkpoint phase is invalid")
    if payload.get("state") not in {
        "starting",
        "preflight",
        "running",
        "complete",
        "interrupted",
        "failed",
    }:
        raise ValueError("checkpoint state is invalid")
    schedule_total = payload.get("schedule_total")
    completed = payload.get("completed_attempts")
    if (
        isinstance(schedule_total, bool)
        or not isinstance(schedule_total, int)
        or schedule_total < 1
        or isinstance(completed, bool)
        or not isinstance(completed, int)
        or completed < 0
        or completed > schedule_total
    ):
        raise ValueError("checkpoint attempt counts are invalid")
    trials = _list_of_mappings(payload.get("trials"), "checkpoint trials")
    if len(trials) != completed or any(trial.get("phase") != phase for trial in trials):
        raise ValueError("checkpoint trials do not match its phase or completed count")
    _validate_trials(trials)
    execution = _mapping(payload.get("execution"), "checkpoint execution")
    if execution.get("state") != payload.get("state"):
        raise ValueError("checkpoint state differs from execution state")


def serialize_json(payload: dict[str, Any]) -> str:
    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n"


def sanitize_result_artifact(
    raw_payload: dict[str, Any],
    *,
    raw_bytes: bytes,
    raw_artifact_path: str,
    preregistration: dict[str, Any],
    normalizer_sha: str,
) -> dict[str, Any]:
    validate_result_artifact(raw_payload, preregistration=preregistration)
    if raw_payload.get("status") != "complete":
        raise ValueError("only complete candidate evidence can be promoted")
    if not safe_relative_path(raw_artifact_path):
        raise ValueError("raw artifact path must be repository-relative")
    if PurePosixPath(raw_artifact_path).parts[0] != "benchmark-results":
        raise ValueError("raw artifact path must be under benchmark-results")
    if not _is_hex(normalizer_sha, _COMMIT_LENGTH):
        raise ValueError("normalizer_sha must be a full Git SHA")
    _validate_promotion_gates(raw_payload, preregistration=preregistration)
    promoted = copy.deepcopy(raw_payload)
    promoted["artifact_status"] = "current_reference"
    promoted["provenance"].update(
        {
            "raw_artifact_path": raw_artifact_path,
            "raw_artifact_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "normalizer_sha": normalizer_sha,
            "sanitizer": "modal_computer_use.benchmarks.modal_v2_candidate",
            "sanitizer_version": 1,
        }
    )
    validate_promoted_artifact(promoted, preregistration=preregistration)
    return promoted


def promotion_gate_failure_reason(
    payload: dict[str, Any],
    *,
    preregistration: dict[str, Any],
) -> str | None:
    """Return the exact first fail-closed promotion blocker, if any."""
    try:
        _validate_promotion_gates(payload, preregistration=preregistration)
    except ValueError as exc:
        return str(exc)
    return None


def lifecycle_gate_failure_reason(
    payload: dict[str, Any],
    *,
    preregistration: dict[str, Any],
) -> str | None:
    """Return the exact lifecycle blocker before throughput is attempted."""
    try:
        _validate_promotion_gates(
            payload,
            preregistration=preregistration,
            require_throughput=False,
        )
    except ValueError as exc:
        return str(exc)
    return None


def classified_raw_artifact_path(path: str, *, status: str) -> str:
    """Place rejected credentialed evidence in an explicit rejected directory."""
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or not candidate.parts or candidate.parts[0] != "benchmark-results":
        raise ValueError("benchmark output must be repository-relative under benchmark-results")
    parts = list(candidate.parts)
    if status == "rejected":
        if "candidates" in parts:
            parts[parts.index("candidates")] = "rejected"
        elif "rejected" not in parts:
            raise ValueError("rejected benchmark output must use a rejected directory")
    return PurePosixPath(*parts).as_posix()


def validate_promoted_artifact(
    payload: dict[str, Any],
    *,
    preregistration: dict[str, Any] | None = None,
) -> None:
    validate_result_artifact(payload, preregistration=preregistration)
    if payload.get("artifact_status") != "current_reference":
        raise ValueError("promoted candidate artifact must be a current reference")
    provenance = _mapping(payload.get("provenance"), "provenance")
    for key, length in (
        ("source_sha", _COMMIT_LENGTH),
        ("raw_artifact_sha256", _DIGEST_LENGTH),
        ("normalizer_sha", _COMMIT_LENGTH),
    ):
        if not _is_hex(provenance.get(key), length):
            raise ValueError(f"promoted provenance {key} is invalid")
    raw_path = provenance.get("raw_artifact_path")
    if (
        not isinstance(raw_path, str)
        or not safe_relative_path(raw_path)
        or PurePosixPath(raw_path).parts[0] != "benchmark-results"
    ):
        raise ValueError("promoted raw artifact path must be under benchmark-results")
    if provenance.get("raw_artifact_tracked") is not False:
        raise ValueError("raw candidate evidence must remain untracked")
    if preregistration is not None:
        _validate_promotion_gates(payload, preregistration=preregistration)


def _build_comparisons(
    summaries: dict[str, Any],
    *,
    pilot_gates: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    gate = pilot_gates["comparisons"][BACKEND_COMPARISON]
    if not gate["eligible"]:
        return {}
    ratios: dict[str, float] = {}
    for metric in gate["ratio_metrics"]:
        v1 = summaries[ARM_V1_TUNNEL]["metrics"][metric]["p50_ms"]
        v2 = summaries[ARM_V2_TUNNEL]["metrics"][metric]["p50_ms"]
        if _positive_number(v1) and _positive_number(v2):
            ratios[metric] = float(v1) / float(v2)
    return {
        BACKEND_COMPARISON: {
            "phase": phase,
            "classification": "backend-causal-transport-matched",
            "ratio_direction": "v1_p50_divided_by_v2_p50",
            "ratios": ratios,
        }
    }


def _full_backend_comparison_eligible(
    trials: list[dict[str, Any]],
    *,
    preregistration: dict[str, Any],
) -> bool:
    configuration = _mapping(preregistration.get("configuration"), "configuration")
    expected = configuration.get("full_samples_per_arm")
    image_identity = _mapping(preregistration.get("environment"), "environment").get(
        "image_identity"
    )
    rows = [
        trial
        for trial in trials
        if trial.get("phase") == "full"
        and trial.get("arm") in {ARM_V1_TUNNEL, ARM_V2_TUNNEL}
    ]
    if any(
        sum(trial.get("arm") == arm for trial in rows) != expected
        for arm in (ARM_V1_TUNNEL, ARM_V2_TUNNEL)
    ):
        return False
    if any(
        trial.get("status") != "valid"
        or trial.get("retry_count") != 0
        or not _verification_passed(trial.get("verification"))
        or not _cleanup_passed(trial.get("cleanup"))
        or bool(_trial_control_mismatches(trial, configuration, image_identity))
        or not _actual_placement_observed(trial)
        or not _actual_cloud_matches_requested(trial)
        or not _actual_placement_matches_capability(trial)
        or not _target_runner_colocated(trial)
        for trial in rows
    ):
        return False
    return (
        len({_placement_signature(trial) for trial in rows}) == 1
        and len({_backend_control_signature(trial) for trial in rows}) == 1
    )


def _validate_promotion_gates(
    payload: dict[str, Any],
    *,
    preregistration: dict[str, Any],
    require_throughput: bool = True,
) -> None:
    configuration = _mapping(preregistration.get("configuration"), "configuration")
    environment = _mapping(preregistration.get("environment"), "environment")
    if environment.get("clean_source_verified") is not True:
        raise ValueError("promotion requires a clean committed benchmark harness")
    image_identity = environment.get("image_identity")
    trials = _list_of_mappings(payload.get("trials"), "trials")
    eligibility = _mapping(payload.get("eligibility"), "eligibility")
    arm_gates = _mapping(eligibility.get("arms"), "arm gates")
    for arm in CANONICAL_ARMS:
        if _mapping(arm_gates.get(arm), arm).get("eligible") is not True:
            raise ValueError(f"promotion requires a passing pilot gate for {arm}")
        for phase, field in (
            ("pilot", "pilot_samples_per_arm"),
            ("full", "full_samples_per_arm"),
        ):
            rows = [
                trial for trial in trials if trial.get("arm") == arm and trial.get("phase") == phase
            ]
            expected = configuration.get(field)
            if len(rows) != expected or any(trial.get("status") != "valid" for trial in rows):
                raise ValueError(f"promotion requires {expected} valid {phase} trials for {arm}")
            if any(not _verification_passed(trial.get("verification")) for trial in rows):
                raise ValueError(f"promotion requires verified {phase} trials for {arm}")
            if any(not _cleanup_passed(trial.get("cleanup")) for trial in rows):
                raise ValueError(f"promotion requires clean {phase} trials for {arm}")
            if any(trial.get("retry_count") != 0 for trial in rows):
                raise ValueError(f"promotion requires retry-free {phase} trials for {arm}")
            if any(
                _trial_control_mismatches(trial, configuration, image_identity)
                for trial in rows
            ):
                raise ValueError(f"promotion requires matched {phase} controls for {arm}")
            if any(not _actual_placement_observed(trial) for trial in rows):
                raise ValueError(f"promotion requires observed {phase} placement for {arm}")
            if any(not _actual_cloud_matches_requested(trial) for trial in rows):
                raise ValueError(
                    f"promotion requires requested/actual {phase} cloud agreement for {arm}"
                )
            if any(not _actual_placement_matches_capability(trial) for trial in rows):
                raise ValueError(
                    f"promotion requires capability-bound {phase} placement for {arm}"
                )
    if len({_placement_signature(trial) for trial in trials}) != 1:
        raise ValueError("promotion requires identical target and runner placement across trials")
    if any(not _target_runner_colocated(trial) for trial in trials):
        raise ValueError("promotion requires exact target and runner colocation")
    execution = _mapping(payload.get("execution"), "execution")
    for phase in ("pilot", "full"):
        phase_cleanup = _mapping(
            _mapping(execution.get(phase), f"{phase} execution").get("run_cleanup"),
            f"{phase} run cleanup",
        )
        if (
            phase_cleanup.get("cleanup_succeeded") is not True
            or phase_cleanup.get("remaining_sandboxes") != 0
            or phase_cleanup.get("termination_failures") != 0
        ):
            raise ValueError(f"promotion requires a successful {phase} run-scoped cleanup sweep")
    if not require_throughput:
        return
    throughput = payload.get("throughput")
    if not isinstance(throughput, list):
        raise ValueError("promotion requires throughput evidence")
    required = {
        (backend, concurrency)
        for backend in ("v1", "v2")
        for concurrency in configuration.get("throughput_concurrency", [])
    }
    observed: set[tuple[Any, Any]] = set()
    throughput_placements: set[tuple[Any, Any]] = set()
    throughput_placement_drift = False
    for row in throughput:
        if not isinstance(row, dict):
            continue
        attempts = row.get("attempts")
        concurrency = row.get("concurrency")
        if isinstance(attempts, list):
            throughput_placement_drift = throughput_placement_drift or any(
                isinstance(attempt, dict)
                and (
                    attempt.get("actual_cloud"),
                    attempt.get("actual_region"),
                )
                != (
                    configuration.get("expected_actual_cloud"),
                    configuration.get("expected_actual_region"),
                )
                for attempt in attempts
            )
        valid_attempts = (
            isinstance(attempts, list)
            and isinstance(concurrency, int)
            and len(attempts) == concurrency
            and all(
                isinstance(attempt, dict)
                and attempt.get("status") == "valid"
                and attempt.get("cleanup_succeeded") is True
                and isinstance(attempt.get("actual_cloud"), str)
                and bool(attempt["actual_cloud"].strip())
                and isinstance(attempt.get("actual_region"), str)
                and bool(attempt["actual_region"].strip())
                and modal_cloud_matches(
                    row.get("requested_cloud"), attempt.get("actual_cloud")
                )
                and attempt.get("actual_cloud") == configuration.get("expected_actual_cloud")
                and attempt.get("actual_region") == configuration.get("expected_actual_region")
                for attempt in attempts
            )
        )
        if (
            row.get("status") == "valid"
            and row.get("cleanup_succeeded") is True
            and valid_attempts
        ):
            observed.add((row.get("backend"), concurrency))
            throughput_placements.update(
                (attempt["actual_cloud"], attempt["actual_region"])
                for attempt in attempts
                if isinstance(attempt, dict)
            )
    if throughput_placement_drift:
        raise ValueError("promotion requires capability-bound throughput placement")
    if not required.issubset(observed):
        raise ValueError("promotion requires valid cleanup-complete throughput at 1, 5, and 20")
    if len(throughput_placements) != 1:
        raise ValueError("promotion requires identical observed placement for throughput attempts")
    lifecycle_target_placements = {
        (
            _mapping(trial.get("actual"), "trial actual placement").get("target_cloud"),
            _mapping(trial.get("actual"), "trial actual placement").get("target_region"),
        )
        for trial in trials
    }
    if throughput_placements != lifecycle_target_placements:
        raise ValueError("promotion requires throughput and lifecycle target placement to match")
    throughput_cleanup = _mapping(
        execution.get("throughput_cleanup"),
        "throughput cleanup",
    )
    if (
        throughput_cleanup.get("cleanup_succeeded") is not True
        or throughput_cleanup.get("remaining_sandboxes") != 0
    ):
        raise ValueError("promotion requires a successful run-scoped throughput cleanup sweep")


def _cost_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    estimated_partial_usd = 0.0
    for trial in trials:
        cost = trial.get("estimated_billed_cost")
        if not isinstance(cost, dict):
            continue
        value = cost.get("estimated_usd")
        if _nonnegative_finite(value):
            estimated_partial_usd += float(value)
    return {
        "estimated_partial_usd": estimated_partial_usd,
        "estimate_scope": ["target_cpu", "target_memory"],
        "excluded_from_estimate": ["runner_compute", "control_plane", "billing_adjustments"],
        "modal_billed_cost": {
            "status": "not_reconciled",
            "amount_usd": None,
            "reason": "Modal billing data is not synchronously attributable at run completion",
        },
    }


def _trial_control_mismatches(
    trial: dict[str, Any],
    configuration: dict[str, Any],
    image_identity: Any,
) -> tuple[str, ...]:
    requested = trial.get("requested")
    if not isinstance(requested, dict):
        return ("requested",)
    expected = {
        "cloud": configuration.get("cloud"),
        "region": configuration.get("region"),
        "expected_actual_cloud": configuration.get("expected_actual_cloud"),
        "expected_actual_region": configuration.get("expected_actual_region"),
        "cpu": configuration.get("cpu"),
        "memory_mib": configuration.get("memory_mib"),
        "browser": configuration.get("browser"),
        "browser_prewarm": configuration.get("browser_prewarm"),
        "width": configuration.get("width"),
        "height": configuration.get("height"),
        "image_identity": image_identity,
        "action_semantics": "click-512-512-left",
        "observation_semantics": "changed-causal-png-binary-envelope",
        "cleanup_policy": "terminate-target-runner-and-detach-target",
    }
    return tuple(key for key, value in expected.items() if requested.get(key) != value)


def _verification_passed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    required = (
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
    return all(value.get(key) is True for key in required)


def _cleanup_passed(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("target_terminated") is True
        and value.get("target_detached") is True
        and value.get("runner_terminated") is True
        and value.get("run_sweep_succeeded") is True
    )


def _actual_placement_observed(trial: dict[str, Any]) -> bool:
    actual = trial.get("actual")
    if not isinstance(actual, dict):
        return False
    return all(
        isinstance(actual.get(key), str) and bool(actual[key].strip())
        for key in ("target_cloud", "target_region", "runner_cloud", "runner_region")
    )


def _actual_cloud_matches_requested(trial: dict[str, Any]) -> bool:
    requested = trial.get("requested")
    actual = trial.get("actual")
    if not isinstance(requested, dict) or not isinstance(actual, dict):
        return False
    return all(
        modal_cloud_matches(requested.get("cloud"), actual.get(key))
        for key in ("target_cloud", "runner_cloud")
    )


def _actual_placement_matches_capability(trial: dict[str, Any]) -> bool:
    requested = trial.get("requested")
    actual = trial.get("actual")
    if not isinstance(requested, dict) or not isinstance(actual, dict):
        return False
    expected = (
        requested.get("expected_actual_cloud"),
        requested.get("expected_actual_region"),
    )
    return expected[0] is not None and all(
        (actual.get(f"{owner}_cloud"), actual.get(f"{owner}_region")) == expected
        for owner in ("target", "runner")
    )


def modal_cloud_matches(requested: Any, actual: Any) -> bool:
    if requested is None or requested == "auto":
        return isinstance(actual, str) and bool(actual.strip())
    expected = {
        "azure": {"azure", "CLOUD_PROVIDER_AZURE"},
        "aws": {"aws", "CLOUD_PROVIDER_AWS"},
        "gcp": {"gcp", "CLOUD_PROVIDER_GCP"},
        "oci": {"oci", "CLOUD_PROVIDER_OCI"},
    }.get(requested)
    return expected is not None and actual in expected


def _target_runner_colocated(trial: dict[str, Any]) -> bool:
    actual = trial.get("actual")
    if not isinstance(actual, dict):
        return False
    return (
        actual.get("target_cloud"),
        actual.get("target_region"),
    ) == (
        actual.get("runner_cloud"),
        actual.get("runner_region"),
    )


def _placement_signature(trial: dict[str, Any]) -> tuple[Any, ...]:
    actual = trial.get("actual")
    if not isinstance(actual, dict):
        return (None, None, None, None)
    return (
        actual.get("target_cloud"),
        actual.get("target_region"),
        actual.get("runner_cloud"),
        actual.get("runner_region"),
    )


def _backend_control_signature(trial: dict[str, Any]) -> tuple[Any, ...]:
    requested = trial.get("requested")
    if not isinstance(requested, dict):
        return (None,)
    return tuple(
        requested.get(key)
        for key in (
            "cloud",
            "region",
            "cpu",
            "memory_mib",
            "image_identity",
            "browser",
            "browser_prewarm",
            "width",
            "height",
            "readiness_boundary",
            "action_semantics",
            "observation_semantics",
            "cleanup_policy",
        )
    )


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).lower().replace("-", "_")
            if key in _FORBIDDEN_KEYS or key.endswith(
                ("_token", "_secret", "_password", "_endpoint", "_endpoint_url")
            ):
                raise ValueError(f"candidate artifact contains forbidden field: {raw_key}")
            _reject_forbidden_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_fields(item)


def _validate_trials(trials: list[dict[str, Any]]) -> None:
    for trial in trials:
        if trial.get("arm") not in CANONICAL_ARMS:
            raise ValueError("trial arm label is invalid")
        if trial.get("phase") not in _ALLOWED_PHASES:
            raise ValueError("trial phase is invalid")
        if trial.get("status") not in _ALLOWED_STATUSES:
            raise ValueError("trial status is invalid")
        if trial.get("arm") == "target-loopback":
            raise ValueError("target-loopback cannot be a product comparison arm")


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _list_of_mappings(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{name} must be a list of objects")
    return value


def _is_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value > 0
    )


def _nonnegative_finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def _nonnegative_int(value: Any) -> int:
    return value if not isinstance(value, bool) and isinstance(value, int) and value >= 0 else 0


def safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts
