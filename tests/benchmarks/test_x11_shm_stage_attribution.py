from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from modal_computer_use.benchmarks import x11_shm_stage_attribution as stage
from modal_computer_use.daemon.desktop.screenshot_capture import (
    NativeCaptureTiming,
    ScreenshotCaptureTimedOut,
)

PROVENANCE = {
    "source_revision": "a" * 40,
    "worktree_clean": True,
    "x11_shm_source_sha256": "b" * 64,
    "cargo_lock_sha256": "c" * 64,
    "image_identity": "inline:browser-chromium-x11-shm",
}
TERMINAL_CLEANUP = {
    "succeeded": True,
    "remaining_sandboxes": 0,
    "survivors_before_sweep": 0,
    "cleanup_error_types": [],
}


def _row(index: int) -> dict[str, object]:
    values = {metric: 0.1 for metric in stage.STAGE_METRICS}
    values["controller_total_ms"] = 60.0 if index == 0 else 10.0
    values["parent_total_ms"] = 1.0
    values["native_total_ms"] = 0.5
    values["native_residual_ms"] = 0.2
    values["parent_outside_io_ms"] = 0.6
    values["controller_boundary_residual_ms"] = (
        values["controller_total_ms"] - values["executor_queue_ms"] - 1.0
    )
    return {
        "schedule_index": index,
        "lane": "full" if index % 2 == 0 else "region",
        **values,
        **{f"cgroup_{field}_delta": 0 for field in stage.CGROUP_FIELDS},
    }


def _observation() -> dict[str, object]:
    rows = [_row(index) for index in range(stage.CAPTURES)]
    return {
        "passed": True,
        "warmups_completed": stage.WARMUPS,
        "captures_completed": stage.CAPTURES,
        "full_captures": stage.CAPTURES // 2,
        "region_captures": stage.CAPTURES // 2,
        "frame_stable_by_lane": True,
        "module_identity": dict(stage.EXPECTED_MODULE_IDENTITY),
        "target_identity": {
            "backend": "x11-shm",
            "codec": "png-deflate-level1-no-filter",
            "module_sha256": "d" * 64,
            "image_object_id": "im-test",
            "cpu": 1.0,
            "quota_usec": 100_000,
            "period_usec": 100_000,
            "memory_bytes": 2048 * 1024**2,
            "machine": "x86_64",
        },
        "worker_identity_before": {"pid": 7, "starttime_ticks": 11},
        "worker_identity_after": {"pid": 7, "starttime_ticks": 11},
        "worker_cgroup_same": True,
        "cgroup_available": True,
        "cgroup_version": "v1",
        "cpu_max": {"quota_usec": 100_000, "period_usec": 100_000},
        "cgroup_cpu_stat_before": {field: 0 for field in stage.CGROUP_FIELDS},
        "cgroup_cpu_stat_after": {field: 0 for field in stage.CGROUP_FIELDS},
        "cgroup_cpu_stat_delta": {field: 0 for field in stage.CGROUP_FIELDS},
        "payload_bytes": {
            "full": {"min": 10, "max": 10},
            "region": {"min": 5, "max": 5},
        },
        "summaries": {
            "combined": stage._summarize(rows),
            "full": stage._summarize(rows[::2]),
            "region": stage._summarize(rows[1::2]),
        },
        "tail_schedule": [
            {
                **rows[0],
                "owner_over_50": "executor_resume_or_boundary",
                "owner_over_500": None,
            }
        ],
        "failure_type": None,
        "failure_phase": None,
    }


