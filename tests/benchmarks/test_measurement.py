from __future__ import annotations

import pytest

from modal_computer_use.benchmarks.measurement import _case_result, _summary


def test_summary_reports_robust_latency_fields() -> None:
    summary = _summary([100.0, 101.0, 99.0, 100.5, 98.5, 102.0, 99.5, 100.0, 101.5, 300.0])

    assert summary["mean"] == pytest.approx(120.2)
    assert summary["p50"] == pytest.approx(100.25)
    assert summary["trimmed_mean"] == pytest.approx(100.4375)
    assert summary["mean_without_high_outliers"] == pytest.approx(100.2222222222)
    assert summary["high_outlier_count"] == 1
    assert summary["high_outlier_indices"] == [9]
    assert summary["jitter_ms"] is not None
    assert summary["mean_p50_delta_ratio"] is not None


def test_case_result_marks_outlier_sensitive_samples() -> None:
    result = _case_result(
        "observe_change",
        iterations=10,
        samples=[100.0, 101.0, 99.0, 100.5, 98.5, 102.0, 99.5, 100.0, 101.5, 300.0],
        failures=[],
    )

    assert result["sample_stability"]["status"] == "outlier_sensitive"
    assert result["sample_stability"]["high_outlier_indices"] == [9]


def test_case_result_marks_stable_samples() -> None:
    result = _case_result(
        "observe_change",
        iterations=5,
        samples=[98.0, 101.0, 100.0, 99.5, 100.5],
        failures=[],
    )

    assert result["sample_stability"]["status"] == "stable"
    assert result["sample_stability"]["high_outlier_count"] == 0
