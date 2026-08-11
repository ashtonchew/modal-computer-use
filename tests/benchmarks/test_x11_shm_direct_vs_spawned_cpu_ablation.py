from __future__ import annotations

import pytest

from modal_computer_use.benchmarks import x11_shm_direct_vs_spawned_cpu_ablation as probe


def _run(*, cpu: float, passed: bool = True) -> dict[str, object]:
    return {
        "observation": {
            "passed": passed,
            "configured_resources": {
                "cpu": cpu,
                "memory_bytes": 2048 * 1024**2,
            },
        },
        "cleanup": {"succeeded": True, "remaining_sandboxes": 0},
        "provenance": {
            "configured_cpu": cpu,
            "configured_memory_bytes": 2048 * 1024**2,
        },
        "sandbox_id": f"sb-{int(cpu)}",
    }


def test_cpu_ablation_contract_has_two_exact_resource_runs(monkeypatch) -> None:
    calls: list[tuple[float, dict[str, object]]] = []

    def fake_build(observation, cleanup, provenance, *, configured_resources=None):
        assert configured_resources is not None
        calls.append((configured_resources["cpu"], dict(observation)))
        return {
            "schema_version": "x11-shm-direct-vs-spawned.v3",
            "benchmark": "x11-shm-direct-vs-spawned",
            "status": "complete",
            "passed": True,
            "geometry": {"x": 7, "y": 9, "width": 511, "height": 383},
            "configured_resources": dict(configured_resources),
            "module_identity": {"module_sha256": "a" * 64},
            "target_identity": {
                "module_sha256": "a" * 64,
                "image_object_id": "im-test",
                "cgroup_available": True,
                "cpu": configured_resources["cpu"],
                "quota_usec": int(configured_resources["cpu"] * 100_000),
                "period_usec": 100_000,
                "memory_bytes": 2048 * 1024**2,
                "cgroup_version": "v2",
                "cgroup_resolution": "namespace-root",
            },
            "worker_cgroup_same": True,
            "provenance": {
                "source_revision": "d" * 40,
                "x11_shm_source_sha256": "e" * 64,
                "cargo_lock_sha256": "f" * 64,
                "image_identity": "inline:browser-chromium-x11-shm",
            },
            "arms": {
                arm: {
                    "observations": [{"elapsed_ms": 1.0, "pixel_hash": "b" * 64}],
                    "summary": {
                        "sample_count": 1,
                        "p50_ms": 1.0,
                        "p95_ms": 1.0,
                        "p99_ms": 1.0,
                        "max_ms": 1.0,
                    },
                }
                for arm in ("direct_native", "spawned_worker")
            },
            "retries": 0,
            "replacement_samples": 0,
            "session_cleanup": {"succeeded": True, "error_types": []},
        }

    monkeypatch.setattr(probe._base, "build_artifact", fake_build)
    runs = {label: _run(cpu=resources["cpu"]) for label, resources in probe.CPU_RUNS.items()}

    artifact = probe.build_artifact(
        runs,
        fixture_identity="f" * 64,
        source_identity={"module_sha256": "a" * 64, "image_object_id": "im-test"},
        execution_order=["cpu_1", "cpu_2"],
    )

    assert artifact["status"] == "complete"
    assert artifact["passed"] is True
    assert set(artifact["runs"]) == {"cpu_1", "cpu_2"}
    assert artifact["configured_resources"] == {
        "cpu_1": {"cpu": 1.0, "memory_bytes": 2048 * 1024**2},
        "cpu_2": {"cpu": 2.0, "memory_bytes": 2048 * 1024**2},
    }
    assert [cpu for cpu, _ in calls] == [1.0, 2.0]
    assert artifact["execution_order"] == ["cpu_1", "cpu_2"]


def test_cpu_ablation_rejects_invalid_execution_order(monkeypatch) -> None:
    monkeypatch.setattr(
        probe._base,
        "build_artifact",
        lambda *_args, **_kwargs: pytest.fail("invalid order must fail before child validation"),
    )
    runs = {label: _run(cpu=resources["cpu"]) for label, resources in probe.CPU_RUNS.items()}

    artifact = probe.build_artifact(
        runs,
        fixture_identity="f" * 64,
        source_identity={"module_sha256": "a" * 64, "image_object_id": "im-test"},
        execution_order=["cpu_1", "cpu_1"],
    )

    assert artifact["status"] == "rejected"
    assert artifact["execution_order"] is None


def test_cpu_ablation_rejects_wrong_child_resource_claim(monkeypatch) -> None:
    monkeypatch.setattr(
        probe._base,
        "build_artifact",
        lambda *_args, **_kwargs: pytest.fail("wrong child claim must fail before validation"),
    )
    runs = {label: _run(cpu=resources["cpu"]) for label, resources in probe.CPU_RUNS.items()}
    runs["cpu_2"]["observation"] = {"passed": True, "configured_resources": {"cpu": 1.0}}

    artifact = probe.build_artifact(
        runs,
        fixture_identity="f" * 64,
        source_identity={"module_sha256": "a" * 64, "image_object_id": "im-test"},
        execution_order=["cpu_1", "cpu_2"],
    )

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"
    assert artifact["runs"] == {}


