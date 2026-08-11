from __future__ import annotations

import asyncio
import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from modal_computer_use.benchmarks import x11_shm_direct_vs_spawned as probe
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
    "configured_cpu": 1.0,
    "configured_memory_bytes": 2048 * 1024**2,
}
TARGET_IDENTITY = {
    "backend": "x11-shm",
    "codec": "png-deflate-level1-no-filter",
    "codec_runtime": "in-process-miniz_oxide",
    "codec_library": "in-process",
    "module_sha256": "d" * 64,
    "image_object_id": "im-test",
    "cpu": 1.0,
    "quota_usec": 100_000,
    "period_usec": 100_000,
    "memory_bytes": 2048 * 1024**2,
    "cgroup_available": True,
    "cgroup_version": "v2",
    "cgroup_resolution": "namespace-root",
    "machine": "x86_64",
    "display": ":99",
    "width": probe.WIDTH,
    "height": probe.HEIGHT,
}
TERMINAL_CLEANUP = {
    "succeeded": True,
    "remaining_sandboxes": 0,
    "survivors_before_sweep": 0,
    "cleanup_error_types": [],
}


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (probe.REGION["width"], probe.REGION["height"]), (255, 255, 255)).save(
        output, format="PNG"
    )
    return output.getvalue()


def _timing() -> NativeCaptureTiming:
    return NativeCaptureTiming(
        x11_reply_ns=11,
        rgb_convert_ns=13,
        png_encode_ns=17,
        native_total_ns=47,
        worker_dispatch_ns=2,
        worker_response_prep_ns=3,
        parent_lock_wait_ns=5,
        parent_send_ns=7,
        parent_header_wait_ns=100,
        parent_payload_read_ns=13,
        parent_total_ns=130,
    )


def _timing_row(*, direct: bool = False) -> dict[str, float]:
    timing = _timing()
    values = {
        "executor_queue_ms": 0.001,
        "x11_reply_ms": timing.x11_reply_ns / 1_000_000,
        "rgb_convert_ms": timing.rgb_convert_ns / 1_000_000,
        "png_encode_ms": timing.png_encode_ns / 1_000_000,
        "native_total_ms": timing.native_total_ns / 1_000_000,
        "worker_dispatch_ms": timing.worker_dispatch_ns / 1_000_000,
        "worker_response_prep_ms": timing.worker_response_prep_ns / 1_000_000,
        "parent_lock_wait_ms": timing.parent_lock_wait_ns / 1_000_000,
        "parent_send_ms": timing.parent_send_ns / 1_000_000,
        "parent_header_wait_ms": timing.parent_header_wait_ns / 1_000_000,
        "parent_payload_read_ms": timing.parent_payload_read_ns / 1_000_000,
        "parent_total_ms": timing.parent_total_ns / 1_000_000,
    }
    if direct:
        values.update(
            {
                "worker_dispatch_ms": 0.0,
                "worker_response_prep_ms": 0.0,
                "parent_lock_wait_ms": 0.0,
                "parent_send_ms": 0.0,
                "parent_header_wait_ms": 0.0,
                "parent_payload_read_ms": 0.0,
                "parent_total_ms": values["native_total_ms"],
            }
        )
    return values


def _observation(*, passed: bool = True) -> dict[str, object]:
    rows = []
    for pair_index in range(probe.PAIRS):
        order = probe.pair_order(pair_index)
        for position, arm in enumerate(order):
            rows.append(
                {
                    "pair_index": pair_index,
                    "sequence": pair_index * 2 + position,
                    "position": position,
                    "arm": arm,
                    "status": "ok",
                    "payload_bytes": 100,
                    "elapsed_ms": 1.0,
                    "png_width": probe.REGION["width"],
                    "png_height": probe.REGION["height"],
                    "pixel_hash": "a" * 64,
                    "timing": _timing_row(direct=arm == probe.DIRECT_ARM),
                }
            )
    return {
        "passed": passed,
        **probe.SCOPE_CONTRACT,
        "configured_resources": dict(probe.CONFIGURED_RESOURCES),
        "display": ":99",
        "geometry": dict(probe.REGION),
        "module_identity": dict(probe.EXPECTED_MODULE_IDENTITY),
        "target_identity": TARGET_IDENTITY,
        "worker_cgroup_same": True,
        "schedule": probe.build_schedule(),
        "warmups_completed": {arm: probe.WARMUPS for arm in probe.ARMS},
        "captures_completed": {arm: probe.PAIRS for arm in probe.ARMS},
        "paired_prefix_samples": probe.PAIRS,
        "unpaired_after_failure_samples": 0,
        "pixel_hash_parity": True,
        "arms": {
            arm: {
                "identity_before": {"pid": index + 10, "starttime_ticks": 20},
                "identity_after": {"pid": index + 10, "starttime_ticks": 20},
                "observations": [row for row in rows if row["arm"] == arm],
                "failure": None,
                "session_cleanup": {"succeeded": True, "error_types": []},
            }
            for index, arm in enumerate(probe.ARMS)
        },
        "retries": 0,
        "replacement_samples": 0,
        "session_cleanup": {"succeeded": True, "error_types": []},
    }


