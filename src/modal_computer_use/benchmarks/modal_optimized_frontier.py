from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from .artifacts import _validate_safe_value
from .modal_v2_candidate import summarize_distribution
from .modal_v2_placement import validate_placement_capability_matrix

ARM_V1_CONNECT = "v1-connect-product"
ARM_V1_TUNNEL = "v1-encrypted-tunnel-optimized"
ARM_V2_TUNNEL = "v2-encrypted-tunnel-diagnostic"
ARM_V2_I6PN = "v2-i6pn-direct-optimized"
PRIMARY_ARMS = (ARM_V1_TUNNEL, ARM_V2_I6PN)
DIAGNOSTIC_ARMS = (ARM_V1_CONNECT, ARM_V2_TUNNEL)
PILOT_ARMS = (*PRIMARY_ARMS, *DIAGNOSTIC_ARMS)
FULL_ARMS = PRIMARY_ARMS
COMPARISON = "modal-v1-v2-optimized-frontier"
METRICS = (
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
    "https://modal.com/docs/guide/sandboxes",
    "https://modal.com/docs/guide/sandbox-resources",
)
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
class OptimizedFrontierConfig:
    image_revision: str
    region: str = "us-west"
    v1_cloud: str = "oci"
    v1_actual_cloud: str = "CLOUD_PROVIDER_OCI"
    v1_actual_region: str = "us-phoenix-1"
    v2_cloud: str | None = None
    v2_actual_cloud: str = "CLOUD_PROVIDER_AZURE"
    v2_actual_region: str = "westus3"
    cpu: float = 4.0
    memory_mib: int = 8192
    runner_cpu: float = 1.0
    runner_memory_mib: int = 1024
    width: int = 1024
    height: int = 768
    browser: str = "chromium"
    browser_prewarm: bool = True
    pilot_samples_per_arm: int = 5
    full_samples_per_primary_arm: int = 30
    order_seed: int = 20260721
    bootstrap_seed: int = 20260722
    bootstrap_resamples: int = 2_000
    throughput_concurrency: tuple[int, ...] = (1, 5, 20)
    max_estimated_cost_usd: float = 20.0
    sandbox_timeout_seconds: int = 900
    readiness_timeout_seconds: int = 180

    def __post_init__(self) -> None:
        if not _is_hex(self.image_revision, 40):
            raise ValueError("image_revision must be a full Git SHA")
        if self.v1_cloud not in {"aws", "gcp", "oci"}:
            raise ValueError("V1 optimized frontier requires an explicit supported cloud")
        if self.v2_cloud not in {None, "auto"}:
            raise ValueError("V2 optimized frontier must use the observed unconstrained path")
        strings = (
            self.region,
            self.v1_actual_cloud,
            self.v1_actual_region,
            self.v2_actual_cloud,
            self.v2_actual_region,
            self.browser,
        )
        if any(not isinstance(value, str) or not value.strip() for value in strings):
            raise ValueError("placement and browser strings must be explicit")
        if self.pilot_samples_per_arm != 5:
            raise ValueError("pilot requires exactly 5 samples per arm")
        if self.full_samples_per_primary_arm != 30:
            raise ValueError("full requires exactly 30 samples per primary arm")
        if self.throughput_concurrency != (1, 5, 20):
            raise ValueError("throughput concurrency must be exactly 1, 5, and 20")
        positive = (
            self.cpu,
            self.memory_mib,
            self.runner_cpu,
            self.runner_memory_mib,
            self.width,
            self.height,
            self.bootstrap_resamples,
            self.max_estimated_cost_usd,
            self.sandbox_timeout_seconds,
            self.readiness_timeout_seconds,
        )
        if any(not _positive_number(value) for value in positive):
            raise ValueError("resource, dimension, cost, and timeout values must be positive")

    def requested_cloud(self, arm: str) -> str | None:
        return self.v1_cloud if arm in {ARM_V1_CONNECT, ARM_V1_TUNNEL} else self.v2_cloud

    def expected_placement(self, arm: str) -> tuple[str, str]:
        if arm in {ARM_V1_CONNECT, ARM_V1_TUNNEL}:
            return self.v1_actual_cloud, self.v1_actual_region
        return self.v2_actual_cloud, self.v2_actual_region


