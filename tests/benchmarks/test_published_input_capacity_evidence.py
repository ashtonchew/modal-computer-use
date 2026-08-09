from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modal_computer_use.benchmarks.input_capacity_gate import (
    validate_input_capacity_artifact,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = tuple(
    ROOT / "benchmark-data" / f"input-capacity-run-{index}-2026-08-08.json"
    for index in range(1, 4)
)
REPORT = ROOT / "docs" / "benchmark-results-2026-08-08-input-capacity.md"
SOURCE_SHA = "eee2b9456c76474a5b50a857af899ff11ca70a32"


def _artifact(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    validate_input_capacity_artifact(payload)
    return payload


def test_published_capacity_artifacts_pass_the_offline_gate() -> None:
    artifacts = [_artifact(path) for path in ARTIFACTS]

    assert [item["summary"]["weighted_tokens_per_sec"] for item in artifacts] == [
        527.398,
        505.135,
        380.704,
    ]
    for artifact in artifacts:
        assert artifact["status"] == "complete"
        assert artifact["failures"] == []
        assert artifact["retries"] == 0
        assert artifact["cleanup"] == {"succeeded": True, "survivors": 0}
        assert artifact["configuration"]["image_identity"] == f"inline-source-{SOURCE_SHA}"
        assert artifact["configuration"]["input_rate_limit_per_sec"] == 2_000
        assert artifact["configuration"]["input_rate_limit_burst"] == 4_000
        assert len(artifact["observations"]) == 80


def test_published_capacity_report_matches_artifact_decision() -> None:
    report = REPORT.read_text(encoding="utf-8")

    for value in ("527.398", "505.135", "380.704", SOURCE_SHA):
        assert value in report
    assert "100 normalized input-work tokens per second" in report
    assert "400-token burst" in report
    assert "Do not promote the earlier `500/1000` or `200/400` candidates" in report
    assert "240 measured batches" in report
    assert "zero survivors" in report