def test_pair_schedule_uses_only_failing_region_and_alternates_ab_ba() -> None:
    schedule = probe.build_schedule(pairs=4)

    assert [(entry["pair_index"], entry["arm"]) for entry in schedule] == [
        (0, "direct_native"),
        (0, "spawned_worker"),
        (1, "spawned_worker"),
        (1, "direct_native"),
        (2, "direct_native"),
        (2, "spawned_worker"),
        (3, "spawned_worker"),
        (3, "direct_native"),
    ]
    assert {tuple(entry["geometry"].values()) for entry in schedule} == {(7, 9, 511, 383)}


def test_artifact_rejects_partial_timeout_but_retains_safe_counts_and_origin() -> None:
    observation = _observation(passed=False)
    observation["paired_prefix_samples"] = 2
    observation["unpaired_after_failure_samples"] = probe.PAIRS - 2
    observation["first_unpaired_pair"] = 2
    observation["captures_completed"] = {
        "direct_native": 2,
        "spawned_worker": probe.PAIRS,
    }
    observation["arms"]["direct_native"]["observations"] = observation["arms"]["direct_native"][
        "observations"
    ][:2]
    observation["arms"]["direct_native"]["failure"] = {
        "pair_index": 2,
        "phase": "measured_capture",
        "failure_type": "X11ScreenshotTimeoutError",
        "timeout_origin": "native_x11_reply_deadline",
    }
    observation["arms"]["spawned_worker"]["failure"] = None
    observation["private_png"] = b"secret-png"

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "X11ScreenshotTimeoutError"
    assert artifact["failure_timeout_origin"] == "native_x11_reply_deadline"
    assert artifact["paired_prefix_samples"] == 2
    assert artifact["arms"]["direct_native"]["captures_completed"] == 2
    rendered = repr(artifact)
    assert "private_error" not in rendered
    assert "secret-png" not in rendered


def test_artifact_rejects_inconsistent_geometry_without_retaining_rows() -> None:
    observation = _observation()
    observation["geometry"] = {"x": 0, "y": 0, "width": 1, "height": 1}

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"
    assert artifact["arms"] == {}


def test_artifact_rejects_unsafe_runtime_identity_labels() -> None:
    observation = _observation()
    observation["target_identity"] = {
        **TARGET_IDENTITY,
        "display": ":99;private",
    }

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"
    assert artifact["target_identity"] is None


def test_artifact_drops_unrecognized_safe_looking_failure_phase() -> None:
    observation = _observation(passed=False)
    observation["failure_type"] = "RuntimeError"
    observation["failure_phase"] = "private_error"

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_phase"] is None


def test_artifact_retains_bounded_preflight_failure_envelope() -> None:
    observation = {
        "passed": False,
        **probe.SCOPE_CONTRACT,
        "warmups_completed": {arm: 0 for arm in probe.ARMS},
        "captures_completed": {arm: 0 for arm in probe.ARMS},
        "configured_resources": dict(probe.CONFIGURED_RESOURCES),
        "worker_cgroup_same": None,
        "paired_prefix_samples": 0,
        "unpaired_after_failure_samples": 0,
        "first_unpaired_pair": None,
        "pixel_hash_parity": False,
        "arms": {},
        "retries": 0,
        "replacement_samples": 0,
        "failure_type": "RuntimeError",
        "failure_phase": "target_cgroup_limits",
    }

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "RuntimeError"
    assert artifact["failure_phase"] == "target_cgroup_limits"


