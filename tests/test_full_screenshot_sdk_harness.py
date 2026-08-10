from __future__ import annotations

import asyncio
import hashlib
import json as json_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from modal_computer_use.benchmarks.full_screenshot_sdk_harness import (
    _EXPECTED_PAYLOAD,
    _validate_sample,
    build_paired_random_schedule,
    measure_full_screenshot_arms,
)
from modal_computer_use.errors import DaemonHTTPError
from scripts.benchmarks.x11_shm_screenshot_runner import (
    TRANSPORT_THRESHOLD_SWEEP_SPECS,
    _build_repeated_bounded_x_server_diagnostic,
    _build_x11_shm_resource_snapshot_diagnostic,
    _build_x11_shm_soak_diagnostic,
    _build_x11_shm_soak_diagnostic_script,
    _build_x11_shm_transport_threshold_diagnostic,
    _is_modal_daemon_cmdline,
    _run_repeated_bounded_x_server_diagnostic,
    _run_x11_shm_soak,
    _run_x11_shm_transport_threshold_diagnostic,
    _validate_bounded_x_server_sample_count,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = b"png-body-for-contract-test"
SHA = hashlib.sha256(DATA).hexdigest()


def test_promotion_runner_uses_the_mounted_chromium_fixture_path() -> None:
    runner = (ROOT / "scripts" / "benchmarks" / "x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "/opt/mcu-scripts/benchmarks/fixtures/x11_shm_chromium_fixture.html" in runner
    assert "/opt/modal-computer-use/native/x11_shm" in runner
    assert 'tags={"benchmark_run": BENCHMARK_RUN_TAG}' in runner
    assert "cpu=(CPU, CPU)" in runner
    assert "memory=(MEMORY_MIB, MEMORY_MIB)" in runner


def test_promotion_runner_allows_the_complete_fixed_campaign_to_finish() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "PROMOTION_RUN_TIMEOUT_SECONDS = 7_200" in runner
    assert "timeout=PROMOTION_RUN_TIMEOUT_SECONDS" in runner


def test_promotion_runner_exposes_the_bounded_x_server_probe() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_bounded_x_server_probe(" in runner
    assert '_ArmContext("auto")' in runner
    assert 'print(json.dumps(result, sort_keys=True))' in runner
    assert '"failure_phase": failure_phase' in runner
    assert '"failure_code": failure_code' in runner
    assert "computer.screenshots.full(), timeout=10.0" in runner
    assert '"python", "-c", constructor_probe, timeout=10' in runner
    assert "and elapsed_ms < 2_500.0" in runner


def test_promotion_runner_exposes_repeated_bounded_x_server_diagnostic() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLES = 10" in runner
    assert "BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLE_COUNTS = frozenset((10, 30))" in runner
    assert "def run_repeated_bounded_x_server_probe(" in runner
    assert "sample_count: int = BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLES" in runner
    assert "provenance: dict[str, str | bool] | None = None" in runner
    assert '"sample_count": sample_count' in runner
    assert '"observations": observations' in runner
    assert '"terminal_cleanup": cleanup' in runner
    assert '"retries": 0' in runner
    assert '"replacement_samples": 0' in runner


def test_repeated_bounded_x_server_diagnostic_persists_the_remote_result() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def repeated_bounded_x_server_main(" in runner
    assert "run_repeated_bounded_x_server_probe.remote(" in runner
    assert 'f"benchmark-data/x11-shm-bounded-x-diagnostic-{sample_count}.json"' in runner
    assert "path.write_text(json.dumps(result, indent=2, sort_keys=True) + \"\\n\")" in runner
    assert "print(json.dumps(result, indent=2, sort_keys=True))" in runner
    assert "provenance=_local_provenance()" in runner


def test_repeated_bounded_x_server_diagnostic_retains_safe_iterations_and_cleanup() -> None:
    observations = [
        {
            "passed": True,
            "public_error_code": "internal_error",
            "public_error_detail_type": "ScreenshotCaptureTimedOut",
            "constructor_elapsed_ms": 2006.9,
        },
        {
            "passed": False,
            "failure_type": "RuntimeError",
            "failure_phase": "capture_after_restart",
        },
    ]
    cleanup = {"succeeded": True, "remaining_sandboxes": 0}
    provenance = {
        "source_revision": "a" * 40,
        "worktree_clean": True,
        "x11_shm_source_sha256": "b" * 64,
        "cargo_lock_sha256": "c" * 64,
        "image_identity": "inline:browser-chromium-x11-shm",
    }

    payload = _build_repeated_bounded_x_server_diagnostic(
        observations, cleanup, provenance
    )

    assert payload["sample_count"] == 2
    assert payload["failure_count"] == 1
    assert payload["passed"] is False
    assert payload["observations"] == observations
    assert payload["terminal_cleanup"] == cleanup
    assert payload["retries"] == 0
    assert payload["replacement_samples"] == 0
    assert payload["schema_version"] == "x11-shm-bounded-x-diagnostic.v1"
    assert payload["provenance"] == provenance


def test_transport_threshold_diagnostic_exposes_fixed_public_sweep() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "TRANSPORT_THRESHOLD_SWEEP_TRIALS = 30" in runner
    assert "TRANSPORT_THRESHOLD_BYTES = 65_536" in runner
    assert "def run_x11_shm_transport_threshold_diagnostic(" in runner
    assert "def x11_shm_transport_threshold_main(" in runner
    assert "run_x11_shm_transport_threshold_diagnostic.remote(" in runner
    assert 'Path(f"benchmark-data/x11-shm-transport-threshold-{trials}.json")' in runner
    assert '"retries": 0' in runner
    assert '"replacement_samples": 0' in runner
    assert [
        (spec["case"], spec["route"], spec["expected_backend"])
        for spec in TRANSPORT_THRESHOLD_SWEEP_SPECS
    ] == [
        ("full-control", "/v1/screenshots/full/raw", "x11-shm"),
        ("region-below", "/v1/screenshots/region/raw", "x11-shm"),
        ("region-around", "/v1/screenshots/region/raw", "x11-shm"),
        ("region-above", "/v1/screenshots/region/raw", "mss"),
    ]


def test_transport_threshold_builder_retains_only_safe_trial_evidence() -> None:
    case_rows = (
        ("full-control", "/v1/screenshots/full/raw", 1024, 768, 1.0, "x11-shm", 65_407, "below"),
        ("region-below", "/v1/screenshots/region/raw", 1024, 736, 1.0, "x11-shm", 62_000, "below"),
        ("region-around", "/v1/screenshots/region/raw", 1024, 768, 1.0, "x11-shm", 65_407, "below"),
        ("region-above", "/v1/screenshots/region/raw", 1024, 768, 1.05, "mss", 66_000, "above"),
    )
    observations = []
    for trial_index in range(30):
        for case, route, width, height, scale, backend, payload_bytes, relation in case_rows:
            observations.append(
                {
                    "case": case,
                    "trial_index": trial_index,
                    "status": "ok",
                    "requested_source": "x11-shm",
                    "public_route": route,
                    "observed_backend": backend,
                    "width": round(width * scale),
                    "height": round(height * scale),
                    "requested_width": width,
                    "requested_height": height,
                    "scale": scale,
                    "requested_scale": scale,
                    "payload_bytes": payload_bytes,
                    "payload_relation": relation,
                    "png_signature_validated": True,
                    "size_header_validated": True,
                    "complete_sdk_ms": 15.0,
                    "residual_sdk_minus_daemon_ms": 10.0,
                    "daemon_timing_ms": {
                        "capture_ms": None,
                        "encode_ms": None,
                        "x11_shm_capture_encode_ms": 4.5,
                        "hash_ms": 0.2,
                        "total_ms": 5.0,
                    },
                    "response_metadata": {
                        "content_length": payload_bytes,
                        "transfer_encoding": None,
                    },
                    "body": "must-not-retain",
                }
            )
    cleanup = {"succeeded": True, "remaining_sandboxes": 0}
    provenance = {
        "source_revision": "a" * 40,
        "worktree_clean": True,
        "x11_shm_source_sha256": "b" * 64,
        "cargo_lock_sha256": "c" * 64,
        "image_identity": "inline:browser-chromium-x11-shm",
    }

    payload = _build_x11_shm_transport_threshold_diagnostic(
        observations, cleanup, provenance, trials=30
    )

    assert payload["schema_version"] == "x11-shm-transport-threshold.v1"
    assert payload["benchmark"] == "x11-shm-transport-threshold"
    assert payload["threshold_bytes"] == 65_536
    assert payload["trials_per_case"] == 30
    assert payload["case_order"] == [
        "full-control",
        "region-below",
        "region-around",
        "region-above",
    ]
    assert payload["retries"] == 0
    assert payload["replacement_samples"] == 0
    assert payload["sample_count"] == 120
    assert payload["passed"] is True
    assert payload["terminal_cleanup"] == cleanup
    def keys(value: object) -> list[str]:
        if isinstance(value, dict):
            return [str(key) for key in value] + [
                item for child in value.values() for item in keys(child)
            ]
        if isinstance(value, list):
            return [item for child in value for item in keys(child)]
        return []

    assert not set(keys(payload)) & {"raw", "body", "screenshot_bytes", "data_base64"}

    invalid = [dict(observation) for observation in observations]
    invalid[5]["residual_sdk_minus_daemon_ms"] = float("nan")
    invalid[5]["body"] = "private-body-must-not-retain"
    rejected = _build_x11_shm_transport_threshold_diagnostic(
        invalid, cleanup, provenance, trials=30
    )
    assert rejected["passed"] is False
    assert rejected["failure_count"] == 1
    assert rejected["observations"][5] == {
        "schedule_index": 5,
        "trial_index": 1,
        "case": "region-below",
        "requested_source": "x11-shm",
        "public_route": "/v1/screenshots/region/raw",
        "status": "failed",
        "failure_type": "EvidenceValidationError",
        "failure_phase": "artifact_validation",
    }
    assert not set(keys(rejected)) & {"raw", "body", "screenshot_bytes", "data_base64"}
    invalid_schedule = [dict(observation) for observation in observations]
    invalid_schedule[1]["case"] = "region-around"
    with pytest.raises(ValueError, match="fixed case order"):
        _build_x11_shm_transport_threshold_diagnostic(
            invalid_schedule, cleanup, provenance, trials=30
        )


@pytest.mark.asyncio
async def test_transport_threshold_runner_retains_order_and_wire_metadata() -> None:
    class FakeClient:
        def __init__(self, source: str) -> None:
            self.source = source
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def post_bytes_with_headers(self, path: str, *, json: dict[str, object]):
            self.calls.append((path, json))
            assert path in {"/v1/screenshots/region/raw", "/v1/screenshots/full/raw"}
            region = json.get("region")
            width = 1024
            height = 768
            if isinstance(region, dict):
                width = int(region["width"])
                height = int(region["height"])
            scale = float(json["scale"])
            payload_bytes = 62_000 if height == 736 else 65_407
            if scale > 1.0:
                payload_bytes = 66_000
            data = b"\x89PNG\r\n\x1a\n" + b"x" * (payload_bytes - 8)
            headers = {
                "content-type": "image/png",
                "content-length": str(payload_bytes),
                "transfer-encoding": "chunked",
                "x-computer-use-size-bytes": str(payload_bytes),
                "x-computer-use-width": str(round(width * scale)),
                "x-computer-use-height": str(round(height * scale)),
                "x-computer-use-capture-backend": (
                    "mss" if scale > 1.0 else self.source
                ),
                "x-computer-use-timing-ms": json_module.dumps(
                    {
                        "capture_ms": None if scale <= 1.0 else 0.0,
                        "encode_ms": None if scale <= 1.0 else 0.0,
                        "x11_shm_capture_encode_ms": 0.0 if scale <= 1.0 else None,
                        "hash_ms": 0.0,
                        "total_ms": 0.0,
                    }
                ),
            }
            return data, headers

    class FakeComputer:
        def __init__(self, source: str) -> None:
            self.client = FakeClient(source)

    class FakeContext:
        def __init__(self, source: str) -> None:
            self.computer = FakeComputer(source)

        async def __aenter__(self) -> FakeComputer:
            return self.computer

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fake_cleanup() -> dict[str, object]:
        return {"succeeded": True, "remaining_sandboxes": 0}

    # Keep this test independent of Modal cleanup and use the public route seam.
    import scripts.benchmarks.x11_shm_screenshot_runner as runner

    original_cleanup = runner._final_sandbox_cleanup
    runner._final_sandbox_cleanup = fake_cleanup
    try:
        provenance = {"source_revision": "a" * 40, "worktree_clean": True}
        result = await _run_x11_shm_transport_threshold_diagnostic(
            lambda: FakeContext("x11-shm"), trials=30, provenance=provenance
        )
    finally:
        runner._final_sandbox_cleanup = original_cleanup

    assert result["sample_count"] == 120
    assert result["failure_count"] == 0
    assert result["terminal_cleanup"]["succeeded"] is True
    rows = result["observations"]
    assert [row["case"] for row in rows] == [
        "full-control",
        "region-below",
        "region-around",
        "region-above",
    ] * 30
    assert [row["trial_index"] for row in rows] == [
        trial for trial in range(30) for _case in range(4)
    ]
    assert all(row["requested_source"] == "x11-shm" for row in rows)
    assert all(
        row["observed_backend"] == ("mss" if row["case"] == "region-above" else "x11-shm")
        for row in rows
    )
    assert rows[0]["public_route"] == "/v1/screenshots/full/raw"
    assert rows[1]["public_route"] == "/v1/screenshots/region/raw"
    assert rows[0]["residual_sdk_minus_daemon_ms"] >= 0
    assert all(
        row["response_metadata"]["transfer_encoding"] == "chunked"
        for row in rows
    )


def test_repeated_bounded_x_server_diagnostic_requires_exactly_ten_samples() -> None:
    with pytest.raises(
        ValueError,
        match="bounded X server diagnostic requires exactly 10 or 30 samples",
    ):
        asyncio.run(_run_repeated_bounded_x_server_diagnostic(sample_count=9))


def test_repeated_bounded_x_server_diagnostic_requires_provenance() -> None:
    with pytest.raises(
        ValueError, match="clean local benchmark provenance is required"
    ):
        asyncio.run(_run_repeated_bounded_x_server_diagnostic(sample_count=10))


def test_repeated_bounded_x_server_diagnostic_allows_only_ten_or_thirty_samples() -> None:
    _validate_bounded_x_server_sample_count(10)
    _validate_bounded_x_server_sample_count(30)
    with pytest.raises(
        ValueError,
        match="bounded X server diagnostic requires exactly 10 or 30 samples",
    ):
        _validate_bounded_x_server_sample_count(20)


def test_promotion_runner_exposes_the_x_server_restart_probe() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_x_server_restart_probe(" in runner
    assert 'return await _run_x_server_restart_probe(lambda: _ArmContext("auto"))' in runner
    assert 'raise RuntimeError("X server restart probe cleanup found live Sandboxes")' in runner
    assert 'print(json.dumps(result, sort_keys=True))' in runner


def test_promotion_runner_exposes_a_retained_100_pair_readiness_replication() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_readiness_replication(" in runner
    assert 'if samples != 100:' in runner
    assert (
        'raise ValueError("readiness replication requires exactly 100 samples per arm")'
        in runner
    )
    assert '"sample_count_per_arm": samples' in runner
    assert 'observation["startup_total_ms"] = round(elapsed_ms, 4)' in runner
    assert 'observation["public_capture_elapsed_ms"]' in runner
    assert '"position": position' in runner
    assert "continue_on_failure=True" in runner
    assert '"failure_count": failure_count' in runner
    assert '"terminal_cleanup": cleanup' in runner
    assert "def readiness_main(" in runner


def test_promotion_runner_exposes_candidate_timeout_origin_diagnostic() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_x11_shm_timeout_origin_probe(" in runner
    assert 'if samples != 100:' in runner
    assert '"sample_count": sample_count' in runner
    assert '"timeout_origin_counts": timeout_origin_counts' in runner
    assert '"retries": 0' in runner
    assert '"terminal_cleanup": cleanup' in runner
    assert "def timeout_origin_main(" in runner


def test_promotion_soak_matches_daemon_argv_token_not_helper_text() -> None:
    daemon_argv = b"python\0-m\0modal_computer_use.daemon\0--display\0:99\0"
    helper_argv = (
        b"python\0-c\0"
        b"modal_computer_use.daemon\0"
    )

    assert _is_modal_daemon_cmdline(daemon_argv) is True
    assert _is_modal_daemon_cmdline(helper_argv) is False

    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )
    soak = runner.split("async def _run_x11_shm_soak", maxsplit=1)[1].split(
        "async def _measure", maxsplit=1
    )[0]
    assert 'argv[index : index + 2] == [b"-m", b"modal_computer_use.daemon"]' in runner
    assert "_is_modal_daemon_cmdline(command)" in soak


def test_x11_shm_soak_diagnostic_retains_signed_counts_identity_and_cleanup() -> None:
    observation = {
        "passed": False,
        "requested_source": "auto",
        "observed_backend": "x11-shm",
        "captures_requested": 10_000,
        "captures_completed": 10_000,
        "full_captures": 5_000,
        "region_captures": 5_000,
        "daemon_identity_before": {
            "pid": 41,
            "starttime_ticks": 123,
            "argv_match": True,
            "argv_module": "modal_computer_use.daemon",
        },
        "daemon_identity_after": {
            "pid": 41,
            "starttime_ticks": 123,
            "argv_match": True,
            "argv_module": "modal_computer_use.daemon",
        },
        "counts_before": {"fd": 12, "mappings": 34, "rss": 56},
        "counts_after": {"fd": 11, "mappings": 34, "rss": 60},
        "signed_deltas": {"fd": -1, "mappings": 0, "rss": 4},
        "resource_metrics_before": {
            "maps": True,
            "fd": True,
            "VmRSS": True,
            "sampled_vm_rss": True,
        },
        "resource_metrics_after": {
            "maps": True,
            "fd": True,
            "VmRSS": True,
            "sampled_vm_rss": True,
        },
        "failure_resource_metric": None,
        "rss_metric_source": "sampled_vm_rss",
        "rss_sample_count": 102,
        "rss_before_bytes": 56,
        "rss_current_bytes": 60,
        "rss_final_bytes": 60,
        "rss_observed_peak_bytes": 63,
        "rss_peak_growth_bytes": 7,
        "final_included": True,
        "failure_phase": None,
        "failure_type": None,
    }
    provenance = {
        "source_revision": "a" * 40,
        "worktree_clean": True,
        "x11_shm_source_sha256": "b" * 64,
        "cargo_lock_sha256": "c" * 64,
        "image_identity": "inline:browser-chromium-x11-shm",
    }
    cleanup = {
        "succeeded": True,
        "remaining_sandboxes": 0,
        "survivors_before_sweep": 0,
    }

    payload = _build_x11_shm_soak_diagnostic(observation, cleanup, provenance)

    assert payload["schema_version"] == "x11-shm-soak-diagnostic.v1"
    assert payload["status"] == "complete"
    assert payload["sample_count"] == 10_000
    assert payload["requested_source"] == "auto"
    assert payload["observed_backend"] == "x11-shm"
    assert payload["daemon_identity_before"] == observation["daemon_identity_before"]
    assert payload["daemon_identity_after"] == observation["daemon_identity_after"]
    assert payload["counts_before"] == observation["counts_before"]
    assert payload["counts_after"] == observation["counts_after"]
    assert payload["signed_deltas"] == observation["signed_deltas"]
    assert payload["resource_metrics_before"] == observation["resource_metrics_before"]
    assert payload["resource_metrics_after"] == observation["resource_metrics_after"]
    assert payload["failure_resource_metric"] is None
    assert payload["rss_metric_source"] == "sampled_vm_rss"
    assert payload["rss_sample_count"] == 102
    assert payload["rss_before_bytes"] == 56
    assert payload["rss_current_bytes"] == 60
    assert payload["rss_final_bytes"] == 60
    assert payload["rss_observed_peak_bytes"] == 63
    assert payload["rss_peak_growth_bytes"] == 7
    assert payload["terminal_cleanup"] == cleanup
    assert payload["provenance"] == provenance
    assert payload["retries"] == 0
    assert payload["replacement_samples"] == 0
    assert payload["passed"] is False


def test_x11_shm_resource_snapshot_diagnostic_retains_failure_metric() -> None:
    observation = {
        "passed": False,
        "requested_source": "auto",
        "observed_backend": "x11-shm",
        "prime_captures": 2,
        "daemon_identity_before": {
            "pid": 41,
            "starttime_ticks": 123,
            "argv_match": True,
            "argv_module": "modal_computer_use.daemon",
        },
        "counts_before": None,
        "resource_metrics_before": {
            "maps": True,
            "fd": True,
            "VmRSS": True,
            "sampled_vm_rss": False,
        },
        "failure_resource_metric": "sampled_vm_rss",
        "failure_phase": "rss_sample_before",
        "failure_type": "ResourceMetricUnavailable",
        "rss_metric_source": "sampled_vm_rss",
        "rss_sample_count": 1,
        "rss_before_bytes": 56,
        "rss_current_bytes": None,
        "rss_final_bytes": None,
        "rss_observed_peak_bytes": 56,
        "rss_peak_growth_bytes": 0,
        "final_included": False,
    }
    provenance = {
        "source_revision": "a" * 40,
        "worktree_clean": True,
        "x11_shm_source_sha256": "b" * 64,
        "cargo_lock_sha256": "c" * 64,
        "image_identity": "inline:browser-chromium-x11-shm",
    }
    cleanup = {
        "succeeded": True,
        "remaining_sandboxes": 0,
        "survivors_before_sweep": 0,
    }

    payload = _build_x11_shm_resource_snapshot_diagnostic(
        observation, cleanup, provenance
    )

    assert payload["schema_version"] == "x11-shm-resource-snapshot.v1"
    assert payload["benchmark"] == "x11-shm-resource-snapshot"
    assert payload["prime_captures"] == 2
    assert payload["resource_metrics_before"] == observation["resource_metrics_before"]
    assert payload["failure_resource_metric"] == "sampled_vm_rss"
    assert payload["failure_phase"] == "rss_sample_before"
    assert payload["rss_metric_source"] == "sampled_vm_rss"
    assert payload["rss_sample_count"] == 1
    assert payload["final_included"] is False
    assert payload["retries"] == 0
    assert payload["replacement_samples"] == 0
    assert payload["terminal_cleanup"] == cleanup
    assert payload["provenance"] == provenance
    assert payload["passed"] is False


def test_x11_shm_soak_diagnostic_rejects_inconsistent_signed_deltas() -> None:
    identity = {
        "pid": 41,
        "starttime_ticks": 123,
        "argv_match": True,
        "argv_module": "modal_computer_use.daemon",
    }
    observation = {
        "passed": True,
        "requested_source": "auto",
        "observed_backend": "x11-shm",
        "captures_completed": 10_000,
        "full_captures": 5_000,
        "region_captures": 5_000,
        "daemon_identity_before": identity,
        "daemon_identity_after": identity,
        "counts_before": {"fd": 12, "mappings": 34, "rss": 56},
        "counts_after": {"fd": 12, "mappings": 34, "rss": 60},
        "signed_deltas": {"fd": 0, "mappings": 0, "rss": 999},
        "resource_metrics_before": {
            "maps": True,
            "fd": True,
            "VmRSS": True,
            "sampled_vm_rss": True,
        },
        "resource_metrics_after": {
            "maps": True,
            "fd": True,
            "VmRSS": True,
            "sampled_vm_rss": True,
        },
        "rss_metric_source": "sampled_vm_rss",
        "rss_sample_count": 102,
        "rss_before_bytes": 56,
        "rss_current_bytes": 60,
        "rss_final_bytes": 60,
        "rss_observed_peak_bytes": 60,
        "rss_peak_growth_bytes": 4,
        "final_included": True,
    }
    cleanup = {"succeeded": True, "remaining_sandboxes": 0}

    payload = _build_x11_shm_soak_diagnostic(observation, cleanup, {})

    assert payload["signed_deltas"] == observation["signed_deltas"]
    assert payload["passed"] is False


def test_x11_shm_soak_diagnostic_script_compiles() -> None:
    script = _build_x11_shm_soak_diagnostic_script(10_000)

    compile(script, "<x11-shm-soak>", "exec")
    snapshot_script = _build_x11_shm_soak_diagnostic_script(
        0, snapshot_only=True
    )
    compile(snapshot_script, "<x11-shm-resource-snapshot>", "exec")
    assert "VmHWM" not in script
    assert '"sampled_vm_rss"' in script


def test_promotion_soak_parses_sampled_rss_contract() -> None:
    payload = {
        "passed": True,
        "captures_requested": 10_000,
        "prime_captures": 2,
        "full_captures": 5_000,
        "region_captures": 5_000,
        "signed_deltas": {"fd": 0, "mappings": 0, "rss": 4},
        "rss_metric_source": "sampled_vm_rss",
        "rss_sample_count": 102,
        "rss_before_bytes": 100,
        "rss_current_bytes": 104,
        "rss_final_bytes": 104,
        "rss_observed_peak_bytes": 110,
        "rss_peak_growth_bytes": 10,
        "final_included": True,
    }

    class FakeWait:
        async def aio(self) -> int:
            return 0

    class FakeRead:
        async def aio(self) -> str:
            return json_module.dumps(payload)

    class FakeStdout:
        read = FakeRead()

    class FakeProcess:
        wait = FakeWait()
        stdout = FakeStdout()


    class FakeExec:
        async def aio(self, *args: object, **kwargs: object) -> FakeProcess:
            assert args[:2] == ("python", "-c")
            assert kwargs["timeout"] == 900
            return FakeProcess()

    class FakeContext:
        def __init__(self) -> None:
            self.computer = SimpleNamespace(_sandbox=SimpleNamespace(exec=FakeExec()))

        async def __aenter__(self) -> SimpleNamespace:
            return self.computer

        async def __aexit__(self, *args: object) -> None:
            return None

    result = asyncio.run(_run_x11_shm_soak(lambda: FakeContext(), captures=10_000))

    assert result["passed"] is True
    assert result["fd_delta"] == 0
    assert result["mapping_delta"] == 0
    assert result["rss_metric_source"] == "sampled_vm_rss"
    assert result["rss_sample_count"] == 102
    assert result["rss_peak_growth_bytes"] == 10
    assert result["final_included"] is True


def test_x11_shm_soak_diagnostic_retains_safe_entrypoint_and_provenance() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_x11_shm_soak_diagnostic(" in runner
    assert "def x11_shm_soak_diagnostic_main(" in runner
    assert "def run_x11_shm_resource_snapshot_diagnostic(" in runner
    assert "def x11_shm_resource_snapshot_main(" in runner
    assert "run_x11_shm_resource_snapshot_diagnostic.remote(" in runner
    assert 'Path("benchmark-data/x11-shm-resource-snapshot.json")' in runner
    assert "SOAK_RSS_SAMPLE_INTERVAL = 100" in runner
    assert (
        "SOAK_RSS_SAMPLE_COUNT = SOAK_DIAGNOSTIC_CAPTURES // SOAK_RSS_SAMPLE_INTERVAL + 2"
        in runner
    )
    assert 'SOAK_RESOURCE_METRICS = ("maps", "fd", "VmRSS", "sampled_vm_rss")' in runner
    assert "SOAK_DIAGNOSTIC_CAPTURES = 10_000" in runner
    assert "captures != SOAK_DIAGNOSTIC_CAPTURES" in runner
    assert "run_x11_shm_soak_diagnostic.remote(" in runner
    assert 'Path(f"benchmark-data/x11-shm-soak-diagnostic-{captures}.json")' in runner
    assert '"schema_version": "x11-shm-soak-diagnostic.v1"' in runner
    assert '"signed_deltas"' in runner
    assert '"daemon_identity_before"' in runner
    assert '"daemon_identity_after"' in runner
    assert '"terminal_cleanup"' in runner
    assert 'provenance=_local_provenance()' in runner


def test_promotion_artifact_retains_missing_resource_deltas_as_unknown() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert '"fd_delta": soak.get("fd_delta"),' in runner
    assert '"mapping_delta": soak.get("mapping_delta"),' in runner
    assert 'soak.get("fd_delta", -1)' not in runner
    assert 'soak.get("mapping_delta", -1)' not in runner


def test_promotion_readiness_retains_sdk_startup_stages() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "SessionStartupTiming" in runner
    assert "timing=self.startup_timing" in runner
    assert 'observation["startup_timing"] = timing.as_dict()' in runner
    assert '"startup_timings": startup_timings[arm]' in runner
    assert 'observation["failure_phase"] = (' in runner
    assert "_startup_failure_phase(context.startup_timing)" in runner
    assert 'if context.enter_phase == "create_sandbox"' in runner
    assert "else context.enter_phase" in runner
    assert '"connection_parameters_ready": "daemon_readiness"' in runner
    assert '"attestation_ready": "attested_tunnel_readiness"' in runner
    assert 'observation.update(_safe_daemon_failure(exc))' in runner
    assert 'details.get("timeout_origin")' in runner
    assert 'result["failure_timeout_origin"]' in runner
    assert 'observation["status"] = "failed"' in runner
    assert 'observation["failure_phase"] = "cleanup"' in runner


def test_promotion_restart_retains_safe_failure_attribution() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert 'phase = "context_enter"' in runner
    assert 'phase = "capture_before_restart"' in runner
    assert 'phase = "lifecycle_restart"' in runner
    assert 'phase = "capture_after_restart"' in runner
    assert '"failure_phase": phase' in runner
    assert '{"failure_phase": "cleanup"}' in runner
    assert "**_safe_daemon_failure(exc)" in runner


def test_promotion_restart_retains_lifecycle_restart_elapsed_time() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )
    restart_probe = runner.split(
        "async def _run_x_server_timeout_probe", maxsplit=1
    )[1].split("async def _run_region_parity_probe", maxsplit=1)[0]

    assert "restart_started = time.perf_counter()" in restart_probe
    assert '"lifecycle_restart_elapsed_ms": lifecycle_restart_elapsed_ms' in restart_probe
    assert "finally:\n            lifecycle_restart_elapsed_ms = round(" in restart_probe


def test_promotion_restart_retains_allowlisted_readiness_categories() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )
    restart_probe = runner.split(
        "async def _run_x_server_timeout_probe", maxsplit=1
    )[1].split("async def _run_region_parity_probe", maxsplit=1)[0]

    assert "_safe_daemon_failure(exc)" in restart_probe
    assert '"failure_readiness_categories"' in restart_probe


def test_promotion_failure_attribution_never_retains_daemon_error_text() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    helper = runner.split("def _safe_daemon_failure", maxsplit=1)[1].split(
        "\n\nasync def _completed_process_stdout_text",
        maxsplit=1,
    )[0]
    assert 'details.get("type")' in helper
    assert 'details.get("errors")' in helper
    assert 'result["failure_readiness_categories"]' in helper
    assert 'details.get("error")' not in helper


class FakeScreenshot:
    format = "png"
    width = 1024
    height = 768
    size_bytes = len(DATA)
    sha256 = SHA
    cursor_visible = False
    cursor_position = SimpleNamespace(x=17, y=23)
    coordinate_space = SimpleNamespace(model_dump=lambda mode=None: {})

    def as_bytes(self) -> bytes:
        return DATA


def _trace(*, backend: str = "mss") -> dict[str, object]:
    return {
        "path": "/v1/screenshots/full/raw",
        "request_json": dict(_EXPECTED_PAYLOAD),
        "response_headers": {
            "content-type": "image/png",
            "x-computer-use-width": "1024",
            "x-computer-use-height": "768",
            "x-computer-use-size-bytes": str(len(DATA)),
            "x-computer-use-sha256": SHA,
            "x-computer-use-capture-backend": backend,
            "x-computer-use-cursor-position": '{"x":17,"y":23}',
            "x-computer-use-timing-ms": json_module.dumps(
                {
                    "capture_ms": 1.0,
                    "encode_ms": 0.2,
                    "hash_ms": 0.25,
                    "total_ms": 1.5,
                }
            ),
        },
    }


def test_schedule_is_reproducible_paired_and_randomized() -> None:
    first = build_paired_random_schedule(
        ("mss", "x11-shm"), sample_count=100, seed=20260808
    )
    second = build_paired_random_schedule(
        ("mss", "x11-shm"), sample_count=100, seed=20260808
    )

    assert first == second
    assert len(first) == 200
    assert all(
        {entry["arm"] for entry in first[index : index + 2]} == {"mss", "x11-shm"}
        for index in range(0, len(first), 2)
    )
    assert {entry["position"] for entry in first} == {0, 1}
    assert any(
        first[index]["arm"] != first[index + 2]["arm"]
        for index in range(0, len(first) - 2, 2)
    )


def test_validate_sample_accepts_canonical_contract() -> None:
    _validate_sample(FakeScreenshot(), _trace())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "/v1/screenshots/full"),
        ("request_json", {"format": "jpeg"}),
    ],
)
def test_validate_sample_rejects_route_or_payload(field: str, value: object) -> None:
    trace = _trace()
    trace[field] = value
    with pytest.raises(AssertionError):
        _validate_sample(FakeScreenshot(), trace)


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("content-type", "image/jpeg"),
        ("x-computer-use-width", "800"),
        ("x-computer-use-sha256", "0" * 64),
        ("x-computer-use-cursor-position", '{"x":1024,"y":0}'),
        ("x-computer-use-timing-ms", '{"total_ms":-1}'),
        ("x-computer-use-timing-ms", '{"total_ms":1}'),
    ],
)
def test_validate_sample_rejects_response_contract(header: str, value: str) -> None:
    trace = _trace()
    headers = trace["response_headers"]
    assert isinstance(headers, dict)
    headers[header] = value
    with pytest.raises(AssertionError):
        _validate_sample(FakeScreenshot(), trace)


