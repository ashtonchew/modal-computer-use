from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from modal_computer_use.benchmarks.modal_v2_placement import (
    DEFAULT_CLOUD_REQUESTS,
    build_placement_capability_binding,
    placement_capability_sha256,
    run_placement_capability_matrix,
    serialize_placement_capability,
    validate_placement_artifact_path,
    validate_placement_capability_matrix,
    validate_placement_output_path,
)
from modal_computer_use.sandbox import ModalCandidatePlacementProbe


def test_capability_matrix_selects_unconstrained_exact_common_placement() -> None:
    assert DEFAULT_CLOUD_REQUESTS == (None, "aws", "gcp", "oci")

    def probe(**kwargs):
        return ModalCandidatePlacementProbe(
            run_id=kwargs["run_id"],
            backend=kwargs["backend"],
            requested_cloud=kwargs["cloud"],
            requested_region=kwargs["region"],
            actual_cloud="CLOUD_PROVIDER_AWS",
            actual_region="us-west-2",
            i6pn_enabled=kwargs["i6pn"],
            i6pn_verified=kwargs["i6pn"],
            sandbox_created=True,
            cleanup_succeeded=True,
            status="valid",
        )

    payload = run_placement_capability_matrix(
        run_id="placement-test-exact",
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        image_revision="a" * 40,
        region="us-west",
        target_cpu=4.0,
        target_memory_mib=8192,
        cloud_requests=(None,),
        probe=probe,
    )

    assert payload["backend_causal_comparison_available"] is True
    assert payload["selected_request"] == {
        "cloud": None,
        "region": "us-west",
        "actual_placement": {
            "cloud": "CLOUD_PROVIDER_AWS",
            "region": "us-west-2",
        },
    }
    assert len(placement_capability_sha256(payload)) == 64
    assert hashlib.sha256(serialize_placement_capability(payload)).hexdigest() == (
        placement_capability_sha256(payload)
    )
    validate_placement_capability_matrix(payload)
    binding = build_placement_capability_binding(
        payload,
        artifact_path=(
            "benchmark-results/modal-v2-candidate-2026-07-19/"
            "diagnostics/placement-capability.json"
        ),
    )
    assert binding["selected_request"] == payload["selected_request"]
    assert binding["sha256"] == placement_capability_sha256(payload)
    assert binding["run_id"] == "placement-test-exact"
    assert binding["evidence"] == payload

    with pytest.raises(ValueError, match="under benchmark-results"):
        build_placement_capability_binding(
            payload,
            artifact_path="benchmark-results/../docs/capability.json",
        )
    with pytest.raises(ValueError, match="under benchmark-results"):
        validate_placement_artifact_path("benchmark-results/../docs/capability.json")

    missing_run_id = dict(payload)
    missing_run_id.pop("run_id")
    try:
        validate_placement_capability_matrix(missing_run_id)
    except ValueError as exc:
        assert "exact run ID" in str(exc)
    else:
        raise AssertionError("placement capability must retain its cleanup run ID")

    tampered = copy.deepcopy(payload)
    tampered["candidates"][0]["eligible"] = False
    try:
        validate_placement_capability_matrix(tampered)
    except ValueError as exc:
        assert "does not match its observations" in str(exc)
    else:
        raise AssertionError("placement eligibility must be recomputed from observations")

    tampered_region = copy.deepcopy(payload)
    tampered_region["candidates"][0]["requested_region"] = "us-east"
    with pytest.raises(ValueError, match="candidate is incomplete"):
        validate_placement_capability_matrix(tampered_region)


def test_placement_output_path_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    benchmark_root = tmp_path / "benchmark-results"
    benchmark_root.mkdir()
    outside = tmp_path / "docs"
    outside.mkdir()
    (benchmark_root / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="symlink"):
        validate_placement_output_path(
            Path("benchmark-results/link/capability.json"),
            benchmark_root=Path("benchmark-results"),
        )


def test_capability_matrix_rejects_cross_cloud_targets_without_ratios() -> None:
    def probe(**kwargs):
        actual_cloud = (
            "CLOUD_PROVIDER_AWS" if kwargs["backend"] == "v1" else "CLOUD_PROVIDER_AZURE"
        )
        actual_region = "us-west-2" if kwargs["backend"] == "v1" else "westus3"
        return ModalCandidatePlacementProbe(
            run_id=kwargs["run_id"],
            backend=kwargs["backend"],
            requested_cloud=kwargs["cloud"],
            requested_region=kwargs["region"],
            actual_cloud=actual_cloud,
            actual_region=actual_region,
            i6pn_enabled=kwargs["i6pn"],
            i6pn_verified=kwargs["i6pn"],
            sandbox_created=True,
            cleanup_succeeded=True,
            status="valid",
        )

    payload = run_placement_capability_matrix(
        run_id="placement-test-cross-cloud",
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        image_revision="a" * 40,
        region="us-west",
        target_cpu=4.0,
        target_memory_mib=8192,
        cloud_requests=(None,),
        probe=probe,
    )

    assert payload["backend_causal_comparison_available"] is False
    assert payload["selected_request"] is None
    assert payload["classification"] == "descriptive-placement-capability-only"
    assert "lack one exact observed placement" in payload["candidates"][0]["reasons"][-1]
    try:
        build_placement_capability_binding(
            payload,
            artifact_path="benchmark-results/modal-v2-candidate-2026-07-19/capability.json",
        )
    except ValueError as exc:
        assert "no comparable placement" in str(exc)
    else:
        raise AssertionError("descriptive-only placement must not bind a preregistration")


def test_capability_matrix_rejects_failed_cleanup() -> None:
    def probe(**kwargs):
        return ModalCandidatePlacementProbe(
            run_id=kwargs["run_id"],
            backend=kwargs["backend"],
            requested_cloud=kwargs["cloud"],
            requested_region=kwargs["region"],
            actual_cloud="CLOUD_PROVIDER_GCP",
            actual_region="us-west1-a",
            i6pn_enabled=kwargs["i6pn"],
            i6pn_verified=kwargs["i6pn"],
            sandbox_created=True,
            cleanup_succeeded=kwargs["backend"] == "v1",
            status="valid",
        )

    payload = run_placement_capability_matrix(
        run_id="placement-test-cleanup",
        source_sha="a" * 40,
        generated_at="2026-07-19T00:00:00Z",
        image_revision="a" * 40,
        region="us-west",
        target_cpu=4.0,
        target_memory_mib=8192,
        cloud_requests=("gcp",),
        probe=probe,
    )

    assert payload["backend_causal_comparison_available"] is False
    assert any("cleanup failed" in reason for reason in payload["candidates"][0]["reasons"])