def arm_definitions() -> dict[str, dict[str, Any]]:
    return {
        ARM_V1_TUNNEL: {
            "backend_generation": "v1",
            "runner_generation": "v1",
            "role": "primary",
            "classification": "optimized-frontier-primary",
            "ingress": "encrypted-tunnel",
            "action_transport": "persistent-hot-session-over-encrypted-tunnel",
            "observation_transport": "binary-envelope-over-encrypted-tunnel",
            "workspace_private": False,
        },
        ARM_V2_I6PN: {
            "backend_generation": "v2",
            "runner_generation": "v2",
            "role": "primary",
            "classification": "optimized-frontier-primary",
            "ingress": "workspace-private-i6pn",
            "action_transport": "persistent-hot-session-over-workspace-private-http",
            "observation_transport": "binary-envelope-over-workspace-private-http",
            "workspace_private": True,
        },
        ARM_V1_CONNECT: {
            "backend_generation": "v1",
            "runner_generation": "v1",
            "role": "diagnostic",
            "classification": "public-product-path-diagnostic",
            "ingress": "connect-endpoint",
            "action_transport": "persistent-hot-session-over-connect",
            "observation_transport": "binary-envelope-over-connect",
            "workspace_private": False,
        },
        ARM_V2_TUNNEL: {
            "backend_generation": "v2",
            "runner_generation": "v2",
            "role": "diagnostic",
            "classification": "encrypted-tunnel-diagnostic",
            "ingress": "encrypted-tunnel",
            "action_transport": "persistent-hot-session-over-encrypted-tunnel",
            "observation_transport": "binary-envelope-over-encrypted-tunnel",
            "workspace_private": False,
        },
    }


def build_trial_schedule(
    *, phase: Literal["pilot", "full"], samples_per_arm: int, seed: int
) -> list[dict[str, Any]]:
    arms = PILOT_ARMS if phase == "pilot" else FULL_ARMS
    expected = 5 if phase == "pilot" else 30
    if samples_per_arm != expected:
        raise ValueError(f"{phase} schedule requires exactly {expected} samples per arm")
    rng = random.Random(seed)  # noqa: S311 - preregistered schedule randomization.
    rows: list[dict[str, Any]] = []
    for lifecycle_index in range(samples_per_arm):
        block = list(arms)
        rng.shuffle(block)
        rows.extend({"arm": arm, "lifecycle_index": lifecycle_index} for arm in block)
    return [{"sequence": sequence, "phase": phase, **row} for sequence, row in enumerate(rows)]


def build_placement_binding(
    payload: dict[str, Any], *, artifact_path: str, artifact_sha256: str
) -> dict[str, Any]:
    validate_placement_capability_matrix(payload)
    path = PurePosixPath(artifact_path)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "benchmark-data"
        or ".." in path.parts
    ):
        raise ValueError("optimized-frontier capability must be a tracked benchmark-data artifact")
    if payload.get("classification") != "descriptive-placement-capability-only":
        raise ValueError("optimized frontier requires the descriptive placement foundation")
    if payload.get("measurement_performed") is not False:
        raise ValueError("placement foundation must remain unmeasured")
    if not _is_hex(artifact_sha256, 64):
        raise ValueError("placement artifact digest must be SHA-256")
    candidates = payload["candidates"]
    v1_candidate = next((item for item in candidates if item.get("requested_cloud") == "oci"), None)
    v2_candidate = next((item for item in candidates if item.get("requested_cloud") is None), None)
    if not isinstance(v1_candidate, dict) or not isinstance(v2_candidate, dict):
        raise ValueError("placement foundation lacks required OCI and unconstrained observations")
    v1 = _validated_observation(v1_candidate, "v1-target")
    v2_target = _validated_observation(v2_candidate, "v2-i6pn-target")
    v2_runner = _validated_observation(v2_candidate, "v2-i6pn-runner")
    if (v2_target["actual_cloud"], v2_target["actual_region"]) != (
        v2_runner["actual_cloud"],
        v2_runner["actual_region"],
    ):
        raise ValueError("placement foundation lacks V2 target/runner colocation")
    return {
        "artifact_path": path.as_posix(),
        "sha256": artifact_sha256,
        "classification": payload["classification"],
        "measurement_performed": False,
        "foundation_source_sha": payload["source_sha"],
        "foundation_image_identity": payload["image_identity"],
        "run_id": payload["run_id"],
        "v1": {
            "requested_cloud": "oci",
            "requested_region": payload["requested_region"],
            "actual_cloud": v1["actual_cloud"],
            "actual_region": v1["actual_region"],
            "role": "v1-target",
        },
        "v2": {
            "requested_cloud": None,
            "requested_region": payload["requested_region"],
            "actual_cloud": v2_target["actual_cloud"],
            "actual_region": v2_target["actual_region"],
            "roles": ["v2-i6pn-target", "v2-i6pn-runner"],
        },
        "observed_common_stratum": False,
        "backend_causal_comparison_available": False,
    }


