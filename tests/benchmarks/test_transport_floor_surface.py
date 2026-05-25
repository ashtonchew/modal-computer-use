from __future__ import annotations

from modal_computer_use.benchmarks.transport_floor_surface import (
    _transport_floor_case_name,
    _transport_floor_summary,
)


def test_transport_floor_case_name_uses_result_metadata() -> None:
    assert (
        _transport_floor_case_name(
            {
                "transport_encoding": "websocket_binary_envelope",
                "requested_size_bytes": 5 * 1024,
            }
        )
        == "transport_floor_websocket_binary_envelope_5kb"
    )


def test_transport_floor_summary_detects_fixed_floor() -> None:
    cases = {
        "ws_0b": {
            "status": "ok",
            "transport_encoding": "websocket_json_metadata_binary_payload",
            "requested_size_bytes": 0,
            "summary_ms": {"p50": 80.0, "mean_without_high_outliers": 81.0},
        },
        "ws_250kb": {
            "name": "ws_250kb",
            "status": "ok",
            "transport_encoding": "websocket_json_metadata_binary_payload",
            "requested_size_bytes": 256000,
            "summary_ms": {"p50": 85.0, "mean_without_high_outliers": 84.0},
        },
        "http_0b": {
            "name": "http_0b",
            "status": "ok",
            "transport_encoding": "http_binary",
            "requested_size_bytes": 0,
            "summary_ms": {"p50": 70.0, "mean_without_high_outliers": 71.0},
        },
    }

    summary = _transport_floor_summary(cases)

    assert summary["fastest_floor_case"]["transport_encoding"] == "http_binary"
    assert summary["fastest_floor_case"]["case"] == "http_0b"
    assert summary["fastest_floor_case"]["p50_ms"] == 70.0
    assert (
        summary["encodings"]["websocket_json_metadata_binary_payload"]["payload_sensitive"]
        is False
    )
    assert "mostly fixed" in summary["interpretation"]


def test_transport_floor_summary_detects_payload_sensitive_path() -> None:
    cases = {
        "ws_0b": {
            "name": "ws_0b",
            "status": "ok",
            "transport_encoding": "websocket_binary_envelope",
            "requested_size_bytes": 0,
            "summary_ms": {"p50": 80.0, "mean_without_high_outliers": 80.0},
        },
        "ws_250kb": {
            "name": "ws_250kb",
            "status": "ok",
            "transport_encoding": "websocket_binary_envelope",
            "requested_size_bytes": 256000,
            "summary_ms": {"p50": 130.0, "mean_without_high_outliers": 130.0},
        },
    }

    summary = _transport_floor_summary(cases)

    assert summary["encodings"]["websocket_binary_envelope"]["payload_sensitive"] is True
    assert "payload-sensitive" in summary["interpretation"]