def test_cpu_ablation_rejects_mismatched_source_identity(monkeypatch) -> None:
    def fake_build(observation, cleanup, provenance, *, configured_resources=None):
        return {
            "schema_version": "x11-shm-direct-vs-spawned.v3",
            "benchmark": "x11-shm-direct-vs-spawned",
            "status": "complete",
            "passed": True,
            "geometry": {"x": 7, "y": 9, "width": 511, "height": 383},
            "configured_resources": dict(configured_resources),
            "module_identity": {
                "module_sha256": "c" * 64 if configured_resources["cpu"] == 2.0 else "a" * 64
            },
            "target_identity": {
                "module_sha256": (
                    "c" * 64 if configured_resources["cpu"] == 2.0 else "a" * 64
                ),
                "image_object_id": "im-test",
                "cgroup_available": True,
                "cpu": configured_resources["cpu"],
                "quota_usec": int(configured_resources["cpu"] * 100_000),
                "period_usec": 100_000,
                "memory_bytes": 2048 * 1024**2,
                "cgroup_version": "v2",
                "cgroup_resolution": "namespace-root",
            },
            "worker_cgroup_same": True,
            "provenance": {
                "source_revision": "d" * 40,
                "x11_shm_source_sha256": "e" * 64,
                "cargo_lock_sha256": "f" * 64,
                "image_identity": "inline:browser-chromium-x11-shm",
            },
            "arms": {},
            "retries": 0,
            "replacement_samples": 0,
            "session_cleanup": {"succeeded": True, "error_types": []},
        }

    monkeypatch.setattr(probe._base, "build_artifact", fake_build)
    runs = {label: _run(cpu=resources["cpu"]) for label, resources in probe.CPU_RUNS.items()}
    artifact = probe.build_artifact(
        runs,
        fixture_identity="f" * 64,
        source_identity={"module_sha256": "a" * 64, "image_object_id": "im-test"},
        execution_order=["cpu_1", "cpu_2"],
    )

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_cpu_ablation_marks_missing_cgroup_attribution_exploratory(monkeypatch) -> None:
    def fake_build(observation, cleanup, provenance, *, configured_resources=None):
        return {
            "status": "complete",
            "passed": True,
            "geometry": {"x": 7, "y": 9, "width": 511, "height": 383},
            "configured_resources": dict(configured_resources),
            "module_identity": {"module_sha256": "a" * 64},
            "target_identity": {
                "module_sha256": "a" * 64,
                "image_object_id": "im-test",
                "cgroup_available": False,
                "cpu": None,
                "quota_usec": None,
                "period_usec": None,
                "memory_bytes": None,
                "cgroup_version": None,
                "cgroup_resolution": None,
            },
            "worker_cgroup_same": None,
            "provenance": {
                "source_revision": "d" * 40,
                "x11_shm_source_sha256": "e" * 64,
                "cargo_lock_sha256": "f" * 64,
                "image_identity": "inline:browser-chromium-x11-shm",
            },
            "arms": {},
        }

    monkeypatch.setattr(probe._base, "build_artifact", fake_build)
    runs = {label: _run(cpu=resources["cpu"]) for label, resources in probe.CPU_RUNS.items()}

    artifact = probe.build_artifact(
        runs,
        fixture_identity="f" * 64,
        source_identity={"module_sha256": "a" * 64, "image_object_id": "im-test"},
        execution_order=["cpu_1", "cpu_2"],
    )

    assert artifact["status"] == "exploratory"
    assert artifact["passed"] is False
    assert artifact["attributable"] is False
    assert artifact["failure_type"] == "CgroupEvidenceUnavailable"

    runs["cpu_2"]["sandbox_id"] = runs["cpu_1"]["sandbox_id"]
    duplicate_target = probe.build_artifact(
        runs,
        fixture_identity="f" * 64,
        source_identity={"module_sha256": "a" * 64, "image_object_id": "im-test"},
        execution_order=["cpu_1", "cpu_2"],
    )
    assert duplicate_target["status"] == "rejected"
    assert duplicate_target["failure_type"] == "EvidenceValidationError"


def test_cpu_ablation_sanitizes_invalid_fixture_identity(monkeypatch) -> None:
    artifact = probe.build_artifact(
        {},
        fixture_identity="not-a-hash",
        source_identity={},
        execution_order=["cpu_1", "cpu_2"],
    )

    assert artifact["status"] == "rejected"
    assert artifact["fixture_identity"] is None
    assert artifact["source_identity"] is None
