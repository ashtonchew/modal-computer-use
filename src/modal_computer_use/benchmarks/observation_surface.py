from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from shlex import quote as shell_quote
from time import perf_counter
from typing import Any, Literal
from urllib.parse import quote

from ..client import DaemonClient
from ..daemon.desktop.screenshots import CapturedRawScreenshot
from ..daemon.routes.observations import _capture_raw_delta_frame
from ..daemon.schemas import ObservationStreamRequest
from ..models import CoordinateSpace, ScreenshotOptions, sha256_bytes
from ..observations import ObservationClient
from ..transports import ObservationStreamTransport
from .measurement import _case_result, _measure_observed_case, _summary
from .operations import (
    _action_result_header,
    _input_backend_result,
    _int_header,
    _str_header,
    _timing_header,
    _transport_http_version,
)
from .safety import _ensure_ok_result, _extract_daemon_ms, _failure, _safe_action_metadata
from .surface_result import _surface_result

OBSERVATION_SCREENSHOT_OPTIONS = {"format": "png", "show_cursor": False}
CLICK_TOGGLE_ACTION = {"type": "click", "x": 512, "y": 512, "button": "left"}
CLICK_TOGGLE_SETTLE_MS = 16


def _run_daemon_observation_surface(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    environment_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    cases = {
        "observation_first_frame": _run_observation_first_frame_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_steady_no_change": _run_observation_no_change_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_small_patch": _run_observation_small_patch_benchmark(
            base_url=base_url,
            token=token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_sparse_patches": _run_observation_sparse_patches_benchmark(
            base_url=base_url,
            token=token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_large_change": _run_observation_large_change_benchmark(
            base_url=base_url,
            token=token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_capture_now_no_change": _run_observation_capture_now_no_change_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_capture_now_small_patch": _run_observation_capture_now_small_patch_benchmark(
            base_url=base_url,
            token=token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_capture_now_sparse_patches": (
            _run_observation_capture_now_sparse_patches_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_capture_now": _run_observation_action_click_capture_now_benchmark(
            base_url=base_url,
            token=token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_action_click_stream_capture": (
            _run_observation_action_click_stream_capture_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_stream_capture_settled": (
            _run_observation_action_click_stream_capture_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                capture_delay_ms=CLICK_TOGGLE_SETTLE_MS,
            )
        ),
        "observation_action_click_observe_change": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                change_signal=None,
            )
        ),
        "observation_action_click_observe_change_poll": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_observe_change_poll",
                change_signal="poll",
            )
        ),
        "observation_action_click_observe_change_adaptive": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_observe_change_adaptive",
                poll_strategy="adaptive",
                change_signal="poll",
            )
        ),
        "observation_action_click_observe_change_region_adaptive": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_observe_change_region_adaptive",
                poll_strategy="adaptive",
                change_detection="auto_region",
                change_signal="poll",
            )
        ),
        "observation_action_click_observe_change_xdamage": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_observe_change_xdamage",
                poll_strategy="adaptive",
                change_signal="xdamage",
            )
        ),
        "observation_action_click_observe_change_auto_signal": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_observe_change_auto_signal",
                poll_strategy="adaptive",
                change_signal="auto",
            )
        ),
        "observation_action_click_observe_change_auto_signal_production": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_observe_change_auto_signal_production",
                poll_strategy="adaptive",
                change_signal="auto",
                transport_timing=False,
            )
        ),
        "observation_action_click_observe_change_auto_signal_binary_envelope": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_observe_change_auto_signal_binary_envelope",
                poll_strategy="adaptive",
                change_signal="auto",
                frame_encoding="binary-envelope",
            )
        ),
        "observation_action_click_observe_change_auto_signal_binary_envelope_production": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_observe_change_auto_signal_binary_envelope_production",
                poll_strategy="adaptive",
                change_signal="auto",
                frame_encoding="binary-envelope",
                transport_timing=False,
            )
        ),
        "observation_action_click_sparse_observe_change_auto_signal": (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_sparse_observe_change_auto_signal",
                poll_strategy="adaptive",
                change_signal="auto",
                page="sparse",
            )
        ),
        "observation_action_click_fused_raw": _run_observation_action_click_fused_raw_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_action_click_observe_change_http_raw": (
            _run_observation_action_click_observe_change_http_raw_benchmark(
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_transport_probe_0b": _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=0,
        ),
        "observation_transport_probe_envelope_0b": _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=0,
            frame_encoding="binary-envelope",
        ),
        "observation_http_transport_probe_0b": _run_observation_http_transport_probe_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=0,
        ),
        "observation_transport_probe_5kb": _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=5 * 1024,
        ),
        "observation_transport_probe_envelope_5kb": _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=5 * 1024,
            frame_encoding="binary-envelope",
        ),
        "observation_http_transport_probe_5kb": _run_observation_http_transport_probe_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=5 * 1024,
        ),
        "observation_transport_probe_50kb": _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=50 * 1024,
        ),
        "observation_transport_probe_envelope_50kb": _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=50 * 1024,
            frame_encoding="binary-envelope",
        ),
        "observation_http_transport_probe_50kb": _run_observation_http_transport_probe_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=50 * 1024,
        ),
        "observation_transport_probe_250kb": _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=250 * 1024,
        ),
        "observation_transport_probe_envelope_250kb": _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=250 * 1024,
            frame_encoding="binary-envelope",
        ),
        "observation_http_transport_probe_250kb": _run_observation_http_transport_probe_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=250 * 1024,
        ),
        "observation_delta_synthetic": _run_observation_delta_synthetic_benchmark(
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
    }
    return _surface_result(
        "daemon-observation-stream",
        cases=cases,
        metadata={
            "transport": "daemon-observation-stream",
            "protocol": "computer-use.observation-stream.v1",
            "canonical_name": _observation_canonical_name(environment_metadata),
            "environment": {
                key: value
                for key, value in (environment_metadata or {}).items()
                if value is not None
            },
        },
        runtime_seconds=None,
    )


def _observation_canonical_name(environment_metadata: dict[str, Any] | None) -> str:
    modal_ingress = (
        None if environment_metadata is None else environment_metadata.get("modal_ingress")
    )
    if modal_ingress == "attested-tunnel":
        return "modal-daemon-attested-observation-stream"
    if modal_ingress == "tunnel":
        return "modal-daemon-observation-stream"
    if modal_ingress == "connect":
        return "modal-daemon-connect-observation-stream"
    return "daemon-observation-stream"