def build_preregistration(
    config: OptimizedFrontierConfig,
    *,
    source_sha: str,
    generated_at: str,
    placement_binding: dict[str, Any],
    sdk_versions: dict[str, str],
    package_versions: dict[str, str],
    commands: dict[str, str],
) -> dict[str, Any]:
    if not _is_hex(source_sha, 40):
        raise ValueError("source_sha must be a full Git SHA")
    _validate_binding_matches_config(placement_binding, config)
    required_commands = {"preregister", "pilot", "full", "sanitize", "check"}
    if set(commands) != required_commands or any(not value.strip() for value in commands.values()):
        raise ValueError("commands must contain the five exact optimized-frontier commands")
    return {
        "schema_version": 1,
        "benchmark": "modal-optimized-frontier-preregistration",
        "generated_at": generated_at,
        "source_sha": source_sha,
        "configuration": asdict(config),
        "arms": arm_definitions(),
        "primary_arms_predeclared": list(PRIMARY_ARMS),
        "diagnostic_arms_predeclared": list(DIAGNOSTIC_ARMS),
        "pilot_schedule": build_trial_schedule(
            phase="pilot", samples_per_arm=5, seed=config.order_seed
        ),
        "full_schedule": build_trial_schedule(
            phase="full", samples_per_arm=30, seed=config.order_seed + 1
        ),
        "measurement": {
            "runner_lifecycle": "create-verify-measure-terminate-per-sample",
            "target_lifecycle": "create-verify-measure-terminate-detach-per-sample",
            "allocation_boundary": "target create call start until registered handle returns",
            "readiness_boundary": (
                "target request start through runner-direct authenticated daemon, browser, "
                "and frame verification"
            ),
            "warm_action_boundary": (
                "correlated click dispatch until matching changed causal binary-envelope frame"
            ),
            "action": {"type": "click", "x": 512, "y": 512, "button": "left"},
            "frame": {"format": "png", "width": 1024, "height": 768},
            "retry_policy": {"harness_retries": 0, "replacement_samples": False},
            "cleanup_policy": (
                "terminate target and runner; detach target; enumerate V1 and V2 lists"
            ),
        },
        "decision_gates": {
            "pilot": (
                "both primary arms require exactly five valid independent lifecycles with "
                "complete verification, expected placement, colocation, retry-free execution, "
                "and dual-list cleanup"
            ),
            "full": (
                "both primary pilot gates must pass before 30 independent lifecycles per "
                "primary arm"
            ),
            "throughput": {
                "requires_full_lifecycle_gate": True,
                "concurrency": [1, 5, 20],
                "max_estimated_cost_usd": config.max_estimated_cost_usd,
            },
            "diagnostics_do_not_block_headline": True,
        },
        "classification_policy": {
            "comparison": "descriptive-best-system",
            "ratio_name": "optimized-frontier-path-ratio",
            "ratio_direction": "v1_optimized_p50_divided_by_v2_optimized_p50",
            "backend_causal_speedup_allowed": False,
            "winner_label_allowed": False,
            "v2_connect_parity_allowed": False,
        },
        "unavoidable_asymmetries": [
            "backend generation",
            "cloud provider",
            "concrete region",
            "runner generation",
            "ingress",
            "transport",
        ],
        "capabilities": {
            "modal_v2": "beta",
            "v2_connect_tokens": "unsupported",
            "v2_encrypted_tunnels": "supported",
            "v2_i6pn": "supported-region-scoped-workspace-private",
            "v2_listing": "Sandbox._experimental_list",
            "v1_listing": "Sandbox.list",
        },
        "environment": {
            "image_identity": f"modal-computer-use-{config.browser}:{config.image_revision}",
            "placement_capability": copy.deepcopy(placement_binding),
            "sdk_versions": dict(sorted(sdk_versions.items())),
            "package_versions": dict(sorted(package_versions.items())),
            "clean_source_verified": True,
        },
        "official_source_urls": list(OFFICIAL_SOURCE_URLS),
        "commands": dict(commands),
    }