def test_runtime_limits_use_namespace_v2_membership_without_cpu_stat(
    monkeypatch, tmp_path: Path
) -> None:
    cgroup = tmp_path / "sandbox"
    cgroup.mkdir()
    (cgroup / "cpu.max").write_text("100000 100000\n", encoding="utf-8")
    (cgroup / "memory.max").write_text(str(2048 * 1024**2), encoding="utf-8")
    monkeypatch.setattr(
        probe,
        "_v2_cgroup_directory",
        lambda: (cgroup, "namespace-root"),
    )

    evidence = probe._runtime_limits()
    assert evidence.cgroup_available is True
    assert evidence.quota_usec == 100_000
    assert evidence.period_usec == 100_000
    assert evidence.memory_bytes == 2048 * 1024**2
    assert evidence.cgroup_version == "v2"
    assert evidence.cgroup_resolution == "namespace-root"


def test_v2_cgroup_directory_parses_root_and_nested_memberships(tmp_path: Path) -> None:
    root_mount = f"24 23 0:22 / {tmp_path} rw - cgroup2 cgroup rw\n"
    assert probe._v2_cgroup_directory("0::/\n", mountinfo_text=root_mount, root=tmp_path) == (
        tmp_path,
        "namespace-root",
    )
    assert probe._v2_cgroup_directory(
        "0::/sandbox/child\n",
        mountinfo_text=root_mount,
        root=tmp_path,
    ) == (
        tmp_path / "sandbox" / "child",
        "namespace-relative",
    )
    namespace_mount = f"24 23 0:22 /host/sandbox/child {tmp_path} rw - cgroup2 cgroup rw\n"
    assert probe._v2_cgroup_directory(
        "0::/\n",
        mountinfo_text=namespace_mount,
        root=tmp_path,
    ) == (tmp_path, "namespace-root")
    assert probe._v2_cgroup_directory(
        "0::/nested\n",
        mountinfo_text=namespace_mount,
        root=tmp_path,
    ) == (tmp_path / "nested", "namespace-relative")


@pytest.mark.parametrize(
    ("cpu_text", "memory_text", "expected_phase"),
    [
        ("max 100000", str(2048 * 1024**2), "target_cpu_limit"),
        ("100000 100000", "max", "target_memory_limit"),
        ("100000 200000", str(2048 * 1024**2), "target_limit_contract"),
        ("100000 100000", str(1024 * 1024**2), "target_limit_contract"),
    ],
)
def test_runtime_limits_report_bounded_preflight_phase(
    monkeypatch,
    tmp_path: Path,
    cpu_text: str | None,
    memory_text: str | None,
    expected_phase: str,
) -> None:
    cgroup = tmp_path / "sandbox"
    cgroup.mkdir()
    if cpu_text is not None:
        (cgroup / "cpu.max").write_text(cpu_text, encoding="utf-8")
    if memory_text is not None:
        (cgroup / "memory.max").write_text(memory_text, encoding="utf-8")
    monkeypatch.setattr(probe, "_v2_cgroup_directory", lambda: (cgroup, "namespace-root"))

    with pytest.raises(probe._TargetPreflightFailure) as exc_info:
        probe._runtime_limits()

    assert exc_info.value.phase == expected_phase


def test_runtime_limits_downgrade_only_when_cgroup_discovery_is_unavailable(
    monkeypatch,
) -> None:
    def unavailable() -> tuple[Path, str]:
        raise probe._CgroupEvidenceUnavailable("discovery unavailable")

    monkeypatch.setattr(probe, "_v2_cgroup_directory", unavailable)

    evidence = probe._runtime_limits()

    assert evidence == probe._RuntimeLimits(False, None, None, None, None, None)


def test_runtime_limits_mapped_missing_cpu_file_is_hard(monkeypatch, tmp_path: Path) -> None:
    cgroup = tmp_path / "sandbox"
    cgroup.mkdir()
    monkeypatch.setattr(probe, "_v2_cgroup_directory", lambda: (cgroup, "namespace-root"))

    with pytest.raises(probe._TargetPreflightFailure) as exc_info:
        probe._runtime_limits()

    assert exc_info.value.phase == "target_cpu_limit"


