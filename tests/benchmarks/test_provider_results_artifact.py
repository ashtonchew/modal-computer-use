from __future__ import annotations

import hashlib
import json
from pathlib import Path

from modal_computer_use.benchmarks.provider_results import (
    OPAQUE_TZAFON_SETTLE_SENTENCE,
    render_provider_results_markdown,
    validate_provider_results,
)

REPORT_SOURCE_SHA = "e57ea35f04efdec4100ffa44196ee8599e9811b2"


def test_tracked_provider_results_match_renderer_and_source_digest() -> None:
    provider_path = Path("benchmark-data/provider-compare-coordinate-command-2026-07-26.json")
    combined_path = Path("benchmark-data/provider-results-2026-07-26.json")
    report_path = Path("docs/benchmark-results-2026-07-26-provider-results.md")
    provider_bytes = provider_path.read_bytes()
    provider = json.loads(provider_bytes)
    combined = json.loads(combined_path.read_text(encoding="utf-8"))

    validate_provider_results(combined)
    provenance = combined["provenance"]
    assert provenance["report_source_sha"] == REPORT_SOURCE_SHA
    assert provenance["evidence_harness_sha"] == REPORT_SOURCE_SHA
    assert provider["provenance"]["harness_commit"] == provenance[
        "evidence_harness_sha"
    ]
    assert provenance["inputs"][0] == {
        "role": "sanitized_provider_defaults",
        "sha256": hashlib.sha256(provider_bytes).hexdigest(),
    }

    rendered = render_provider_results_markdown(combined)
    table = rendered[rendered.index("| Case |") : rendered.index("\n\n## Modal-only")]
    report = report_path.read_text(encoding="utf-8")
    assert report.count("| Case | Modal optimized |") == 1
    assert table in report
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