class FakeClient:
    def __init__(self, backend: str, *, fail_on_call: int | None = None) -> None:
        self.backend = backend
        self.fail_on_call = fail_on_call
        self.calls = 0

    async def post_bytes_with_headers(self, path: str, *, json: dict[str, object]):
        assert path == "/v1/screenshots/full/raw"
        assert json == _EXPECTED_PAYLOAD
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise DaemonHTTPError(
                "internal server error",
                status_code=500,
                code="internal_error",
                details={
                    "error_type": "ScreenshotCaptureTimedOut",
                    "token": "must-not-appear",
                    "errors": [
                        "screenshot capture failed",
                        "https://private.invalid/must-not-appear",
                    ],
                },
            )
        return DATA, _trace(backend=self.backend)["response_headers"]


class FakeScreenshots:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def full(self) -> FakeScreenshot:
        await self._client.post_bytes_with_headers(
            "/v1/screenshots/full/raw", json=dict(_EXPECTED_PAYLOAD)
        )
        return FakeScreenshot()


class FakeComputer:
    def __init__(self, backend: str, *, fail_on_call: int | None = None) -> None:
        self.client = FakeClient(backend, fail_on_call=fail_on_call)
        self.screenshots = FakeScreenshots(self.client)


class FakeContext:
    def __init__(
        self,
        backend: str,
        *,
        fail_cleanup: bool = False,
        fail_on_call: int | None = None,
    ) -> None:
        self.computer = FakeComputer(backend, fail_on_call=fail_on_call)
        self.fail_cleanup = fail_cleanup

    async def __aenter__(self) -> FakeComputer:
        return self.computer

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.fail_cleanup:
            raise RuntimeError("cleanup failed")