def preregistration_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(serialize_json(payload).encode()).hexdigest()


def evaluate_gates(
    trials: list[dict[str, Any]], *, preregistration: dict[str, Any]
) -> dict[str, Any]:
    config = _mapping(preregistration.get("configuration"), "configuration")
    arms: dict[str, Any] = {}
    for arm in PILOT_ARMS:
        rows = [
            trial for trial in trials if trial.get("phase") == "pilot" and trial.get("arm") == arm
        ]
        reasons = _trial_gate_reasons(rows, arm=arm, expected=5, config=config)
        arms[arm] = {
            "eligible": not reasons,
            "role": "primary" if arm in PRIMARY_ARMS else "diagnostic",
            "reasons": reasons,
        }
    primary_eligible = all(arms[arm]["eligible"] for arm in PRIMARY_ARMS)
    return {
        "arms": arms,
        "primary_pilot_eligible": primary_eligible,
        "advance_to_full": list(PRIMARY_ARMS) if primary_eligible else [],
        "comparison": {
            "name": COMPARISON,
            "eligible": primary_eligible,
            "classification": "descriptive-best-system",
            "backend_causal": False,
            "ratio_label": "optimized-frontier-path-ratio",
            "reasons": [] if primary_eligible else ["one or more primary pilot gates failed"],
        },
    }


def summarize_trials(
    trials: list[dict[str, Any]], *, bootstrap_seed: int, bootstrap_resamples: int
) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for index, arm in enumerate(PILOT_ARMS):
        rows = [trial for trial in trials if trial.get("arm") == arm]
        summaries[arm] = {
            "attempted": len(rows),
            "valid": sum(row.get("status") == "valid" for row in rows),
            "verification_rate": (
                sum(_verification_passed(row.get("verification")) for row in rows) / len(rows)
                if rows
                else None
            ),
            "cleanup_rate": (
                sum(_cleanup_passed(row.get("cleanup")) for row in rows) / len(rows)
                if rows
                else None
            ),
            "metrics": {
                metric: summarize_distribution(
                    [
                        float(row["metrics"][metric])
                        for row in rows
                        if row.get("status") == "valid"
                        and isinstance(row.get("metrics"), dict)
                        and _nonnegative_number(row["metrics"].get(metric))
                    ],
                    bootstrap_seed=bootstrap_seed + index * 100 + metric_index,
                    bootstrap_resamples=bootstrap_resamples,
                )
                for metric_index, metric in enumerate(METRICS)
            },
        }
    return summaries


