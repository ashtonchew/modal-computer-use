from __future__ import annotations

from modal_computer_use.benchmarks.modal_region_ab import (
    modal_region_ab_comparison,
    modal_region_ab_markdown_summary,
)


def test_modal_region_ab_comparison_reads_real_transport_floor_shape() -> None:
    comparison = modal_region_ab_comparison(
        {
            "default": _region_result("default", p50=97.3),
            "us-west": _region_result("us-west", p50=51.4),
        }
    )

    assert comparison["fastest_region"] == "us-west"
    assert comparison["fastest_floor_p50_ms"] == 51.4
    default = comparison["regions"]["default"]
    assert default["fastest_floor_case"] == "transport_floor_websocket_binary_envelope_0b"
    assert default["fastest_floor_encoding"] == "websocket_binary_envelope"
    assert default["fastest_floor_p50_ms"] == 97.3
    assert default["fastest_floor_inlier_mean_ms"] == 96.3
    assert default["fastest_floor_outlier_count"] == 1
    assert default["http_binary_0b_p50_ms"] == 117.3
    assert default["websocket_binary_envelope_250kb_p50_ms"] == 157.3
    assert default["delta_vs_fastest_ms"] == 45.9
    assert comparison["regions"]["us-west"]["ratio_vs_fastest"] == 1.0


def test_modal_region_ab_markdown_summary_renders_copyable_table() -> None:
    result = {
        "benchmark": "modal-region-ab",
        "metadata": {
            "modal_ingress": "attested-tunnel",
            "daemon_http_version": "1.1",
        },
        "runs": {
            "default": _region_result("default", p50=97.3),
            "us-west": _region_result("us-west", p50=51.4),
        },
    }

    markdown = modal_region_ab_markdown_summary(result)

    assert "### Modal region benchmark (attested-tunnel, HTTP/1.1)" in markdown
    assert "| Region | Fastest 0B p50 | Delta vs fastest |" in markdown
    assert "| default | 97.3ms | 45.9ms |" in markdown
    assert "| us-west | 51.4ms | 0.0ms |" in markdown
    assert "Fastest region: `us-west` at `51.4ms` p50." in markdown


def test_modal_region_ab_markdown_summary_recomputes_stale_comparison() -> None:
    result = {
        "benchmark": "modal-region-ab",
        "metadata": {
            "modal_ingress": "attested-tunnel",
            "daemon_http_version": "1.1",
        },
        "runs": {
            "default": _region_result("default", p50=97.3),
            "us-west": _region_result("us-west", p50=51.4),
        },
        "comparison": {
            "fastest_region": "default",
            "fastest_floor_p50_ms": 97.3,
            "regions": {
                "default": {
                    "fastest_floor_p50_ms": 97.3,
                    "delta_vs_fastest_ms": 0.0,
                    "fastest_floor_encoding": "websocket_binary_envelope",
                    "http_binary_0b_p50_ms": None,
                    "websocket_binary_envelope_0b_p50_ms": None,
                    "websocket_json_metadata_binary_payload_0b_p50_ms": None,
                    "websocket_binary_envelope_250kb_p50_ms": None,
                }
            },
        },
    }

    markdown = modal_region_ab_markdown_summary(result)

    assert "Fastest region: `us-west` at `51.4ms` p50." in markdown
    assert "| default | 97.3ms | 45.9ms | `websocket_binary_envelope` | 117.3ms |" in markdown


def _region_result(region: str, *, p50: float) -> dict[str, object]:
    return {
        "ok": True,
        "surfaces": {
            "daemon-transport-floor": {
                "metadata": {"environment": {"modal_region_label": region}},
                "transport_floor_summary": {
                    "encodings": {
                        "http_binary": {
                            "cases": [
                                _case("transport_floor_http_binary_0b", 0, p50 + 20.0),
                                _case(
                                    "transport_floor_http_binary_250kb",
                                    250 * 1024,
                                    p50 + 60.0,
                                ),
                            ],
                        },
                        "websocket_binary_envelope": {
                            "cases": [
                                _case(
                                    "transport_floor_websocket_binary_envelope_0b",
                                    0,
                                    p50,
                                    outliers=1,
                                ),
                                _case(
                                    "transport_floor_websocket_binary_envelope_250kb",
                                    250 * 1024,
                                    p50 + 60.0,
                                ),
                            ],
                        },
                        "websocket_json_metadata_binary_payload": {
                            "cases": [
                                _case(
                                    "transport_floor_websocket_json_metadata_binary_payload_0b",
                                    0,
                                    p50 + 10.0,
                                ),
                                _case(
                                    "transport_floor_websocket_json_metadata_binary_payload_250kb",
                                    250 * 1024,
                                    p50 + 70.0,
                                ),
                            ],
                        },
                    },
                    "fastest_floor_case": {
                        "case": "transport_floor_websocket_binary_envelope_0b",
                        "requested_size_bytes": 0,
                        "p50_ms": p50,
                        "inlier_mean_ms": p50 - 1.0,
                        "outlier_count": 1,
                        "transport_encoding": "websocket_binary_envelope",
                    },
                },
            }
        },
        "failures": [],
    }


def _case(name: str, size_bytes: int, p50: float, *, outliers: int = 0) -> dict[str, object]:
    return {
        "case": name,
        "requested_size_bytes": size_bytes,
        "p50_ms": p50,
        "inlier_mean_ms": p50 - 1.0,
        "outlier_count": outliers,
    }