def test_runtime_limits_mapped_missing_memory_file_is_hard(monkeypatch, tmp_path: Path) -> None:
    cgroup = tmp_path / "sandbox"
    cgroup.mkdir()
    (cgroup / "cpu.max").write_text("100000 100000\n", encoding="utf-8")
    monkeypatch.setattr(probe, "_v2_cgroup_directory", lambda: (cgroup, "namespace-root"))

    with pytest.raises(probe._TargetPreflightFailure) as exc_info:
        probe._runtime_limits()

    assert exc_info.value.phase == "target_memory_limit"


def test_runtime_limits_downgrade_when_mapping_file_is_unavailable(monkeypatch) -> None:
    def unavailable() -> tuple[Path, str]:
        raise probe._CgroupEvidenceUnavailable("mapping unavailable")

    monkeypatch.setattr(probe, "_v2_cgroup_directory", unavailable)

    evidence = probe._runtime_limits()

    assert evidence.cgroup_available is False
    assert evidence.cgroup_resolution is None
    assert evidence.quota_usec is None
    assert evidence.period_usec is None
    assert evidence.memory_bytes is None


def test_v2_cgroup_directory_rejects_ambiguous_or_unsafe_memberships(tmp_path: Path) -> None:
    for membership in (
        "2:cpu:/legacy\n",
        "0::/one\n0::/two\n",
        "0::/../sibling\n",
    ):
        with pytest.raises(RuntimeError):
            probe._v2_cgroup_directory(
                membership,
                mountinfo_text=f"24 23 0:22 / {tmp_path} rw - cgroup2 cgroup rw\n",
                root=tmp_path,
            )


def test_artifact_rejects_unknown_cgroup_resolution() -> None:
    observation = _observation()
    observation["target_identity"] = {
        **TARGET_IDENTITY,
        "cgroup_resolution": "parent-fallback",
    }

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_artifact_accepts_explicit_unavailable_cgroup_evidence() -> None:
    observation = _observation()
    observation["target_identity"] = {
        **TARGET_IDENTITY,
        "cpu": None,
        "cgroup_available": False,
        "quota_usec": None,
        "period_usec": None,
        "memory_bytes": None,
        "cgroup_version": None,
        "cgroup_resolution": None,
    }
    observation["worker_cgroup_same"] = None

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "complete"
    assert artifact["target_identity"]["cgroup_available"] is False
    assert artifact["target_identity"]["cpu"] is None
    assert artifact["target_identity"]["cgroup_resolution"] is None
    assert artifact["target_identity"]["quota_usec"] is None
    assert artifact["target_identity"]["period_usec"] is None
    assert artifact["target_identity"]["memory_bytes"] is None
    assert artifact["cgroup_scope"] == "configured-resource-only"
    assert artifact["schema_version"] == "x11-shm-direct-vs-spawned.v3"


@pytest.mark.parametrize(
    "patch",
    [
        {"cgroup_available": False, "quota_usec": 100_000},
        {"cgroup_available": True, "quota_usec": None, "period_usec": None, "memory_bytes": None},
    ],
)
def test_artifact_rejects_inconsistent_cgroup_tri_state(patch: dict[str, object]) -> None:
    observation = _observation()
    target = {
        **TARGET_IDENTITY,
        "cpu": None,
        "cgroup_available": False,
        "quota_usec": None,
        "period_usec": None,
        "memory_bytes": None,
        "cgroup_version": None,
        "cgroup_resolution": None,
    }
    target.update(patch)
    observation["target_identity"] = target

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_spawned_worker_identity_rejects_different_cgroup(monkeypatch) -> None:
    session = SimpleNamespace(_session=SimpleNamespace(_process=SimpleNamespace(pid=99)))
    monkeypatch.setattr(probe, "_same_cgroup", lambda _pid: False)

    with pytest.raises(RuntimeError, match="cgroup differs"):
        probe._spawned_worker_identity(session)


