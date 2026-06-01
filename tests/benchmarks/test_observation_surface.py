from __future__ import annotations

import pytest

from modal_computer_use.benchmarks import observation_surface
from modal_computer_use.benchmarks.observation_surface import (
    _add_frame_observations,
    _add_observation_latency_diagnosis,
)


def test_daemon_observation_surface_runs_only_selected_cases(monkeypatch) -> None:
    ran: list[str] = []

    monkeypatch.setattr(
        observation_surface,
        "_observation_case_factories",
        lambda **_: {
            "case_a": lambda: ran.append("case_a") or {"status": "ok"},
            "case_b": lambda: ran.append("case_b") or {"status": "ok"},
        },
    )

    result = observation_surface._run_daemon_observation_surface(
        base_url="http://daemon.test",
        token=None,
        client=object(),  # type: ignore[arg-type]
        iterations=1,
        warmup_iterations=0,
        environment_metadata=None,
        observation_cases=["case_b"],
    )

    assert ran == ["case_b"]
    assert list(result["cases"]) == ["case_b"]
    assert result["metadata"]["selected_cases"] == ["case_b"]


def test_daemon_observation_surface_rejects_unknown_case(monkeypatch) -> None:
    monkeypatch.setattr(
        observation_surface,
        "_observation_case_factories",
        lambda **_: {"case_a": lambda: {"status": "ok"}},
    )

    with pytest.raises(ValueError, match="unknown observation benchmark case: missing"):
        observation_surface._run_daemon_observation_surface(
            base_url="http://daemon.test",
            token=None,
            client=object(),  # type: ignore[arg-type]
            iterations=1,
            warmup_iterations=0,
            environment_metadata=None,
            observation_cases=["missing"],
        )


def test_observation_latency_diagnosis_identifies_client_receive_wait() -> None:
    result = {
        "summary_ms": {"p50": 110.0},
        "daemon_summary_ms": {"p50": 18.0},
        "overhead_summary_ms": {"p50": 92.0},
        "receive_minus_server_pre_emit_and_send_summary_ms": {"p50": 70.0},
        "sample_stability": {"status": "stable"},
    }

    _add_observation_latency_diagnosis(result)

    assert result["latency_diagnosis"]["bottleneck"] == "client_receive_or_tunnel_wait"
    assert (
        result["latency_diagnosis"]["reason"]
        == "client receive wait after server pre-emit is at least 50ms p50"
    )
    assert result["latency_diagnosis"]["sample_stability"] == "stable"
    assert result["latency_diagnosis"]["total_p50_ms"] == 110.0
    assert result["latency_diagnosis"]["daemon_p50_ms"] == 18.0
    assert result["latency_diagnosis"]["overhead_p50_ms"] == 92.0
    assert result["latency_diagnosis"]["action_end_to_signal_detect_p50_ms"] is None
    assert result["latency_diagnosis"]["receive_minus_server_pre_emit_and_send_p50_ms"] == 70.0


def test_observation_latency_diagnosis_identifies_daemon_work() -> None:
    result = {
        "summary_ms": {"p50": 40.0},
        "daemon_summary_ms": {"p50": 25.0},
        "overhead_summary_ms": {"p50": 15.0},
        "sample_stability": {"status": "outlier_sensitive"},
    }

    _add_observation_latency_diagnosis(result)

    assert result["latency_diagnosis"]["bottleneck"] == "daemon_capture_or_diff"
    assert result["latency_diagnosis"]["sample_stability"] == "outlier_sensitive"


def test_frame_observations_attach_latency_diagnosis() -> None:
    result = {
        "summary_ms": {"p50": 80.0},
        "sample_stability": {"status": "stable"},
    }
    observations = [
        {
            "size_bytes": 100,
            "unchanged": False,
            "frame_encoding": "json-binary",
            "screenshot_daemon_timing_ms": {"observation_total_ms": 15.0},
            "benchmark_timing_ms": {"receive_frame_ms": 70.0},
            "change_stage_timing_ms": {"server_pre_emit_ms": 10.0},
            "observation_transport_timing": {
                "server_emit_timing_ms": {"emit_total_ms": 0.5},
            },
        },
        {
            "size_bytes": 100,
            "unchanged": False,
            "frame_encoding": "json-binary",
            "screenshot_daemon_timing_ms": {"observation_total_ms": 16.0},
            "benchmark_timing_ms": {"receive_frame_ms": 72.0},
            "change_stage_timing_ms": {"server_pre_emit_ms": 11.0},
            "observation_transport_timing": {
                "server_emit_timing_ms": {"emit_total_ms": 0.5},
            },
        },
    ]

    _add_frame_observations(result, [80.0, 82.0], observations)

    assert result["latency_diagnosis"]["bottleneck"] == "client_receive_or_tunnel_wait"
    assert result["latency_diagnosis"]["sample_stability"] == "stable"


def test_frame_observations_summarize_dirty_frame_producer_metadata() -> None:
    result = {"summary_ms": {"p50": 50.0}}
    observations = [
        {
            "size_bytes": 100,
            "unchanged": False,
            "dirty_frame_producer": True,
            "dirty_frame_producer_used": True,
            "dirty_frame_age_ms": 0.1,
            "change_detected": True,
            "change_stage_timing_ms": {
                "server_pre_emit_ms": 20.0,
                "dirty_producer_wait_ms": 15.0,
            },
            "action_observe_attribution_ms": {
                "action_end_to_signal_detect_ms": 10.0,
                "action_end_to_pre_emit_ms": 20.0,
                "capture_start_to_delta_ready_ms": 3.0,
            },
        },
        {
            "size_bytes": 100,
            "unchanged": False,
            "dirty_frame_producer": True,
            "dirty_frame_producer_used": False,
            "dirty_frame_producer_fallback_reason": "no_changed_frame",
            "change_detected": True,
            "change_stage_timing_ms": {
                "server_pre_emit_ms": 30.0,
                "dirty_producer_wait_ms": 25.0,
            },
            "action_observe_attribution_ms": {
                "action_end_to_signal_detect_ms": 30.0,
                "action_end_to_pre_emit_ms": 30.0,
                "capture_start_to_delta_ready_ms": 5.0,
            },
        },
    ]

    _add_frame_observations(result, [50.0, 55.0], observations)

    assert result["dirty_frame_producer_frames"] == 2
    assert result["dirty_frame_producer_used_frames"] == 1
    assert result["dirty_frame_producer_fallback_reasons"] == ["no_changed_frame"]
    assert result["dirty_frame_age_summary_ms"]["p50"] == 0.1
    assert result["change_stage_timing_summary_ms"]["dirty_producer_wait_ms"]["p50"] == 20.0
    assert (
        result["action_observe_attribution_summary_ms"]["action_end_to_signal_detect_ms"]["p50"]
        == 20.0
    )
    assert result["latency_diagnosis"]["bottleneck"] == "action_to_damage_signal"