@pytest.mark.asyncio
async def test_measure_full_screenshot_records_public_and_daemon_boundaries() -> None:
    result = await measure_full_screenshot_arms(
        {"mss": lambda: FakeContext("mss"), "x11-shm": lambda: FakeContext("x11-shm")},
        sample_count=100,
        warmup_iterations=10,
        decode_parity=lambda _data: True,
        expected_capture_backends={"mss": "mss", "x11-shm": "x11-shm"},
        schedule_seed=20260808,
    )

    assert result["public_call"] == "await computer.screenshots.full()"
    assert len(result["schedule"]) == 200
    for arm in ("mss", "x11-shm"):
        observations = result["arms"][arm]["observations"]
        assert len(observations) == 100
        assert observations[0]["daemon_total_ms"] == 1.5
        assert observations[0]["hash_ms"] == 0.25
        assert observations[0]["payload_bytes"] == len(DATA)
        assert observations[0]["metadata_parity"] is True


@pytest.mark.asyncio
async def test_measure_full_screenshot_requires_promotion_sample_and_warmup_counts() -> None:
    with pytest.raises(ValueError, match="100"):
        await measure_full_screenshot_arms(
            {"mss": lambda: FakeContext("mss"), "x11-shm": lambda: FakeContext("x11-shm")},
            sample_count=30,
            warmup_iterations=10,
            decode_parity=lambda _data: True,
        )
    with pytest.raises(ValueError, match="10 warmup"):
        await measure_full_screenshot_arms(
            {"mss": lambda: FakeContext("mss"), "x11-shm": lambda: FakeContext("x11-shm")},
            sample_count=100,
            warmup_iterations=3,
            decode_parity=lambda _data: True,
        )


