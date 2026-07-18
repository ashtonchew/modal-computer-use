from __future__ import annotations

import json
from pathlib import Path

import pytest

from modal_computer_use.benchmarks.artifacts import (
    generate_sanitized_provider_benchmark,
    sanitize_provider_benchmark,
    serialize_provider_benchmark,
    validate_sanitized_provider_benchmark,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_provider_artifact_generation_is_deterministic_and_removes_ephemeral_metadata(
    tmp_path,
) -> None:
    raw_payload = {
        "ok": True,
        "base_url": "https://user:password@example.com/connect?token=secret",
        "metadata": {
            "environment": {
                "modal_run_id": "run-secret",
                "modal_sandbox_id": "sb-secret",
                "browser": "chromium",
            }
        },
        "providers": {},
    }
    raw_bytes = json.dumps(raw_payload, sort_keys=True).encode()
    kwargs = {
        "raw_bytes": raw_bytes,
        "raw_artifact_path": "benchmark-results/candidates/provider.json",
        "harness_commit": "a" * 40,
        "status": "current_reference",
        "scope": "provider-default SDK paths, three measured iterations",
    }

    first = sanitize_provider_benchmark(raw_payload, **kwargs)
    second = sanitize_provider_benchmark(raw_payload, **kwargs)

    assert serialize_provider_benchmark(first) == serialize_provider_benchmark(second)
    serialized = serialize_provider_benchmark(first)
    assert "run-secret" not in serialized
    assert "sb-secret" not in serialized
    assert "password" not in serialized
    assert first["base_url"] is None

    raw_path = tmp_path / "raw.json"
    output_path = tmp_path / "sanitized.json"
    raw_path.write_bytes(raw_bytes)
    assert generate_sanitized_provider_benchmark(
        raw_path=raw_path,
        output_path=output_path,
        check=False,
        **{key: value for key, value in kwargs.items() if key != "raw_bytes"},
    )
    assert generate_sanitized_provider_benchmark(
        raw_path=raw_path,
        output_path=output_path,
        check=True,
        **{key: value for key, value in kwargs.items() if key != "raw_bytes"},
    )
    output_path.write_text("{}\n", encoding="utf-8")
    assert not generate_sanitized_provider_benchmark(
        raw_path=raw_path,
        output_path=output_path,
        check=True,
        **{key: value for key, value in kwargs.items() if key != "raw_bytes"},
    )


def test_provider_artifact_rejects_secret_bearing_keys() -> None:
    with pytest.raises(ValueError, match="secret-bearing key"):
        sanitize_provider_benchmark(
            {"base_url": None, "api_key": "secret"},
            raw_bytes=b"{}",
            raw_artifact_path="benchmark-results/raw.json",
            harness_commit="b" * 40,
            status="historical",
            scope="test",
        )


def test_rejected_provider_artifact_requires_status_reason() -> None:
    with pytest.raises(ValueError, match="requires status_reason"):
        sanitize_provider_benchmark(
            {"base_url": None},
            raw_bytes=b"{}",
            raw_artifact_path="benchmark-results/raw.json",
            harness_commit="b" * 40,
            status="rejected",
            scope="test",
        )


def test_candidate_provider_artifact_tracks_dirty_harness_diff() -> None:
    payload = sanitize_provider_benchmark(
        {"base_url": None},
        raw_bytes=b"{}",
        raw_artifact_path="benchmark-results/raw.json",
        harness_commit="c" * 40,
        harness_diff_sha256="d" * 64,
        status="candidate",
        scope="test",
    )

    assert payload["provenance"]["harness_state"] == "dirty"
    assert payload["provenance"]["harness_diff_sha256"] == "d" * 64


def test_tracked_provider_artifact_is_rejected_and_secret_safe() -> None:
    payload = json.loads(
        (REPO_ROOT / "benchmark-data/provider-compare-2026-07-18.json").read_text()
    )

    validate_sanitized_provider_benchmark(payload)
    assert payload["provenance"]["status"] == "rejected"
    assert payload["provenance"]["raw_artifact_tracked"] is False


def test_corrected_provider_candidate_has_complete_samples_and_provenance() -> None:
    payload = json.loads(
        (
            REPO_ROOT
            / "benchmark-data/provider-compare-2026-07-18-corrected-candidate.json"
        ).read_text()
    )

    validate_sanitized_provider_benchmark(payload)
    assert payload["ok"] is True
    assert payload["provenance"]["status"] == "candidate"
    assert payload["provenance"]["harness_state"] == "dirty"
    for provider in ("modal-daemon", "daytona", "e2b"):
        result = payload["providers"][provider]
        lifecycle = result["cases"]["product_create_to_first_screenshot"]
        assert result["status"] == "ok"
        assert lifecycle["status"] == "ok"
        assert len(lifecycle["samples_ms"]) == 3
        assert result["cases"]["cold_create_to_ready"]["deprecated"] is True