def test_spawned_worker_identity_continues_when_cgroup_is_unreadable(monkeypatch) -> None:
    session = SimpleNamespace(_session=SimpleNamespace(_process=SimpleNamespace(pid=99)))
    monkeypatch.setattr(
        probe,
        "_same_cgroup",
        lambda _pid: (_ for _ in ()).throw(OSError("cgroup unavailable")),
    )
    monkeypatch.setattr(
        probe,
        "_process_identity",
        lambda _pid: {"pid": 99, "starttime_ticks": 1},
    )

    identity, cgroup_same = probe._spawned_worker_identity_and_cgroup(session, verify_cgroup=False)
    assert identity == {
        "pid": 99,
        "starttime_ticks": 1,
    }
    assert cgroup_same is None


def test_spawned_worker_identity_rejects_readable_mismatch_when_limits_unavailable(
    monkeypatch,
) -> None:
    session = SimpleNamespace(_session=SimpleNamespace(_process=SimpleNamespace(pid=99)))
    monkeypatch.setattr(probe, "_same_cgroup", lambda _pid: False)

    with pytest.raises(RuntimeError, match="cgroup differs"):
        probe._spawned_worker_identity_and_cgroup(session, verify_cgroup=False)


def test_artifact_rejects_nonpositive_process_identity_and_boolean_cpu() -> None:
    observation = _observation()
    observation["arms"]["direct_native"]["identity_before"] = {
        "pid": 0,
        "starttime_ticks": 0,
    }
    observation["target_identity"] = {**TARGET_IDENTITY, "cpu": True}

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_artifact_accepts_complete_production_shaped_observation() -> None:
    artifact = probe.build_artifact(_observation(), TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "complete"
    assert artifact["passed"] is True
    assert artifact["paired_prefix_samples"] == probe.PAIRS
    assert artifact["unpaired_after_failure_samples"] == 0
    assert all(artifact["arms"][arm]["captures_completed"] == probe.PAIRS for arm in probe.ARMS)


def test_artifact_rejects_noncontiguous_arm_prefix() -> None:
    observation = _observation()
    observation["arms"]["direct_native"]["observations"][1]["pair_index"] = 2

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"
    assert artifact["arms"] == {}


def test_artifact_rejects_measured_failure_index_after_prefix() -> None:
    observation = _observation(passed=False)
    observation["paired_prefix_samples"] = 2
    observation["unpaired_after_failure_samples"] = probe.PAIRS - 2
    observation["first_unpaired_pair"] = 2
    observation["captures_completed"] = {
        "direct_native": 2,
        "spawned_worker": probe.PAIRS,
    }
    observation["arms"]["direct_native"]["observations"] = observation["arms"]["direct_native"][
        "observations"
    ][:2]
    observation["arms"]["direct_native"]["failure"] = {
        "pair_index": 3,
        "phase": "measured_capture",
        "failure_type": "X11ScreenshotTimeoutError",
        "timeout_origin": "native_x11_reply_deadline",
    }

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"
    assert artifact["arms"] == {}


def test_artifact_rejects_timing_algebra_mutation() -> None:
    observation = _observation()
    observation["arms"]["direct_native"]["observations"][0]["timing"]["parent_send_ms"] = 0.001

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_artifact_rejects_spawned_nested_timing_mutation() -> None:
    observation = _observation()
    observation["arms"]["spawned_worker"]["observations"][0]["timing"]["parent_header_wait_ms"] = (
        0.0
    )

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "EvidenceValidationError"


def test_artifact_retains_safe_hashes_but_rejects_pixel_parity_mismatch() -> None:
    observation = _observation(passed=False)
    observation["arms"]["spawned_worker"]["observations"][0]["pixel_hash"] = "b" * 64
    observation["pixel_hash_parity"] = False

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "PixelParityMismatch"
    assert artifact["pixel_hash_parity"] is False
    assert artifact["arms"]["spawned_worker"]["observations"][0]["pixel_hash"] == "b" * 64


def test_pixel_parity_failure_cannot_be_marked_complete_by_child() -> None:
    observation = _observation()
    observation["arms"]["spawned_worker"]["observations"][0]["pixel_hash"] = "b" * 64
    observation["pixel_hash_parity"] = False

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["passed"] is False
    assert artifact["failure_type"] == "PixelParityMismatch"


def test_cleanup_only_failure_is_classified_as_cleanup_error() -> None:
    observation = _observation(passed=False)
    observation["arms"]["direct_native"]["session_cleanup"] = {
        "succeeded": False,
        "error_types": ["CleanupError"],
    }
    observation["session_cleanup"] = {
        "succeeded": False,
        "error_types": ["CleanupError"],
    }

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["failure_type"] == "CleanupError"


def test_arm_cleanup_failure_cannot_be_marked_complete_by_child() -> None:
    observation = _observation()
    observation["arms"]["spawned_worker"]["session_cleanup"] = {
        "succeeded": False,
        "error_types": ["CleanupError"],
    }

    artifact = probe.build_artifact(observation, TERMINAL_CLEANUP, PROVENANCE)

    assert artifact["status"] == "rejected"
    assert artifact["passed"] is False
    assert artifact["failure_type"] == "CleanupError"


def test_run_child_maps_direct_typed_timeout_and_spawned_origins(monkeypatch) -> None:
    class NativeTimeoutError(RuntimeError):
        pass

    class DirectSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png_timed(self, *_args: object) -> object:
            raise NativeTimeoutError("private direct detail")

        def close(self) -> None:
            pass

    class SpawnedSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._session = SimpleNamespace(
                _process=SimpleNamespace(pid=99),
            )

        def capture_png_with_timing(self, **_kwargs: object) -> tuple[bytes, NativeCaptureTiming]:
            raise ScreenshotCaptureTimedOut(
                "private spawned detail", timeout_origin="worker_process_deadline"
            )

        def close(self) -> None:
            pass

    module = SimpleNamespace(
        __name__=probe.MODULE_NAME,
        X11ScreenshotTimeoutError=NativeTimeoutError,
        X11SharedMemoryScreenshotSession=DirectSession,
        backend="x11-shm",
        codec="png-deflate-level1-no-filter",
        codec_runtime="in-process-miniz_oxide",
        codec_library="in-process",
        __file__=__file__,
    )
    monkeypatch.setattr(probe.importlib, "import_module", lambda _: module)
    monkeypatch.setattr(probe, "SpawnedWorkerSession", SpawnedSession)
    monkeypatch.setattr(probe, "_process_identity", lambda pid: {"pid": pid, "starttime_ticks": 1})
    monkeypatch.setattr(probe, "_same_cgroup", lambda _pid: True)
    monkeypatch.setattr(probe, "_target_identity", lambda *_args, **_kwargs: TARGET_IDENTITY)
    monkeypatch.setattr(probe, "_probe_x11_setup", lambda _display: None)
    monkeypatch.setattr(probe, "PAIRS", 1)
    monkeypatch.setattr(probe, "WARMUPS", 0)

    result = asyncio.run(probe._run_child_inner(pairs=1, warmups=0, progress=probe._Progress()))

    assert result["passed"] is False
    assert result["arms"]["direct_native"]["failure"]["timeout_origin"] == (
        "native_x11_reply_deadline"
    )
    assert result["arms"]["spawned_worker"]["failure"]["timeout_origin"] == (
        "worker_process_deadline"
    )
    assert "private direct detail" not in repr(result)
    assert "private spawned detail" not in repr(result)


def test_sessions_are_constructed_used_and_closed_on_pinned_threads(monkeypatch) -> None:
    event_loop_thread = threading.get_ident()
    png = _png_bytes()
    thread_ids = {arm: [] for arm in probe.ARMS}

    class NativeTimeoutError(RuntimeError):
        pass

    class NativeSession:
        def __init__(self, *_args: object) -> None:
            thread_ids[probe.DIRECT_ARM].append(("construct", threading.get_ident()))

        def capture_png_timed(self, *_args: object) -> tuple[bytes, tuple[int, int, int, int]]:
            thread_ids[probe.DIRECT_ARM].append(("capture", threading.get_ident()))
            return png, (1, 1, 1, 3)

        def close(self) -> None:
            thread_ids[probe.DIRECT_ARM].append(("close", threading.get_ident()))

        def __del__(self) -> None:
            thread_ids[probe.DIRECT_ARM].append(("drop", threading.get_ident()))

    class SpawnedSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            thread_ids[probe.SPAWNED_ARM].append(("construct", threading.get_ident()))
            self._session = SimpleNamespace(_process=SimpleNamespace(pid=99))

        def capture_png_with_timing(self, **_kwargs: object) -> tuple[bytes, NativeCaptureTiming]:
            thread_ids[probe.SPAWNED_ARM].append(("capture", threading.get_ident()))
            return png, _timing()

        def close(self) -> None:
            thread_ids[probe.SPAWNED_ARM].append(("close", threading.get_ident()))

    module = SimpleNamespace(
        __name__=probe.MODULE_NAME,
        X11ScreenshotTimeoutError=NativeTimeoutError,
        X11SharedMemoryScreenshotSession=NativeSession,
        backend="x11-shm",
        codec="png-deflate-level1-no-filter",
        codec_runtime="in-process-miniz_oxide",
        codec_library="in-process",
        __file__=__file__,
    )
    monkeypatch.setattr(probe.importlib, "import_module", lambda _: module)
    monkeypatch.setattr(probe, "SpawnedWorkerSession", SpawnedSession)
    monkeypatch.setattr(probe, "_process_identity", lambda pid: {"pid": pid, "starttime_ticks": 1})
    monkeypatch.setattr(probe, "_same_cgroup", lambda _pid: True)
    monkeypatch.setattr(probe, "_target_identity", lambda *_args, **_kwargs: TARGET_IDENTITY)
    monkeypatch.setattr(probe, "_probe_x11_setup", lambda _display: None)
    monkeypatch.setattr(probe, "PAIRS", 1)
    monkeypatch.setattr(probe, "WARMUPS", 0)

    result = asyncio.run(probe._run_child_inner(pairs=1, warmups=0, progress=probe._Progress()))

    assert result["passed"] is True
    assert thread_ids[probe.DIRECT_ARM][-1][0] == "drop"
    for arm in probe.ARMS:
        observed_threads = {thread for _, thread in thread_ids[arm]}
        assert len(observed_threads) == 1
        assert event_loop_thread not in observed_threads


def test_outer_benchmark_deadline_is_terminal_and_drops_survivor_rows(monkeypatch) -> None:
    release = threading.Event()

    class NativeTimeoutError(RuntimeError):
        pass

    class HungNativeSession:
        def __init__(self, *_args: object) -> None:
            pass

        def capture_png_timed(self, *_args: object) -> object:
            release.wait()
            return _png_bytes(), (1, 1, 1, 3)

        def close(self) -> None:
            pass

    class SpawnedSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._session = SimpleNamespace(_process=SimpleNamespace(pid=99))

        def close(self) -> None:
            pass

    module = SimpleNamespace(
        __name__=probe.MODULE_NAME,
        X11ScreenshotTimeoutError=NativeTimeoutError,
        X11SharedMemoryScreenshotSession=HungNativeSession,
        backend="x11-shm",
        codec="png-deflate-level1-no-filter",
        codec_runtime="in-process-miniz_oxide",
        codec_library="in-process",
        __file__=__file__,
    )
    monkeypatch.setattr(probe.importlib, "import_module", lambda _: module)
    monkeypatch.setattr(probe, "SpawnedWorkerSession", SpawnedSession)
    monkeypatch.setattr(probe, "_process_identity", lambda pid: {"pid": pid, "starttime_ticks": 1})
    monkeypatch.setattr(probe, "_same_cgroup", lambda _pid: True)
    monkeypatch.setattr(probe, "_target_identity", lambda *_args, **_kwargs: TARGET_IDENTITY)
    monkeypatch.setattr(probe, "_probe_x11_setup", lambda _display: None)
    monkeypatch.setattr(probe, "PAIRS", 1)
    monkeypatch.setattr(probe, "WARMUPS", 0)
    monkeypatch.setattr(probe, "OPERATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(probe, "CLEANUP_TIMEOUT_SECONDS", 0.01)

    try:
        result = asyncio.run(probe._run_child(pairs=1, warmups=0, progress=probe._Progress()))
    finally:
        release.set()
        time.sleep(0.05)

    assert result["failure_type"] == "TimeoutError"
    assert result["failure_timeout_origin"] == "benchmark_call_deadline"
    assert result["arms"] == {}
