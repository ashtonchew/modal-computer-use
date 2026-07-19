from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Any

from modal_computer_use.sandbox import (
    ModalCandidatePlacementProbe,
    probe_modal_candidate_placement,
)

DEFAULT_CLOUD_REQUESTS: tuple[str | None, ...] = (None, "aws", "gcp", "oci")
PROBE_ROLES = (
    "v1-target",
    "v2-tunnel-target",
    "v2-i6pn-target",
    "v2-i6pn-runner",
)


def run_placement_capability_matrix(
    *,
    run_id: str,
    source_sha: str,
    generated_at: str,
    image_revision: str,
    region: str,
    target_cpu: float,
    target_memory_mib: int,
    cloud_requests: Iterable[str | None] = DEFAULT_CLOUD_REQUESTS,
    app_name: str = "modal-computer-use-v2-placement-probe",
    probe: Callable[..., ModalCandidatePlacementProbe] = probe_modal_candidate_placement,
) -> dict[str, Any]:
    if not run_id.strip():
        raise ValueError("placement capability matrix requires an exact run ID")
    requests = tuple(cloud_requests)
    if not requests or len(set(requests)) != len(requests):
        raise ValueError("placement capability cloud requests must be non-empty and unique")
    if any(value not in {None, "aws", "gcp", "oci"} for value in requests):
        raise ValueError("placement capability cloud request is unsupported")
    candidates: list[dict[str, Any]] = []
    for requested_cloud in requests:
        label = cloud_request_label(requested_cloud)
        observations = {
            "v1-target": probe(
                app_name=app_name,
                image_revision=image_revision,
                run_id=f"{run_id}-{label}-v1-target",
                backend="v1",
                cloud=requested_cloud,
                region=region,
                cpu=target_cpu,
                memory_mib=target_memory_mib,
                i6pn=False,
                tags={"computer-use.probe_request": label, "computer-use.probe_role": "v1-target"},
            ),
            "v2-tunnel-target": probe(
                app_name=app_name,
                image_revision=image_revision,
                run_id=f"{run_id}-{label}-v2-tunnel-target",
                backend="v2",
                cloud=requested_cloud,
                region=region,
                cpu=target_cpu,
                memory_mib=target_memory_mib,
                i6pn=False,
                tags={
                    "computer-use.probe_request": label,
                    "computer-use.probe_role": "v2-tunnel-target",
                },
            ),
            "v2-i6pn-target": probe(
                app_name=app_name,
                image_revision=image_revision,
                run_id=f"{run_id}-{label}-v2-i6pn-target",
                backend="v2",
                cloud=requested_cloud,
                region=region,
                cpu=target_cpu,
                memory_mib=target_memory_mib,
                i6pn=True,
                tags={
                    "computer-use.probe_request": label,
                    "computer-use.probe_role": "v2-i6pn-target",
                },
            ),
            "v2-i6pn-runner": probe(
                app_name=app_name,
                image_revision=image_revision,
                run_id=f"{run_id}-{label}-v2-i6pn-runner",
                backend="v2",
                cloud=requested_cloud,
                region=region,
                cpu=1.0,
                memory_mib=1024,
                i6pn=True,
                tags={
                    "computer-use.probe_request": label,
                    "computer-use.probe_role": "v2-i6pn-runner",
                },
            ),
        }
        candidates.append(
            evaluate_placement_candidate(
                requested_cloud=requested_cloud,
                requested_region=region,
                observations=observations,
            )
        )

    selected = next((candidate for candidate in candidates if candidate["eligible"] is True), None)
    payload = {
        "schema_version": 1,
        "benchmark": "modal-v2-placement-capability-matrix",
        "run_id": run_id,
        "generated_at": generated_at,
        "source_sha": source_sha,
        "image_identity": f"modal-computer-use-chromium:{image_revision}",
        "requested_region": region,
        "target_resources": {"cpu": target_cpu, "memory_mib": target_memory_mib},
        "runner_resources": {"cpu": 1.0, "memory_mib": 1024},
        "candidate_order": [cloud_request_label(value) for value in requests],
        "candidates": candidates,
        "selected_request": (
            None
            if selected is None
            else {
                "cloud": selected["requested_cloud"],
                "region": selected["requested_region"],
                "actual_placement": selected["actual_placement"],
            }
        ),
        "backend_causal_comparison_available": selected is not None,
        "classification": (
            "exact-common-placement-available"
            if selected is not None
            else "descriptive-placement-capability-only"
        ),
        "measurement_performed": False,
    }
    validate_placement_capability_matrix(payload)
    return payload