@pytest.mark.asyncio
async def test_measure_full_screenshot_requires_pixel_parity_callback() -> None:
    with pytest.raises(ValueError, match="pixel parity"):
        await measure_full_screenshot_arms(
            {"mss": lambda: FakeContext("mss"), "x11-shm": lambda: FakeContext("x11-shm")},
            sample_count=100,
            warmup_iterations=10,
        )


@pytest.mark.asyncio
async def test_measure_full_screenshot_fails_cleanup() -> None:
    with pytest.raises(RuntimeError, match="cleanup"):
        await measure_full_screenshot_arms(
            {
                "mss": lambda: FakeContext("mss", fail_cleanup=True),
                "x11-shm": lambda: FakeContext("x11-shm"),
            },
            sample_count=100,
            warmup_iterations=10,
            decode_parity=lambda _data: True,
        )


@pytest.mark.asyncio
async def test_measure_full_screenshot_annotates_daemon_failure_without_secrets() -> None:
    with pytest.raises(DaemonHTTPError) as caught:
        await measure_full_screenshot_arms(
            {
                "mss": lambda: FakeContext("mss"),
                "x11-shm": lambda: FakeContext("x11-shm", fail_on_call=11),
            },
            sample_count=100,
            warmup_iterations=10,
            decode_parity=lambda _data: True,
            schedule_seed=20260808,
        )

    notes = "\n".join(caught.value.__notes__)
    assert "arm=x11-shm phase=sample sample_index=0" in notes
    assert "status_code=500 code=internal_error" in notes
    assert "error_type=ScreenshotCaptureTimedOut" in notes
    assert "readiness_errors=screenshot,unknown" in notes
    assert "must-not-appear" not in notes
