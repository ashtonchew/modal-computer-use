from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .artifacts import _validate_safe_value
from .measurement import _percentile

PROFILE_PROVIDER_DEFAULT = "provider-default"
PROFILE_MODAL_ON_DEMAND = "modal-platform-optimized-on-demand"
PROFILE_MODAL_WARM_AVAILABILITY = "modal-warm-availability"
PROFILE_MODAL_V2 = "modal-v2-ab"
MINIMUM_P95_SAMPLES = 20
OPTIMIZED_ACTION_CASE = (
    "observation_action_click_act_and_observe_auto_signal_binary_envelope_production"
)
_FORBIDDEN_ARTIFACT_FIELDS = {
    "authorization",
    "base_url",
    "clipboard",
    "clipboard_text",
    "frame_bytes",
    "screenshot",
    "screenshot_bytes",
    "stderr",
    "stdout",
    "token",
    "typed_text",
}
_PROVIDER_NAMES = {
    "modal-daemon": "modal",
    "daytona": "daytona",
    "e2b": "e2b",
}
_REGION_MULTIPLIERS = {
    "us-west": 1.75,
    "us-east": 1.75,
    "eu-west": 1.75,
    "ap-southeast": 1.75,
}
_MODAL_CPU_USD_PER_CORE_SECOND = 0.00003942
_MODAL_MEMORY_USD_PER_GIB_SECOND = 0.00000667


@dataclass(frozen=True, slots=True)
class ModalOptimizationConfig:
    region: str
    image_revision: str
    cold_attempts: int = 30
    warm_action_attempts: int = 30
    warm_claim_attempts: int = 30
    warm_pool_target: int = 3
    warm_idle_seconds: float = 30.0
    browser: str = "chromium"
    ingress: str = "attested-tunnel"
    cpu: float = 4.0
    memory_mib: int = 8192
    sandbox_timeout_seconds: int = 900
    readiness_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not _nonempty_text(self.region):
            raise ValueError("region must be explicit")
        if not _is_hex(self.image_revision, 40):
            raise ValueError("image_revision must be a full Git SHA")
        counts = (
            self.cold_attempts,
            self.warm_action_attempts,
            self.warm_claim_attempts,
            self.warm_pool_target,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in counts
        ):
            raise ValueError("attempt and pool counts must be positive")
        if self.warm_idle_seconds < 0:
            raise ValueError("warm_idle_seconds must be nonnegative")


def build_preregistration(
    config: ModalOptimizationConfig,
    *,
    source_sha: str,
    dependency_sha: str,
    generated_at: str,
    runner_identity: dict[str, Any],
    sdk_versions: dict[str, str],
    commands: dict[str, str],
) -> dict[str, Any]:
    if not _is_hex(source_sha, 40) or not _is_hex(dependency_sha, 40):
        raise ValueError("source and dependency SHAs must be full Git SHAs")
    required_commands = {
        "provider_default",
        "provider_default_normalize",
        "region_selection",
        "region_selection_attest",
        "publish_image",
        "benchmark",
        "normalize",
    }
    if set(commands) != required_commands or any(
        not _nonempty_text(command) for command in commands.values()
    ):
        raise ValueError("commands must contain the seven exact benchmark commands")
    return {
        "schema_version": 1,
        "benchmark": "modal-optimization-preregistration",
        "generated_at": generated_at,
        "source_sha": source_sha,
        "dependency": {
            "pull_request": 114,
            "head_sha": dependency_sha,
            "state": "open_unmerged",
        },
        "configuration": asdict(config),
        "environment": {
            "runner": copy.deepcopy(runner_identity),
            "sdk_versions": dict(sorted(sdk_versions.items())),
            "image_identity": f"modal-computer-use-{config.browser}:{config.image_revision}",
            "requested_region": config.region,
            "region_policy": (
                "Select the fastest valid explicit narrow region from the preregistered "
                "us-west and us-east probes by transport p50. Treat a difference within "
                "5 percent as a tie, then select the lower cold create-to-ready p50."
            ),
            "ingress": config.ingress,
        },
        "sample_policy": {
            "independent_cold_attempts": config.cold_attempts,
            "warm_action_attempts": config.warm_action_attempts,
            "warm_claim_attempts": config.warm_claim_attempts,
            "warm_pool_target": config.warm_pool_target,
            "warm_idle_seconds": config.warm_idle_seconds,
        },
        "metric_boundaries": {
            "cold_request_to_first_valid_authenticated_frame": (
                "Immediately before ComputerSandbox.create until a protected screenshot "
                "decodes to a supported format with positive dimensions."
            ),
            "warm_action_to_causal_frame": (
                "Immediately before the correlated action dispatch until the first valid "
                "observation with matching identifiers, causal=true, action success, a "
                "changed frame, and a reconstructable image."
            ),
            "warm_claim_request_to_authenticated": (
                "Immediately before the nonblocking queue claim until the claimed or cold "
                "fallback session passes protected browser readiness."
            ),
            "warm_claim_request_to_first_valid_frame": (
                "Immediately before the nonblocking queue claim until the claimed or cold "
                "fallback session returns a valid protected screenshot."
            ),
            "scheduled": "not observable; report N/A",
            "daemon_started": "not observable; report N/A",
        },
        "timeout_policy": {
            "sandbox_seconds": config.sandbox_timeout_seconds,
            "readiness_seconds": config.readiness_timeout_seconds,
            "timed_out_attempt_status": "timeout",
        },
        "retry_policy": {
            "harness_retries": 0,
            "replacement_samples": False,
            "provider_sdk_internal_retries": "not_observable",
        },
        "failure_policy": {
            "drop_failed_attempts": False,
            "drop_timed_out_attempts": False,
            "inspect_every_failure": True,
            "cleanup_recorded_per_attempt": True,
            "valid_sample_exclusions": [
                "failed final authentication",
                "invalid or undecodable frame",
                "noncausal action observation",
            ],
        },
        "cost_policy": {
            "formula": (
                "duration_seconds * (cpu * 0.00003942 + memory_gib * 0.00000667) "
                "* regional_multiplier"
            ),
            "regional_multiplier": _REGION_MULTIPLIERS.get(config.region),
            "completeness": "partial target-only lower bound",
            "excluded": ["runner_compute", "control_plane", "billing_adjustments"],
            "source_urls": [
                "https://modal.com/products/sandboxes",
                "https://modal.com/docs/guide/sandbox-resources",
                "https://modal.com/docs/guide/region-selection",
            ],
        },
        "artifact_policy": {
            "raw": "ignored and local",
            "tracked": "sanitized normalized JSON only",
            "raw_digest": "sha256",
            "minimum_p95_samples": MINIMUM_P95_SAMPLES,
            "failed_samples_retained": True,
        },
        "commands": dict(commands),
    }


