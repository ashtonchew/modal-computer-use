"""Private 1-vs-2 CPU ablation for the direct/spawned X11-SHM discriminator.

The capture child remains the exact paired, region-only workload.  This module
only composes two fresh target-Sandbox results (one at 1 CPU and one at 2 CPU)
into one non-gating artifact; it does not alter public screenshot routing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from modal_computer_use.benchmarks import x11_shm_direct_vs_spawned as _base

SCHEMA_VERSION = "x11-shm-direct-vs-spawned-cpu-ablation.v1"
BENCHMARK_NAME = "x11-shm-direct-vs-spawned-cpu-ablation"
CPU_RUNS = {
    "cpu_1": {"cpu": 1.0, "memory_bytes": 2048 * 1024**2},
    "cpu_2": {"cpu": 2.0, "memory_bytes": 2048 * 1024**2},
}
SCOPE_CONTRACT = {
    "same_workload": True,
    "same_region": True,
    "same_image": True,
    "same_fixture": True,
    "fresh_target_sandboxes": True,
    "non_gating": True,
    "retries": 0,
    "replacement_samples": 0,
}
_HASH_FIELDS = frozenset({"module_sha256", "image_object_id"})
_SAFE_FAILURE_TYPES = frozenset(
    set(_base.SAFE_ARM_FAILURE_TYPES)
    | {"EvidenceValidationError", "CleanupError", "CgroupEvidenceUnavailable"}
)


def _sha256_label(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"invalid {label}")
    return value


def _image_label(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("im-")
        or len(value) > 128
        or not all(character.isalnum() or character in "_.-" for character in value)
    ):
        raise ValueError("invalid image identity")
    return value


def _validate_source_identity(value: object) -> dict[str, str]:
    if value == {}:
        return {}
    if not isinstance(value, Mapping) or set(value) != _HASH_FIELDS:
        raise ValueError("invalid source identity")
    return {
        "module_sha256": _sha256_label(value["module_sha256"], "module identity"),
        "image_object_id": _image_label(value["image_object_id"]),
    }


def _validate_fixture_identity(value: object) -> str:
    return _sha256_label(value, "fixture identity")


def _validate_sandbox_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sb-")
        or len(value) > 128
        or not all(character.isalnum() or character in "_.-" for character in value)
    ):
        raise ValueError("invalid target Sandbox identity")
    return value


def _validate_resources(
    value: object, expected: Mapping[str, float | int]
) -> dict[str, float | int]:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise ValueError("invalid configured resources")
    cpu = value["cpu"]
    memory = value["memory_bytes"]
    if (
        isinstance(cpu, bool)
        or not isinstance(cpu, float)
        or cpu != expected["cpu"]
        or isinstance(memory, bool)
        or not isinstance(memory, int)
        or memory != expected["memory_bytes"]
    ):
        raise ValueError("configured resources differ from the CPU arm")
    return {"cpu": cpu, "memory_bytes": memory}


def _validate_nested_source(artifact: Mapping[str, Any], source: Mapping[str, str]) -> None:
    target_identity = artifact.get("target_identity")
    if not isinstance(target_identity, Mapping):
        raise ValueError("child artifact has no target identity")
    if target_identity.get("module_sha256") != source["module_sha256"]:
        raise ValueError("CPU runs used different native modules")
    if target_identity.get("image_object_id") != source["image_object_id"]:
        raise ValueError("CPU runs used different image identities")


def _cgroup_attributable(artifact: Mapping[str, Any], expected: Mapping[str, float | int]) -> bool:
    target = artifact.get("target_identity")
    if not isinstance(target, Mapping):
        return False
    quota = target.get("quota_usec")
    period = target.get("period_usec")
    if not isinstance(quota, int) or not isinstance(period, int):
        return False
    return (
        target.get("cgroup_available") is True
        and target.get("cpu") == expected["cpu"]
        and quota == int(period * float(expected["cpu"]))
        and quota > 0
        and period > 0
        and target.get("memory_bytes") == expected["memory_bytes"]
        and target.get("cgroup_version") == "v2"
        and target.get("cgroup_resolution") in {"namespace-root", "namespace-relative"}
        and artifact.get("worker_cgroup_same") is True
    )


def _pixel_hashes(artifact: Mapping[str, Any]) -> set[str]:
    hashes: set[str] = set()
    arms = artifact.get("arms")
    if not isinstance(arms, Mapping):
        return hashes
    for arm in _base.ARMS:
        cell = arms.get(arm)
        if not isinstance(cell, Mapping):
            continue
        rows = cell.get("observations")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, Mapping) and isinstance(row.get("pixel_hash"), str):
                hashes.add(row["pixel_hash"])
    return hashes


def _comparison(artifact: Mapping[str, Any]) -> dict[str, Any]:
    by_arm: dict[str, dict[str, Any]] = {}
    arms = artifact.get("arms")
    for arm in _base.ARMS:
        cell = arms.get(arm) if isinstance(arms, Mapping) else None
        summary = cell.get("summary") if isinstance(cell, Mapping) else None
        if not isinstance(summary, Mapping):
            summary = {
                "sample_count": 0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "max_ms": 0.0,
            }
        by_arm[arm] = {
            key: summary.get(key, 0.0 if key != "sample_count" else 0)
            for key in ("sample_count", "p50_ms", "p95_ms", "p99_ms", "max_ms")
        }
    return {"by_arm": by_arm}


def _invalid_artifact(*, fixture_identity: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "status": "rejected",
        "passed": False,
        "geometry": dict(_base.REGION),
        **SCOPE_CONTRACT,
        "fixture_identity": fixture_identity,
        "configured_resources": {label: dict(resources) for label, resources in CPU_RUNS.items()},
        "source_identity": None,
        "runs": {},
        "comparison": {},
        "target_attestation": {},
        "cgroup_attribution": {},
        "attributable": False,
        "cross_cpu_pixel_hash_parity": False,
        "failure_type": "EvidenceValidationError",
        "failure_phase": "artifact_validation",
    }


def build_artifact(
    runs: Mapping[str, Mapping[str, Any]],
    *,
    fixture_identity: str,
    source_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate two fixed-resource child results into one safe artifact."""

    try:
        if set(runs) != set(CPU_RUNS):
            raise ValueError("CPU ablation requires exactly 1CPU and 2CPU runs")
        fixture = _validate_fixture_identity(fixture_identity)
        source = _validate_source_identity(source_identity)
        retained_runs: dict[str, Any] = {}
        pixel_sets: list[set[str]] = []
        target_attestation: dict[str, str] = {}
        cgroup_attribution: dict[str, bool] = {}
        provenance_identity: tuple[object, ...] | None = None
        prepared: dict[
            str, tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], str]
        ] = {}
        for label, expected in CPU_RUNS.items():
            run = runs[label]
            if not isinstance(run, Mapping) or set(run) != {
                "observation",
                "cleanup",
                "provenance",
                "sandbox_id",
            }:
                raise ValueError("invalid CPU run envelope")
            observation = run["observation"]
            if not isinstance(observation, Mapping):
                raise ValueError("invalid CPU child observation")
            sandbox_id = _validate_sandbox_id(run["sandbox_id"])
            _validate_resources(observation.get("configured_resources"), expected)
            provenance = run["provenance"]
            if not isinstance(provenance, Mapping):
                raise ValueError("invalid CPU provenance")
            _validate_resources(
                {
                    "cpu": provenance.get("configured_cpu"),
                    "memory_bytes": provenance.get("configured_memory_bytes"),
                },
                expected,
            )
            prepared[label] = (
                observation,
                run["cleanup"] if isinstance(run["cleanup"], Mapping) else {},
                provenance,
                sandbox_id,
            )
        for label, expected in CPU_RUNS.items():
            observation, cleanup, provenance, sandbox_id = prepared[label]
            nested = _base.build_artifact(
                observation,
                cleanup,
                provenance,
                configured_resources=expected,
            )
            if not isinstance(nested, Mapping):
                raise ValueError("invalid child artifact")
            if nested.get("configured_resources") != dict(expected):
                raise ValueError("child artifact resource identity changed")
            child_complete = nested.get("status") == "complete"
            if child_complete:
                if not source:
                    raise ValueError("complete CPU child requires source identity")
                _validate_nested_source(nested, source)
            if nested.get("geometry") != dict(_base.REGION):
                raise ValueError("CPU runs used different capture geometry")
            pixel_sets.append(_pixel_hashes(nested))
            target_attestation[label] = sandbox_id
            cgroup_attribution[label] = child_complete and _cgroup_attributable(
                nested, expected
            )
            nested_provenance = nested.get("provenance")
            if child_complete:
                if not isinstance(nested_provenance, Mapping):
                    raise ValueError("child artifact has no provenance")
                identity = tuple(
                    nested_provenance.get(key)
                    for key in (
                        "source_revision",
                        "x11_shm_source_sha256",
                        "cargo_lock_sha256",
                        "image_identity",
                    )
                )
                if provenance_identity is None:
                    provenance_identity = identity
                elif provenance_identity != identity:
                    raise ValueError("CPU runs used different source provenance")
            retained_runs[label] = {
                "configured_resources": dict(expected),
                "sandbox_id": sandbox_id,
                "status": nested.get("status"),
                "passed": nested.get("passed") is True,
                "attributable": cgroup_attribution[label],
                "comparison": _comparison(nested),
                "artifact": dict(nested),
            }
        parity = len(pixel_sets) == 2 and pixel_sets[0] == pixel_sets[1]
        complete = all(run["passed"] for run in retained_runs.values())
        attributable = all(cgroup_attribution.values())
        fresh_targets = len(set(target_attestation.values())) == 2
        measurement_complete = complete and parity and fresh_targets
        passed = measurement_complete and attributable
        status = (
            "complete"
            if passed
            else "exploratory"
            if measurement_complete
            else "rejected"
        )
        child_failure = next(
            (
                nested["artifact"].get("failure_type")
                for nested in retained_runs.values()
                if isinstance(nested.get("artifact"), Mapping)
                and nested["artifact"].get("failure_type") in _SAFE_FAILURE_TYPES
            ),
            None,
        )
        failure_type = (
            None
            if passed
            else "CgroupEvidenceUnavailable"
            if measurement_complete and not attributable
            else child_failure or "EvidenceValidationError"
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "benchmark": BENCHMARK_NAME,
            "status": status,
            "passed": passed,
            "geometry": dict(_base.REGION),
            **SCOPE_CONTRACT,
            "fixture_identity": fixture,
            "configured_resources": {
                label: dict(resources) for label, resources in CPU_RUNS.items()
            },
            "source_identity": source,
            "runs": retained_runs,
            "comparison": {label: retained_runs[label]["comparison"] for label in CPU_RUNS},
            "target_attestation": target_attestation,
            "cgroup_attribution": cgroup_attribution,
            "attributable": attributable,
            "cross_cpu_pixel_hash_parity": parity,
            "failure_type": failure_type,
            "failure_phase": None if passed else "summary",
        }
    except (TypeError, ValueError, KeyError, OverflowError):
        safe_fixture: str | None
        try:
            safe_fixture = _validate_fixture_identity(fixture_identity)
        except (TypeError, ValueError):
            safe_fixture = None
        return _invalid_artifact(fixture_identity=safe_fixture)


def fixture_identity(html: str) -> str:
    """Return a secret-free identity for the mounted deterministic fixture."""

    return hashlib.sha256(html.encode("utf-8")).hexdigest()