def build_result_artifact(
    *,
    source_sha: str,
    generated_at: str,
    preregistration: dict[str, Any],
    trials: list[dict[str, Any]],
    throughput: list[dict[str, Any]],
    execution_status: Literal["candidate", "rejected", "complete"],
    status_reason: str,
    execution: dict[str, Any],
) -> dict[str, Any]:
    config = _mapping(preregistration.get("configuration"), "configuration")
    gates = evaluate_gates(trials, preregistration=preregistration)
    summaries = {
        phase: summarize_trials(
            [trial for trial in trials if trial.get("phase") == phase],
            bootstrap_seed=int(config["bootstrap_seed"]) + index * 10_000,
            bootstrap_resamples=int(config["bootstrap_resamples"]),
        )
        for index, phase in enumerate(("pilot", "full"))
    }
    phase = "full" if _full_lifecycle_eligible(trials, config=config) else "pilot"
    comparisons: dict[str, Any] = {}
    if gates["comparison"]["eligible"] and (
        phase == "pilot" or _full_lifecycle_eligible(trials, config=config)
    ):
        ratios: dict[str, float] = {}
        for metric in METRICS:
            v1 = summaries[phase][ARM_V1_TUNNEL]["metrics"][metric].get("p50_ms")
            v2 = summaries[phase][ARM_V2_I6PN]["metrics"][metric].get("p50_ms")
            if _positive_number(v1) and _positive_number(v2):
                ratios[metric] = float(v1) / float(v2)
        comparisons[COMPARISON] = {
            "phase": phase,
            "classification": "descriptive-best-system",
            "ratio_label": "optimized-frontier-path-ratio",
            "ratio_direction": "v1_optimized_p50_divided_by_v2_optimized_p50",
            "ratios": ratios,
            "backend_causal": False,
            "asymmetries": list(preregistration["unavoidable_asymmetries"]),
        }
    result = {
        "schema_version": 1,
        "benchmark": "modal-optimized-frontier-results",
        "generated_at": generated_at,
        "status": execution_status,
        "status_reason": status_reason,
        "source_sha": source_sha,
        "preregistration_sha256": preregistration_sha256(preregistration),
        "configuration": copy.deepcopy(config),
        "arms": arm_definitions(),
        "trials": copy.deepcopy(trials),
        "summaries": summaries,
        "comparisons": comparisons,
        "throughput": copy.deepcopy(throughput),
        "cost": _cost_summary(trials),
        "execution": copy.deepcopy(execution),
        "eligibility": gates,
        "claims": {
            "comparison": "descriptive-best-system",
            "backend_causal_speedup": False,
            "winner": None,
            "v2_connect_parity": False,
            "optimized_frontier_ratio_emitted": bool(comparisons),
        },
        "provenance": {
            "source_sha": source_sha,
            "image_identity": preregistration["environment"]["image_identity"],
            "placement_capability": copy.deepcopy(
                preregistration["environment"]["placement_capability"]
            ),
            "sdk_versions": copy.deepcopy(preregistration["environment"]["sdk_versions"]),
            "official_source_urls": list(OFFICIAL_SOURCE_URLS),
            "raw_artifact_tracked": False,
        },
    }
    validate_result_artifact(result, preregistration=preregistration)
    return result


def validate_result_artifact(
    payload: dict[str, Any], *, preregistration: dict[str, Any] | None = None
) -> None:
    if payload.get("schema_version") != 1 or payload.get("benchmark") != (
        "modal-optimized-frontier-results"
    ):
        raise ValueError("optimized-frontier result schema is invalid")
    _reject_forbidden_fields(payload)
    _validate_safe_value(payload)
    if set(_mapping(payload.get("arms"), "arms")) != set(PILOT_ARMS):
        raise ValueError("optimized-frontier result arms are invalid")
    claims = _mapping(payload.get("claims"), "claims")
    if claims.get("backend_causal_speedup") is not False or claims.get("winner") is not None:
        raise ValueError("optimized-frontier evidence cannot claim causality or a winner")
    if claims.get("v2_connect_parity") is not False:
        raise ValueError("V2 Connect parity is unsupported")
    comparisons = _mapping(payload.get("comparisons"), "comparisons")
    if any(
        isinstance(value, dict)
        and (
            value.get("classification") != "descriptive-best-system"
            or value.get("backend_causal") is not False
            or value.get("ratio_label") != "optimized-frontier-path-ratio"
        )
        for value in comparisons.values()
    ):
        raise ValueError("optimized-frontier comparison classification is invalid")
    trials = _list_of_mappings(payload.get("trials"), "trials")
    for trial in trials:
        if trial.get("arm") not in PILOT_ARMS or trial.get("phase") not in {"pilot", "full"}:
            raise ValueError("optimized-frontier trial label is invalid")
        if trial.get("phase") == "full" and trial.get("arm") not in PRIMARY_ARMS:
            raise ValueError("diagnostic arms cannot appear in the full phase")
        if trial.get("status") not in {"valid", "failed", "timeout"}:
            raise ValueError("optimized-frontier trial status is invalid")
    if preregistration is not None:
        if payload.get("source_sha") != preregistration.get("source_sha"):
            raise ValueError("result source SHA differs from preregistration")
        if payload.get("preregistration_sha256") != preregistration_sha256(preregistration):
            raise ValueError("result preregistration digest differs")
        if (
            payload["provenance"]["placement_capability"]
            != (preregistration["environment"]["placement_capability"])
        ):
            raise ValueError("result placement provenance differs from preregistration")