def validate_preregistered_config(
    config: ModalOptimizationConfig,
    preregistration: dict[str, Any],
) -> None:
    frozen = copy.deepcopy(
        _mapping(preregistration.get("configuration"), "preregistration configuration")
    )
    effective = asdict(config)
    frozen_region = frozen.pop("region", None)
    effective.pop("region", None)
    if frozen_region not in {"selection-pending", config.region}:
        raise ValueError("effective region differs from preregistration")
    if frozen != effective:
        raise ValueError("effective benchmark configuration differs from preregistration")


def action_attempts_from_case(
    case: dict[str, Any],
    *,
    expected_attempts: int,
) -> list[dict[str, Any]]:
    if expected_attempts < 1:
        raise ValueError("expected_attempts must be positive")
    samples = case.get("action_to_frame_samples_ms")
    failures = case.get("failures")
    if not isinstance(samples, list) or not isinstance(failures, list):
        raise ValueError("case samples and failures must be lists")
    measured_failures: dict[int, dict[str, Any]] = {}
    for failure in failures:
        if not isinstance(failure, dict) or failure.get("phase") != "measure":
            continue
        iteration = failure.get("iteration")
        if (
            isinstance(iteration, bool)
            or not isinstance(iteration, int)
            or iteration < 0
            or iteration >= expected_attempts
            or iteration in measured_failures
        ):
            raise ValueError("action sample accounting contains an invalid failure index")
        measured_failures[iteration] = failure
    if len(samples) + len(measured_failures) != expected_attempts:
        raise ValueError("action sample accounting does not match expected attempts")
    sample_iterator = iter(samples)
    attempts: list[dict[str, Any]] = []
    for index in range(expected_attempts):
        failure = measured_failures.get(index)
        if failure is None:
            elapsed = next(sample_iterator)
            if not _is_nonnegative_finite(elapsed):
                raise ValueError("action samples must be finite and nonnegative")
            attempts.append(_attempt_row(index, status="valid", elapsed_ms=float(elapsed)))
            continue
        error_type = failure.get("error_type", failure.get("type"))
        if not _nonempty_text(error_type):
            raise ValueError("action failures require error_type")
        status = "timeout" if "timeout" in error_type.lower() else "failed"
        attempts.append(
            _attempt_row(
                index,
                status=status,
                failure={"phase": "measure", "error_type": error_type},
            )
        )
    return attempts