def evaluate_placement_candidate(
    *,
    requested_cloud: str | None,
    requested_region: str,
    observations: dict[str, ModalCandidatePlacementProbe],
) -> dict[str, Any]:
    if set(observations) != set(PROBE_ROLES):
        raise ValueError("placement candidate requires all four probe roles")
    serialized = {role: asdict(observations[role]) for role in PROBE_ROLES}
    reasons: list[str] = []
    for role, observation in serialized.items():
        if observation["sandbox_created"] is not True:
            reasons.append(f"{role} sandbox was not created")
        if observation["status"] != "valid":
            reasons.append(f"{role} probe failed: {observation['error_type'] or 'unknown error'}")
        if observation["cleanup_succeeded"] is not True:
            reasons.append(f"{role} cleanup failed")
        if observation["actual_cloud"] is None or observation["actual_region"] is None:
            reasons.append(f"{role} placement was not observed")
        if role.startswith("v2-i6pn") and observation["i6pn_verified"] is not True:
            reasons.append(f"{role} i6pn address was not verified")
        if requested_cloud is not None and not _cloud_matches(
            requested_cloud, observation["actual_cloud"]
        ):
            reasons.append(f"{role} did not honor requested cloud {requested_cloud}")
    placements = {
        (observation["actual_cloud"], observation["actual_region"])
        for observation in serialized.values()
        if observation["actual_cloud"] is not None and observation["actual_region"] is not None
    }
    if len(placements) != 1:
        reasons.append("V1 target, V2 targets, and V2 runner lack one exact observed placement")
    placement = next(iter(placements)) if len(placements) == 1 else None
    return {
        "requested_cloud": requested_cloud,
        "requested_region": requested_region,
        "observations": serialized,
        "actual_placement": (
            None if placement is None else {"cloud": placement[0], "region": placement[1]}
        ),
        "eligible": not reasons,
        "reasons": reasons,
    }