def _run_observation_first_frame_benchmark(
    *,
    base_url: str,
    token: str | None,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples, observations = _measure_observed_case(
        name="observation_first_frame",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _collect_first_frame(base_url, token),
        failures=failures,
    )
    result = _case_result("observation_first_frame", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    return result


def _run_observation_no_change_benchmark(
    *,
    base_url: str,
    token: str | None,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples, observations = _measure_observed_case(
        name="observation_steady_no_change",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _collect_no_change_frame(base_url, token),
        failures=failures,
    )
    result = _case_result("observation_steady_no_change", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    return result


def _run_observation_small_patch_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    state = {"variant": False}
    _open_synthetic_page(client, mode="small", variant=state["variant"])
    samples, observations = _measure_observed_case(
        name="observation_small_patch",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _collect_visual_change(
            base_url, token, client, mode="small", state=state
        ),
        failures=failures,
    )
    result = _case_result("observation_small_patch", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    return result


def _run_observation_sparse_patches_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    state = {"variant": False}
    _open_synthetic_page(client, mode="sparse", variant=state["variant"])
    samples, observations = _measure_observed_case(
        name="observation_sparse_patches",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _collect_visual_change(
            base_url, token, client, mode="sparse", state=state
        ),
        failures=failures,
    )
    result = _case_result("observation_sparse_patches", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    return result


def _run_observation_large_change_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    state = {"variant": False}
    _open_synthetic_page(client, mode="large", variant=state["variant"])
    samples, observations = _measure_observed_case(
        name="observation_large_change",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _collect_visual_change(
            base_url, token, client, mode="large", state=state
        ),
        failures=failures,
    )
    result = _case_result("observation_large_change", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    return result


def _run_observation_capture_now_no_change_benchmark(
    *,
    base_url: str,
    token: str | None,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples, observations = _measure_capture_now_loop(
        name="observation_capture_now_no_change",
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        mutate=None,
        failures=failures,
    )
    result = _case_result("observation_capture_now_no_change", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    return result


def _run_observation_capture_now_small_patch_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    state = {"variant": False}
    _open_synthetic_page(client, mode="small", variant=state["variant"])

    def mutate() -> None:
        state["variant"] = not state["variant"]
        _open_synthetic_page(client, mode="small", variant=state["variant"])

    samples, observations = _measure_capture_now_loop(
        name="observation_capture_now_small_patch",
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        mutate=mutate,
        failures=failures,
    )
    result = _case_result("observation_capture_now_small_patch", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    return result


def _run_observation_capture_now_sparse_patches_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    state = {"variant": False}
    _open_synthetic_page(client, mode="sparse", variant=state["variant"])

    def mutate() -> None:
        state["variant"] = not state["variant"]
        _open_synthetic_page(client, mode="sparse", variant=state["variant"])

    samples, observations = _measure_capture_now_loop(
        name="observation_capture_now_sparse_patches",
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        mutate=mutate,
        failures=failures,
    )
    result = _case_result("observation_capture_now_sparse_patches", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    return result


def _run_observation_action_click_capture_now_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)

    def mutate() -> dict[str, Any]:
        return _run_click_toggle_action(client)

    samples, observations = _measure_capture_now_loop(
        name="observation_action_click_capture_now",
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        mutate=mutate,
        failures=failures,
    )
    result = _case_result("observation_action_click_capture_now", iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    result.update(
        {
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "daemon_action_click",
        }
    )
    return result


def _run_observation_action_click_stream_capture_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    capture_delay_ms: int = 0,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    name = (
        "observation_action_click_stream_capture"
        if capture_delay_ms == 0
        else "observation_action_click_stream_capture_settled"
    )
    _open_click_toggle_page(client)
    samples, observations = _measure_stream_action_capture_loop(
        name=name,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        capture_delay_ms=capture_delay_ms,
    )
    result = _case_result(name, iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    result.update(
        {
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "stream_action_click",
            "capture_delay_ms": capture_delay_ms,
        }
    )
    return result


def _run_observation_action_click_observe_change_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    name: str = "observation_action_click_observe_change",
    poll_strategy: str = "fixed",
    change_detection: str = "full",
    change_signal: str | None = "poll",
    page: str = "default",
    frame_encoding: Literal["json-binary", "binary-envelope"] | None = None,
    transport_timing: bool = True,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    if page == "sparse":
        _open_sparse_click_toggle_page(client)
    else:
        _open_click_toggle_page(client)
    samples, observations = _measure_stream_action_capture_loop(
        name=name,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        capture_delay_ms=0,
        observe_change=True,
        poll_strategy=poll_strategy,
        change_detection=change_detection,
        change_signal=change_signal,
        frame_encoding=frame_encoding,
        transport_timing=transport_timing,
    )
    result = _case_result(name, iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    result.update(
        {
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "stream_action_click_observe_change",
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": poll_strategy,
            "change_detection": change_detection,
            "change_signal": change_signal or "default",
            "page": page,
            "frame_encoding": frame_encoding or "json-binary",
            "transport_timing": transport_timing,
        }
    )
    return result


def _run_observation_action_click_fused_raw_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    samples, observations = _measure_observed_case(
        name="observation_action_click_fused_raw",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _run_click_toggle_fused_raw(client),
        failures=failures,
    )
    result = _case_result("observation_action_click_fused_raw", iterations, samples, failures)
    result.update(
        {
            "request": OBSERVATION_SCREENSHOT_OPTIONS,
            "transport_encoding": "binary",
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "samples_bytes": [
                item["size_bytes"] for item in observations if item.get("size_bytes") is not None
            ],
            "summary_bytes": _summary(
                [
                    float(item["size_bytes"])
                    for item in observations
                    if item.get("size_bytes") is not None
                ]
            ),
            "last_result": observations[-1] if observations else None,
        }
    )
    daemon_samples = [
        sample
        for item in observations
        if isinstance((sample := item.get("daemon_ms")), int | float)
    ]
    if daemon_samples:
        result["daemon_samples_ms"] = daemon_samples
        result["daemon_summary_ms"] = _summary(daemon_samples)
        result["overhead_samples_ms"] = [
            sample - daemon_sample
            for sample, daemon_sample in zip(samples, daemon_samples, strict=False)
        ]
        result["overhead_summary_ms"] = _summary(result["overhead_samples_ms"])
    _add_observation_latency_diagnosis(result)
    return result


def _run_observation_action_click_observe_change_http_raw_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    samples, observations = _measure_observed_case(
        name="observation_action_click_observe_change_http_raw",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _run_click_toggle_observe_change_http_raw(client),
        failures=failures,
    )
    result = _case_result(
        "observation_action_click_observe_change_http_raw",
        iterations,
        samples,
        failures,
    )
    result.update(
        {
            "request": OBSERVATION_SCREENSHOT_OPTIONS,
            "transport_encoding": "http_binary",
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "samples_bytes": [
                item["size_bytes"] for item in observations if item.get("size_bytes") is not None
            ],
            "summary_bytes": _summary(
                [
                    float(item["size_bytes"])
                    for item in observations
                    if item.get("size_bytes") is not None
                ]
            ),
            "last_result": observations[-1] if observations else None,
        }
    )
    daemon_samples = [
        sample
        for item in observations
        if isinstance((sample := item.get("change_timing_ms", {}).get("total_ms")), int | float)
    ]
    if daemon_samples:
        result["daemon_samples_ms"] = daemon_samples
        result["daemon_summary_ms"] = _summary(daemon_samples)
        result["overhead_samples_ms"] = [
            sample - daemon_sample
            for sample, daemon_sample in zip(samples, daemon_samples, strict=False)
        ]
        result["overhead_summary_ms"] = _summary(result["overhead_samples_ms"])
    _add_direct_nested_timing_summary(
        result,
        observations,
        nested_key="change_timing_ms",
        result_key="change_timing_summary_ms",
    )
    return result


def _run_observation_transport_probe_benchmark(
    *,
    base_url: str,
    token: str | None,
    iterations: int,
    warmup_iterations: int,
    size_bytes: int,
    frame_encoding: str | None = None,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    name = (
        f"observation_transport_probe_envelope_{_size_label(size_bytes)}"
        if frame_encoding == "binary-envelope"
        else f"observation_transport_probe_{_size_label(size_bytes)}"
    )
    transport = ObservationStreamTransport(base_url, token=token)
    try:
        samples, observations = _measure_observed_case(
            name=name,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            operation=lambda: transport.transport_probe(
                size_bytes=size_bytes,
                frame_encoding=frame_encoding,
            ),
            failures=failures,
        )
    finally:
        transport.close()
    result = _case_result(name, iterations, samples, failures)
    result.update(
        {
            "transport_encoding": "websocket_binary_envelope"
            if frame_encoding == "binary-envelope"
            else "websocket_json_metadata_binary_payload",
            "requested_size_bytes": size_bytes,
            "frame_encoding": frame_encoding or "json-binary",
            "samples_bytes": [
                item["size_bytes"] for item in observations if item.get("size_bytes") is not None
            ],
            "summary_bytes": _summary(
                [
                    float(item["size_bytes"])
                    for item in observations
                    if item.get("size_bytes") is not None
                ]
            ),
            "last_result": observations[-1] if observations else None,
        }
    )
    _add_probe_timing_observations(result, observations)
    return result


def _run_observation_http_transport_probe_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    size_bytes: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    name = f"observation_http_transport_probe_{_size_label(size_bytes)}"
    samples, observations = _measure_observed_case(
        name=name,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _run_http_transport_probe(client, size_bytes=size_bytes),
        failures=failures,
    )
    result = _case_result(name, iterations, samples, failures)
    result.update(
        {
            "transport_encoding": "http_binary",
            "requested_size_bytes": size_bytes,
            "samples_bytes": [
                item["size_bytes"] for item in observations if item.get("size_bytes") is not None
            ],
            "summary_bytes": _summary(
                [
                    float(item["size_bytes"])
                    for item in observations
                    if item.get("size_bytes") is not None
                ]
            ),
            "last_result": observations[-1] if observations else None,
        }
    )
    _add_direct_nested_timing_summary(
        result,
        observations,
        nested_key="server_emit_timing_ms",
        result_key="server_emit_timing_summary_ms",
    )
    return result


def _run_http_transport_probe(client: DaemonClient, *, size_bytes: int) -> dict[str, Any]:
    payload, headers = client.post_bytes_with_headers(
        "/v1/observations/transport-probe",
        json={"size_bytes": size_bytes},
    )
    return {
        "size_bytes": len(payload),
        "requested_size_bytes": size_bytes,
        "server_emit_timing_ms": _json_timing_header(
            headers,
            "x-computer-use-transport-timing-ms",
        ),
        "transport_http_version": _transport_http_version(client),
    }


def _run_observation_delta_synthetic_benchmark(
    *,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    cases = {
        "unchanged": _measure_synthetic_delta_case(
            name="unchanged",
            mutation_rects=[],
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "single_rect": _measure_synthetic_delta_case(
            name="single_rect",
            mutation_rects=[{"x": 128, "y": 128, "width": 96, "height": 96}],
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "two_sparse_rects": _measure_synthetic_delta_case(
            name="two_sparse_rects",
            mutation_rects=[
                {"x": 64, "y": 64, "width": 96, "height": 96},
                {"x": 832, "y": 576, "width": 96, "height": 96},
            ],
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "four_sparse_rects": _measure_synthetic_delta_case(
            name="four_sparse_rects",
            mutation_rects=[
                {"x": 64, "y": 64, "width": 64, "height": 64},
                {"x": 832, "y": 64, "width": 64, "height": 64},
                {"x": 64, "y": 576, "width": 64, "height": 64},
                {"x": 832, "y": 576, "width": 64, "height": 64},
            ],
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "large_fallback": _measure_synthetic_delta_case(
            name="large_fallback",
            mutation_rects=[{"x": 128, "y": 96, "width": 768, "height": 576}],
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
    }
    return {
        "status": "ok" if all(case.get("status") == "ok" for case in cases.values()) else "error",
        "iterations": iterations,
        "cases": cases,
    }


def _measure_synthetic_delta_case(
    *,
    name: str,
    mutation_rects: list[dict[str, int]],
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples: list[float] = []
    observations: list[dict[str, Any]] = []
    request = ObservationStreamRequest(
        format="png",
        show_cursor=False,
        fps=1,
        keyframe_interval=10_000,
        tile_size=64,
        max_patch_rects=8,
        multi_rect_min_savings=0.1,
    )
    options = ScreenshotOptions(format="png", show_cursor=False)
    width = 1024
    height = 768
    baseline = _synthetic_raw_frame(width=width, height=height, rects=[])
    previous_metadata, _previous_payload = _capture_raw_delta_frame(
        raw=baseline,
        request=request,
        options=options,
        seq=1,
        last_source_sha256=None,
        previous_tile_hashes=None,
        previous_seq=None,
        stream_id="synthetic",
        captured_started=perf_counter(),
    )
    previous_hashes = previous_metadata.pop("_current_tile_hashes")
    previous_seq = 1

    total_iterations = warmup_iterations + iterations
    for index in range(total_iterations):
        seq = index + 2
        raw = _synthetic_raw_frame(
            width=width,
            height=height,
            rects=mutation_rects if index % 2 == 0 else [],
        )
        started = perf_counter()
        try:
            metadata, payload = _capture_raw_delta_frame(
                raw=raw,
                request=request,
                options=options,
                seq=seq,
                last_source_sha256=baseline.sha256,
                previous_tile_hashes=previous_hashes,
                previous_seq=previous_seq,
                stream_id="synthetic",
                captured_started=started,
            )
        except Exception as exc:
            failures.append(_failure(name, phase="measure", iteration=index, exc=exc))
            continue
        elapsed_ms = _elapsed_benchmark_ms(started)
        current_hashes = metadata.pop("_current_tile_hashes")
        observation = {
            "kind": metadata.get("kind"),
            "size_bytes": len(payload),
            "dirty_rect": metadata.get("dirty_rect"),
            "dirty_ratio": metadata.get("dirty_ratio"),
            "patch_count": metadata.get("patch_count"),
            "patch_rects": metadata.get("patch_rects"),
            "timing_ms": metadata.get("timing_ms"),
        }
        if index >= warmup_iterations:
            samples.append(elapsed_ms)
            observations.append(observation)
        previous_hashes = current_hashes
        previous_seq = seq
        baseline = raw

    result = _case_result(f"observation_delta_synthetic_{name}", iterations, samples, failures)
    result.update(
        {
            "mutation_rects": mutation_rects,
            "samples_bytes": [item["size_bytes"] for item in observations],
            "summary_bytes": _summary([float(item["size_bytes"]) for item in observations]),
            "last_result": observations[-1] if observations else None,
        }
    )
    patch_counts = [
        item["patch_count"]
        for item in observations
        if isinstance(item.get("patch_count"), int | float)
    ]
    if patch_counts:
        result["patch_count_summary"] = _summary([float(item) for item in patch_counts])
    _add_direct_nested_timing_summary(
        result,
        observations,
        nested_key="timing_ms",
        result_key="timing_summary_ms",
    )
    return result


def _synthetic_raw_frame(
    *,
    width: int,
    height: int,
    rects: list[dict[str, int]],
) -> CapturedRawScreenshot:
    rgb = bytearray(b"\xff" * (width * height * 3))
    row_stride = width * 3
    for rect_index, rect in enumerate(rects):
        color = (
            (17 + rect_index * 53) % 256,
            (41 + rect_index * 71) % 256,
            (97 + rect_index * 97) % 256,
        )
        row = bytes(color) * rect["width"]
        for y in range(rect["y"], rect["y"] + rect["height"]):
            start = y * row_stride + rect["x"] * 3
            rgb[start : start + rect["width"] * 3] = row
    data = bytes(rgb)
    return CapturedRawScreenshot(
        width=width,
        height=height,
        rgb=data,
        sha256=sha256_bytes(data),
        captured_at=datetime.now(UTC),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=width,
            desktop_height=height,
            image_width=width,
            image_height=height,
        ),
        cursor_visible=False,
        capture_backend="synthetic-raw",
        timings_ms={"total_ms": 0.0},
    )


def _elapsed_benchmark_ms(started: float) -> float:
    return (perf_counter() - started) * 1000


def _size_label(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0b"
    if size_bytes % 1024 == 0:
        return f"{size_bytes // 1024}kb"
    return f"{size_bytes}b"


def _add_nested_timing_summary(
    result: dict[str, Any],
    observations: list[Any],
    *,
    source_key: str,
    nested_key: str,
    result_key: str,
) -> None:
    names = sorted(
        {
            key
            for item in observations
            if isinstance((source := item.get(source_key)), dict)
            and isinstance((timing := source.get(nested_key)), dict)
            for key, value in timing.items()
            if isinstance(value, int | float)
        }
    )
    if not names:
        return
    result[result_key] = {
        name: _summary(
            [
                float(timing[name])
                for item in observations
                if isinstance((source := item.get(source_key)), dict)
                and isinstance((timing := source.get(nested_key)), dict)
                and isinstance(timing.get(name), int | float)
            ]
        )
        for name in names
    }


def _add_probe_timing_observations(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    _add_direct_nested_timing_summary(
        result,
        observations,
        nested_key="server_emit_timing_ms",
        result_key="server_emit_timing_summary_ms",
    )
    _add_direct_nested_timing_summary(
        result,
        observations,
        nested_key="client_receive_timing_ms",
        result_key="client_receive_timing_summary_ms",
    )


def _add_direct_nested_timing_summary(
    result: dict[str, Any],
    observations: list[Any],
    *,
    nested_key: str,
    result_key: str,
) -> None:
    names = sorted(
        {
            key
            for item in observations
            if isinstance((timing := item.get(nested_key)), dict)
            for key, value in timing.items()
            if isinstance(value, int | float)
        }
    )
    if not names:
        return
    result[result_key] = {
        name: _summary(
            [
                float(timing[name])
                for item in observations
                if isinstance((timing := item.get(nested_key)), dict)
                and isinstance(timing.get(name), int | float)
            ]
        )
        for name in names
    }


def _measure_capture_now_loop(
    *,
    name: str,
    base_url: str,
    token: str | None,
    iterations: int,
    warmup_iterations: int,
    mutate: Any,
    failures: list[dict[str, Any]],
) -> tuple[list[float], list[dict[str, Any]]]:
    samples: list[float] = []
    observations: list[dict[str, Any]] = []
    try:
        with ObservationClient(
            ObservationStreamTransport(base_url, token=token),
            options=OBSERVATION_SCREENSHOT_OPTIONS,
            fps=0.01,
        ) as stream:
            frames = stream.frames()
            next(frames)
            for warmup_index in range(warmup_iterations):
                try:
                    _capture_now_iteration(stream, frames, mutate=mutate)
                except Exception as exc:
                    failures.append(
                        _failure(name, phase="warmup", iteration=warmup_index, exc=exc)
                    )
                    return samples, observations
            for iteration in range(iterations):
                start = perf_counter()
                try:
                    observation = _capture_now_iteration(stream, frames, mutate=mutate)
                except Exception as exc:
                    elapsed_ms = (perf_counter() - start) * 1000
                    failures.append(
                        _failure(
                            name,
                            phase="measure",
                            iteration=iteration,
                            exc=exc,
                            elapsed_ms=elapsed_ms,
                        )
                    )
                    continue
                samples.append((perf_counter() - start) * 1000)
                observations.append(observation)
    except Exception as exc:
        failures.append(_failure(name, phase="setup", iteration=0, exc=exc))
    return samples, observations


def _measure_stream_action_capture_loop(
    *,
    name: str,
    base_url: str,
    token: str | None,
    iterations: int,
    warmup_iterations: int,
    failures: list[dict[str, Any]],
    capture_delay_ms: int,
    observe_change: bool = False,
    poll_strategy: str = "fixed",
    change_detection: str = "full",
    change_signal: str | None = "poll",
    frame_encoding: Literal["json-binary", "binary-envelope"] | None = None,
    transport_timing: bool = True,
) -> tuple[list[float], list[dict[str, Any]]]:
    samples: list[float] = []
    observations: list[dict[str, Any]] = []
    try:
        with ObservationClient(
            ObservationStreamTransport(base_url, token=token),
            options=OBSERVATION_SCREENSHOT_OPTIONS,
            fps=0.01,
            transport_timing=transport_timing,
            frame_encoding=frame_encoding,
        ) as stream:
            frames = None
            if transport_timing:
                stream.transport.start(stream.payload)
                stream.transport.recv_frame_with_timing()
            else:
                frames = stream.frames()
                next(frames)
            for warmup_index in range(warmup_iterations):
                try:
                    _stream_action_capture_iteration(
                        stream,
                        frames,
                        capture_delay_ms=capture_delay_ms,
                        observe_change=observe_change,
                        poll_strategy=poll_strategy,
                        change_detection=change_detection,
                        change_signal=change_signal,
                    )
                except Exception as exc:
                    failures.append(
                        _failure(name, phase="warmup", iteration=warmup_index, exc=exc)
                    )
                    return samples, observations
            for iteration in range(iterations):
                start = perf_counter()
                try:
                    observation = _stream_action_capture_iteration(
                        stream,
                        frames,
                        capture_delay_ms=capture_delay_ms,
                        observe_change=observe_change,
                        poll_strategy=poll_strategy,
                        change_detection=change_detection,
                        change_signal=change_signal,
                    )
                except Exception as exc:
                    elapsed_ms = (perf_counter() - start) * 1000
                    failures.append(
                        _failure(
                            name,
                            phase="measure",
                            iteration=iteration,
                            exc=exc,
                            elapsed_ms=elapsed_ms,
                        )
                    )
                    continue
                samples.append((perf_counter() - start) * 1000)
                observations.append(observation)
    except Exception as exc:
        failures.append(_failure(name, phase="setup", iteration=0, exc=exc))
    return samples, observations


def _capture_now_iteration(
    stream: ObservationClient,
    frames: Any,
    *,
    mutate: Any,
) -> dict[str, Any]:
    mutation_ms = 0.0
    mutation_result: dict[str, Any] | None = None
    if mutate is not None:
        mutation_started = perf_counter()
        result = mutate()
        mutation_ms = (perf_counter() - mutation_started) * 1000
        mutation_result = result if isinstance(result, dict) else None

    request_started = perf_counter()
    stream.request_frame()
    request_frame_ms = (perf_counter() - request_started) * 1000

    receive_started = perf_counter()
    frame = next(frames)
    receive_frame_ms = (perf_counter() - receive_started) * 1000

    observation = _frame_observation(frame)
    observation["benchmark_timing_ms"] = {
        "mutation_ms": mutation_ms,
        "request_frame_ms": request_frame_ms,
        "receive_frame_ms": receive_frame_ms,
        "action_to_frame_ms": mutation_ms + request_frame_ms + receive_frame_ms,
    }
    if mutation_result is not None:
        observation["mutation_result"] = mutation_result
        action_daemon_ms = mutation_result.get("daemon_ms")
        if isinstance(action_daemon_ms, int | float):
            observation["benchmark_timing_ms"]["action_daemon_ms"] = action_daemon_ms
            observation["benchmark_timing_ms"]["action_transport_overhead_ms"] = max(
                mutation_ms - action_daemon_ms,
                0.0,
            )
    return observation


def _stream_action_capture_iteration(
    stream: ObservationClient,
    frames: Any,
    *,
    capture_delay_ms: int,
    observe_change: bool,
    poll_strategy: str,
    change_detection: str,
    change_signal: str | None,
) -> dict[str, Any]:
    request_started = perf_counter()
    payload = {
        "actions": [CLICK_TOGGLE_ACTION],
        "source": "benchmark",
        "capture_delay_ms": capture_delay_ms,
    }
    if observe_change:
        payload.update(
            {
                "change_timeout_ms": 100,
                "poll_interval_ms": 8,
                "poll_strategy": poll_strategy,
                "change_detection": change_detection,
            }
        )
        if change_signal is not None:
            payload["change_signal"] = change_signal
        stream.run_actions_observe_change(**payload)
    else:
        stream.run_actions_capture(**payload)
    request_frame_ms = (perf_counter() - request_started) * 1000

    receive_started = perf_counter()
    frame = stream.transport.recv_frame_with_timing() if frames is None else next(frames)
    receive_frame_ms = (perf_counter() - receive_started) * 1000

    observation = _frame_observation(frame)
    action_result = observation.get("action_result")
    observation["benchmark_timing_ms"] = {
        "mutation_ms": 0.0,
        "capture_delay_ms": capture_delay_ms,
        "observe_change": observe_change,
        "poll_strategy": poll_strategy,
        "change_detection": change_detection,
        "change_signal": change_signal or "default",
        "request_frame_ms": request_frame_ms,
        "receive_frame_ms": receive_frame_ms,
        "action_to_frame_ms": request_frame_ms + receive_frame_ms,
    }
    if isinstance(action_result, dict):
        _ensure_ok_result(action_result)
        action_daemon_ms = _extract_daemon_ms(action_result)
        if isinstance(action_daemon_ms, int | float):
            observation["benchmark_timing_ms"]["action_daemon_ms"] = action_daemon_ms
    return observation


def _run_click_toggle_action(client: DaemonClient) -> dict[str, Any]:
    result = client.post_json(
        "/v1/actions/run",
        json={"actions": [CLICK_TOGGLE_ACTION], "source": "benchmark"},
    )
    _ensure_ok_result(result)
    return {
        "daemon_ms": _extract_daemon_ms(result),
        "transport_http_version": _transport_http_version(client),
        "input_backend": _input_backend_result(result),
    }


def _run_click_toggle_fused_raw(client: DaemonClient) -> dict[str, Any]:
    payload, headers = client.post_bytes_with_headers(
        "/v1/actions/run/raw-screenshot",
        json={
            "actions": [CLICK_TOGGLE_ACTION],
            "screenshot_after": True,
            "screenshot_options": OBSERVATION_SCREENSHOT_OPTIONS,
            "source": "benchmark",
        },
    )
    action_result = _action_result_header(headers)
    _ensure_ok_result(action_result)
    screenshot_timing = _timing_header(headers)
    return {
        "format": OBSERVATION_SCREENSHOT_OPTIONS["format"],
        "width": _int_header(headers, "x-computer-use-width"),
        "height": _int_header(headers, "x-computer-use-height"),
        "size_bytes": len(payload),
        "storage": "inline",
        "artifact_backed": False,
        "cursor_visible": OBSERVATION_SCREENSHOT_OPTIONS["show_cursor"],
        "capture_backend": _str_header(headers, "x-computer-use-capture-backend"),
        "daemon_ms": _extract_daemon_ms(action_result),
        "transport_http_version": _transport_http_version(client),
        "input_backend": _input_backend_result(action_result),
        "action_result": {
            "ok": action_result.get("ok"),
            "results_count": len(action_result.get("results", []))
            if isinstance(action_result.get("results"), list)
            else None,
        },
        "screenshot_daemon_timing_ms": screenshot_timing,
    }


def _run_click_toggle_observe_change_http_raw(client: DaemonClient) -> dict[str, Any]:
    payload, headers = client.post_bytes_with_headers(
        "/v1/actions/run/observe-change/raw-screenshot",
        json={
            "actions": [CLICK_TOGGLE_ACTION],
            "screenshot_options": OBSERVATION_SCREENSHOT_OPTIONS,
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_signal": "auto",
            "source": "benchmark",
        },
    )
    action_result = _action_result_header(headers)
    _ensure_ok_result(action_result)
    return {
        "format": OBSERVATION_SCREENSHOT_OPTIONS["format"],
        "width": _int_header(headers, "x-computer-use-width"),
        "height": _int_header(headers, "x-computer-use-height"),
        "size_bytes": len(payload),
        "storage": "inline",
        "artifact_backed": False,
        "cursor_visible": OBSERVATION_SCREENSHOT_OPTIONS["show_cursor"],
        "capture_backend": _str_header(headers, "x-computer-use-capture-backend"),
        "transport_http_version": _transport_http_version(client),
        "input_backend": _input_backend_result(action_result),
        "action_result": {
            "ok": action_result.get("ok"),
            "results_count": len(action_result.get("results", []))
            if isinstance(action_result.get("results"), list)
            else None,
        },
        "change_result": _encoded_json_header(headers, "x-computer-use-change-result"),
        "change_timing_ms": _json_timing_header(headers, "x-computer-use-change-timing-ms"),
        "screenshot_daemon_timing_ms": _timing_header(headers),
    }


def _collect_first_frame(base_url: str, token: str | None) -> dict[str, Any]:
    with ObservationClient(
        ObservationStreamTransport(base_url, token=token),
        options=OBSERVATION_SCREENSHOT_OPTIONS,
        fps=30,
        max_frames=1,
    ) as stream:
        return _frame_observation(next(stream.frames()))


def _encoded_json_header(headers: Any, name: str) -> dict[str, Any]:
    value = headers.get(name) if hasattr(headers, "get") else None
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(base64.b64decode(value).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_timing_header(headers: Any, name: str) -> dict[str, float]:
    value = headers.get(name) if hasattr(headers, "get") else None
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(value)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): float(item)
        for key, item in data.items()
        if not isinstance(item, bool) and isinstance(item, int | float)
    }


def _collect_no_change_frame(base_url: str, token: str | None) -> dict[str, Any]:
    with ObservationClient(
        ObservationStreamTransport(base_url, token=token),
        options=OBSERVATION_SCREENSHOT_OPTIONS,
        fps=1,
        max_frames=2,
    ) as stream:
        frames = list(stream.frames())
    return _frame_observation(frames[-1])


def _collect_visual_change(
    base_url: str,
    token: str | None,
    client: DaemonClient,
    *,
    mode: str,
    state: dict[str, bool],
) -> dict[str, Any]:
    with ObservationClient(
        ObservationStreamTransport(base_url, token=token),
        options=OBSERVATION_SCREENSHOT_OPTIONS,
        fps=5,
        max_frames=3,
    ) as stream:
        frames = stream.frames()
        next(frames)
        state["variant"] = not state["variant"]
        _open_synthetic_page(client, mode=mode, variant=state["variant"])
        changed_frames = list(frames)
        return _frame_observation(changed_frames[-1])


def _open_synthetic_page(client: DaemonClient, *, mode: str, variant: bool) -> None:
    if mode == "small":
        color = "#ffffff"
        square = "#ef4444" if variant else "#22c55e"
        body = (
            "<!doctype html>"
            "<html style='margin:0;width:100%;height:100%;overflow:hidden;'>"
            "<body style='margin:0;width:100%;height:100%;overflow:hidden;'>"
            f"<div style='position:fixed;inset:0;background:{color};'></div>"
            "<div id='target' style='position:fixed;left:72px;top:72px;"
            f"width:96px;height:96px;background:{square};'></div>"
            "</body></html>"
        )
    elif mode == "sparse":
        color = "#ffffff"
        square = "#ef4444" if variant else "#22c55e"
        other = "#0f172a" if variant else "#f59e0b"
        body = (
            "<!doctype html>"
            "<html style='margin:0;width:100%;height:100%;overflow:hidden;'>"
            "<body style='margin:0;width:100%;height:100%;overflow:hidden;'>"
            f"<div style='position:fixed;inset:0;background:{color};'></div>"
            "<div style='position:fixed;left:72px;top:72px;"
            f"width:96px;height:96px;background:{square};'></div>"
            "<div style='position:fixed;right:72px;bottom:72px;"
            f"width:96px;height:96px;background:{other};'></div>"
            "</body></html>"
        )
    else:
        color = "#14213d" if variant else "#fca311"
        body = (
            "<!doctype html>"
            "<html style='margin:0;width:100%;height:100%;overflow:hidden;'>"
            "<body style='margin:0;width:100%;height:100%;overflow:hidden;'>"
            f"<div style='position:fixed;inset:0;background:{color};'></div>"
            "</body></html>"
        )
    cache_key = str(time.monotonic_ns())
    _serve_synthetic_page(client, body)
    client.post_json(
        "/v1/browser/open-url",
        json={
            "url": f"http://127.0.0.1:8766/index.html?{quote(cache_key)}",
            "wait_for_window": True,
        },
    )


def _open_click_toggle_page(client: DaemonClient) -> None:
    body = (
        "<!doctype html>"
        "<html style='margin:0;width:100%;height:100%;overflow:hidden;'>"
        "<body style='margin:0;width:100%;height:100%;overflow:hidden;"
        "background:#ffffff;'>"
        "<button id='target' aria-label='toggle' "
        "style='position:fixed;left:360px;top:240px;width:256px;height:192px;"
        "border:0;background:#22c55e;color:#111827;font:32px sans-serif;'>0</button>"
        "<script>"
        "let n=0;"
        "const t=document.getElementById('target');"
        "function paint(){"
        "t.textContent=String(n);"
        "t.style.background=(n%2)?'#ef4444':'#22c55e';"
        "}"
        "document.addEventListener('click',()=>{n++;paint();});"
        "paint();"
        "</script>"
        "</body></html>"
    )
    cache_key = str(time.monotonic_ns())
    _serve_synthetic_page(client, body)
    client.post_json(
        "/v1/browser/open-url",
        json={
            "url": f"http://127.0.0.1:8766/index.html?action-observe={quote(cache_key)}",
            "wait_for_window": True,
        },
    )


def _open_sparse_click_toggle_page(client: DaemonClient) -> None:
    body = (
        "<!doctype html>"
        "<html style='margin:0;width:100%;height:100%;overflow:hidden;'>"
        "<body style='margin:0;width:100%;height:100%;overflow:hidden;"
        "background:#ffffff;'>"
        "<div id='a' style='position:fixed;left:72px;top:72px;width:96px;height:96px;'></div>"
        "<div id='b' style='position:fixed;right:72px;bottom:72px;width:96px;height:96px;'></div>"
        "<script>"
        "let n=0;"
        "const a=document.getElementById('a');"
        "const b=document.getElementById('b');"
        "function paint(){"
        "a.style.background=(n%2)?'#ef4444':'#22c55e';"
        "b.style.background=(n%2)?'#0f172a':'#f59e0b';"
        "}"
        "document.addEventListener('click',()=>{n++;paint();});"
        "paint();"
        "</script>"
        "</body></html>"
    )
    cache_key = str(time.monotonic_ns())
    _serve_synthetic_page(client, body)
    client.post_json(
        "/v1/browser/open-url",
        json={
            "url": f"http://127.0.0.1:8766/index.html?sparse-action={quote(cache_key)}",
            "wait_for_window": True,
        },
    )


def _serve_synthetic_page(client: DaemonClient, body: str) -> None:
    script = (
        "set -eu; "
        "dir=/tmp/modal-computer-use-observation; "
        "mkdir -p \"$dir\"; "
        f"printf %s {shell_quote(body)} > \"$dir/index.html\"; "
        "if ! pgrep -f 'http.server 8766' >/dev/null 2>&1; then "
        "python3 -m http.server 8766 --bind 127.0.0.1 --directory \"$dir\" "
        ">/tmp/modal-computer-use-observation-http.log 2>&1 & "
        "fi"
    )
    client.post_json(
        "/v1/commands/run",
        json={"command": ["sh", "-lc", script], "timeout": 5},
    )


def _frame_observation(frame) -> dict[str, Any]:
    metadata = frame.metadata
    timing = metadata.get("timing_ms")
    transport_timing = getattr(frame, "transport_timing", None)
    return {
        "transport_http_version": "websocket",
        "content_type": metadata.get("content_type"),
        "format": metadata.get("format"),
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "size_bytes": 0 if frame.payload is None else len(frame.payload),
        "metadata_size_bytes": metadata.get("size_bytes"),
        "full_size_bytes": metadata.get("full_size_bytes"),
        "kind": metadata.get("kind"),
        "unchanged": metadata.get("unchanged"),
        "dirty_rect": metadata.get("dirty_rect"),
        "dirty_ratio": metadata.get("dirty_ratio"),
        "capture_backend": metadata.get("capture_backend"),
        "tile_size": metadata.get("tile_size"),
        "tile_hash_backend": metadata.get("tile_hash_backend"),
        "patch_count": metadata.get("patch_count"),
        "patch_rects": metadata.get("patch_rects"),
        "patch_sizes_bytes": metadata.get("patch_sizes_bytes"),
        "source_version": metadata.get("source_version"),
        "previous_source_version": metadata.get("previous_source_version"),
        "emit_version": metadata.get("emit_version"),
        "delivery": metadata.get("delivery"),
        "frame_encoding": metadata.get("frame_encoding"),
        "coalesced_scheduled_frames": metadata.get("coalesced_scheduled_frames"),
        "trigger": metadata.get("trigger"),
        "action_result": metadata.get("action_result"),
        "change_detected": metadata.get("change_detected"),
        "change_attempts": metadata.get("change_attempts"),
        "change_wait_ms": metadata.get("change_wait_ms"),
        "change_timeout_reached": metadata.get("change_timeout_reached"),
        "change_region_attempts": metadata.get("change_region_attempts"),
        "change_region_detected": metadata.get("change_region_detected"),
        "change_detection": metadata.get("change_detection"),
        "change_detection_region": metadata.get("change_detection_region"),
        "change_signal": metadata.get("change_signal"),
        "change_signal_active": metadata.get("change_signal_active"),
        "change_signal_available": metadata.get("change_signal_available"),
        "change_signal_detected": metadata.get("change_signal_detected"),
        "change_signal_wait_ms": metadata.get("change_signal_wait_ms"),
        "change_signal_reason": metadata.get("change_signal_reason"),
        "change_signal_version": metadata.get("change_signal_version"),
        "change_stage_timing_ms": metadata.get("change_stage_timing_ms"),
        "baseline_source_version": metadata.get("baseline_source_version"),
        "baseline_source_sha256": metadata.get("baseline_source_sha256"),
        "poll_strategy": metadata.get("poll_strategy"),
        "screenshot_daemon_timing_ms": timing if isinstance(timing, dict) else {},
        "observation_transport_timing": transport_timing
        if isinstance(transport_timing, dict)
        else {},
    }


def _add_frame_observations(
    result: dict[str, Any],
    samples: list[float],
    observations: list[Any],
) -> None:
    result.update(
        {
            "request": OBSERVATION_SCREENSHOT_OPTIONS,
            "transport_encoding": _observation_transport_encoding(observations),
            "samples_bytes": [
                item["size_bytes"] for item in observations if item.get("size_bytes") is not None
            ],
            "summary_bytes": _summary(
                [
                    float(item["size_bytes"])
                    for item in observations
                    if item.get("size_bytes") is not None
                ]
            ),
            "last_result": observations[-1] if observations else None,
        }
    )
    changed_count = sum(1 for item in observations if item.get("unchanged") is False)
    if observations:
        result["changed_frames"] = changed_count
        result["unchanged_frames"] = len(observations) - changed_count
        result["changed_frame_ratio"] = changed_count / len(observations)
        patch_count_samples = [
            item["patch_count"]
            for item in observations
            if isinstance(item.get("patch_count"), int | float)
        ]
        if patch_count_samples:
            result["patch_count_samples"] = patch_count_samples
            result["patch_count_summary"] = _summary([float(item) for item in patch_count_samples])
        change_detected_count = sum(1 for item in observations if item.get("change_detected"))
        if any(item.get("change_detected") is not None for item in observations):
            result["change_detected_frames"] = change_detected_count
            result["change_detected_ratio"] = change_detected_count / len(observations)
            result["change_timeout_frames"] = sum(
                1 for item in observations if item.get("change_timeout_reached")
            )
            result["change_region_detected_frames"] = sum(
                1 for item in observations if item.get("change_region_detected")
            )
            change_wait_samples = [
                item["change_wait_ms"]
                for item in observations
                if isinstance(item.get("change_wait_ms"), int | float)
            ]
            if change_wait_samples:
                result["change_wait_samples_ms"] = change_wait_samples
                result["change_wait_summary_ms"] = _summary(change_wait_samples)
            signal_wait_samples = [
                item["change_signal_wait_ms"]
                for item in observations
                if isinstance(item.get("change_signal_wait_ms"), int | float)
            ]
            if signal_wait_samples:
                result["change_signal_wait_samples_ms"] = signal_wait_samples
                result["change_signal_wait_summary_ms"] = _summary(signal_wait_samples)
                result["change_signal_detected_frames"] = sum(
                    1 for item in observations if item.get("change_signal_detected")
                )
            stage_names = sorted(
                {
                    key
                    for item in observations
                    if isinstance((timing := item.get("change_stage_timing_ms")), dict)
                    for key, value in timing.items()
                    if isinstance(value, int | float)
                }
            )
            if stage_names:
                result["change_stage_timing_summary_ms"] = {
                    name: _summary(
                        [
                            float(timing[name])
                            for item in observations
                            if isinstance((timing := item.get("change_stage_timing_ms")), dict)
                            and isinstance(timing.get(name), int | float)
                        ]
                    )
                    for name in stage_names
                }
    _add_nested_timing_summary(
        result,
        observations,
        source_key="observation_transport_timing",
        nested_key="server_emit_timing_ms",
        result_key="server_emit_timing_summary_ms",
    )
    _add_nested_timing_summary(
        result,
        observations,
        source_key="observation_transport_timing",
        nested_key="client_receive_timing_ms",
        result_key="client_receive_timing_summary_ms",
    )
    action_to_frame_samples = [
        timing["action_to_frame_ms"]
        for item in observations
        if isinstance((timing := item.get("benchmark_timing_ms")), dict)
        and isinstance(timing.get("action_to_frame_ms"), int | float)
    ]
    if action_to_frame_samples:
        result["action_to_frame_samples_ms"] = action_to_frame_samples
        result["action_to_frame_summary_ms"] = _summary(action_to_frame_samples)

    receive_minus_pre_emit_samples = [
        timing["receive_frame_ms"] - stage_timing["server_pre_emit_ms"]
        for item in observations
        if isinstance((timing := item.get("benchmark_timing_ms")), dict)
        and isinstance((stage_timing := item.get("change_stage_timing_ms")), dict)
        and isinstance(timing.get("receive_frame_ms"), int | float)
        and isinstance(stage_timing.get("server_pre_emit_ms"), int | float)
    ]
    if receive_minus_pre_emit_samples:
        result["receive_minus_server_pre_emit_samples_ms"] = receive_minus_pre_emit_samples
        result["receive_minus_server_pre_emit_summary_ms"] = _summary(
            receive_minus_pre_emit_samples
        )
    server_emit_minus_pre_emit_samples = [
        timing["receive_frame_ms"]
        - stage_timing["server_pre_emit_ms"]
        - server_timing["emit_total_ms"]
        for item in observations
        if isinstance((timing := item.get("benchmark_timing_ms")), dict)
        and isinstance((stage_timing := item.get("change_stage_timing_ms")), dict)
        and isinstance((transport := item.get("observation_transport_timing")), dict)
        and isinstance((server_timing := transport.get("server_emit_timing_ms")), dict)
        and isinstance(timing.get("receive_frame_ms"), int | float)
        and isinstance(stage_timing.get("server_pre_emit_ms"), int | float)
        and isinstance(server_timing.get("emit_total_ms"), int | float)
    ]
    if server_emit_minus_pre_emit_samples:
        result["receive_minus_server_pre_emit_and_send_samples_ms"] = (
            server_emit_minus_pre_emit_samples
        )
        result["receive_minus_server_pre_emit_and_send_summary_ms"] = _summary(
            server_emit_minus_pre_emit_samples
        )
    mutation_samples = [
        timing["mutation_ms"]
        for item in observations
        if isinstance((timing := item.get("benchmark_timing_ms")), dict)
        and isinstance(timing.get("mutation_ms"), int | float)
    ]
    if mutation_samples:
        result["mutation_samples_ms"] = mutation_samples
        result["mutation_summary_ms"] = _summary(mutation_samples)
    receive_samples = [
        timing["receive_frame_ms"]
        for item in observations
        if isinstance((timing := item.get("benchmark_timing_ms")), dict)
        and isinstance(timing.get("receive_frame_ms"), int | float)
    ]
    if receive_samples:
        result["receive_frame_samples_ms"] = receive_samples
        result["receive_frame_summary_ms"] = _summary(receive_samples)
    action_daemon_samples = [
        timing["action_daemon_ms"]
        for item in observations
        if isinstance((timing := item.get("benchmark_timing_ms")), dict)
        and isinstance(timing.get("action_daemon_ms"), int | float)
    ]
    if action_daemon_samples:
        result["action_daemon_samples_ms"] = action_daemon_samples
        result["action_daemon_summary_ms"] = _summary(action_daemon_samples)
    daemon_samples = [
        _timing["observation_total_ms"]
        for item in observations
        if isinstance((_timing := item.get("screenshot_daemon_timing_ms")), dict)
        and isinstance(_timing.get("observation_total_ms"), int | float)
    ]
    if daemon_samples:
        result["daemon_samples_ms"] = daemon_samples
        result["daemon_summary_ms"] = _summary(daemon_samples)
        result["overhead_samples_ms"] = [
            sample - daemon_sample
            for sample, daemon_sample in zip(samples, daemon_samples, strict=False)
        ]
        result["overhead_summary_ms"] = _summary(result["overhead_samples_ms"])
    _add_observation_latency_diagnosis(result)


def _observation_transport_encoding(observations: list[Any]) -> str:
    encodings = {
        item.get("frame_encoding")
        for item in observations
        if isinstance(item, dict) and isinstance(item.get("frame_encoding"), str)
    }
    if encodings == {"binary-envelope"}:
        return "websocket_binary_envelope"
    return "websocket_json_metadata_binary_payload"


def _add_observation_latency_diagnosis(result: dict[str, Any]) -> None:
    total_p50 = _summary_value(result.get("summary_ms"), "p50")
    daemon_p50 = _summary_value(result.get("daemon_summary_ms"), "p50")
    overhead_p50 = _summary_value(result.get("overhead_summary_ms"), "p50")
    receive_wait_p50 = _summary_value(
        result.get("receive_minus_server_pre_emit_and_send_summary_ms"),
        "p50",
    )
    stability = result.get("sample_stability")
    stability_status = stability.get("status") if isinstance(stability, dict) else "unknown"
    bottleneck = "unknown"
    reason = "insufficient timing attribution"
    if (
        isinstance(overhead_p50, int | float)
        and isinstance(daemon_p50, int | float)
        and isinstance(total_p50, int | float)
    ):
        if overhead_p50 >= max(daemon_p50 * 2.0, total_p50 * 0.5):
            bottleneck = "transport_or_client_wait"
            reason = "overhead p50 dominates daemon p50"
        elif daemon_p50 >= total_p50 * 0.5:
            bottleneck = "daemon_capture_or_diff"
            reason = "daemon p50 is at least half of total p50"
        else:
            bottleneck = "mixed"
            reason = "no single stage dominates p50 latency"
    if isinstance(receive_wait_p50, int | float) and receive_wait_p50 >= 50.0:
        bottleneck = "client_receive_or_tunnel_wait"
        reason = "client receive wait after server pre-emit is at least 50ms p50"
    result["latency_diagnosis"] = {
        "bottleneck": bottleneck,
        "reason": reason,
        "sample_stability": stability_status,
        "total_p50_ms": total_p50,
        "daemon_p50_ms": daemon_p50,
        "overhead_p50_ms": overhead_p50,
        "receive_minus_server_pre_emit_and_send_p50_ms": receive_wait_p50,
    }


def _summary_value(summary: Any, key: str) -> float | None:
    if not isinstance(summary, dict):
        return None
    value = summary.get(key)
    return float(value) if isinstance(value, int | float) else None