def build_modal_optimization_artifact(
    config: ModalOptimizationConfig,
    *,
    source_sha: str,
    dependency_sha: str,
    generated_at: str,
    preregistration_sha256: str,
    provider_default_payload: dict[str, Any],
    cold_attempts: list[dict[str, Any]],
    warm_action_attempts: list[dict[str, Any]],
    warm_action_metadata: dict[str, Any],
    claim_attempts: list[dict[str, Any]],
    claim_metadata: dict[str, Any],
    region_selection: dict[str, Any],
) -> dict[str, Any]:
    cold_summary = summarize_attempts(cold_attempts)
    warm_summary = summarize_attempts(warm_action_attempts)
    claim_summary = summarize_attempts(claim_attempts)
    cold_seconds = sum(float(item.get("resource_duration_seconds", 0.0)) for item in cold_attempts)
    on_demand_seconds = cold_seconds + float(
        warm_action_metadata.get("target_resource_duration_seconds", 0.0)
    )
    on_demand_cost = estimate_resource_cost(
        duration_seconds=on_demand_seconds,
        cpu=config.cpu,
        memory_mib=config.memory_mib,
        requested_region=config.region,
        includes_runner=False,
    )
    idle_seconds = sum(
        float(item["idle_resource_seconds"])
        for item in claim_attempts
        if _is_nonnegative_finite(item.get("idle_resource_seconds"))
    )
    idle_cost = sum(
        float(item["estimated_idle_cost_usd"])
        for item in claim_attempts
        if _is_nonnegative_finite(item.get("estimated_idle_cost_usd"))
    )
    hit_count = sum(item.get("pool_hit") is True for item in claim_attempts)
    miss_count = sum(item.get("pool_miss") is True for item in claim_attempts)
    fallback_count = sum(item.get("cold_fallback") is True for item in claim_attempts)
    failures = [
        {"profile": profile, "attempt": item["attempt"], **item["failure"]}
        for profile, attempts in (
            (PROFILE_MODAL_ON_DEMAND, cold_attempts),
            (PROFILE_MODAL_ON_DEMAND, warm_action_attempts),
            (PROFILE_MODAL_WARM_AVAILABILITY, claim_attempts),
        )
        for item in attempts
        if isinstance(item.get("failure"), dict)
    ]
    return {
        "schema_version": 1,
        "benchmark": "modal-optimization-results",
        "generated_at": generated_at,
        "profiles": {
            PROFILE_PROVIDER_DEFAULT: extract_provider_default_profile(
                provider_default_payload
            ),
            PROFILE_MODAL_ON_DEMAND: {
                "comparison_scope": "modal-provider-native-on-demand",
                "warm_capacity_enabled": False,
                "cold_attempts": cold_attempts,
                "cold_summary": cold_summary,
                "warm_action_attempts": warm_action_attempts,
                "warm_action_summary": warm_summary,
                "execution": warm_action_metadata,
                "estimated_billed_cost": on_demand_cost,
            },
            PROFILE_MODAL_WARM_AVAILABILITY: {
                "comparison_scope": "modal-on-demand-only",
                "cross_provider_default_comparison": False,
                "claim_attempts": claim_attempts,
                "claim_summary": claim_summary,
                "pool_hit_count": hit_count,
                "pool_hit_rate": hit_count / len(claim_attempts) if claim_attempts else 0.0,
                "pool_miss_count": miss_count,
                "pool_miss_rate": miss_count / len(claim_attempts) if claim_attempts else 0.0,
                "cold_fallback_count": fallback_count,
                "cold_fallback_rate": (
                    fallback_count / len(claim_attempts) if claim_attempts else 0.0
                ),
                "idle_resource_seconds": idle_seconds,
                "estimated_billed_cost": {
                    "status": "partial",
                    "estimated_idle_usd": idle_cost,
                    "excluded": [
                        "pool_startup_compute",
                        "claimed_session_compute",
                        "control_plane",
                        "billing_adjustments",
                    ],
                    "source_urls": [
                        "https://modal.com/products/sandboxes",
                        "https://modal.com/docs/guide/region-selection",
                    ],
                },
                "execution": claim_metadata,
            },
            PROFILE_MODAL_V2: {
                "status": "not_run",
                "reason": (
                    "Modal V2 documentation and the pinned Modal 1.5.2 SDK show that "
                    "Sandbox Connect Tokens are unavailable. The canonical authenticated "
                    "path requires create_connect_token(), so lifecycle parity is unavailable."
                ),
                "source_url": "https://modal.com/docs/guide/sandbox-v2",
                "v2_is_default": False,
            },
        },
        "failures": failures,
        "provenance": {
            "source_sha": source_sha,
            "dependency": {
                "pull_request": 114,
                "head_sha": dependency_sha,
                "state": "open_unmerged",
            },
            "branch": "feat/modal-optimization-benchmark-results",
            "execution_date_utc": generated_at[:10],
            "modal_sdk_version": "1.5.2",
            "image_identity": f"modal-computer-use-{config.browser}:{config.image_revision}",
            "requested_region": config.region,
            "region_selection": region_selection,
            "preregistration_sha256": preregistration_sha256,
            "raw_artifact_sha256": "0" * 64,
            "raw_artifact_path": "benchmark-results/modal-optimization/raw.json",
        },
    }