def validate_placement_capability_matrix(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1 or payload.get("benchmark") != (
        "modal-v2-placement-capability-matrix"
    ):
        raise ValueError("placement capability matrix schema is invalid")
    if payload.get("measurement_performed") is not False:
        raise ValueError("placement capability probes must not contain measurements")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("placement capability matrix requires an exact run ID")
    source_sha = payload.get("source_sha")
    if not isinstance(source_sha, str) or len(source_sha) != 40 or not _is_hex(source_sha):
        raise ValueError("placement capability matrix requires a full source SHA")
    requested_region = payload.get("requested_region")
    if not isinstance(requested_region, str) or not requested_region.strip():
        raise ValueError("placement capability matrix requires an explicit region")
    image_identity = payload.get("image_identity")
    if not isinstance(image_identity, str) or not image_identity.startswith(
        "modal-computer-use-chromium:"
    ):
        raise ValueError("placement capability matrix image identity is invalid")
    image_revision = image_identity.rsplit(":", 1)[-1]
    if len(image_revision) != 40 or not _is_hex(image_revision):
        raise ValueError("placement capability matrix image revision is invalid")
    target_resources = payload.get("target_resources")
    if not isinstance(target_resources, dict) or set(target_resources) != {
        "cpu",
        "memory_mib",
    }:
        raise ValueError("placement capability target resources are invalid")
    if (
        not isinstance(target_resources["cpu"], (int, float))
        or isinstance(target_resources["cpu"], bool)
        or target_resources["cpu"] <= 0
        or not isinstance(target_resources["memory_mib"], int)
        or isinstance(target_resources["memory_mib"], bool)
        or target_resources["memory_mib"] <= 0
    ):
        raise ValueError("placement capability target resources are invalid")
    if payload.get("runner_resources") != {"cpu": 1.0, "memory_mib": 1024}:
        raise ValueError("placement capability runner resources are invalid")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("placement capability matrix requires candidates")
    expected_order: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("placement capability candidate must be an object")
        requested_cloud = candidate.get("requested_cloud")
        requested_region = candidate.get("requested_region")
        observations = candidate.get("observations")
        if requested_cloud not in {None, "aws", "gcp", "oci"}:
            raise ValueError("placement capability candidate cloud is unsupported")
        if candidate.get("requested_region") != requested_region or not isinstance(
            observations, dict
        ):
            raise ValueError("placement capability candidate is incomplete")
        try:
            typed_observations = {
                role: ModalCandidatePlacementProbe(**observations[role]) for role in PROBE_ROLES
            }
        except (KeyError, TypeError) as exc:
            raise ValueError("placement capability observations are invalid") from exc
        label = cloud_request_label(requested_cloud)
        expected_contract = {
            "v1-target": ("v1", False),
            "v2-tunnel-target": ("v2", False),
            "v2-i6pn-target": ("v2", True),
            "v2-i6pn-runner": ("v2", True),
        }
        for role, observation in typed_observations.items():
            backend, i6pn = expected_contract[role]
            if (
                observation.run_id != f"{run_id}-{label}-{role}"
                or observation.backend != backend
                or observation.i6pn_enabled is not i6pn
                or observation.requested_cloud != requested_cloud
                or observation.requested_region != requested_region
            ):
                raise ValueError("placement capability observation contract is invalid")
        expected = evaluate_placement_candidate(
            requested_cloud=requested_cloud,
            requested_region=requested_region,
            observations=typed_observations,
        )
        if candidate != expected:
            raise ValueError("placement capability candidate does not match its observations")
        expected_order.append(cloud_request_label(requested_cloud))
    if payload.get("candidate_order") != expected_order:
        raise ValueError("placement capability candidate order is invalid")
    eligible = [candidate for candidate in candidates if candidate.get("eligible") is True]
    selected = payload.get("selected_request")
    first = eligible[0] if eligible else None
    expected_selected = (
        None
        if first is None
        else {
            "cloud": first["requested_cloud"],
            "region": first["requested_region"],
            "actual_placement": first["actual_placement"],
        }
    )
    if selected != expected_selected:
        raise ValueError("selected placement request differs from eligible candidates")
    if payload.get("backend_causal_comparison_available") is not bool(eligible):
        raise ValueError("placement causal eligibility is inconsistent")
    expected_classification = (
        "exact-common-placement-available"
        if eligible
        else "descriptive-placement-capability-only"
    )
    if payload.get("classification") != expected_classification:
        raise ValueError("placement capability classification is inconsistent")


def build_placement_capability_binding(
    payload: dict[str, Any], *, artifact_path: str
) -> dict[str, Any]:
    validate_placement_capability_matrix(payload)
    path = PurePosixPath(artifact_path)
    if path.is_absolute() or not path.parts or path.parts[0] != "benchmark-results":
        raise ValueError("placement capability artifact must be under benchmark-results")
    if payload.get("backend_causal_comparison_available") is not True:
        raise ValueError("placement capability matrix found no comparable placement")
    selected = payload.get("selected_request")
    if not isinstance(selected, dict):
        raise ValueError("placement capability matrix has no selected request")
    return {
        "artifact_path": path.as_posix(),
        "sha256": placement_capability_sha256(payload),
        "run_id": payload.get("run_id"),
        "source_sha": payload.get("source_sha"),
        "image_identity": payload.get("image_identity"),
        "requested_region": payload.get("requested_region"),
        "target_resources": payload.get("target_resources"),
        "runner_resources": payload.get("runner_resources"),
        "selected_request": selected,
        "classification": payload.get("classification"),
        "measurement_performed": payload.get("measurement_performed"),
    }


def placement_capability_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def cloud_request_label(value: str | None) -> str:
    return "unconstrained" if value is None else value


def _cloud_matches(requested: str, actual: Any) -> bool:
    expected = {
        "aws": {"aws", "CLOUD_PROVIDER_AWS"},
        "gcp": {"gcp", "CLOUD_PROVIDER_GCP"},
        "oci": {"oci", "CLOUD_PROVIDER_OCI"},
    }.get(requested)
    return expected is not None and actual in expected


def _is_hex(value: str) -> bool:
    return all(character in "0123456789abcdef" for character in value)
