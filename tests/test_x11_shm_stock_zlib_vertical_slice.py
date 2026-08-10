from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.benchmarks import x11_shm_stock_zlib_vertical_slice as runner
from scripts.benchmarks.full_screenshot_sdk_harness import build_paired_schedule


def _observation(*, backend: str, sdk_ms: float, payload: int) -> dict[str, object]:
    return {
        "sample_index": 0,
        "status": "ok",
        "complete_sdk_ms": sdk_ms,
        "daemon_total_ms": sdk_ms - 1.0,
        "capture_ms": 1.0,
        "encode_ms": 1.0,
        "x11_shm_capture_encode_ms": None,
        "hash_ms": 0.1,
        "payload_bytes": payload,
        "decoded_pixel_parity": True,
        "metadata_parity": True,
        "capture_backend": backend,
    }


def _live_result() -> dict[str, object]:
    observations = {
        "mss": [_observation(backend="mss", sdk_ms=20.0, payload=52_315) for _ in range(100)],
        "x11-shm": [
            _observation(backend="x11-shm", sdk_ms=10.0, payload=53_000)
            for _ in range(100)
        ],
    }
    measurement = {
        "status": "complete",
        "schedule": build_paired_schedule(
            ("mss", "x11-shm"), sample_count=100, seed=20260810, order="alternating"
        ),
        "schedule_order": "alternating",
        "warmup_schedule": build_paired_schedule(
            ("mss", "x11-shm"),
            sample_count=10,
            seed=20260810,
            order="alternating",
            minimum_sample_count=1,
        ),
        "warmup_completed_per_arm": {"mss": 10, "x11-shm": 10},
        "fallback_counts": {"mss": 0, "x11-shm": 0},
        "cleanup": {"succeeded": True, "errors": []},
        "arms": {
            arm: {
                "observations": rows,
                "transport_traces": [{} for _ in rows],
            }
            for arm, rows in observations.items()
        },
    }
    identities = {
        arm: {
            "backend": "x11-shm",
            "codec": runner.STOCK_ZLIB_CODEC,
            "codec_runtime": "1.2.13",
            "codec_library": "system-libz",
            "module_sha256": "a" * 64,
            "image_object_id": "im-test",
            "cpu": 1.0,
            "memory_bytes": 2_048 * 1024 * 1024,
        }
        for arm in ("mss", "x11-shm")
    }
    return {
        "measurement": measurement,
        "target_identities": identities,
        "target_identity_match": True,
        "fixture_verified": True,
        "terminal_cleanup": {
            "succeeded": True,
            "survivors_before_sweep": 0,
            "remaining_sandboxes": 0,
            "terminal_zero_survivors": True,
        },
    }


def test_remote_relocation_uses_guarded_root_and_fixture_fallback(tmp_path: Path) -> None:
    remote_path = Path("/root/x11_shm_stock_zlib_vertical_slice.py")
    assert runner._project_root_for(remote_path) == Path("/root")
    fallback = tmp_path / "x11_shm_chromium_fixture.html"
    fallback.write_text("<html>relocated</html>", encoding="utf-8")
    assert runner._load_fixture_html(remote_path, fallback) == "<html>relocated</html>"


def test_builder_records_preregistered_p95_tail_and_live_payload_rules() -> None:
    artifact = runner._build_artifact(
        codec_proof={
            "miniz": {"codec": runner.MINIZ_CODEC, "pixel_hash": "a"},
            "stock-zlib": {"codec": runner.STOCK_ZLIB_CODEC, "pixel_hash": "a"},
        },
        provenance={"worktree_clean": True, "source_revision": "a" * 40},
        live=_live_result(),
    )

    assert artifact["status"] == "complete"
    assert artifact["non_gating"] is True
    assert artifact["pass_criteria"]["tail_thresholds_ms"] == [100, 500]
    assert artifact["pass_evaluation"]["exact_samples"] is True
    assert artifact["pass_evaluation"]["payload_p50_growth_limit"] is True
    assert artifact["pass_evaluation"]["sdk_p95_regression_limit"] is True
    assert artifact["pass_evaluation"]["tail_counts_no_worse"] is True
    assert artifact["pass_evaluation"]["terminal_cleanup"] is True
    assert artifact["arms"]["x11-shm"]["observed_codec"] == runner.STOCK_ZLIB_CODEC


@pytest.mark.parametrize(
    "mutation",
    [
        lambda live: live["measurement"].update({"schedule": []}),
        lambda live: live["measurement"]["schedule"].__setitem__(0, {"arm": "mss"}),
        lambda live: live["measurement"]["warmup_schedule"].pop(),
    ],
)
def test_builder_rejects_malformed_fixed_schedule(mutation) -> None:  # type: ignore[no-untyped-def]
    live = _live_result()
    mutation(live)
    with pytest.raises(ValueError, match="schedule"):
        runner._build_artifact(
            codec_proof={},
            provenance={},
            live=live,
        )


def test_builder_rejects_retained_measurement_failure() -> None:
    live = _live_result()
    live["measurement"]["status"] = "rejected"
    live["measurement"]["failure"] = {
        "phase": "cleanup",
        "exception_type": "RuntimeError",
    }
    with pytest.raises(ValueError, match="did not complete"):
        runner._build_artifact(
            codec_proof={},
            provenance={},
            live=live,
        )


def test_rejected_artifact_keeps_safe_partial_counts_without_secrets() -> None:
    live = _live_result()
    live["measurement"]["arms"]["x11-shm"]["observations"][0]["body"] = "secret"
    rejected = runner._build_rejected_artifact(
        phase="sample",
        exception_type="AssertionError",
        provenance={"source_revision": "a" * 40, "worktree_clean": False},
        live=live,
    )

    encoded = json.dumps(rejected)
    assert rejected["status"] == "rejected"
    assert rejected["partial_evidence"]["arms"]["x11-shm"]["observations_completed"] == 100
    assert "secret" not in encoded
    assert "body" not in encoded


def test_builder_rejects_unsafe_live_evidence() -> None:
    live = _live_result()
    live["measurement"]["arms"]["mss"]["observations"][0]["url"] = "https://private.invalid"
    with pytest.raises(ValueError, match="unsafe"):
        runner._build_artifact(
            codec_proof={},
            provenance={},
            live=live,
        )


def test_artifact_writer_requires_explicit_path_and_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    runner._write_artifact(path, {"status": "rejected"})
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "rejected"
    with pytest.raises(FileExistsError):
        runner._write_artifact(path, {"status": "complete"})


def test_main_preflights_existing_output_before_codec_or_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "existing.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "_run_local_codec_proof",
        lambda: (_ for _ in ()).throw(AssertionError("proof must not run")),
    )
    with pytest.raises(FileExistsError):
        runner.main(output=str(path))