def estimate_resource_cost(
    *,
    duration_seconds: float,
    cpu: float,
    memory_mib: int,
    requested_region: str,
    includes_runner: bool,
) -> dict[str, Any]:
    if not _is_nonnegative_finite(duration_seconds):
        raise ValueError("duration_seconds must be finite and nonnegative")
    if not _is_nonnegative_finite(cpu) or cpu <= 0:
        raise ValueError("cpu must be finite and positive")
    if isinstance(memory_mib, bool) or not isinstance(memory_mib, int) or memory_mib <= 0:
        raise ValueError("memory_mib must be a positive integer")
    multiplier = _REGION_MULTIPLIERS.get(requested_region)
    if multiplier is None:
        raise ValueError("requested_region has no preregistered pricing multiplier")
    memory_gib = memory_mib / 1024
    estimate = duration_seconds * (
        cpu * _MODAL_CPU_USD_PER_CORE_SECOND
        + memory_gib * _MODAL_MEMORY_USD_PER_GIB_SECOND
    ) * multiplier
    included = ["target_cpu", "target_memory"]
    excluded = ["control_plane", "billing_adjustments"]
    if includes_runner:
        included.append("runner_compute")
    else:
        excluded.insert(0, "runner_compute")
    return {
        "status": "partial",
        "estimated_usd": estimate,
        "duration_seconds": duration_seconds,
        "region_multiplier": multiplier,
        "included": included,
        "excluded": excluded,
        "rates": {
            "cpu_usd_per_core_second": _MODAL_CPU_USD_PER_CORE_SECOND,
            "memory_usd_per_gib_second": _MODAL_MEMORY_USD_PER_GIB_SECOND,
        },
        "source_urls": [
            "https://modal.com/products/sandboxes",
            "https://modal.com/docs/guide/sandbox-resources",
            "https://modal.com/docs/guide/region-selection",
        ],
    }


def extract_provider_default_profile(payload: dict[str, Any]) -> dict[str, Any]:
    providers = _mapping(payload.get("providers"), "providers")
    results: dict[str, Any] = {}
    for source_name, canonical_name in _PROVIDER_NAMES.items():
        provider = _mapping(providers.get(source_name), source_name)
        cases = _mapping(provider.get("cases"), f"{source_name}.cases")
        cold = _mapping(
            cases.get("product_create_to_first_screenshot"),
            f"{source_name}.product_create_to_first_screenshot",
        )
        action = _mapping(cases.get("move_click"), f"{source_name}.move_click")
        cold_summary = _mapping(cold.get("summary_ms"), "cold summary")
        action_summary = _mapping(action.get("summary_ms"), "action summary")
        attempted = payload.get("iterations")
        results[canonical_name] = {
            "status": provider.get("status"),
            "cold_valid": cold.get("successful_iterations"),
            "cold_attempted": attempted,
            "cold_request_to_first_frame_p50_ms": cold_summary.get("p50"),
            "cold_request_to_first_frame_p95_ms": cold_summary.get("p95"),
            "warm_action_valid": action.get("successful_iterations"),
            "warm_action_attempted": attempted,
            "warm_action_move_click_p50_ms": action_summary.get("p50"),
            "warm_action_move_click_p95_ms": action_summary.get("p95"),
            "failure_count": len(provider.get("failures", [])),
        }
    provenance = _mapping(payload.get("provenance", {}), "provenance")
    return {
        "comparison_scope": "cross-provider-default-only",
        "providers": ["modal", "daytona", "e2b"],
        "results": results,
        "attempts_per_provider": payload.get("iterations"),
        "raw_artifact_sha256": provenance.get("raw_artifact_sha256"),
        "execution_source_sha": provenance.get("harness_commit"),
        "artifact_status": provenance.get("status"),
    }


