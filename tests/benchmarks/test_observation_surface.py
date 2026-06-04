from __future__ import annotations

import pytest

from modal_computer_use.benchmarks import observation_surface
from modal_computer_use.benchmarks.observation_surface import (
    _add_frame_observations,
    _add_observation_latency_diagnosis,
    _frame_observation,
    _paired_ab_comparison,
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


def test_transport_probe_setup_failure_returns_failed_case(monkeypatch) -> None:
    def fake_transport(*_args, **_kwargs):
        raise TimeoutError("timed out while waiting for handshake response")

    monkeypatch.setattr(observation_surface, "ObservationStreamTransport", fake_transport)

    result = observation_surface._run_observation_transport_probe_benchmark(
        base_url="https://daemon.example",
        token="secret-token",
        iterations=1,
        warmup_iterations=0,
        size_bytes=0,
        connect_backoff_seconds=0,
    )

    assert result["status"] == "failed"
    assert result["successful_iterations"] == 0
    assert result["failures"][0]["phase"] == "setup"
    assert result["failures"][0]["type"] == "TimeoutError"
    assert result["setup"] == {
        "attempts": 0,
        "retry_count": 0,
        "elapsed_ms": None,
        "retry_errors": [],
    }
    assert "secret-token" not in str(result)


def test_transport_probe_records_websocket_setup_retries(monkeypatch) -> None:
    class FakeTransport:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.setup_attempts = 2
            self.setup_elapsed_ms = 25.0
            self.setup_retry_errors = [
                {
                    "type": "TimeoutError",
                    "message": "timed out while waiting for handshake response",
                }
            ]

        def transport_probe(self, *, size_bytes, frame_encoding=None):
            return {
                "size_bytes": size_bytes,
                "requested_size_bytes": size_bytes,
                "frame_encoding": frame_encoding or "json-binary",
            }

        def close(self):
            pass

    monkeypatch.setattr(observation_surface, "ObservationStreamTransport", FakeTransport)

    result = observation_surface._run_observation_transport_probe_benchmark(
        base_url="https://daemon.example",
        token="secret-token",
        iterations=1,
        warmup_iterations=0,
        size_bytes=1024,
        connect_backoff_seconds=0,
    )

    assert result["status"] == "ok"
    assert result["setup"] == {
        "attempts": 2,
        "retry_count": 1,
        "elapsed_ms": 25.0,
        "retry_errors": [
            {
                "type": "TimeoutError",
                "message": "timed out while waiting for handshake response",
            }
        ],
    }


def test_causal_binary_envelope_cases_use_production_action_observe_flags(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    client = object()

    def fake_action_observe_benchmark(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "name": kwargs["name"]}

    monkeypatch.setattr(
        observation_surface,
        "_run_observation_action_click_observe_change_benchmark",
        fake_action_observe_benchmark,
    )

    factories = observation_surface._observation_case_factories(
        base_url="http://daemon.test",
        token=None,
        client=client,  # type: ignore[arg-type]
        iterations=1,
        warmup_iterations=0,
    )

    signal = factories[
        "observation_action_click_act_and_observe_auto_signal_binary_envelope_production"
    ]()
    region = factories[
        "observation_action_click_act_and_observe_auto_region_binary_envelope_production"
    ]()

    assert signal["status"] == "ok"
    assert region["status"] == "ok"
    assert calls == [
        {
            "base_url": "http://daemon.test",
            "token": None,
            "client": client,
            "iterations": 1,
            "warmup_iterations": 0,
            "name": (
                "observation_action_click_act_and_observe_auto_signal_"
                "binary_envelope_production"
            ),
            "poll_strategy": "adaptive",
            "change_signal": "auto",
            "frame_encoding": "binary-envelope",
            "transport_timing": False,
            "causal_action_observe": True,
        },
        {
            "base_url": "http://daemon.test",
            "token": None,
            "client": client,
            "iterations": 1,
            "warmup_iterations": 0,
            "name": (
                "observation_action_click_act_and_observe_auto_region_"
                "binary_envelope_production"
            ),
            "poll_strategy": "adaptive",
            "change_detection": "auto_region",
            "change_signal": "auto",
            "frame_encoding": "binary-envelope",
            "transport_timing": False,
            "causal_action_observe": True,
        },
    ]


def test_sdk_default_causal_case_uses_auto_policy(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    client = object()

    def fake_action_observe_benchmark(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "name": kwargs["name"]}

    monkeypatch.setattr(
        observation_surface,
        "_run_observation_action_click_observe_change_benchmark",
        fake_action_observe_benchmark,
    )

    factories = observation_surface._observation_case_factories(
        base_url="http://daemon.test",
        token=None,
        client=client,  # type: ignore[arg-type]
        iterations=1,
        warmup_iterations=0,
    )

    result = factories["observation_action_click_act_and_observe_sdk_default_production"]()

    assert result["status"] == "ok"
    assert calls == [
        {
            "base_url": "http://daemon.test",
            "token": None,
            "client": client,
            "iterations": 1,
            "warmup_iterations": 0,
            "name": "observation_action_click_act_and_observe_sdk_default_production",
            "poll_strategy": "adaptive",
            "change_detection": "auto",
            "change_signal": "auto",
            "transport_timing": False,
            "causal_action_observe": True,
            "use_sdk_default_frame_encoding": True,
        }
    ]


def test_action_observe_benchmark_keeps_raw_json_binary_matrix_explicit(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_measure(**kwargs):
        captured.update(kwargs)
        return [1.0], [{"frame_encoding": kwargs["frame_encoding"] or "binary-envelope"}]

    monkeypatch.setattr(observation_surface, "_measure_stream_action_capture_loop", fake_measure)
    monkeypatch.setattr(observation_surface, "_open_click_toggle_page", lambda _client: None)

    result = observation_surface._run_observation_action_click_observe_change_benchmark(
        base_url="http://daemon.test",
        token=None,
        client=object(),  # type: ignore[arg-type]
        iterations=1,
        warmup_iterations=0,
        transport_timing=False,
        causal_action_observe=True,
    )

    assert captured["frame_encoding"] == "json-binary"
    assert result["frame_encoding"] == "json-binary"
    assert result["frame_encoding_policy"] == "benchmark-explicit"


def test_action_observe_benchmark_can_measure_sdk_default_frame_encoding(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_measure(**kwargs):
        captured.update(kwargs)
        return [1.0], [{"frame_encoding": kwargs["frame_encoding"] or "binary-envelope"}]

    monkeypatch.setattr(observation_surface, "_measure_stream_action_capture_loop", fake_measure)
    monkeypatch.setattr(observation_surface, "_open_click_toggle_page", lambda _client: None)

    result = observation_surface._run_observation_action_click_observe_change_benchmark(
        base_url="http://daemon.test",
        token=None,
        client=object(),  # type: ignore[arg-type]
        iterations=1,
        warmup_iterations=0,
        transport_timing=False,
        causal_action_observe=True,
        use_sdk_default_frame_encoding=True,
    )

    assert captured["frame_encoding"] is None
    assert result["frame_encoding"] == "binary-envelope"
    assert result["frame_encoding_policy"] == "sdk-default"


def test_causal_action_observe_diagnostic_includes_sdk_default_case() -> None:
    assert (
        "observation_action_click_act_and_observe_sdk_default_production"
        in observation_surface.CAUSAL_ACTION_OBSERVE_DIAGNOSTIC_CASES
    )
    assert (
        "observation_action_click_act_and_observe_paired_envelope_ab_production"
        in observation_surface.CAUSAL_ACTION_OBSERVE_DIAGNOSTIC_CASES
    )
    assert (
        "observation_action_click_act_and_observe_paired_dirty_producer_ab_production"
        in observation_surface.CAUSAL_ACTION_OBSERVE_DIAGNOSTIC_CASES
    )


def test_paired_ab_comparison_reports_variant_win_rate() -> None:
    result = _paired_ab_comparison([-4.0, 2.0, -1.0, 0.0])

    assert result["status"] == "measured"
    assert result["samples"] == 4
    assert result["delta_summary_ms"]["p50"] == -0.5
    assert result["variant_wins"] == 2
    assert result["baseline_wins"] == 1
    assert result["ties"] == 1
    assert result["variant_win_rate"] == 0.5


def test_paired_envelope_case_reports_delta_samples(monkeypatch) -> None:
    monkeypatch.setattr(observation_surface, "_open_click_toggle_page", lambda _client: None)
    monkeypatch.setattr(
        observation_surface,
        "_measure_paired_stream_action_observe_loop",
        lambda **_: {
            "baseline_samples_ms": [100.0, 110.0],
            "variant_samples_ms": [80.0, 120.0],
            "paired_delta_samples_ms": [-20.0, 10.0],
            "paired_observations": [
                {
                    "pair_index": 0,
                    "order": ["baseline", "variant"],
                    "baseline_ms": 100.0,
                    "variant_ms": 80.0,
                    "delta_ms": -20.0,
                    "ratio": 0.8,
                }
            ],
        },
    )

    result = observation_surface._run_observation_action_click_paired_envelope_benchmark(
        base_url="http://daemon.test",
        token=None,
        client=object(),  # type: ignore[arg-type]
        iterations=2,
        warmup_iterations=0,
    )

    assert result["status"] == "ok"
    assert result["metric"] == "paired_delta_ms"
    assert result["summary_ms"]["p50"] == -5.0
    assert result["baseline"]["summary_ms"]["p50"] == 105.0
    assert result["variant"]["summary_ms"]["p50"] == 100.0
    assert result["paired_comparison"]["variant_wins"] == 1
    assert result["paired_comparison"]["baseline_wins"] == 1
    assert result["pairing"]["order_policy"] == "seeded_random_ab_ba"
    assert result["pairing"]["scope"] == (
        "same sandbox/client path/page/stream, per-command frame encoding"
    )


def test_paired_dirty_producer_case_reports_delta_samples(monkeypatch) -> None:
    monkeypatch.setattr(observation_surface, "_open_click_toggle_page", lambda _client: None)
    monkeypatch.setattr(
        observation_surface,
        "_measure_paired_stream_action_observe_loop",
        lambda **_: {
            "baseline_samples_ms": [120.0, 130.0],
            "variant_samples_ms": [90.0, 140.0],
            "paired_delta_samples_ms": [-30.0, 10.0],
            "paired_observations": [
                {
                    "pair_index": 0,
                    "order": ["baseline", "variant"],
                    "baseline_ms": 120.0,
                    "variant_ms": 90.0,
                    "delta_ms": -30.0,
                    "ratio": 0.75,
                }
            ],
        },
    )

    result = observation_surface._run_observation_action_click_paired_dirty_producer_benchmark(
        base_url="http://daemon.test",
        token=None,
        client=object(),  # type: ignore[arg-type]
        iterations=2,
        warmup_iterations=0,
    )

    assert result["status"] == "ok"
    assert result["metric"] == "paired_delta_ms"
    assert result["summary_ms"]["p50"] == -10.0
    assert result["baseline"]["dirty_frame_producer"] == "off"
    assert result["baseline"]["summary_ms"]["p50"] == 125.0
    assert result["variant"]["dirty_frame_producer"] == "auto"
    assert result["variant"]["summary_ms"]["p50"] == 115.0
    assert result["paired_comparison"]["variant_wins"] == 1
    assert result["paired_comparison"]["baseline_wins"] == 1
    assert result["pairing"]["order_policy"] == "seeded_random_ab_ba"
    assert result["pairing"]["scope"] == (
        "same sandbox/client path/page/stream, per-command dirty producer policy"
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


def test_frame_observations_record_per_iteration_diagnostics() -> None:
    result = {
        "summary_ms": {
            "p50": 12.0,
            "high_outlier_indices": [2],
        },
        "sample_stability": {"status": "outlier_sensitive"},
    }
    observations = [
        {
            "size_bytes": 100,
            "metadata_size_bytes": 200,
            "kind": "patch",
            "unchanged": False,
            "frame_encoding": "json-binary",
            "dirty_frame_producer": True,
            "dirty_frame_producer_used": True,
            "dirty_frame_producer_wait_budget_ms": 20,
            "dirty_frame_capture_region_source": "action_region",
            "dirty_frame_capture_region": {"x": 0, "y": 0, "width": 64, "height": 64},
            "change_stage_timing_ms": {"server_pre_emit_ms": 10.0},
            "action_observe_attribution_ms": {
                "action_end_to_signal_detect_ms": 5.0,
            },
            "benchmark_timing_ms": {"receive_frame_ms": 12.0},
            "screenshot_daemon_timing_ms": {"observation_total_ms": 7.0},
            "observation_transport_timing": {
                "server_emit_timing_ms": {"emit_total_ms": 0.5},
                "client_receive_timing_ms": {"payload_wait_ms": 1.0},
            },
        },
        {
            "size_bytes": 101,
            "kind": "patch",
            "unchanged": False,
            "dirty_frame_producer": True,
            "dirty_frame_producer_used": False,
            "dirty_frame_producer_fallback_reason": "no_changed_frame",
            "frame_poll_skipped_reason": "deadline_exhausted_after_region_confirmation",
            "dirty_region_confirmation_result": "unchanged",
            "change_wait_ms": 13.0,
            "benchmark_timing_ms": {"receive_frame_ms": 13.0},
        },
        {
            "size_bytes": 102,
            "kind": "patch",
            "unchanged": False,
            "dirty_frame_producer": True,
            "dirty_frame_producer_used": True,
            "dirty_frame_capture_region_source": "xdamage_dirty_rect",
            "xdamage_dirty_rect": {"x": 40, "y": 40, "width": 4, "height": 4},
            "xdamage_dirty_rects": [{"x": 40, "y": 40, "width": 4, "height": 4}],
            "xdamage_dirty_ratio": 0.0625,
            "change_wait_ms": 100.0,
            "benchmark_timing_ms": {"receive_frame_ms": 100.0},
        },
    ]

    _add_frame_observations(result, [12.0, 13.0, 100.0], observations)

    assert len(result["sample_observations"]) == 3
    first = result["sample_observations"][0]
    assert first["iteration"] == 0
    assert first["sample_ms"] == 12.0
    assert first["high_outlier"] is False
    assert first["dirty_frame_producer_wait_budget_ms"] == 20
    assert first["dirty_frame_capture_region_source"] == "action_region"
    assert first["server_emit_timing_ms"] == {"emit_total_ms": 0.5}
    assert first["client_receive_timing_ms"] == {"payload_wait_ms": 1.0}
    assert result["sample_observations"][1]["frame_poll_skipped_reason"] == (
        "deadline_exhausted_after_region_confirmation"
    )
    assert result["sample_observations"][1]["dirty_region_confirmation_result"] == "unchanged"

    outliers = result["outlier_observations"]
    assert len(outliers) == 1
    assert outliers[0]["iteration"] == 2
    assert outliers[0]["high_outlier"] is True
    assert outliers[0]["dirty_frame_capture_region_source"] == "xdamage_dirty_rect"
    assert outliers[0]["xdamage_dirty_rects"] == [
        {"x": 40, "y": 40, "width": 4, "height": 4}
    ]
    assert outliers[0]["benchmark_timing_ms"] == {"receive_frame_ms": 100.0}


def test_frame_observations_summarize_dirty_frame_producer_metadata() -> None:
    result = {"summary_ms": {"p50": 50.0}}
    observations = [
        {
            "size_bytes": 100,
            "unchanged": False,
            "dirty_frame_producer": True,
            "dirty_frame_producer_used": True,
            "dirty_frame_producer_wait_budget_ms": 20,
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
            "dirty_frame_producer_wait_budget_ms": 20,
            "dirty_frame_producer_fallback_reason": "no_changed_frame",
            "frame_poll_skipped_reason": "dirty_region_confirmation_changed",
            "dirty_region_confirmation_result": "changed",
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
    assert result["frame_poll_skipped_reasons"] == ["dirty_region_confirmation_changed"]
    assert result["dirty_region_confirmation_results"] == ["changed"]
    assert result["dirty_frame_producer_wait_budget_summary_ms"]["p50"] == 20.0
    assert result["dirty_frame_age_summary_ms"]["p50"] == 0.1
    assert result["change_stage_timing_summary_ms"]["dirty_producer_wait_ms"]["p50"] == 20.0
    assert (
        result["action_observe_attribution_summary_ms"]["action_end_to_signal_detect_ms"]["p50"]
        == 20.0
    )
    assert result["latency_diagnosis"]["bottleneck"] == "action_to_damage_signal"


def test_frame_observation_preserves_xdamage_dirty_metadata() -> None:
    class FakeFrame:
        def __init__(self) -> None:
            self.payload = b"patch"
            self.transport_timing = None
            self.metadata = {
                "dirty_frame_capture_region": {"x": 32, "y": 32, "width": 16, "height": 16},
                "dirty_frame_capture_region_source": "xdamage_dirty_rect",
                "dirty_frame_producer_wait_budget_ms": 20,
                "xdamage_dirty_rect": {"x": 40, "y": 40, "width": 4, "height": 4},
                "xdamage_dirty_rects": [{"x": 40, "y": 40, "width": 4, "height": 4}],
                "xdamage_dirty_ratio": 0.0625,
            }

    observation = _frame_observation(FakeFrame())

    assert observation["dirty_frame_capture_region"] == {
        "x": 32,
        "y": 32,
        "width": 16,
        "height": 16,
    }
    assert observation["dirty_frame_capture_region_source"] == "xdamage_dirty_rect"
    assert observation["dirty_frame_producer_wait_budget_ms"] == 20
    assert observation["xdamage_dirty_rect"] == {"x": 40, "y": 40, "width": 4, "height": 4}
    assert observation["xdamage_dirty_rects"] == [
        {"x": 40, "y": 40, "width": 4, "height": 4}
    ]
    assert observation["xdamage_dirty_ratio"] == 0.0625
