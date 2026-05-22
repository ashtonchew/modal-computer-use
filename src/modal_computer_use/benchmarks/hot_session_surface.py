from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..hot_session import HotSessionClient
from .constants import MOVE_CLICK_ACTIONS, MOVE_CLICK_SEQUENCE_ACTIONS
from .measurement import _attributed_case_result, _case_result, _measure_observed_case, _summary
from .operations import _input_backend_result
from .safety import _ensure_ok_result, _extract_daemon_ms, _safe_screenshot_result
from .surface_result import _surface_result


def _run_daemon_hot_session_surface(
    *,
    hot_session: HotSessionClient,
    iterations: int,
    warmup_iterations: int,
    environment_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    cases = {
        "screenshot_full_raw": _run_hot_screenshot_raw_benchmark(
            hot_session=hot_session,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "move_click": _run_hot_move_click_benchmark(
            hot_session=hot_session,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "click_screenshot_raw": _run_hot_click_screenshot_raw_benchmark(
            hot_session=hot_session,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "move_click_sequence": _run_hot_move_click_sequence_benchmark(
            hot_session=hot_session,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
    }
    return _surface_result(
        "daemon-hot-session",
        cases=cases,
        metadata={
            "transport": "daemon-hot-session",
            "canonical_name": _hot_session_canonical_name(environment_metadata),
            "protocol": "computer-use.hot-session.v1",
            "environment": {
                key: value
                for key, value in (environment_metadata or {}).items()
                if value is not None
            },
        },
        runtime_seconds=None,
    )


def _hot_session_canonical_name(environment_metadata: dict[str, Any] | None) -> str:
    modal_ingress = (
        None if environment_metadata is None else environment_metadata.get("modal_ingress")
    )
    if modal_ingress == "attested-tunnel":
        return "modal-daemon-attested-hot-session"
    if modal_ingress == "tunnel":
        return "modal-daemon-hot-session"
    if modal_ingress == "connect":
        return "modal-daemon-connect-hot-session"
    return "daemon-hot-session"


def _run_hot_move_click_benchmark(
    *,
    hot_session: HotSessionClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples, observations = _measure_observed_case(
        name="move_click",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _run_hot_actions(hot_session, MOVE_CLICK_ACTIONS),
        failures=failures,
    )
    result = _attributed_case_result("move_click", iterations, samples, observations, failures)
    result.update({"action_count": len(MOVE_CLICK_ACTIONS)})
    return result


def _run_hot_move_click_sequence_benchmark(
    *,
    hot_session: HotSessionClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples, observations = _measure_observed_case(
        name="move_click_sequence",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _run_hot_actions(hot_session, MOVE_CLICK_SEQUENCE_ACTIONS),
        failures=failures,
    )
    result = _attributed_case_result(
        "move_click_sequence", iterations, samples, observations, failures
    )
    result.update({"action_count": len(MOVE_CLICK_SEQUENCE_ACTIONS)})
    return result


def _run_hot_click_screenshot_raw_benchmark(
    *,
    hot_session: HotSessionClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples, observations = _measure_observed_case(
        name="click_screenshot_raw",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _run_hot_action_screenshot(hot_session),
        failures=failures,
    )
    result = _attributed_case_result(
        "click_screenshot_raw", iterations, samples, observations, failures
    )
    result.update(
        {
            "request": {"format": "png", "show_cursor": False},
            "transport_encoding": "websocket_binary",
            "action_count": len(MOVE_CLICK_ACTIONS),
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
    return result


def _run_hot_screenshot_raw_benchmark(
    *,
    hot_session: HotSessionClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples, observations = _measure_observed_case(
        name="screenshot_full_raw",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: _run_hot_screenshot(hot_session),
        failures=failures,
    )
    result = _case_result("screenshot_full_raw", iterations, samples, failures)
    result.update(
        {
            "request": {"format": "png", "show_cursor": False},
            "transport_encoding": "websocket_binary",
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
        float(item["daemon_ms"])
        for item in observations
        if item.get("daemon_ms") is not None
    ]
    if daemon_samples:
        result["daemon_samples_ms"] = daemon_samples
        result["daemon_summary_ms"] = _summary(daemon_samples)
        result["overhead_samples_ms"] = [
            sample - daemon_sample
            for sample, daemon_sample in zip(samples, daemon_samples, strict=False)
        ]
        result["overhead_summary_ms"] = _summary(result["overhead_samples_ms"])
    return result


def _run_hot_actions(
    hot_session: HotSessionClient, actions: list[dict[str, Any]]
) -> dict[str, Any]:
    result = hot_session.run_actions(actions).model_dump(mode="json")
    _ensure_ok_result(result)
    return {
        "daemon_ms": _extract_daemon_ms(result),
        "input_backend": _input_backend_result(result),
        "transport_http_version": "websocket",
    }


def _run_hot_action_screenshot(hot_session: HotSessionClient) -> dict[str, Any]:
    shot = hot_session.run_actions_with_raw_screenshot(
        MOVE_CLICK_ACTIONS,
        screenshot_options={"format": "png", "show_cursor": False},
    )
    action_result = shot.result or {}
    _ensure_ok_result(action_result)
    timing = _timing_header(shot.headers)
    return {
        "format": "png",
        "width": _int_header(shot.headers, "x-computer-use-width"),
        "height": _int_header(shot.headers, "x-computer-use-height"),
        "size_bytes": len(shot.payload),
        "storage": "inline",
        "artifact_backed": False,
        "capture_backend": _str_header(shot.headers, "x-computer-use-capture-backend"),
        "daemon_ms": _extract_daemon_ms(action_result),
        "input_backend": _input_backend_result(action_result),
        "transport_http_version": "websocket",
        "action_result": action_result,
        "screenshot_daemon_timing_ms": timing,
    }


def _run_hot_screenshot(hot_session: HotSessionClient) -> dict[str, Any]:
    shot = hot_session.screenshot_raw({"format": "png", "show_cursor": False})
    timing = _timing_header(shot.headers)
    return {
        **_safe_screenshot_result(
            {
                "format": "png",
                "width": _int_header(shot.headers, "x-computer-use-width"),
                "height": _int_header(shot.headers, "x-computer-use-height"),
                "size_bytes": len(shot.payload),
                "storage": "inline",
                "artifact_backed": False,
            }
        ),
        "capture_backend": _str_header(shot.headers, "x-computer-use-capture-backend"),
        "daemon_ms": timing.get("total_ms"),
        "daemon_timing_ms": timing,
        "transport_http_version": "websocket",
    }


def _timing_header(headers: Mapping[str, str]) -> dict[str, float]:
    value = headers.get("x-computer-use-timing-ms")
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


def _str_header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    return value if isinstance(value, str) and value else None


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name)
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)
