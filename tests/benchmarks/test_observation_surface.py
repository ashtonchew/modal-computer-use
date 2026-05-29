from __future__ import annotations

from modal_computer_use.benchmarks.observation_surface import (
    _add_frame_observations,
    _add_observation_latency_diagnosis,
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

    assert result["latency_diagnosis"] == {
        "bottleneck": "client_receive_or_tunnel_wait",
        "reason": "client receive wait after server pre-emit is at least 50ms p50",
        "sample_stability": "stable",
        "total_p50_ms": 110.0,
        "daemon_p50_ms": 18.0,
        "overhead_p50_ms": 92.0,
        "receive_minus_server_pre_emit_and_send_p50_ms": 70.0,
    }


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
        },
    ]

    _add_frame_observations(result, [50.0, 55.0], observations)

    assert result["dirty_frame_producer_frames"] == 2
    assert result["dirty_frame_producer_used_frames"] == 1
    assert result["dirty_frame_producer_fallback_reasons"] == ["no_changed_frame"]
    assert result["dirty_frame_age_summary_ms"]["p50"] == 0.1
    assert result["change_stage_timing_summary_ms"]["dirty_producer_wait_ms"]["p50"] == 20.0