def lifecycle_gate_failure_reason(
    payload: dict[str, Any], *, preregistration: dict[str, Any]
) -> str | None:
    try:
        _validate_lifecycle_promotion(payload, preregistration=preregistration)
    except ValueError as exc:
        return str(exc)
    return None


def promotion_gate_failure_reason(
    payload: dict[str, Any], *, preregistration: dict[str, Any]
) -> str | None:
    try:
        _validate_lifecycle_promotion(payload, preregistration=preregistration)
        _validate_throughput(payload, preregistration=preregistration)
    except ValueError as exc:
        return str(exc)
    return None


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
        raise ValueError("only complete optimized-frontier evidence can be promoted")
    _validate_lifecycle_promotion(raw_payload, preregistration=preregistration)
    _validate_throughput(raw_payload, preregistration=preregistration)
    path = PurePosixPath(raw_artifact_path)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "benchmark-results"
        or ".." in path.parts
    ):
        raise ValueError("raw artifact path must be under benchmark-results")
    if not _is_hex(normalizer_sha, 40):
        raise ValueError("normalizer_sha must be a full Git SHA")
    promoted = copy.deepcopy(raw_payload)
    promoted["artifact_status"] = "current_reference"
    promoted["provenance"].update(
        {
            "raw_artifact_path": path.as_posix(),
            "raw_artifact_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "normalizer_sha": normalizer_sha,
            "sanitizer": "modal_computer_use.benchmarks.modal_optimized_frontier",
            "sanitizer_version": 1,
        }
    )
    validate_result_artifact(promoted, preregistration=preregistration)
    return promoted


def classified_raw_artifact_path(path: str, *, status: str) -> str:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or not candidate.parts or candidate.parts[0] != "benchmark-results":
        raise ValueError("benchmark output must be under benchmark-results")
    if ".." in candidate.parts:
        raise ValueError("benchmark output cannot traverse directories")
    parts = list(candidate.parts)
    if status == "rejected":
        if "candidates" not in parts:
            raise ValueError("rejected output must originate in candidates")
        parts[parts.index("candidates")] = "rejected"
    return PurePosixPath(*parts).as_posix()


def serialize_json(payload: dict[str, Any]) -> str:
    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n"


def _validate_lifecycle_promotion(
    payload: dict[str, Any], *, preregistration: dict[str, Any]
) -> None:
    validate_result_artifact(payload, preregistration=preregistration)
    config = preregistration["configuration"]
    trials = payload["trials"]
    for arm in PRIMARY_ARMS:
        for phase, expected in (("pilot", 5), ("full", 30)):
            rows = [row for row in trials if row.get("arm") == arm and row.get("phase") == phase]
            reasons = _trial_gate_reasons(rows, arm=arm, expected=expected, config=config)
            if reasons:
                raise ValueError(f"{arm} {phase} lifecycle gate failed: {reasons[0]}")
    for phase in ("pilot", "full"):
        cleanup = _mapping(_mapping(payload.get("execution"), "execution").get(phase), phase).get(
            "run_cleanup"
        )
        if not _run_cleanup_passed(cleanup):
            raise ValueError(f"{phase} requires zero survivors from both V1 and V2 listings")


def _validate_throughput(payload: dict[str, Any], *, preregistration: dict[str, Any]) -> None:
    rows = _list_of_mappings(payload.get("throughput"), "throughput")
    required = {(arm, count) for arm in PRIMARY_ARMS for count in (1, 5, 20)}
    observed: set[tuple[str, int]] = set()
    config = preregistration["configuration"]
    for row in rows:
        arm = row.get("arm")
        concurrency = row.get("concurrency")
        attempts = row.get("attempts")
        if arm not in PRIMARY_ARMS or concurrency not in (1, 5, 20):
            continue
        expected = _expected_placement(config, str(arm))
        if (
            row.get("status") == "valid"
            and row.get("cleanup_succeeded") is True
            and isinstance(attempts, list)
            and len(attempts) == concurrency
            and all(
                isinstance(attempt, dict)
                and attempt.get("status") == "valid"
                and attempt.get("cleanup_succeeded") is True
                and (attempt.get("actual_cloud"), attempt.get("actual_region")) == expected
                for attempt in attempts
            )
        ):
            observed.add((str(arm), int(concurrency)))
    if observed != required:
        raise ValueError(
            "throughput requires valid cleanup-complete 1, 5, and 20 batches per primary arm"
        )
    cleanup = payload["execution"].get("throughput_cleanup")
    if not _run_cleanup_passed(cleanup):
        raise ValueError("throughput requires zero survivors from both V1 and V2 listings")