def test_stage_row_preserves_nested_timing_algebra() -> None:
    timing = NativeCaptureTiming(
        x11_reply_ns=11,
        rgb_convert_ns=13,
        png_encode_ns=17,
        native_total_ns=47,
        worker_dispatch_ns=2,
        worker_response_prep_ns=3,
        parent_lock_wait_ns=5,
        parent_send_ns=7,
        parent_header_wait_ns=60,
        parent_payload_read_ns=11,
        parent_total_ns=100,
    )

    row = stage._stage_row(
        timing, controller_total_ns=130, executor_queue_ns=10
    )

    assert row["native_residual_ms"] == 6 / 1_000_000
    assert row["parent_outside_io_ms"] == 17 / 1_000_000
    assert row["controller_boundary_residual_ms"] == 20 / 1_000_000


def test_cpu_cgroup_path_supports_v1_and_v2() -> None:
    assert stage._parse_cpu_cgroup_paths(
        "0::/unified\n2:cpu:/legacy-cpu\n3:cpuacct:/legacy-usage\n"
    ) == (
        ("v2", "/unified"),
        ("v1", "/legacy-cpu"),
        ("v1_cpuacct", "/legacy-usage"),
    )


def test_namespaced_parent_cgroup_uses_only_verified_hierarchy_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "cpu.max").write_text("100000 100000\n", encoding="utf-8")
    (tmp_path / "cpu.stat").write_text("usage_usec 0\n", encoding="utf-8")
    membership = (("v2", "/../../private"),)

    (tmp_path / "cgroup.procs").write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert stage._select_cpu_cgroup_source(tmp_path, membership) == (
        stage._CpuCgroupSource("v2", tmp_path, None)
    )

    (tmp_path / "cgroup.procs").write_text("99999999\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unavailable"):
        stage._select_cpu_cgroup_source(tmp_path, membership)


