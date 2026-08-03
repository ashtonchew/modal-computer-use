from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

import pytest

from modal_computer_use.benchmarks.measurement import _percentile

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_benchmark_artifact(name: str) -> tuple[Path, dict[str, Any]]:
    path = REPO_ROOT / "benchmark-data" / name
    return path, json.loads(path.read_text())


def assert_sha256(path: Path, expected: str) -> None:
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


def assert_recomputed_summary(
    samples: list[float], summary: dict[str, float], *, expected_count: int = 30
) -> None:
    assert len(samples) == expected_count
    assert summary["mean"] == pytest.approx(statistics.fmean(samples), abs=1e-12)
    assert summary["p50"] == float(statistics.median(samples))
    assert summary["p95"] == pytest.approx(_percentile(sorted(samples), 95), abs=1e-12)


def assert_artifact_secret_safe(path: Path) -> None:
    serialized = path.read_text().lower()
    for forbidden in (
        "modal.host",
        "sb-",
        "run_",
        "api_key",
        "access_token",
        "base_url",
        "bearer",
        "://",
        "/users/",
        "sandbox_id",
    ):
        assert forbidden not in serialized
