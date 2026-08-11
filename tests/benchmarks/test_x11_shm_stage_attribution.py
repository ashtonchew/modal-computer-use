from __future__ import annotations

from types import SimpleNamespace

from modal_computer_use.benchmarks import x11_shm_stage_attribution as stage
from modal_computer_use.daemon.desktop.screenshot_capture import (
    NativeCaptureTiming,
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
            "memory_bytes": 2048 * 1024**2,
            "machine": "x86_64",
        },
        "worker_identity_before": {"pid": 7, "starttime_ticks": 11},
        "worker_identity_after": {"pid": 7, "starttime_ticks": 11},
        "worker_cgroup_same": True,
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


def test_stage_child_retains_safe_preflight_failure_phase(monkeypatch) -> None:
    def fail_cgroup_directory() -> None:
        raise RuntimeError("private target detail")

    monkeypatch.setattr(stage, "_cgroup_directory", fail_cgroup_directory)

    result = stage.run_child()

    assert result["passed"] is False
    assert result["failure_type"] == "RuntimeError"
    assert result["failure_phase"] == "cgroup_directory"


def test_stage_child_retains_first_capture_failure_phase(monkeypatch) -> None:
    class FakeSession:
        _session = SimpleNamespace(_process=SimpleNamespace(pid=42))

        def __init__(self, **_: object) -> None:
            pass

        def close(self) -> None:
            raise RuntimeError("private close detail")

    async def fail_capture(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private capture detail")

    monkeypatch.setattr(stage, "_cgroup_directory", lambda: None)
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
    monkeypatch.setattr(stage, "_unified_cgroup_path", lambda *args: "/same")
    monkeypatch.setattr(
        stage,
        "_cpu_stat",
        lambda _: {field: 0 for field in stage.CGROUP_FIELDS},
    )
    monkeypatch.setattr(stage, "_capture_once", fail_capture)

    result = stage.run_child()

    assert result["failure_type"] == "RuntimeError"
    assert result["failure_phase"] == "warmup_full_capture"


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