def select_modal_optimization_region(
    payload: dict[str, Any],
    *,
    raw_bytes: bytes,
    expected_source_sha: str | None = None,
) -> tuple[str, dict[str, Any]]:
    artifact_digest = hashlib.sha256(raw_bytes).hexdigest()
    envelope_digest: str | None = None
    execution_source_sha: str | None = None
    if payload.get("benchmark") == "modal-region-selection-evidence":
        envelope_digest = artifact_digest
        provenance = _mapping(payload.get("provenance"), "region evidence provenance")
        execution_source_sha = provenance.get("execution_source_sha")
        if not _is_hex(execution_source_sha, 40):
            raise ValueError("region evidence execution source must be a full Git SHA")
        if expected_source_sha is not None and execution_source_sha != expected_source_sha:
            raise ValueError("region evidence source SHA does not match the requested revision")
        measured_payload = _mapping(payload.get("payload"), "region evidence payload")
        canonical_digest = hashlib.sha256(
            json.dumps(measured_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if provenance.get("payload_sha256") != canonical_digest:
            raise ValueError("region evidence payload digest does not match its envelope")
        artifact_digest = str(provenance.get("raw_artifact_sha256"))
        if not _is_hex(artifact_digest, 64):
            raise ValueError("region evidence raw artifact digest must be SHA-256")
        payload = measured_payload
    elif expected_source_sha is not None:
        raise ValueError("region selection requires a source-bound evidence envelope")
    if payload.get("benchmark") != "modal-region-ab":
        raise ValueError("region selection artifact must be a modal-region-ab result")
    if payload.get("iterations") != 30:
        raise ValueError("region selection requires exactly 30 transport iterations")
    comparison = _mapping(payload.get("comparison"), "region comparison")
    runs = _mapping(payload.get("runs"), "region runs")
    rows = _mapping(comparison.get("regions"), "region rows")
    candidates: dict[str, dict[str, Any]] = {}
    for region in ("us-west", "us-east"):
        row = rows.get(region)
        run = runs.get(region)
        if not isinstance(row, dict) or not isinstance(run, dict):
            continue
        transport = row.get("fastest_floor_p50_ms")
        metadata = run.get("metadata")
        environment = metadata.get("environment") if isinstance(metadata, dict) else None
        cold = (
            environment.get("modal_cold_create_to_ready_ms")
            if isinstance(environment, dict)
            else None
        )
        if _is_nonnegative_finite(transport) and _is_nonnegative_finite(cold):
            candidates[region] = {
                "transport_p50_ms": float(transport),
                "cold_create_to_ready_ms": float(cold),
                "ok": run.get("ok") is True,
                "failure_count": len(run.get("failures", [])),
            }
    if set(candidates) != {"us-west", "us-east"}:
        raise ValueError("both explicit region candidates must have valid evidence")
    ordered = sorted(candidates, key=lambda region: candidates[region]["transport_p50_ms"])
    fastest, other = ordered
    fastest_ms = candidates[fastest]["transport_p50_ms"]
    other_ms = candidates[other]["transport_p50_ms"]
    tied = fastest_ms == 0 or (other_ms - fastest_ms) / fastest_ms <= 0.05
    selected = (
        min(candidates, key=lambda region: candidates[region]["cold_create_to_ready_ms"])
        if tied
        else fastest
    )
    if candidates[selected]["ok"] is not True:
        successful = [region for region, row in candidates.items() if row["ok"] is True]
        if not successful:
            raise ValueError("region selection has no clean explicit candidate")
        selected = min(
            successful,
            key=lambda region: candidates[region]["transport_p50_ms"],
        )
    return selected, {
        "artifact_sha256": artifact_digest,
        "evidence_envelope_sha256": envelope_digest,
        "execution_source_sha": execution_source_sha,
        "candidates": candidates,
        "selected": selected,
        "rule": "transport p50; within 5 percent use lower cold create-to-ready",
        "default_region_excluded_from_optimized_placement": True,
        "candidate_failures_retained": True,
    }


def build_modal_region_evidence_envelope(
    payload: dict[str, Any],
    *,
    raw_bytes: bytes,
    raw_artifact_path: str,
    execution_source_sha: str,
) -> dict[str, Any]:
    if payload.get("benchmark") != "modal-region-ab":
        raise ValueError("region evidence input must be a modal-region-ab result")
    if not _safe_relative_path(raw_artifact_path):
        raise ValueError("region evidence raw path must be repository-relative")
    if not _is_hex(execution_source_sha, 40):
        raise ValueError("region evidence source must be a full Git SHA")
    return {
        "schema_version": 1,
        "benchmark": "modal-region-selection-evidence",
        "payload": copy.deepcopy(payload),
        "provenance": {
            "execution_source_sha": execution_source_sha,
            "raw_artifact_path": raw_artifact_path,
            "raw_artifact_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "payload_sha256": hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "raw_artifact_tracked": False,
        },
    }


def _attempt_row(
    index: int,
    *,
    status: str,
    elapsed_ms: float | None = None,
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "attempt": index,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "retry_count": 0,
        "failure": failure,
        "cleanup": {
            "attempted": False,
            "succeeded": None,
            "error_type": None,
            "reason": "persistent target cleanup is recorded at profile scope",
        },
    }




def _nested_number(value: dict[str, Any], *keys: str) -> float | None:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return float(current) if _is_nonnegative_finite(current) else None


def summarize_attempts(
    attempts: list[dict[str, Any]],
    *,
    minimum_p95_samples: int = MINIMUM_P95_SAMPLES,
) -> dict[str, Any]:
    if minimum_p95_samples < 1:
        raise ValueError("minimum_p95_samples must be positive")
    counts = {"valid": 0, "failed": 0, "timeout": 0}
    samples: list[float] = []
    for attempt in attempts:
        status = attempt.get("status")
        if status not in counts:
            raise ValueError("attempt status must be valid, failed, or timeout")
        counts[status] += 1
        elapsed = attempt.get("elapsed_ms")
        if status == "valid":
            if not _is_nonnegative_finite(elapsed):
                raise ValueError("valid attempts require finite nonnegative elapsed_ms")
            samples.append(float(elapsed))
        elif elapsed is not None:
            raise ValueError("failed and timeout attempts must not report elapsed_ms")
    ordered = sorted(samples)
    p95_supported = len(ordered) >= minimum_p95_samples
    return {
        "attempted": len(attempts),
        "valid": counts["valid"],
        "failed": counts["failed"],
        "timeout": counts["timeout"],
        "p50_ms": statistics.median(ordered) if ordered else None,
        "p95_ms": _percentile(ordered, 95) if p95_supported else None,
        "p95_status": "reported" if p95_supported else "insufficient_valid_samples",
        "minimum_p95_samples": minimum_p95_samples,
    }


def validate_modal_optimization_artifact(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise ValueError("modal optimization artifact schema_version must be 1")
    if payload.get("benchmark") != "modal-optimization-results":
        raise ValueError("modal optimization artifact benchmark name is invalid")
    profiles = _mapping(payload.get("profiles"), "profiles")
    required_profiles = {
        PROFILE_PROVIDER_DEFAULT,
        PROFILE_MODAL_ON_DEMAND,
        PROFILE_MODAL_WARM_AVAILABILITY,
        PROFILE_MODAL_V2,
    }
    if set(profiles) != required_profiles:
        raise ValueError("modal optimization artifact requires exactly four canonical profiles")
    provider_default = _mapping(profiles[PROFILE_PROVIDER_DEFAULT], PROFILE_PROVIDER_DEFAULT)
    if provider_default.get("comparison_scope") != "cross-provider-default-only":
        raise ValueError("provider-default must use cross-provider defaults only")
    if provider_default.get("providers") != ["modal", "daytona", "e2b"]:
        raise ValueError("provider-default must retain Modal, Daytona, and E2B")

    on_demand = _mapping(profiles[PROFILE_MODAL_ON_DEMAND], PROFILE_MODAL_ON_DEMAND)
    if on_demand.get("comparison_scope") != "modal-provider-native-on-demand":
        raise ValueError("Modal optimized on-demand must stay provider-native")
    _validate_attempt_collection(on_demand, "cold")
    _validate_attempt_collection(on_demand, "warm_action")

    warm = _mapping(profiles[PROFILE_MODAL_WARM_AVAILABILITY], PROFILE_MODAL_WARM_AVAILABILITY)
    if warm.get("comparison_scope") != "modal-on-demand-only":
        raise ValueError("warm availability must compare with Modal on-demand only")
    claims = _list_of_mappings(warm.get("claim_attempts"), "claim_attempts")
    _validate_attempt_indices(claims)
    for claim in claims:
        hit = claim.get("pool_hit") is True
        miss = claim.get("pool_miss") is True
        fallback = claim.get("cold_fallback") is True
        if claim.get("status") == "valid" and (hit == miss or (fallback and not miss)):
            raise ValueError("claim hit, miss, and fallback fields are inconsistent")
        if claim.get("status") != "valid" and (hit or miss or fallback):
            raise ValueError("failed claims must not invent availability outcomes")
    _validate_optional_summary(warm, "claim", claims)

    v2 = _mapping(profiles[PROFILE_MODAL_V2], PROFILE_MODAL_V2)
    if v2.get("status") != "not_run":
        raise ValueError("Modal V2 must remain not_run without Connect Token parity")
    if not _nonempty_text(v2.get("reason")) or not _nonempty_text(v2.get("source_url")):
        raise ValueError("Modal V2 not_run result requires a reason and official source URL")

    failures = payload.get("failures")
    if not isinstance(failures, list):
        raise ValueError("modal optimization artifact failures must be a list")
    provenance = _mapping(payload.get("provenance"), "provenance")
    if not _is_hex(provenance.get("source_sha"), 40):
        raise ValueError("provenance source_sha must be a full Git SHA")
    if not _is_hex(provenance.get("raw_artifact_sha256"), 64):
        raise ValueError("provenance raw_artifact_sha256 must be SHA-256")
    if not _is_hex(provenance.get("normalizer_sha"), 40):
        raise ValueError("provenance normalizer_sha must be a full Git SHA")
    raw_path = provenance.get("raw_artifact_path")
    if not isinstance(raw_path, str) or not _safe_relative_path(raw_path):
        raise ValueError("provenance raw_artifact_path must be repository-relative")


def sanitize_modal_optimization_benchmark(
    raw_payload: dict[str, Any],
    *,
    raw_bytes: bytes,
    raw_artifact_path: str,
    harness_commit: str,
    preregistration_payload: dict[str, Any] | None = None,
    preregistration_bytes: bytes | None = None,
    region_evidence_payload: dict[str, Any] | None = None,
    region_evidence_bytes: bytes | None = None,
    normalizer_commit: str | None = None,
) -> dict[str, Any]:
    if not _is_hex(harness_commit, 40):
        raise ValueError("harness_commit must be a full Git SHA")
    if not _safe_relative_path(raw_artifact_path):
        raise ValueError("raw_artifact_path must be repository-relative")
    _reject_forbidden_fields(raw_payload)
    _validate_safe_value(raw_payload)
    payload = copy.deepcopy(raw_payload)
    _add_derived_summaries(payload)
    if preregistration_payload is not None:
        if preregistration_bytes is None:
            raise ValueError("preregistration_bytes are required with a preregistration payload")
        if preregistration_payload.get("source_sha") != harness_commit:
            raise ValueError("preregistration source SHA must match the execution harness")
        preregistration_digest = hashlib.sha256(preregistration_bytes).hexdigest()
        existing_digest = _mapping(payload.get("provenance", {}), "provenance").get(
            "preregistration_sha256"
        )
        if existing_digest != preregistration_digest:
            raise ValueError("preregistration digest does not match the raw artifact")
        command_manifest = _portable_command_manifest(
            _mapping(preregistration_payload.get("commands"), "commands")
        )
        _add_region_attestation_command(
            command_manifest,
            region_evidence_payload=region_evidence_payload,
        )
        payload["command_manifest"] = command_manifest
        payload["command_manifest_sanitization"] = (
            "Normalized the runner-specific credential file path to repository-relative .env. "
            "Added the digest-bound region attestation command when the execution-time "
            "preregistration did not contain that later provenance step."
        )
        payload["environment_manifest"] = copy.deepcopy(
            _mapping(preregistration_payload.get("environment"), "environment")
        )
        payload["measurement_manifest"] = {
            key: copy.deepcopy(preregistration_payload[key])
            for key in (
                "sample_policy",
                "metric_boundaries",
                "timeout_policy",
                "retry_policy",
                "failure_policy",
                "cost_policy",
                "artifact_policy",
            )
        }
    region_attestation: dict[str, Any] | None = None
    if region_evidence_payload is not None:
        if region_evidence_bytes is None:
            raise ValueError("region_evidence_bytes are required with region evidence")
        raw_region = _mapping(
            _mapping(payload.get("provenance", {}), "provenance").get("region_selection"),
            "raw region selection",
        )
        expected_source = raw_region.get("execution_source_sha")
        selected, evidence = select_modal_optimization_region(
            region_evidence_payload,
            raw_bytes=region_evidence_bytes,
            expected_source_sha=expected_source,
        )
        for key in ("artifact_sha256", "candidates"):
            if evidence.get(key) != raw_region.get(key):
                raise ValueError("source-bound region evidence differs from the raw artifact")
        if selected != raw_region.get("selected"):
            raise ValueError("source-bound region selection differs from the raw artifact")
        region_attestation = {
            "status": "validated",
            "execution_source_sha": expected_source,
            "evidence_envelope_sha256": hashlib.sha256(region_evidence_bytes).hexdigest(),
            "raw_artifact_sha256": evidence["artifact_sha256"],
        }
    normalizer = normalizer_commit or harness_commit
    if not _is_hex(normalizer, 40):
        raise ValueError("normalizer_commit must be a full Git SHA")
    payload["provenance"] = {
        **_mapping(payload.get("provenance", {}), "provenance"),
        "source_sha": harness_commit,
        "raw_artifact_path": raw_artifact_path,
        "raw_artifact_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_artifact_tracked": False,
        "sanitizer": "modal_computer_use.benchmarks.modal_optimization",
        "sanitizer_version": 1,
        "normalizer_sha": normalizer,
    }
    if region_attestation is not None:
        payload["provenance"]["region_selection_attestation"] = region_attestation
    _reject_forbidden_fields(payload)
    _validate_safe_value(payload)
    validate_modal_optimization_artifact(payload)
    return payload


def serialize_modal_optimization_benchmark(payload: dict[str, Any]) -> str:
    validate_modal_optimization_artifact(payload)
    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n"


def generate_sanitized_modal_optimization_benchmark(
    *,
    raw_path: Path,
    output_path: Path,
    raw_artifact_path: str,
    harness_commit: str,
    preregistration_path: Path,
    region_evidence_path: Path,
    normalizer_commit: str,
    check: bool = False,
) -> bool:
    raw_bytes = raw_path.read_bytes()
    raw_payload = json.loads(raw_bytes)
    if not isinstance(raw_payload, dict):
        raise ValueError("modal optimization payload must be a JSON object")
    preregistration_bytes = preregistration_path.read_bytes()
    preregistration_payload = json.loads(preregistration_bytes)
    if not isinstance(preregistration_payload, dict):
        raise ValueError("preregistration payload must be a JSON object")
    region_evidence_bytes = region_evidence_path.read_bytes()
    region_evidence_payload = json.loads(region_evidence_bytes)
    if not isinstance(region_evidence_payload, dict):
        raise ValueError("region evidence payload must be a JSON object")
    sanitized = sanitize_modal_optimization_benchmark(
        raw_payload,
        raw_bytes=raw_bytes,
        raw_artifact_path=raw_artifact_path,
        harness_commit=harness_commit,
        preregistration_payload=preregistration_payload,
        preregistration_bytes=preregistration_bytes,
        region_evidence_payload=region_evidence_payload,
        region_evidence_bytes=region_evidence_bytes,
        normalizer_commit=normalizer_commit,
    )
    rendered = serialize_modal_optimization_benchmark(sanitized)
    if check:
        return output_path.is_file() and output_path.read_text(encoding="utf-8") == rendered
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return True


def _add_derived_summaries(payload: dict[str, Any]) -> None:
    profiles = _mapping(payload.get("profiles"), "profiles")
    on_demand = _mapping(profiles.get(PROFILE_MODAL_ON_DEMAND), PROFILE_MODAL_ON_DEMAND)
    cold = _list_of_mappings(on_demand.get("cold_attempts"), "cold_attempts")
    on_demand["cold_summary"] = summarize_attempts(cold)
    warm_actions = _list_of_mappings(
        on_demand.get("warm_action_attempts"),
        "warm_action_attempts",
    )
    on_demand["warm_action_summary"] = summarize_attempts(warm_actions)
    stage_names = sorted(
        {
            str(stage)
            for attempt in cold
            for stage in _mapping(attempt.get("stages", {}), "stages")
        }
    )
    stage_summaries: dict[str, Any] = {}
    for stage in stage_names:
        observed = []
        unsupported_reasons: set[str] = set()
        for attempt in cold:
            stage_value = _mapping(attempt.get("stages", {}), "stages").get(stage)
            if not isinstance(stage_value, dict):
                continue
            if stage_value.get("status") == "observed" and _is_nonnegative_finite(
                stage_value.get("elapsed_ms")
            ):
                observed.append(float(stage_value["elapsed_ms"]))
            elif stage_value.get("status") == "unsupported" and _nonempty_text(
                stage_value.get("reason")
            ):
                unsupported_reasons.add(str(stage_value["reason"]))
        if observed:
            stage_summaries[stage] = {
                "status": "observed",
                **_summary_from_values(observed),
            }
        else:
            stage_summaries[stage] = {
                "status": "unsupported",
                "reasons": sorted(unsupported_reasons),
            }
    on_demand["cold_stage_summaries"] = stage_summaries

    warm = _mapping(
        profiles.get(PROFILE_MODAL_WARM_AVAILABILITY),
        PROFILE_MODAL_WARM_AVAILABILITY,
    )
    claims = _list_of_mappings(warm.get("claim_attempts"), "claim_attempts")
    warm["claim_summary"] = summarize_attempts(claims)
    warm["request_to_authenticated_summary"] = _summary_from_attempt_field(
        claims,
        "request_to_authenticated_ms",
    )
    warm["request_to_first_frame_summary"] = _summary_from_attempt_field(
        claims,
        "elapsed_ms",
    )
    warm["claim_acquisition_summary"] = _summary_from_attempt_field(
        claims,
        "claim_elapsed_ms",
    )


def _portable_command_manifest(commands: dict[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for name, command in commands.items():
        if not _nonempty_text(command):
            raise ValueError("command manifest values must be nonempty strings")
        output[str(name)] = re.sub(
            r"--env-file\s+(?:\"[^\"]+\"|'[^']+'|\S+)",
            "--env-file .env",
            str(command),
        )
    return output


def _add_region_attestation_command(
    commands: dict[str, str],
    *,
    region_evidence_payload: dict[str, Any] | None,
) -> None:
    if "region_selection_attest" in commands or region_evidence_payload is None:
        return
    provenance = _mapping(
        region_evidence_payload.get("provenance"),
        "region evidence provenance",
    )
    raw_path = provenance.get("raw_artifact_path")
    source_sha = provenance.get("execution_source_sha")
    if not isinstance(raw_path, str) or not _safe_relative_path(raw_path):
        raise ValueError("region evidence raw path must be repository-relative")
    if not _is_hex(source_sha, 40):
        raise ValueError("region evidence source must be a full Git SHA")
    raw = Path(raw_path)
    attested = raw.with_name(f"{raw.stem}-attested{raw.suffix}")
    commands["region_selection_attest"] = (
        "uv run python scripts/run_modal_optimization_benchmark.py attest-region "
        f"{raw.as_posix()} {attested.as_posix()} --source-sha {source_sha} "
        f"--raw-artifact-path {raw.as_posix()}"
    )


def _summary_from_attempt_field(
    attempts: list[dict[str, Any]],
    field: str,
) -> dict[str, Any]:
    values = [
        float(attempt[field])
        for attempt in attempts
        if attempt.get("status") == "valid" and _is_nonnegative_finite(attempt.get(field))
    ]
    return _summary_from_values(values)


def _summary_from_values(values: list[float]) -> dict[str, Any]:
    rows = [
        {
            "attempt": index,
            "status": "valid",
            "elapsed_ms": value,
            "retry_count": 0,
            "failure": None,
            "cleanup": {"attempted": False, "succeeded": None, "error_type": None},
        }
        for index, value in enumerate(values)
    ]
    return summarize_attempts(rows)


def _validate_attempt_collection(profile: dict[str, Any], prefix: str) -> None:
    attempts = _list_of_mappings(profile.get(f"{prefix}_attempts"), f"{prefix}_attempts")
    _validate_attempt_indices(attempts)
    _validate_optional_summary(profile, prefix, attempts)


def _validate_attempt_indices(attempts: list[dict[str, Any]]) -> None:
    indices: list[int] = []
    for attempt in attempts:
        index = attempt.get("attempt")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("attempt indices must be nonnegative integers")
        indices.append(index)
    if len(indices) != len(set(indices)):
        raise ValueError("attempt indices must be unique")
    summarize_attempts(attempts)


def _validate_optional_summary(
    profile: dict[str, Any],
    prefix: str,
    attempts: list[dict[str, Any]],
) -> None:
    raw_summary = profile.get(f"{prefix}_summary")
    if raw_summary is None:
        return
    summary = _mapping(raw_summary, f"{prefix}_summary")
    expected = summarize_attempts(attempts)
    for key in (
        "attempted",
        "valid",
        "failed",
        "timeout",
        "p50_ms",
        "p95_ms",
        "p95_status",
        "minimum_p95_samples",
    ):
        if summary.get(key) != expected[key]:
            raise ValueError(f"{prefix} summary does not match attempt rows")


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).lower().replace("-", "_")
            if key in _FORBIDDEN_ARTIFACT_FIELDS:
                raise ValueError(f"modal optimization artifact contains forbidden field: {raw_key}")
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


def _is_nonnegative_finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts
