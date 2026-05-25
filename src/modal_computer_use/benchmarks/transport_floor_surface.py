from __future__ import annotations

from typing import Any

from ..client import DaemonClient
from .measurement import _summary
from .observation_surface import (
    _run_observation_http_transport_probe_benchmark,
    _run_observation_transport_probe_benchmark,
)
from .surface_result import _surface_result

TRANSPORT_FLOOR_SIZES_BYTES: tuple[int, ...] = (
    0,
    1024,
    5 * 1024,
    50 * 1024,
    250 * 1024,
)


def _run_daemon_transport_floor_surface(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    environment_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    cases: dict[str, Any] = {}
    for size_bytes in TRANSPORT_FLOOR_SIZES_BYTES:
        websocket_default = _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=size_bytes,
        )
        websocket_envelope = _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=size_bytes,
            frame_encoding="binary-envelope",
        )
        http_binary = _run_observation_http_transport_probe_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=size_bytes,
        )
        cases[_transport_floor_case_name(websocket_default)] = websocket_default
        cases[_transport_floor_case_name(websocket_envelope)] = websocket_envelope
        cases[_transport_floor_case_name(http_binary)] = http_binary

    result = _surface_result(
        "daemon-transport-floor",
        cases=cases,
        metadata={
            "canonical_name": _transport_floor_canonical_name(environment_metadata),
            "transport": "daemon-transport-floor",
            "protocols": [
                "observation-websocket-json-binary",
                "observation-websocket-binary-envelope",
                "http-binary",
            ],
            "payload_sizes_bytes": list(TRANSPORT_FLOOR_SIZES_BYTES),
            "environment": {
                key: value
                for key, value in (environment_metadata or {}).items()
                if value is not None
            },
        },
    )
    result["transport_floor_summary"] = _transport_floor_summary(cases)
    return result


def _transport_floor_canonical_name(environment_metadata: dict[str, Any] | None) -> str:
    ingress = None if environment_metadata is None else environment_metadata.get("modal_ingress")
    if ingress:
        return f"modal-daemon-{ingress}-transport-floor"
    return "daemon-transport-floor"


def _transport_floor_case_name(case: dict[str, Any]) -> str:
    encoding = case.get("transport_encoding")
    size_bytes = case.get("requested_size_bytes")
    if not isinstance(encoding, str) or not isinstance(size_bytes, int):
        return "transport_floor_unknown"
    return f"transport_floor_{encoding}_{_size_label(size_bytes)}"


def _size_label(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0b"
    if size_bytes % 1024 == 0:
        return f"{size_bytes // 1024}kb"
    return f"{size_bytes}b"


def _transport_floor_summary(cases: dict[str, Any]) -> dict[str, Any]:
    by_encoding: dict[str, list[dict[str, Any]]] = {}
    for case_name, case in cases.items():
        if case.get("status") != "ok":
            continue
        encoding = case.get("transport_encoding")
        size_bytes = case.get("requested_size_bytes")
        summary = case.get("summary_ms")
        if not isinstance(encoding, str) or not isinstance(size_bytes, int):
            continue
        if not isinstance(summary, dict):
            continue
        p50 = summary.get("p50")
        inlier_mean = summary.get("mean_without_high_outliers")
        if not isinstance(p50, int | float):
            continue
        by_encoding.setdefault(encoding, []).append(
            {
                "case": case.get("name") if isinstance(case.get("name"), str) else case_name,
                "requested_size_bytes": size_bytes,
                "p50_ms": float(p50),
                "inlier_mean_ms": float(inlier_mean)
                if isinstance(inlier_mean, int | float)
                else None,
                "outlier_count": summary.get("high_outlier_count"),
            }
        )

    encodings: dict[str, Any] = {}
    for encoding, rows in sorted(by_encoding.items()):
        sorted_rows = sorted(rows, key=lambda row: row["requested_size_bytes"])
        p50_samples = [row["p50_ms"] for row in sorted_rows]
        smallest = sorted_rows[0] if sorted_rows else None
        largest = sorted_rows[-1] if sorted_rows else None
        delta_ms = None
        delta_ratio = None
        if smallest is not None and largest is not None:
            delta_ms = largest["p50_ms"] - smallest["p50_ms"]
            delta_ratio = delta_ms / smallest["p50_ms"] if smallest["p50_ms"] > 0 else None
        encodings[encoding] = {
            "cases": sorted_rows,
            "p50_summary_ms": _summary(p50_samples),
            "smallest_to_largest_p50_delta_ms": delta_ms,
            "smallest_to_largest_p50_delta_ratio": delta_ratio,
            "payload_sensitive": bool(delta_ratio is not None and delta_ratio >= 0.25),
        }

    fastest = _fastest_floor_case(encodings)
    return {
        "encodings": encodings,
        "fastest_floor_case": fastest,
        "interpretation": _transport_floor_interpretation(encodings, fastest),
    }


def _fastest_floor_case(encodings: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for encoding, payload in encodings.items():
        for case in payload.get("cases", []):
            if case.get("requested_size_bytes") == 0 and isinstance(case.get("p50_ms"), float):
                candidates.append({**case, "transport_encoding": encoding})
    if not candidates:
        return None
    return min(candidates, key=lambda item: item["p50_ms"])


def _transport_floor_interpretation(
    encodings: dict[str, Any],
    fastest: dict[str, Any] | None,
) -> str:
    if not encodings:
        return "insufficient successful transport floor cases"
    payload_sensitive = [
        encoding for encoding, payload in encodings.items() if payload.get("payload_sensitive")
    ]
    if payload_sensitive:
        return (
            "transport floor has payload-sensitive paths: "
            + ", ".join(sorted(payload_sensitive))
        )
    if fastest is None:
        return "transport floor appears mostly fixed, but no 0B floor case succeeded"
    return (
        "transport floor appears mostly fixed across tested payload sizes; fastest 0B path is "
        f"{fastest['transport_encoding']} at {fastest['p50_ms']:.2f}ms p50"
    )
