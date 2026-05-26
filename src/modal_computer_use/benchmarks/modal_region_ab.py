from __future__ import annotations

from typing import Any

DEFAULT_MODAL_REGION_AB_REGIONS: tuple[str, ...] = ("default", "us-west", "us-east")

_ENCODING_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("http_binary_0b_p50_ms", "http_binary", 0),
    ("websocket_binary_envelope_0b_p50_ms", "websocket_binary_envelope", 0),
    (
        "websocket_json_metadata_binary_payload_0b_p50_ms",
        "websocket_json_metadata_binary_payload",
        0,
    ),
    ("http_binary_250kb_p50_ms", "http_binary", 250 * 1024),
    (
        "websocket_binary_envelope_250kb_p50_ms",
        "websocket_binary_envelope",
        250 * 1024,
    ),
    (
        "websocket_json_metadata_binary_payload_250kb_p50_ms",
        "websocket_json_metadata_binary_payload",
        250 * 1024,
    ),
)


def modal_region_ab_comparison(runs: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    fastest_region: str | None = None
    fastest_ms: float | None = None
    for region, result in runs.items():
        summary = _transport_floor_summary(result)
        fastest_case = _dict_value(summary.get("fastest_floor_case"))
        region_fastest_ms = _float_value(fastest_case.get("p50_ms"))
        if region_fastest_ms is not None and (
            fastest_ms is None or region_fastest_ms < fastest_ms
        ):
            fastest_ms = region_fastest_ms
            fastest_region = region
        row = {
            "ok": bool(result.get("ok")),
            "fastest_floor_case": fastest_case.get("case"),
            "fastest_floor_encoding": fastest_case.get("transport_encoding"),
            "fastest_floor_p50_ms": region_fastest_ms,
            "fastest_floor_inlier_mean_ms": _float_value(
                fastest_case.get("inlier_mean_ms"),
            ),
            "fastest_floor_outlier_count": _int_value(fastest_case.get("outlier_count")),
        }
        for key, encoding, size_bytes in _ENCODING_COLUMNS:
            row[key] = _transport_floor_case_p50(summary, encoding, size_bytes)
        rows[region] = row

    for row in rows.values():
        region_fastest_ms = row["fastest_floor_p50_ms"]
        row["delta_vs_fastest_ms"] = (
            None
            if region_fastest_ms is None or fastest_ms is None
            else region_fastest_ms - fastest_ms
        )
        row["ratio_vs_fastest"] = (
            None
            if region_fastest_ms is None or fastest_ms is None or fastest_ms == 0
            else region_fastest_ms / fastest_ms
        )
    return {
        "fastest_region": fastest_region,
        "fastest_floor_p50_ms": fastest_ms,
        "regions": rows,
    }


def modal_region_ab_markdown_summary(result: dict[str, Any]) -> str:
    runs = _dict_value(result.get("runs"))
    comparison = modal_region_ab_comparison(runs) if runs else _dict_value(result.get("comparison"))
    rows = _dict_value(comparison.get("regions"))
    metadata = _dict_value(result.get("metadata"))
    title = "Modal region benchmark"
    ingress = metadata.get("modal_ingress")
    http_version = metadata.get("daemon_http_version")
    if isinstance(ingress, str) and isinstance(http_version, str):
        title = f"{title} ({ingress}, HTTP/{http_version})"

    lines = [
        f"### {title}",
        "",
    ]
    caller_region_label = metadata.get("caller_region_label")
    if isinstance(caller_region_label, str) and caller_region_label:
        lines.extend([f"Caller region label: `{caller_region_label}`.", ""])
    lines.extend(
        [
            (
                "| Region | Fastest 0B p50 | Delta vs fastest | Fastest encoding | "
                "HTTP 0B | WS envelope 0B | WS JSON 0B | WS envelope 250KB |"
            ),
            "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for region, row in rows.items():
        if not isinstance(row, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    str(region),
                    _format_ms(row.get("fastest_floor_p50_ms")),
                    _format_ms(row.get("delta_vs_fastest_ms")),
                    _format_text(row.get("fastest_floor_encoding")),
                    _format_ms(row.get("http_binary_0b_p50_ms")),
                    _format_ms(row.get("websocket_binary_envelope_0b_p50_ms")),
                    _format_ms(row.get("websocket_json_metadata_binary_payload_0b_p50_ms")),
                    _format_ms(row.get("websocket_binary_envelope_250kb_p50_ms")),
                ]
            )
            + " |"
        )

    fastest_region = comparison.get("fastest_region")
    fastest_ms = comparison.get("fastest_floor_p50_ms")
    if isinstance(fastest_region, str) and isinstance(fastest_ms, int | float):
        lines.extend(
            [
                "",
                f"Fastest region: `{fastest_region}` at `{float(fastest_ms):.1f}ms` p50.",
            ]
        )
    return "\n".join(lines)


def _transport_floor_summary(result: dict[str, Any]) -> dict[str, Any]:
    surface = _dict_value(_dict_value(result.get("surfaces")).get("daemon-transport-floor"))
    return _dict_value(surface.get("transport_floor_summary"))


def _transport_floor_case_p50(
    summary: dict[str, Any],
    encoding: str,
    size_bytes: int,
) -> float | None:
    encoding_payload = _dict_value(_dict_value(summary.get("encodings")).get(encoding))
    for case in encoding_payload.get("cases", []):
        if not isinstance(case, dict):
            continue
        if case.get("requested_size_bytes") == size_bytes:
            return _float_value(case.get("p50_ms"))
    return None


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float_value(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _int_value(value: object) -> int | None:
    return int(value) if isinstance(value, int) else None


def _format_ms(value: object) -> str:
    if isinstance(value, int | float):
        return f"{float(value):.1f}ms"
    return "n/a"


def _format_text(value: object) -> str:
    return f"`{value}`" if isinstance(value, str) and value else "n/a"