def test_v1_cpu_cgroup_normalizes_quota_and_counters(tmp_path: Path) -> None:
    cpu_directory = tmp_path / "cpu"
    cpu_directory.mkdir()
    usage_file = tmp_path / "cpuacct" / "cpuacct.usage"
    usage_file.parent.mkdir()
    (cpu_directory / "cpu.cfs_quota_us").write_text("100000\n", encoding="utf-8")
    (cpu_directory / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    usage_file.write_text("7000\n", encoding="utf-8")
    (cpu_directory / "cpu.stat").write_text(
        "nr_periods 11\nnr_throttled 3\nthrottled_time 5000\n",
        encoding="utf-8",
    )
    source = stage._CpuCgroupSource(
        version="v1",
        directory=cpu_directory,
        usage_file=usage_file,
    )

    assert stage._cpu_max(source) == {
        "quota_usec": 100_000,
        "period_usec": 100_000,
    }
    assert stage._cpu_stat(source) == {
        "usage_usec": 7,
        "nr_periods": 11,
        "nr_throttled": 3,
        "throttled_usec": 5,
    }


def test_hybrid_cgroup_probe_selects_available_v1_cpu_files(tmp_path: Path) -> None:
    cpu_directory = tmp_path / "cpu" / "legacy-cpu"
    cpu_directory.mkdir(parents=True)
    (cpu_directory / "cpu.cfs_quota_us").write_text("100000\n", encoding="utf-8")
    (cpu_directory / "cpu.stat").write_text("nr_periods 0\n", encoding="utf-8")
    usage_file = tmp_path / "cpuacct" / "legacy-usage" / "cpuacct.usage"
    usage_file.parent.mkdir(parents=True)
    usage_file.write_text("0\n", encoding="utf-8")

    source = stage._select_cpu_cgroup_source(
        tmp_path,
        (
            ("v2", "/unified"),
            ("v1", "/legacy-cpu"),
            ("v1_cpuacct", "/legacy-usage"),
        ),
    )

    assert source == stage._CpuCgroupSource("v1", cpu_directory, usage_file)


def test_stage_child_continues_when_cgroup_counters_are_unavailable(monkeypatch) -> None:
    def fail_cgroup_directory() -> None:
        raise stage._CgroupEvidenceUnavailable("private target detail")

    monkeypatch.setattr(stage, "_cgroup_source", fail_cgroup_directory)
    monkeypatch.setattr(
        stage.importlib,
        "import_module",
        lambda _: (_ for _ in ()).throw(ModuleNotFoundError()),
    )

    result = stage.run_child()

    assert result["passed"] is False
    assert result["failure_type"] == "ModuleNotFoundError"
    assert result["failure_phase"] == "native_import"


def test_stage_child_does_not_downgrade_invalid_cpu_quota(monkeypatch) -> None:
    monkeypatch.setattr(
        stage,
        "_cgroup_source",
        lambda: stage._CpuCgroupSource("v1", Path("/unused"), None),
    )
    monkeypatch.setattr(
        stage,
        "_cpu_max",
        lambda _: (_ for _ in ()).throw(RuntimeError("invalid quota")),
    )

    result = stage.run_child()

    assert result["failure_type"] == "RuntimeError"
    assert result["failure_phase"] == "cpu_limit"


def test_unavailable_cgroup_evidence_requires_explicit_null_fields() -> None:
    observation = _observation()
    observation.update(
        {
            "cgroup_available": False,
            "worker_cgroup_same": None,
            "cgroup_version": None,
            "cpu_max": None,
            "cgroup_cpu_stat_before": None,
            "cgroup_cpu_stat_after": None,
            "cgroup_cpu_stat_delta": None,
        }
    )
    for row in observation["tail_schedule"]:
        for field in stage.CGROUP_FIELDS:
            row[f"cgroup_{field}_delta"] = None
    observation.pop("cpu_max")

    artifact = stage.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_unavailable_cgroup_tail_requires_explicit_null_fields() -> None:
    observation = _observation()
    observation.update(
        {
            "cgroup_available": False,
            "worker_cgroup_same": None,
            "cgroup_version": None,
            "cpu_max": None,
            "cgroup_cpu_stat_before": None,
            "cgroup_cpu_stat_after": None,
            "cgroup_cpu_stat_delta": None,
        }
    )
    for row in observation["tail_schedule"]:
        for field in stage.CGROUP_FIELDS:
            row[f"cgroup_{field}_delta"] = None
    observation["tail_schedule"][0].pop("cgroup_usage_usec_delta")

    artifact = stage.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_target_identity_requires_exact_integer_cpu_quota() -> None:
    observation = _observation()
    observation["target_identity"] = {
        **observation["target_identity"],
        "cpu": 1.0,
        "quota_usec": 100_001,
        "period_usec": 100_000,
    }

    artifact = stage.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_stage_child_retains_first_capture_failure_phase(monkeypatch) -> None:
    class FakeSession:
        _session = SimpleNamespace(_process=SimpleNamespace(pid=42))

        def __init__(self, **_: object) -> None:
            pass

        def close(self) -> None:
            raise RuntimeError("private close detail")

    async def fail_capture(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private capture detail")

    monkeypatch.setattr(
        stage,
        "_cgroup_source",
        lambda: stage._CpuCgroupSource("v1", Path("/unused"), None),
    )
    monkeypatch.setattr(
        stage,
        "_cpu_max",
        lambda _: {"quota_usec": 100_000, "period_usec": 100_000},
    )
    monkeypatch.setattr(
        stage.importlib,
        "import_module",
        lambda _: SimpleNamespace(**stage.EXPECTED_MODULE_IDENTITY),
    )
    monkeypatch.setattr(stage, "X11SharedMemoryScreenshotSession", FakeSession)
    monkeypatch.setattr(
        stage,
        "_process_identity",
        lambda _: {"pid": 42, "starttime_ticks": 7},
    )
    monkeypatch.setattr(
        stage,
        "_cpu_cgroup_paths",
        lambda *args: (("v1", "/same"),),
    )
    monkeypatch.setattr(
        stage,
        "_cpu_stat",
        lambda _: {field: 0 for field in stage.CGROUP_FIELDS},
    )
    monkeypatch.setattr(stage, "_capture_once", fail_capture)

    result = stage.run_child()

    assert result["failure_type"] == "RuntimeError"
    assert result["failure_phase"] == "warmup_full_capture"


def test_stage_child_retains_timeout_origin_and_completed_counts(monkeypatch) -> None:
    async def fail_after_progress(
        *, captures: int, warmups: int, progress: stage._StageProgress
    ) -> dict[str, object]:
        assert captures == stage.CAPTURES
        assert warmups == stage.WARMUPS
        progress.warmups_completed = stage.WARMUPS
        progress.captures_completed = 1
        progress.full_captures = 1
        progress.region_captures = 0
        progress.phase = "measured_region_capture"
        raise ScreenshotCaptureTimedOut(
            "private timeout detail",
            timeout_origin="native_x11_reply_deadline",
        )

    monkeypatch.setattr(stage, "_run_child_inner", fail_after_progress)

    result = stage.run_child()

    assert result == {
        "passed": False,
        "warmups_completed": stage.WARMUPS,
        "captures_completed": 1,
        "full_captures": 1,
        "region_captures": 0,
        "failure_type": "ScreenshotCaptureTimedOut",
        "failure_phase": "measured_region_capture",
        "failure_timeout_origin": "native_x11_reply_deadline",
    }


def test_rejected_stage_artifact_retains_safe_timeout_origin() -> None:
    observation = {
        "passed": False,
        "warmups_completed": stage.WARMUPS,
        "captures_completed": 1,
        "full_captures": 1,
        "region_captures": 0,
        "failure_type": "ScreenshotCaptureTimedOut",
        "failure_phase": "measured_region_capture",
        "failure_timeout_origin": "worker_process_deadline",
        "target_identity": _observation()["target_identity"],
        "cgroup_available": False,
        "cgroup_version": None,
    }

    artifact = stage.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["warmups_completed"] == stage.WARMUPS
    assert artifact["captures_completed"] == 1
    assert artifact["failure_timeout_origin"] == "worker_process_deadline"
    assert artifact["target_identity"]["image_object_id"] == "im-test"
    assert artifact["cgroup_scope"] == "unavailable"
    assert artifact["daemon_requested_source"] == "mss"


def test_rejected_stage_artifact_rejects_inconsistent_capture_phase() -> None:
    observation = {
        "passed": False,
        "warmups_completed": stage.WARMUPS,
        "captures_completed": 1,
        "full_captures": 1,
        "region_captures": 0,
        "failure_type": "ScreenshotCaptureTimedOut",
        "failure_phase": "measured_full_capture",
        "failure_timeout_origin": "native_x11_reply_deadline",
    }

    artifact = stage.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"
    assert artifact["failure_timeout_origin"] is None


def test_rejected_child_stage_artifact_rejects_unknown_safe_phase() -> None:
    observation = {
        "passed": False,
        "warmups_completed": 0,
        "captures_completed": 0,
        "full_captures": 0,
        "region_captures": 0,
        "failure_type": "RuntimeError",
        "failure_phase": "plausible_but_unknown",
        "target_identity": _observation()["target_identity"],
    }

    artifact = stage.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_complete_stage_artifact_requires_fixed_safe_contract() -> None:
    artifact = stage.build_artifact(
        _observation(),
        TERMINAL_CLEANUP,
        PROVENANCE,
    )

    assert artifact["passed"] is True
    assert artifact["non_gating"] is True
    assert artifact["promotion_proxy"] is False
    assert artifact["captures_completed"] == stage.CAPTURES
    assert artifact["cgroup_version"] == "v1"
    assert artifact["tail_schedule"][0]["owner_over_50"] == (
        "executor_resume_or_boundary"
    )
    forbidden = {"raw", "body", "png", "bytes", "token", "url", "path", "command"}

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {
                key for child in value.values() for key in keys(child)
            }
        if isinstance(value, list):
            return {key for child in value for key in keys(child)}
        return set()

    assert not keys(artifact) & forbidden


def test_complete_stage_artifact_can_disclose_unavailable_cgroup_counters() -> None:
    observation = _observation()
    observation.update(
        {
            "cgroup_available": False,
            "worker_cgroup_same": None,
            "cgroup_version": None,
            "cpu_max": None,
            "cgroup_cpu_stat_before": None,
            "cgroup_cpu_stat_after": None,
            "cgroup_cpu_stat_delta": None,
        }
    )
    for row in observation["tail_schedule"]:
        for field in stage.CGROUP_FIELDS:
            row[f"cgroup_{field}_delta"] = None

    artifact = stage.build_artifact(
        observation,
        TERMINAL_CLEANUP,
        PROVENANCE,
    )

    assert artifact["passed"] is True
    assert artifact["cgroup_available"] is False
    assert artifact["cgroup_version"] is None
    assert artifact["cgroup_cpu_stat_delta"] is None
    assert artifact["schema_version"] == "x11-shm-stage-attribution.v3"


def test_stage_artifact_rejects_mixed_unavailable_cgroup_evidence() -> None:
    observation = _observation()
    observation.update(
        {
            "cgroup_available": False,
            "worker_cgroup_same": None,
            "cgroup_version": None,
            "cpu_max": None,
            "cgroup_cpu_stat_before": None,
            "cgroup_cpu_stat_after": None,
            "cgroup_cpu_stat_delta": {field: 0 for field in stage.CGROUP_FIELDS},
        }
    )

    artifact = stage.build_artifact(
        observation,
        TERMINAL_CLEANUP,
        PROVENANCE,
    )

    assert artifact["passed"] is False
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_stage_artifact_rejects_malformed_tail_owner() -> None:
    observation = _observation()
    observation["tail_schedule"][0]["owner_over_50"] = "secret-path"

    artifact = stage.build_artifact(
        observation,
        TERMINAL_CLEANUP,
        PROVENANCE,
    )

    assert artifact["passed"] is False
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_stage_artifact_rejects_impossible_tail_algebra() -> None:
    observation = _observation()
    observation["tail_schedule"][0]["native_total_ms"] = 0.1
    observation["tail_schedule"][0]["x11_reply_ms"] = 1.0

    artifact = stage.build_artifact(
        observation,
        TERMINAL_CLEANUP,
        PROVENANCE,
    )

    assert artifact["status"] == "rejected"
    assert artifact["passed"] is False
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_stage_artifact_rejects_fabricated_derived_residual() -> None:
    observation = _observation()
    observation["tail_schedule"][0]["controller_boundary_residual_ms"] = 100.0
    observation["tail_schedule"][0]["owner_over_50"] = "executor_resume_or_boundary"

    artifact = stage.build_artifact(
        observation,
        TERMINAL_CLEANUP,
        PROVENANCE,
    )

    assert artifact["status"] == "rejected"
    assert artifact["passed"] is False
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_stage_artifact_rejects_unbounded_numeric_evidence() -> None:
    observation = _observation()
    observation["summaries"]["combined"]["metrics"]["x11_reply_ms"][
        "p50_ms"
    ] = 10**1000

    artifact = stage.build_artifact(
        observation,
        TERMINAL_CLEANUP,
        PROVENANCE,
    )

    assert artifact["status"] == "rejected"
    assert artifact["passed"] is False
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_stage_artifact_rejects_unattested_provenance_without_retaining_extras() -> None:
    provenance = {**PROVENANCE, "worktree_clean": False, "token": "private"}

    artifact = stage.build_artifact(
        _observation(),
        TERMINAL_CLEANUP,
        provenance,
    )

    assert artifact["passed"] is False
    assert artifact["failure_type"] == "EvidenceValidationError"
    assert artifact["provenance"] is None
    assert "token" not in artifact