def _trial_gate_reasons(
    rows: list[dict[str, Any]], *, arm: str, expected: int, config: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    if len(rows) != expected:
        reasons.append(f"expected {expected} attempts, observed {len(rows)}")
    expected_placement = _expected_placement(config, arm)
    for row in rows:
        if row.get("status") != "valid":
            reasons.append("attempt was not valid")
        if row.get("retry_count") != 0:
            reasons.append("retry policy was violated")
        if not _verification_passed(row.get("verification")):
            reasons.append("verification was incomplete")
        if not _cleanup_passed(row.get("cleanup")):
            reasons.append("lifecycle cleanup was incomplete")
        actual = row.get("actual")
        if (
            not isinstance(actual, dict)
            or (
                actual.get("target_cloud"),
                actual.get("target_region"),
            )
            != expected_placement
        ):
            reasons.append("target placement differed from the predeclared frontier")
        if (
            not isinstance(actual, dict)
            or (
                actual.get("runner_cloud"),
                actual.get("runner_region"),
            )
            != expected_placement
        ):
            reasons.append("runner placement differed from the predeclared frontier")
        requested = row.get("requested")
        if not isinstance(requested, dict) or requested != _requested_controls(config, arm):
            reasons.append("requested controls differed from preregistration")
    return list(dict.fromkeys(reasons))


def requested_controls(config: OptimizedFrontierConfig, arm: str) -> dict[str, Any]:
    return _requested_controls(asdict(config), arm)


def _requested_controls(config: dict[str, Any], arm: str) -> dict[str, Any]:
    definition = arm_definitions()[arm]
    v1 = arm in {ARM_V1_CONNECT, ARM_V1_TUNNEL}
    return {
        "backend_generation": definition["backend_generation"],
        "runner_generation": definition["runner_generation"],
        "ingress": definition["ingress"],
        "action_transport": definition["action_transport"],
        "observation_transport": definition["observation_transport"],
        "cloud": config["v1_cloud"] if v1 else config["v2_cloud"],
        "region": config["region"],
        "expected_actual_cloud": config["v1_actual_cloud"] if v1 else config["v2_actual_cloud"],
        "expected_actual_region": config["v1_actual_region"] if v1 else config["v2_actual_region"],
        "cpu": config["cpu"],
        "memory_mib": config["memory_mib"],
        "runner_cpu": config["runner_cpu"],
        "runner_memory_mib": config["runner_memory_mib"],
        "image_identity": f"modal-computer-use-{config['browser']}:{config['image_revision']}",
        "browser": config["browser"],
        "browser_prewarm": config["browser_prewarm"],
        "width": config["width"],
        "height": config["height"],
        "readiness_semantics": "runner-direct-authenticated-daemon-browser-frame",
        "action_semantics": "click-512-512-left",
        "observation_semantics": "changed-causal-png-binary-envelope",
        "runner_lifecycle": "independent-per-sample",
        "target_lifecycle": "independent-per-sample",
        "retry_policy": "zero-retries-no-replacements",
        "cleanup_policy": "terminate-target-and-runner-detach-target-dual-list-sweep",
        "snapshot_policy": "none",
        "pool_policy": "none",
    }


def _full_lifecycle_eligible(trials: list[dict[str, Any]], *, config: dict[str, Any]) -> bool:
    return all(
        not _trial_gate_reasons(
            [row for row in trials if row.get("phase") == "full" and row.get("arm") == arm],
            arm=arm,
            expected=30,
            config=config,
        )
        for arm in PRIMARY_ARMS
    )


def _cost_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    estimated = sum(
        float(row["estimated_billed_cost"]["estimated_usd"])
        for row in trials
        if isinstance(row.get("estimated_billed_cost"), dict)
        and _nonnegative_number(row["estimated_billed_cost"].get("estimated_usd"))
    )
    return {
        "strongest_available_proxy": (
            "requested CPU and memory resource-seconds for target and runner with "
            "narrow-region multiplier"
        ),
        "estimated_usd": estimated,
        "included": [
            "target_cpu",
            "target_memory",
            "runner_cpu",
            "runner_memory",
            "narrow_region_multiplier",
        ],
        "excluded": ["actual usage above request", "control plane", "billing adjustments"],
        "modal_billed_cost": {"status": "not_reconciled", "amount_usd": None},
    }


def _validated_observation(candidate: dict[str, Any], role: str) -> dict[str, Any]:
    observation = _mapping(_mapping(candidate.get("observations"), "observations").get(role), role)
    if (
        observation.get("status") != "valid"
        or observation.get("cleanup_succeeded") is not True
        or not isinstance(observation.get("actual_cloud"), str)
        or not isinstance(observation.get("actual_region"), str)
    ):
        raise ValueError(f"placement foundation role {role} is not valid and clean")
    return observation


def _validate_binding_matches_config(
    binding: dict[str, Any], config: OptimizedFrontierConfig
) -> None:
    if binding.get("classification") != "descriptive-placement-capability-only":
        raise ValueError("placement binding classification is invalid")
    if binding.get("measurement_performed") is not False:
        raise ValueError("placement binding must remain unmeasured")
    v1 = _mapping(binding.get("v1"), "v1 placement")
    v2 = _mapping(binding.get("v2"), "v2 placement")
    if (
        v1.get("requested_cloud"),
        v1.get("requested_region"),
        v1.get("actual_cloud"),
        v1.get("actual_region"),
    ) != (config.v1_cloud, config.region, config.v1_actual_cloud, config.v1_actual_region):
        raise ValueError("V1 frontier differs from the placement foundation")
    if (
        v2.get("requested_cloud"),
        v2.get("requested_region"),
        v2.get("actual_cloud"),
        v2.get("actual_region"),
    ) != (config.v2_cloud, config.region, config.v2_actual_cloud, config.v2_actual_region):
        raise ValueError("V2 frontier differs from the placement foundation")


def _expected_placement(config: dict[str, Any], arm: str) -> tuple[Any, Any]:
    if arm in {ARM_V1_CONNECT, ARM_V1_TUNNEL}:
        return config.get("v1_actual_cloud"), config.get("v1_actual_region")
    return config.get("v2_actual_cloud"), config.get("v2_actual_region")


def _verification_passed(value: Any) -> bool:
    required = (
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
    return isinstance(value, dict) and all(value.get(key) is True for key in required)


def _cleanup_passed(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("target_terminated") is True
        and value.get("target_detached") is True
        and value.get("runner_terminated") is True
        and value.get("run_sweep_succeeded") is True
    )


def _run_cleanup_passed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    enumeration = value.get("enumeration")
    return (
        value.get("cleanup_succeeded") is True
        and value.get("remaining_sandboxes") == 0
        and value.get("termination_failures") == 0
        and isinstance(enumeration, dict)
        and enumeration.get("apis") == ["Sandbox.list", "Sandbox._experimental_list"]
        and isinstance(enumeration.get("before"), dict)
        and isinstance(enumeration.get("after"), dict)
        and set(enumeration["before"]) == {"list", "_experimental_list"}
        and set(enumeration["after"]) == {"list", "_experimental_list"}
        and all(
            not isinstance(count, bool) and isinstance(count, int) and count >= 0
            for inventory in (enumeration["before"], enumeration["after"])
            for count in inventory.values()
        )
    )


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).lower().replace("-", "_")
            if key in _FORBIDDEN_KEYS or key.endswith(
                ("_token", "_secret", "_password", "_endpoint", "_endpoint_url")
            ):
                raise ValueError(f"optimized-frontier artifact contains forbidden field: {raw_key}")
            _reject_forbidden_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_fields(item)


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


def _nonnegative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )
