from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path

from modal_computer_use.benchmarks.provider_results import (
    OPAQUE_TZAFON_SETTLE_SENTENCE,
    render_provider_results_markdown,
    validate_provider_results,
)

REPORT_SOURCE_SHA = "e57ea35f04efdec4100ffa44196ee8599e9811b2"


def test_tracked_provider_results_match_renderer_and_source_digest() -> None:
    provider_path = Path("benchmark-data/provider-compare-coordinate-command-2026-07-26.json")
    optimized_path = Path("benchmark-data/modal-optimized-provider-2026-07-26.json")
    observation_path = Path("benchmark-data/modal-observation-2026-07-26.json")
    combined_path = Path("benchmark-data/provider-results-2026-07-26.json")
    report_path = Path("docs/benchmark-results-2026-07-26-provider-results.md")
    provider_bytes = provider_path.read_bytes()
    optimized_bytes = optimized_path.read_bytes()
    observation_bytes = observation_path.read_bytes()
    provider = json.loads(provider_bytes)
    combined = json.loads(combined_path.read_text(encoding="utf-8"))

    validate_provider_results(combined)
    provenance = combined["provenance"]
    assert provenance["report_source_sha"] == REPORT_SOURCE_SHA
    assert provenance["evidence_harness_sha"] == REPORT_SOURCE_SHA
    assert provider["provenance"]["harness_commit"] == provenance[
        "evidence_harness_sha"
    ]
    assert provenance["inputs"] == [
        {"role": role, "sha256": hashlib.sha256(raw).hexdigest()}
        for role, raw in (
            ("sanitized_provider_defaults", provider_bytes),
            ("sanitized_modal_optimized", optimized_bytes),
            ("sanitized_modal_observation", observation_bytes),
        )
    ]

    rendered = render_provider_results_markdown(combined)
    report = report_path.read_text(encoding="utf-8")
    assert report == rendered
    assert report.count("| Case | Modal optimized |") == 1
    assert OPAQUE_TZAFON_SETTLE_SENTENCE in report
    assert "70.88 / 83.49 ms" in report
    for forbidden in (
        "Tzafon experimental",
        "XDamage",
        "access_token",
        "api_key",
        "noVNC",
        "resource_id",
    ):
        assert forbidden.lower() not in rendered.lower()


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    rank = 0.95 * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    p95 = ordered[lower] + (rank - lower) * (ordered[upper] - ordered[lower])
    return {
        "min_ms": min(samples),
        "max_ms": max(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": p95,
    }


def test_tracked_modal_values_are_independently_recomputed_from_allowlisted_samples() -> None:
    optimized = json.loads(
        Path("benchmark-data/modal-optimized-provider-2026-07-26.json").read_text()
    )
    observation = json.loads(
        Path("benchmark-data/modal-observation-2026-07-26.json").read_text()
    )
    combined = json.loads(Path("benchmark-data/provider-results-2026-07-26.json").read_text())

    for row in combined["headline"]["rows"]:
        case = optimized["cases"][row["case"]]
        value = row["values"]["modal_optimized"]
        assert value["sample_count"] == len(case["samples_ms"]) == 30
        assert {key: value[key] for key in _summary(case["samples_ms"])} == _summary(
            case["samples_ms"]
        )

    experiment = combined["experiment"]
    summary = _summary(observation["case"]["samples_ms"])
    assert experiment["p50_ms"] == summary["p50_ms"]
    assert experiment["p95_ms"] == summary["p95_ms"]
