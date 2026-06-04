from __future__ import annotations

import base64
import json
import random
import time
from collections.abc import Callable
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
CLICK_TOGGLE_LOWER_ACTION = {"type": "click", "x": 512, "y": 650, "button": "left"}
CLICK_TOGGLE_TARGET_LEFT = 0
CLICK_TOGGLE_TARGET_TOP = 0
CLICK_TOGGLE_TARGET_WIDTH = 1024
CLICK_TOGGLE_TARGET_HEIGHT = 768
CLICK_TOGGLE_PAGE_READY_SETTLE_MS = 250
CLICK_TOGGLE_SETTLE_MS = 16
CLICK_TOGGLE_READY_TIMEOUT_MS = 10_000
CLICK_TOGGLE_HTTP_LOG_PATH = "/tmp/modal-computer-use-observation-http.log"  # noqa: S108
CAUSAL_ACTION_OBSERVE_DIAGNOSTIC_CASES: tuple[str, ...] = (
    "observation_transport_probe_0b",
    "observation_transport_probe_5kb",
    "observation_transport_probe_50kb",
    "observation_transport_probe_250kb",
    "observation_action_click_act_and_observe_sdk_default_production",
    "observation_action_click_act_and_observe_sdk_default_timeout_200ms_production",
    "observation_action_click_act_and_observe_click_beacon_production",
    "observation_action_click_act_and_observe_click_target_state_production",
    "observation_action_click_act_and_observe_lower_click_target_state_production",
    "observation_action_click_act_and_observe_auto_signal_production",
    "observation_action_click_act_and_observe_auto_signal_binary_envelope_production",
    "observation_action_click_act_and_observe_auto_region_production",
    "observation_action_click_act_and_observe_auto_region_binary_envelope_production",
    "observation_action_click_act_and_observe_paired_envelope_ab_production",
    "observation_action_click_act_and_observe_paired_dirty_producer_ab_production",
    "observation_action_click_act_and_observe_paired_full_frame_fallback_ab_production",
    "observation_action_click_act_and_observe_paired_region_radius_ab_production",
    "observation_action_click_act_and_observe_paired_regional_producer_wait_ab_production",
    "observation_action_click_act_and_observe_paired_dirty_region_confirmation_ab_production",
    "observation_action_click_act_and_observe_paired_confirmation_off_producer_wait_ab_production",
    "observation_action_click_act_and_observe_paired_timeout_ab_production",
)
PAIRED_ENVELOPE_ORDER_SEED = 20260602
PAIRED_DIRTY_PRODUCER_ORDER_SEED = 20260603
PAIRED_DIRTY_PRODUCER_XDAMAGE_ORDER_SEED = 20260604
PAIRED_DIRTY_PRODUCER_CASE = (
    "observation_action_click_act_and_observe_paired_dirty_producer_ab_production"
)
PAIRED_DIRTY_PRODUCER_XDAMAGE_CASE = (
    "observation_action_click_act_and_observe_paired_dirty_producer_xdamage_ab_production"
)
PAIRED_FULL_FRAME_FALLBACK_ORDER_SEED = 20260605
PAIRED_FULL_FRAME_FALLBACK_CASE = (
    "observation_action_click_act_and_observe_paired_full_frame_fallback_ab_production"
)
PAIRED_REGION_RADIUS_ORDER_SEED = 20260606
PAIRED_REGION_RADIUS_CASE = (
    "observation_action_click_act_and_observe_paired_region_radius_ab_production"
)
PAIRED_REGIONAL_PRODUCER_WAIT_ORDER_SEED = 20260607
PAIRED_REGIONAL_PRODUCER_WAIT_CASE = (
    "observation_action_click_act_and_observe_paired_regional_producer_wait_ab_production"
)
PAIRED_DIRTY_REGION_CONFIRMATION_ORDER_SEED = 20260608
PAIRED_DIRTY_REGION_CONFIRMATION_CASE = (
    "observation_action_click_act_and_observe_paired_dirty_region_confirmation_ab_production"
)
PAIRED_CONFIRMATION_OFF_PRODUCER_WAIT_ORDER_SEED = 20260609
PAIRED_CONFIRMATION_OFF_PRODUCER_WAIT_CASE = (
    "observation_action_click_act_and_observe_paired_confirmation_off_producer_wait_ab_production"
)
PAIRED_TIMEOUT_ORDER_SEED = 20260610
PAIRED_TIMEOUT_CASE = "observation_action_click_act_and_observe_paired_timeout_ab_production"
ObservationCaseFactory = Callable[[], dict[str, Any]]


class _ClickBeaconSetupError(Exception):
    def __init__(self, step: str, cause: Exception) -> None:
        super().__init__(f"click beacon setup step failed: {step}")
        self.step = step
        self.cause = cause


def _run_daemon_observation_surface(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    environment_metadata: dict[str, Any] | None,
    observation_cases: list[str] | None = None,
) -> dict[str, Any]:
    factories = _observation_case_factories(
        base_url=base_url,
        token=token,
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    selected = list(factories) if observation_cases is None else observation_cases
    invalid = [name for name in selected if name not in factories]
    if invalid:
        raise ValueError(f"unknown observation benchmark case: {', '.join(invalid)}")
    cases = {name: factories[name]() for name in selected}
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
            "selected_cases": selected,
        },
        runtime_seconds=None,
    )


def _observation_case_factories(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, ObservationCaseFactory]:
    return {
        "observation_first_frame": lambda: _run_observation_first_frame_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_steady_no_change": lambda: _run_observation_no_change_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_small_patch": lambda: _run_observation_small_patch_benchmark(
            base_url=base_url,
            token=token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_sparse_patches": lambda: _run_observation_sparse_patches_benchmark(
            base_url=base_url,
            token=token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_large_change": lambda: _run_observation_large_change_benchmark(
            base_url=base_url,
            token=token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "observation_capture_now_no_change": lambda: (
            _run_observation_capture_now_no_change_benchmark(
                base_url=base_url,
                token=token,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_capture_now_small_patch": lambda: (
            _run_observation_capture_now_small_patch_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_capture_now_sparse_patches": lambda: (
            _run_observation_capture_now_sparse_patches_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_capture_now": lambda: (
            _run_observation_action_click_capture_now_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_stream_capture": lambda: (
            _run_observation_action_click_stream_capture_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_stream_capture_settled": lambda: (
            _run_observation_action_click_stream_capture_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                capture_delay_ms=CLICK_TOGGLE_SETTLE_MS,
            )
        ),
        "observation_action_click_observe_change": lambda: (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                change_signal=None,
            )
        ),
        "observation_action_click_observe_change_poll": lambda: (
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
        "observation_action_click_observe_change_adaptive": lambda: (
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
        "observation_action_click_observe_change_region_adaptive": lambda: (
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
        "observation_action_click_observe_change_xdamage": lambda: (
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
        "observation_action_click_observe_change_auto_signal": lambda: (
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
        "observation_action_click_observe_change_auto_signal_production": lambda: (
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
        "observation_action_click_act_and_observe_auto_signal_production": lambda: (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_act_and_observe_auto_signal_production",
                poll_strategy="adaptive",
                change_signal="auto",
                transport_timing=False,
                causal_action_observe=True,
            )
        ),
        "observation_action_click_act_and_observe_sdk_default_production": lambda: (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_act_and_observe_sdk_default_production",
                poll_strategy="adaptive",
                change_detection="auto",
                change_signal="auto",
                transport_timing=False,
                causal_action_observe=True,
                use_sdk_default_frame_encoding=True,
            )
        ),
        "observation_action_click_act_and_observe_sdk_default_timeout_200ms_production": lambda: (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name=(
                    "observation_action_click_act_and_observe_sdk_default_"
                    "timeout_200ms_production"
                ),
                poll_strategy="adaptive",
                change_detection="auto",
                change_signal="auto",
                change_timeout_ms=200,
                transport_timing=False,
                causal_action_observe=True,
                use_sdk_default_frame_encoding=True,
            )
        ),
        "observation_action_click_act_and_observe_click_beacon_production": lambda: (
            _run_observation_action_click_beacon_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_act_and_observe_click_target_state_production": lambda: (
            _run_observation_action_click_target_state_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_act_and_observe_lower_click_target_state_production": lambda: (
            _run_observation_action_lower_click_target_state_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_act_and_observe_auto_region_production": lambda: (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_act_and_observe_auto_region_production",
                poll_strategy="adaptive",
                change_detection="auto_region",
                change_signal="auto",
                transport_timing=False,
                causal_action_observe=True,
            )
        ),
        "observation_action_click_act_and_observe_auto_signal_binary_envelope_production": lambda: (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_act_and_observe_auto_signal_binary_envelope_production",
                poll_strategy="adaptive",
                change_signal="auto",
                frame_encoding="binary-envelope",
                transport_timing=False,
                causal_action_observe=True,
            )
        ),
        "observation_action_click_act_and_observe_auto_region_binary_envelope_production": lambda: (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_act_and_observe_auto_region_binary_envelope_production",
                poll_strategy="adaptive",
                change_detection="auto_region",
                change_signal="auto",
                frame_encoding="binary-envelope",
                transport_timing=False,
                causal_action_observe=True,
            )
        ),
        "observation_action_click_act_and_observe_paired_envelope_ab_production": lambda: (
            _run_observation_action_click_paired_envelope_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        PAIRED_DIRTY_PRODUCER_CASE: lambda: (
            _run_observation_action_click_paired_dirty_producer_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        PAIRED_DIRTY_PRODUCER_XDAMAGE_CASE: lambda: (
            _run_observation_action_click_paired_dirty_producer_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name=PAIRED_DIRTY_PRODUCER_XDAMAGE_CASE,
                change_detection="full",
                change_signal="xdamage",
                order_seed=PAIRED_DIRTY_PRODUCER_XDAMAGE_ORDER_SEED,
            )
        ),
        PAIRED_FULL_FRAME_FALLBACK_CASE: lambda: (
            _run_observation_action_click_paired_full_frame_fallback_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        PAIRED_REGION_RADIUS_CASE: lambda: (
            _run_observation_action_click_paired_region_radius_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        PAIRED_REGIONAL_PRODUCER_WAIT_CASE: lambda: (
            _run_observation_action_click_paired_regional_producer_wait_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        PAIRED_DIRTY_REGION_CONFIRMATION_CASE: lambda: (
            _run_observation_action_click_paired_dirty_region_confirmation_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        PAIRED_CONFIRMATION_OFF_PRODUCER_WAIT_CASE: lambda: (
            _run_observation_action_click_paired_confirmation_off_producer_wait_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        PAIRED_TIMEOUT_CASE: lambda: (
            _run_observation_action_click_paired_timeout_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_act_and_observe_auto_signal_production_sync": lambda: (
            _run_observation_action_click_observe_change_benchmark(
                base_url=base_url,
                token=token,
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                name="observation_action_click_act_and_observe_auto_signal_production_sync",
                poll_strategy="adaptive",
                change_signal="auto",
                dirty_frame_producer="off",
                transport_timing=False,
                causal_action_observe=True,
            )
        ),
        "observation_action_click_observe_change_auto_signal_binary_envelope": lambda: (
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
        "observation_action_click_observe_change_auto_signal_binary_envelope_production": lambda: (
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
        "observation_action_click_sparse_observe_change_auto_signal": lambda: (
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
        "observation_action_click_fused_raw": lambda: (
            _run_observation_action_click_fused_raw_benchmark(
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_action_click_observe_change_http_raw": lambda: (
            _run_observation_action_click_observe_change_http_raw_benchmark(
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
        ),
        "observation_transport_probe_0b": lambda: _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=0,
        ),
        "observation_transport_probe_envelope_0b": lambda: (
            _run_observation_transport_probe_benchmark(
                base_url=base_url,
                token=token,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                size_bytes=0,
                frame_encoding="binary-envelope",
            )
        ),
        "observation_http_transport_probe_0b": lambda: (
            _run_observation_http_transport_probe_benchmark(
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                size_bytes=0,
            )
        ),
        "observation_transport_probe_5kb": lambda: _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=5 * 1024,
        ),
        "observation_transport_probe_envelope_5kb": lambda: (
            _run_observation_transport_probe_benchmark(
                base_url=base_url,
                token=token,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                size_bytes=5 * 1024,
                frame_encoding="binary-envelope",
            )
        ),
        "observation_http_transport_probe_5kb": lambda: (
            _run_observation_http_transport_probe_benchmark(
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                size_bytes=5 * 1024,
            )
        ),
        "observation_transport_probe_50kb": lambda: _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=50 * 1024,
        ),
        "observation_transport_probe_envelope_50kb": lambda: (
            _run_observation_transport_probe_benchmark(
                base_url=base_url,
                token=token,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                size_bytes=50 * 1024,
                frame_encoding="binary-envelope",
            )
        ),
        "observation_http_transport_probe_50kb": lambda: (
            _run_observation_http_transport_probe_benchmark(
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                size_bytes=50 * 1024,
            )
        ),
        "observation_transport_probe_250kb": lambda: _run_observation_transport_probe_benchmark(
            base_url=base_url,
            token=token,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            size_bytes=250 * 1024,
        ),
        "observation_transport_probe_envelope_250kb": lambda: (
            _run_observation_transport_probe_benchmark(
                base_url=base_url,
                token=token,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                size_bytes=250 * 1024,
                frame_encoding="binary-envelope",
            )
        ),
        "observation_http_transport_probe_250kb": lambda: (
            _run_observation_http_transport_probe_benchmark(
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                size_bytes=250 * 1024,
            )
        ),
        "observation_delta_synthetic": lambda: _run_observation_delta_synthetic_benchmark(
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
    }


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
    dirty_frame_producer: Literal["auto", "off"] = "auto",
    page: str = "default",
    frame_encoding: Literal["json-binary", "binary-envelope"] | None = None,
    transport_timing: bool = True,
    causal_action_observe: bool = False,
    use_sdk_default_frame_encoding: bool = False,
    change_timeout_ms: int = 100,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    effective_frame_encoding = (
        None
        if use_sdk_default_frame_encoding
        else frame_encoding or "json-binary"
    )
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
        dirty_frame_producer=dirty_frame_producer,
        frame_encoding=effective_frame_encoding,
        transport_timing=transport_timing,
        causal_action_observe=causal_action_observe,
        change_timeout_ms=change_timeout_ms,
    )
    result = _case_result(name, iterations, samples, failures)
    _add_frame_observations(result, samples, observations)
    result.update(
        {
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "stream_action_click_observe_change",
            "change_timeout_ms": change_timeout_ms,
            "poll_interval_ms": 8,
            "poll_strategy": poll_strategy,
            "change_detection": change_detection,
            "change_signal": change_signal or "default",
            "dirty_frame_producer": dirty_frame_producer,
            "page": page,
            "frame_encoding": effective_frame_encoding or "binary-envelope",
            "frame_encoding_policy": "sdk-default"
            if use_sdk_default_frame_encoding
            else "benchmark-explicit",
            "transport_timing": transport_timing,
            "causal_action_observe": causal_action_observe,
        }
    )
    return result


def _run_observation_action_click_beacon_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    name: str = "observation_action_click_act_and_observe_click_beacon_production",
    mutation_kind: str = "stream_action_click_observe_change_click_beacon",
    state_probe: bool = False,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    action = action or CLICK_TOGGLE_ACTION
    try:
        beacon_token = _open_click_toggle_beacon_page(client)
    except _ClickBeaconSetupError as exc:
        failure = _failure(
            name,
            phase=f"setup:{exc.step}",
            iteration=0,
            exc=exc.cause,
        )
        failure["setup_step"] = exc.step
        failures.append(failure)
        result = _case_result(name, iterations, [], failures)
        result.update(
            {
                "actions": [_safe_action_metadata(action)],
                "action_count": 1,
                "mutation_kind": mutation_kind,
                "page": "click-beacon",
                "setup_step": exc.step,
                "frame_encoding": "binary-envelope",
                "frame_encoding_policy": "sdk-default",
                "transport_timing": False,
                "causal_action_observe": True,
            }
        )
        return result
    server_probe_before_actions = _probe_click_page_server(client, beacon_token)
    index_events_before_actions = _read_click_index_count(client, beacon_token)
    ready_events_before_actions = _read_click_ready_count(client, beacon_token)
    state_before = _read_click_target_state(client) if state_probe else None
    samples, observations = _measure_stream_action_capture_loop(
        name=name,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        capture_delay_ms=0,
        observe_change=True,
        poll_strategy="adaptive",
        change_detection="auto",
        change_signal="auto",
        transport_timing=False,
        causal_action_observe=True,
        action=action,
    )
    expected_events = iterations + warmup_iterations
    beacon_events = _wait_for_click_beacon_count(
        client,
        beacon_token,
        expected_events=expected_events,
    )
    server_probe_after_actions = _probe_click_page_server(client, beacon_token)
    index_events_after_actions = _read_click_index_count(client, beacon_token)
    ready_events_after_actions = _read_click_ready_count(client, beacon_token)
    state_after = _read_click_target_state(client) if state_probe else None
    direct_action_probe = _probe_direct_action_click_beacon(client, beacon_token, action)
    result = _case_result(
        name,
        iterations,
        samples,
        failures,
    )
    _add_frame_observations(result, samples, observations)
    result.update(
        {
            "actions": [_safe_action_metadata(action)],
            "action_count": 1,
            "mutation_kind": mutation_kind,
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": "auto",
            "change_signal": "auto",
            "page": "click-beacon",
            "frame_encoding": "binary-envelope",
            "frame_encoding_policy": "sdk-default",
            "transport_timing": False,
            "causal_action_observe": True,
            "click_beacon_expected_events": expected_events,
            "click_beacon_events": beacon_events,
            "click_beacon_missing_events": max(expected_events - beacon_events, 0),
            "click_index_events_before_actions": index_events_before_actions,
            "click_index_events_after_actions": index_events_after_actions,
            "click_ready_events_before_actions": ready_events_before_actions,
            "click_ready_events_after_actions": ready_events_after_actions,
            "click_server_probe_before_actions": server_probe_before_actions,
            "click_server_probe_after_actions": server_probe_after_actions,
            "click_direct_action_probe": direct_action_probe,
        }
    )
    if state_probe:
        result["_click_target_state_before"] = state_before
        result["_click_target_state_after"] = state_after
    return result


def _run_observation_action_click_target_state_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    result = _run_observation_action_click_beacon_benchmark(
        base_url=base_url,
        token=token,
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        name="observation_action_click_act_and_observe_click_target_state_production",
        mutation_kind="stream_action_click_observe_change_click_target_state",
        state_probe=True,
    )
    result["click_target_state_before"] = result.pop("_click_target_state_before", None)
    result["click_target_state_after"] = result.pop("_click_target_state_after", None)
    return result


def _run_observation_action_lower_click_target_state_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    result = _run_observation_action_click_beacon_benchmark(
        base_url=base_url,
        token=token,
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        name="observation_action_click_act_and_observe_lower_click_target_state_production",
        mutation_kind="stream_action_click_observe_change_lower_click_target_state",
        state_probe=True,
        action=CLICK_TOGGLE_LOWER_ACTION,
    )
    result["click_target_state_before"] = result.pop("_click_target_state_before", None)
    result["click_target_state_after"] = result.pop("_click_target_state_after", None)
    return result


def _run_observation_action_click_paired_envelope_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    name: str = "observation_action_click_act_and_observe_paired_envelope_ab_production",
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    paired = _measure_paired_stream_action_observe_loop(
        name=name,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        baseline_frame_encoding="json-binary",
        variant_frame_encoding="binary-envelope",
        order_seed=PAIRED_ENVELOPE_ORDER_SEED,
    )
    paired_deltas = paired["paired_delta_samples_ms"]
    result = _case_result(name, iterations, paired_deltas, failures)
    result.update(
        {
            "metric": "paired_delta_ms",
            "delta_direction": "variant_minus_baseline",
            "negative_delta_interpretation": "variant_faster",
            "baseline": {
                "label": "json-binary",
                "frame_encoding": "json-binary",
                "samples_ms": paired["baseline_samples_ms"],
                "summary_ms": _summary(paired["baseline_samples_ms"]),
            },
            "variant": {
                "label": "binary-envelope",
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["variant_samples_ms"],
                "summary_ms": _summary(paired["variant_samples_ms"]),
            },
            "paired_comparison": _paired_ab_comparison(paired_deltas),
            "paired_observations": paired["paired_observations"],
            "pair_order_seed": PAIRED_ENVELOPE_ORDER_SEED,
            "pairing": {
                "scope": "same sandbox/client path/page/stream, per-command frame encoding",
                "order_policy": "seeded_random_ab_ba",
                "reason": "frame_encoding is overridden per action-observe command",
            },
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "stream_action_click_observe_change_paired_ab",
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": "auto",
            "change_signal": "auto",
            "dirty_frame_producer": "auto",
            "transport_timing": False,
            "causal_action_observe": True,
        }
    )
    _add_dirty_frame_producer_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_dirty_frame_producer_rollups(result["variant"], paired.get("variant_observations", []))
    _add_change_stage_timing_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_change_stage_timing_rollups(result["variant"], paired.get("variant_observations", []))
    _add_action_observe_receive_residual_rollups(
        result["baseline"],
        paired.get("baseline_observations", []),
    )
    _add_action_observe_receive_residual_rollups(
        result["variant"],
        paired.get("variant_observations", []),
    )
    return result


def _run_observation_action_click_paired_dirty_producer_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
    name: str = PAIRED_DIRTY_PRODUCER_CASE,
    change_detection: str = "auto",
    change_signal: str = "auto",
    order_seed: int = PAIRED_DIRTY_PRODUCER_ORDER_SEED,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    paired = _measure_paired_stream_action_observe_loop(
        name=name,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        baseline_frame_encoding="binary-envelope",
        variant_frame_encoding="binary-envelope",
        baseline_dirty_frame_producer="off",
        variant_dirty_frame_producer="auto",
        change_detection=change_detection,
        change_signal=change_signal,
        order_seed=order_seed,
    )
    paired_deltas = paired["paired_delta_samples_ms"]
    result = _case_result(name, iterations, paired_deltas, failures)
    result.update(
        {
            "metric": "paired_delta_ms",
            "delta_direction": "variant_minus_baseline",
            "negative_delta_interpretation": "variant_faster",
            "baseline": {
                "label": "dirty-producer-off",
                "dirty_frame_producer": "off",
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["baseline_samples_ms"],
                "summary_ms": _summary(paired["baseline_samples_ms"]),
            },
            "variant": {
                "label": "dirty-producer-auto",
                "dirty_frame_producer": "auto",
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["variant_samples_ms"],
                "summary_ms": _summary(paired["variant_samples_ms"]),
            },
            "paired_comparison": _paired_ab_comparison(paired_deltas),
            "paired_observations": paired["paired_observations"],
            "pair_order_seed": order_seed,
            "pairing": {
                "scope": "same sandbox/client path/page/stream, per-command dirty producer policy",
                "order_policy": "seeded_random_ab_ba",
                "reason": "dirty_frame_producer is overridden per action-observe command",
            },
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "stream_action_click_observe_change_paired_dirty_producer_ab",
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": change_detection,
            "change_signal": change_signal,
            "transport_timing": False,
            "causal_action_observe": True,
        }
    )
    _add_dirty_frame_producer_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_dirty_frame_producer_rollups(result["variant"], paired.get("variant_observations", []))
    _add_change_stage_timing_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_change_stage_timing_rollups(result["variant"], paired.get("variant_observations", []))
    _add_action_observe_receive_residual_rollups(
        result["baseline"],
        paired.get("baseline_observations", []),
    )
    _add_action_observe_receive_residual_rollups(
        result["variant"],
        paired.get("variant_observations", []),
    )
    return result


def _run_observation_action_click_paired_full_frame_fallback_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    paired = _measure_paired_stream_action_observe_loop(
        name=PAIRED_FULL_FRAME_FALLBACK_CASE,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        baseline_frame_encoding="binary-envelope",
        variant_frame_encoding="binary-envelope",
        baseline_dirty_frame_producer="auto",
        variant_dirty_frame_producer="auto",
        baseline_full_frame_fallback=True,
        variant_full_frame_fallback=False,
        change_detection="auto",
        change_signal="auto",
        order_seed=PAIRED_FULL_FRAME_FALLBACK_ORDER_SEED,
    )
    paired_deltas = paired["paired_delta_samples_ms"]
    result = _case_result(PAIRED_FULL_FRAME_FALLBACK_CASE, iterations, paired_deltas, failures)
    result.update(
        {
            "metric": "paired_delta_ms",
            "delta_direction": "variant_minus_baseline",
            "negative_delta_interpretation": "variant_faster",
            "baseline": {
                "label": "full-frame-fallback-on",
                "dirty_frame_producer": "auto",
                "full_frame_fallback": True,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["baseline_samples_ms"],
                "summary_ms": _summary(paired["baseline_samples_ms"]),
            },
            "variant": {
                "label": "full-frame-fallback-off",
                "dirty_frame_producer": "auto",
                "full_frame_fallback": False,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["variant_samples_ms"],
                "summary_ms": _summary(paired["variant_samples_ms"]),
            },
            "paired_comparison": _paired_ab_comparison(paired_deltas),
            "paired_observations": paired["paired_observations"],
            "pair_order_seed": PAIRED_FULL_FRAME_FALLBACK_ORDER_SEED,
            "pairing": {
                "scope": (
                    "same sandbox/client path/page/stream, per-command full-frame fallback policy"
                ),
                "order_policy": "seeded_random_ab_ba",
                "reason": "full_frame_fallback is overridden per action-observe command",
            },
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "stream_action_click_observe_change_paired_full_frame_fallback_ab",
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": "auto",
            "change_signal": "auto",
            "transport_timing": False,
            "causal_action_observe": True,
        }
    )
    _add_dirty_frame_producer_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_dirty_frame_producer_rollups(result["variant"], paired.get("variant_observations", []))
    _add_change_stage_timing_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_change_stage_timing_rollups(result["variant"], paired.get("variant_observations", []))
    _add_action_observe_receive_residual_rollups(
        result["baseline"],
        paired.get("baseline_observations", []),
    )
    _add_action_observe_receive_residual_rollups(
        result["variant"],
        paired.get("variant_observations", []),
    )
    return result


def _run_observation_action_click_paired_region_radius_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    paired = _measure_paired_stream_action_observe_loop(
        name=PAIRED_REGION_RADIUS_CASE,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        baseline_frame_encoding="binary-envelope",
        variant_frame_encoding="binary-envelope",
        baseline_dirty_frame_producer="auto",
        variant_dirty_frame_producer="auto",
        baseline_full_frame_fallback=False,
        variant_full_frame_fallback=False,
        baseline_change_region_radius=96,
        variant_change_region_radius=64,
        change_detection="auto_region",
        change_signal="auto",
        order_seed=PAIRED_REGION_RADIUS_ORDER_SEED,
    )
    paired_deltas = paired["paired_delta_samples_ms"]
    result = _case_result(PAIRED_REGION_RADIUS_CASE, iterations, paired_deltas, failures)
    result.update(
        {
            "metric": "paired_delta_ms",
            "delta_direction": "variant_minus_baseline",
            "negative_delta_interpretation": "variant_faster",
            "baseline": {
                "label": "region-radius-96",
                "dirty_frame_producer": "auto",
                "full_frame_fallback": False,
                "change_region_radius": 96,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["baseline_samples_ms"],
                "summary_ms": _summary(paired["baseline_samples_ms"]),
            },
            "variant": {
                "label": "region-radius-64",
                "dirty_frame_producer": "auto",
                "full_frame_fallback": False,
                "change_region_radius": 64,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["variant_samples_ms"],
                "summary_ms": _summary(paired["variant_samples_ms"]),
            },
            "paired_comparison": _paired_ab_comparison(paired_deltas),
            "paired_observations": paired["paired_observations"],
            "pair_order_seed": PAIRED_REGION_RADIUS_ORDER_SEED,
            "pairing": {
                "scope": "same sandbox/client path/page/stream, per-command auto-region radius",
                "order_policy": "seeded_random_ab_ba",
                "reason": "change_region_radius is overridden per action-observe command",
            },
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "stream_action_click_observe_change_paired_region_radius_ab",
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": "auto_region",
            "change_signal": "auto",
            "transport_timing": False,
            "causal_action_observe": True,
        }
    )
    _add_dirty_frame_producer_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_dirty_frame_producer_rollups(result["variant"], paired.get("variant_observations", []))
    _add_change_stage_timing_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_change_stage_timing_rollups(result["variant"], paired.get("variant_observations", []))
    _add_action_observe_receive_residual_rollups(
        result["baseline"],
        paired.get("baseline_observations", []),
    )
    _add_action_observe_receive_residual_rollups(
        result["variant"],
        paired.get("variant_observations", []),
    )
    return result


def _run_observation_action_click_paired_timeout_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    paired = _measure_paired_stream_action_observe_loop(
        name=PAIRED_TIMEOUT_CASE,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        baseline_frame_encoding="binary-envelope",
        variant_frame_encoding="binary-envelope",
        baseline_dirty_frame_producer="auto",
        variant_dirty_frame_producer="auto",
        baseline_change_timeout_ms=100,
        variant_change_timeout_ms=200,
        change_detection="auto",
        change_signal="auto",
        order_seed=PAIRED_TIMEOUT_ORDER_SEED,
    )
    paired_deltas = paired["paired_delta_samples_ms"]
    result = _case_result(PAIRED_TIMEOUT_CASE, iterations, paired_deltas, failures)
    result.update(
        {
            "metric": "paired_delta_ms",
            "delta_direction": "variant_minus_baseline",
            "negative_delta_interpretation": "variant_faster",
            "baseline": {
                "label": "timeout-100ms",
                "dirty_frame_producer": "auto",
                "change_timeout_ms": 100,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["baseline_samples_ms"],
                "summary_ms": _summary(paired["baseline_samples_ms"]),
            },
            "variant": {
                "label": "timeout-200ms",
                "dirty_frame_producer": "auto",
                "change_timeout_ms": 200,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["variant_samples_ms"],
                "summary_ms": _summary(paired["variant_samples_ms"]),
            },
            "paired_comparison": _paired_ab_comparison(paired_deltas),
            "paired_observations": paired["paired_observations"],
            "pair_order_seed": PAIRED_TIMEOUT_ORDER_SEED,
            "pairing": {
                "scope": "same sandbox/client path/page/stream, per-command timeout budget",
                "order_policy": "seeded_random_ab_ba",
                "reason": "change_timeout_ms is overridden per action-observe command",
            },
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": "stream_action_click_observe_change_paired_timeout_ab",
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": "auto",
            "change_signal": "auto",
            "transport_timing": False,
            "causal_action_observe": True,
        }
    )
    _add_dirty_frame_producer_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_dirty_frame_producer_rollups(result["variant"], paired.get("variant_observations", []))
    _add_change_stage_timing_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_change_stage_timing_rollups(result["variant"], paired.get("variant_observations", []))
    _add_action_observe_receive_residual_rollups(
        result["baseline"],
        paired.get("baseline_observations", []),
    )
    _add_action_observe_receive_residual_rollups(
        result["variant"],
        paired.get("variant_observations", []),
    )
    return result


def _run_observation_action_click_paired_regional_producer_wait_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    paired = _measure_paired_stream_action_observe_loop(
        name=PAIRED_REGIONAL_PRODUCER_WAIT_CASE,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        baseline_frame_encoding="binary-envelope",
        variant_frame_encoding="binary-envelope",
        baseline_dirty_frame_producer="auto",
        variant_dirty_frame_producer="auto",
        baseline_full_frame_fallback=False,
        variant_full_frame_fallback=False,
        baseline_change_region_radius=64,
        variant_change_region_radius=64,
        baseline_dirty_frame_producer_wait_ms=2,
        variant_dirty_frame_producer_wait_ms=1,
        change_detection="auto_region",
        change_signal="auto",
        order_seed=PAIRED_REGIONAL_PRODUCER_WAIT_ORDER_SEED,
    )
    paired_deltas = paired["paired_delta_samples_ms"]
    result = _case_result(PAIRED_REGIONAL_PRODUCER_WAIT_CASE, iterations, paired_deltas, failures)
    result.update(
        {
            "metric": "paired_delta_ms",
            "delta_direction": "variant_minus_baseline",
            "negative_delta_interpretation": "variant_faster",
            "baseline": {
                "label": "regional-producer-wait-2ms",
                "dirty_frame_producer": "auto",
                "dirty_frame_producer_wait_ms": 2,
                "full_frame_fallback": False,
                "change_region_radius": 64,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["baseline_samples_ms"],
                "summary_ms": _summary(paired["baseline_samples_ms"]),
            },
            "variant": {
                "label": "regional-producer-wait-1ms",
                "dirty_frame_producer": "auto",
                "dirty_frame_producer_wait_ms": 1,
                "full_frame_fallback": False,
                "change_region_radius": 64,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["variant_samples_ms"],
                "summary_ms": _summary(paired["variant_samples_ms"]),
            },
            "paired_comparison": _paired_ab_comparison(paired_deltas),
            "paired_observations": paired["paired_observations"],
            "pair_order_seed": PAIRED_REGIONAL_PRODUCER_WAIT_ORDER_SEED,
            "pairing": {
                "scope": (
                    "same sandbox/client path/page/stream, per-command regional dirty producer "
                    "wait budget"
                ),
                "order_policy": "seeded_random_ab_ba",
                "reason": "dirty_frame_producer_wait_ms is overridden per action-observe command",
            },
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": (
                "stream_action_click_observe_change_paired_regional_producer_wait_ab"
            ),
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": "auto_region",
            "change_signal": "auto",
            "transport_timing": False,
            "causal_action_observe": True,
        }
    )
    _add_dirty_frame_producer_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_dirty_frame_producer_rollups(result["variant"], paired.get("variant_observations", []))
    _add_change_stage_timing_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_change_stage_timing_rollups(result["variant"], paired.get("variant_observations", []))
    _add_action_observe_receive_residual_rollups(
        result["baseline"],
        paired.get("baseline_observations", []),
    )
    _add_action_observe_receive_residual_rollups(
        result["variant"],
        paired.get("variant_observations", []),
    )
    return result


def _run_observation_action_click_paired_dirty_region_confirmation_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    paired = _measure_paired_stream_action_observe_loop(
        name=PAIRED_DIRTY_REGION_CONFIRMATION_CASE,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        baseline_frame_encoding="binary-envelope",
        variant_frame_encoding="binary-envelope",
        baseline_dirty_frame_producer="auto",
        variant_dirty_frame_producer="auto",
        baseline_full_frame_fallback=False,
        variant_full_frame_fallback=False,
        baseline_change_region_radius=64,
        variant_change_region_radius=64,
        baseline_dirty_region_confirmation="auto",
        variant_dirty_region_confirmation="off",
        change_detection="auto_region",
        change_signal="auto",
        order_seed=PAIRED_DIRTY_REGION_CONFIRMATION_ORDER_SEED,
    )
    paired_deltas = paired["paired_delta_samples_ms"]
    result = _case_result(
        PAIRED_DIRTY_REGION_CONFIRMATION_CASE,
        iterations,
        paired_deltas,
        failures,
    )
    result.update(
        {
            "metric": "paired_delta_ms",
            "delta_direction": "variant_minus_baseline",
            "negative_delta_interpretation": "variant_faster",
            "baseline": {
                "label": "dirty-region-confirmation-auto",
                "dirty_frame_producer": "auto",
                "dirty_region_confirmation": "auto",
                "full_frame_fallback": False,
                "change_region_radius": 64,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["baseline_samples_ms"],
                "summary_ms": _summary(paired["baseline_samples_ms"]),
            },
            "variant": {
                "label": "dirty-region-confirmation-off",
                "dirty_frame_producer": "auto",
                "dirty_region_confirmation": "off",
                "full_frame_fallback": False,
                "change_region_radius": 64,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["variant_samples_ms"],
                "summary_ms": _summary(paired["variant_samples_ms"]),
            },
            "paired_comparison": _paired_ab_comparison(paired_deltas),
            "paired_observations": paired["paired_observations"],
            "pair_order_seed": PAIRED_DIRTY_REGION_CONFIRMATION_ORDER_SEED,
            "pairing": {
                "scope": (
                    "same sandbox/client path/page/stream, per-command dirty region "
                    "confirmation policy"
                ),
                "order_policy": "seeded_random_ab_ba",
                "reason": "dirty_region_confirmation is overridden per action-observe command",
            },
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": (
                "stream_action_click_observe_change_paired_dirty_region_confirmation_ab"
            ),
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": "auto_region",
            "change_signal": "auto",
            "transport_timing": False,
            "causal_action_observe": True,
        }
    )
    _add_dirty_frame_producer_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_dirty_frame_producer_rollups(result["variant"], paired.get("variant_observations", []))
    _add_change_stage_timing_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_change_stage_timing_rollups(result["variant"], paired.get("variant_observations", []))
    _add_action_observe_receive_residual_rollups(
        result["baseline"],
        paired.get("baseline_observations", []),
    )
    _add_action_observe_receive_residual_rollups(
        result["variant"],
        paired.get("variant_observations", []),
    )
    return result


def _run_observation_action_click_paired_confirmation_off_producer_wait_benchmark(
    *,
    base_url: str,
    token: str | None,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    _open_click_toggle_page(client)
    paired = _measure_paired_stream_action_observe_loop(
        name=PAIRED_CONFIRMATION_OFF_PRODUCER_WAIT_CASE,
        base_url=base_url,
        token=token,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        failures=failures,
        baseline_frame_encoding="binary-envelope",
        variant_frame_encoding="binary-envelope",
        baseline_dirty_frame_producer="auto",
        variant_dirty_frame_producer="auto",
        baseline_full_frame_fallback=False,
        variant_full_frame_fallback=False,
        baseline_change_region_radius=64,
        variant_change_region_radius=64,
        baseline_dirty_frame_producer_wait_ms=2,
        variant_dirty_frame_producer_wait_ms=1,
        baseline_dirty_region_confirmation="off",
        variant_dirty_region_confirmation="off",
        change_detection="auto_region",
        change_signal="auto",
        order_seed=PAIRED_CONFIRMATION_OFF_PRODUCER_WAIT_ORDER_SEED,
    )
    paired_deltas = paired["paired_delta_samples_ms"]
    result = _case_result(
        PAIRED_CONFIRMATION_OFF_PRODUCER_WAIT_CASE,
        iterations,
        paired_deltas,
        failures,
    )
    result.update(
        {
            "metric": "paired_delta_ms",
            "delta_direction": "variant_minus_baseline",
            "negative_delta_interpretation": "variant_faster",
            "baseline": {
                "label": "confirmation-off-producer-wait-2ms",
                "dirty_frame_producer": "auto",
                "dirty_frame_producer_wait_ms": 2,
                "dirty_region_confirmation": "off",
                "full_frame_fallback": False,
                "change_region_radius": 64,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["baseline_samples_ms"],
                "summary_ms": _summary(paired["baseline_samples_ms"]),
            },
            "variant": {
                "label": "confirmation-off-producer-wait-1ms",
                "dirty_frame_producer": "auto",
                "dirty_frame_producer_wait_ms": 1,
                "dirty_region_confirmation": "off",
                "full_frame_fallback": False,
                "change_region_radius": 64,
                "frame_encoding": "binary-envelope",
                "samples_ms": paired["variant_samples_ms"],
                "summary_ms": _summary(paired["variant_samples_ms"]),
            },
            "paired_comparison": _paired_ab_comparison(paired_deltas),
            "paired_observations": paired["paired_observations"],
            "pair_order_seed": PAIRED_CONFIRMATION_OFF_PRODUCER_WAIT_ORDER_SEED,
            "pairing": {
                "scope": (
                    "same sandbox/client path/page/stream, confirmation disabled, "
                    "per-command regional dirty producer wait budget"
                ),
                "order_policy": "seeded_random_ab_ba",
                "reason": (
                    "dirty_frame_producer_wait_ms is overridden while "
                    "dirty_region_confirmation is held off"
                ),
            },
            "actions": [_safe_action_metadata(CLICK_TOGGLE_ACTION)],
            "action_count": 1,
            "mutation_kind": (
                "stream_action_click_observe_change_paired_confirmation_off_producer_wait_ab"
            ),
            "change_timeout_ms": 100,
            "poll_interval_ms": 8,
            "poll_strategy": "adaptive",
            "change_detection": "auto_region",
            "change_signal": "auto",
            "transport_timing": False,
            "causal_action_observe": True,
        }
    )
    _add_dirty_frame_producer_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_dirty_frame_producer_rollups(result["variant"], paired.get("variant_observations", []))
    _add_change_stage_timing_rollups(result["baseline"], paired.get("baseline_observations", []))
    _add_change_stage_timing_rollups(result["variant"], paired.get("variant_observations", []))
    _add_action_observe_receive_residual_rollups(
        result["baseline"],
        paired.get("baseline_observations", []),
    )
    _add_action_observe_receive_residual_rollups(
        result["variant"],
        paired.get("variant_observations", []),
    )
    return result


def _measure_paired_stream_action_observe_loop(
    *,
    name: str,
    base_url: str,
    token: str | None,
    iterations: int,
    warmup_iterations: int,
    failures: list[dict[str, Any]],
    baseline_frame_encoding: Literal["json-binary", "binary-envelope"],
    variant_frame_encoding: Literal["json-binary", "binary-envelope"],
    order_seed: int,
    baseline_dirty_frame_producer: Literal["auto", "off"] = "auto",
    variant_dirty_frame_producer: Literal["auto", "off"] = "auto",
    baseline_full_frame_fallback: bool | None = None,
    variant_full_frame_fallback: bool | None = None,
    baseline_change_region_radius: int | None = None,
    variant_change_region_radius: int | None = None,
    baseline_dirty_frame_producer_wait_ms: int | None = None,
    variant_dirty_frame_producer_wait_ms: int | None = None,
    baseline_dirty_region_confirmation: Literal["auto", "off"] = "auto",
    variant_dirty_region_confirmation: Literal["auto", "off"] = "auto",
    baseline_change_timeout_ms: int = 100,
    variant_change_timeout_ms: int = 100,
    change_detection: str = "auto",
    change_signal: str = "auto",
) -> dict[str, Any]:
    baseline_samples: list[float] = []
    variant_samples: list[float] = []
    baseline_observations: list[dict[str, Any]] = []
    variant_observations: list[dict[str, Any]] = []
    paired_deltas: list[float] = []
    paired_observations: list[dict[str, Any]] = []
    arms = {
        "baseline": {
            "frame_encoding": baseline_frame_encoding,
            "dirty_frame_producer": baseline_dirty_frame_producer,
            "full_frame_fallback": baseline_full_frame_fallback,
            "change_region_radius": baseline_change_region_radius,
            "dirty_frame_producer_wait_ms": baseline_dirty_frame_producer_wait_ms,
            "dirty_region_confirmation": baseline_dirty_region_confirmation,
            "change_timeout_ms": baseline_change_timeout_ms,
        },
        "variant": {
            "frame_encoding": variant_frame_encoding,
            "dirty_frame_producer": variant_dirty_frame_producer,
            "full_frame_fallback": variant_full_frame_fallback,
            "change_region_radius": variant_change_region_radius,
            "dirty_frame_producer_wait_ms": variant_dirty_frame_producer_wait_ms,
            "dirty_region_confirmation": variant_dirty_region_confirmation,
            "change_timeout_ms": variant_change_timeout_ms,
        },
    }
    try:
        with ObservationClient(
            ObservationStreamTransport(base_url, token=token),
            options=OBSERVATION_SCREENSHOT_OPTIONS,
            fps=0.01,
            transport_timing=False,
            frame_encoding=baseline_frame_encoding,
        ) as stream:
            for warmup_index in range(warmup_iterations):
                for arm_label in ("baseline", "variant"):
                    try:
                        _measure_paired_stream_action_observe_arm(
                            stream,
                            frame_encoding=arms[arm_label]["frame_encoding"],
                            dirty_frame_producer=arms[arm_label]["dirty_frame_producer"],
                            full_frame_fallback=arms[arm_label]["full_frame_fallback"],
                            change_region_radius=arms[arm_label]["change_region_radius"],
                            dirty_frame_producer_wait_ms=arms[arm_label][
                                "dirty_frame_producer_wait_ms"
                            ],
                            dirty_region_confirmation=arms[arm_label][
                                "dirty_region_confirmation"
                            ],
                            change_timeout_ms=arms[arm_label]["change_timeout_ms"],
                            change_detection=change_detection,
                            change_signal=change_signal,
                        )
                    except Exception as exc:
                        failures.append(
                            _failure(name, phase="warmup", iteration=warmup_index, exc=exc)
                        )
                        return {
                            "baseline_samples_ms": baseline_samples,
                            "variant_samples_ms": variant_samples,
                            "baseline_observations": baseline_observations,
                            "variant_observations": variant_observations,
                            "paired_delta_samples_ms": paired_deltas,
                            "paired_observations": paired_observations,
                        }

            rng = random.Random(order_seed)  # noqa: S311 - deterministic benchmark ordering only.
            for pair_index in range(iterations):
                order = ["baseline", "variant"]
                if rng.getrandbits(1):
                    order.reverse()
                pair_samples: dict[str, float] = {}
                pair_frames: dict[str, dict[str, Any]] = {}
                for arm_label in order:
                    started = perf_counter()
                    try:
                        observation = _measure_paired_stream_action_observe_arm(
                            stream,
                            frame_encoding=arms[arm_label]["frame_encoding"],
                            dirty_frame_producer=arms[arm_label]["dirty_frame_producer"],
                            full_frame_fallback=arms[arm_label]["full_frame_fallback"],
                            change_region_radius=arms[arm_label]["change_region_radius"],
                            dirty_frame_producer_wait_ms=arms[arm_label][
                                "dirty_frame_producer_wait_ms"
                            ],
                            dirty_region_confirmation=arms[arm_label][
                                "dirty_region_confirmation"
                            ],
                            change_timeout_ms=arms[arm_label]["change_timeout_ms"],
                            change_detection=change_detection,
                            change_signal=change_signal,
                        )
                    except Exception as exc:
                        elapsed_ms = (perf_counter() - started) * 1000
                        failures.append(
                            _failure(
                                name,
                                phase=f"measure:{arm_label}",
                                iteration=pair_index,
                                exc=exc,
                                elapsed_ms=elapsed_ms,
                            )
                        )
                        break
                    sample_ms = (perf_counter() - started) * 1000
                    pair_samples[arm_label] = sample_ms
                    pair_frames[arm_label] = observation
                if set(pair_samples) != {"baseline", "variant"}:
                    continue
                baseline_ms = pair_samples["baseline"]
                variant_ms = pair_samples["variant"]
                delta_ms = variant_ms - baseline_ms
                baseline_samples.append(baseline_ms)
                variant_samples.append(variant_ms)
                baseline_observations.append(pair_frames["baseline"])
                variant_observations.append(pair_frames["variant"])
                paired_deltas.append(delta_ms)
                paired_observations.append(
                    {
                        "pair_index": pair_index,
                        "order": order,
                        "baseline_ms": baseline_ms,
                        "variant_ms": variant_ms,
                        "delta_ms": delta_ms,
                        "ratio": variant_ms / baseline_ms if baseline_ms else None,
                        "baseline_observation": _compact_observation_sample(
                            pair_frames["baseline"]
                        ),
                        "variant_observation": _compact_observation_sample(
                            pair_frames["variant"]
                        ),
                    }
                )
    except Exception as exc:
        failures.append(_failure(name, phase="setup", iteration=0, exc=exc))
    return {
        "baseline_samples_ms": baseline_samples,
        "variant_samples_ms": variant_samples,
        "baseline_observations": baseline_observations,
        "variant_observations": variant_observations,
        "paired_delta_samples_ms": paired_deltas,
        "paired_observations": paired_observations,
    }


def _measure_paired_stream_action_observe_arm(
    stream: ObservationClient,
    *,
    frame_encoding: Literal["json-binary", "binary-envelope"],
    dirty_frame_producer: Literal["auto", "off"],
    full_frame_fallback: bool | None,
    change_region_radius: int | None,
    dirty_frame_producer_wait_ms: int | None,
    dirty_region_confirmation: Literal["auto", "off"],
    change_timeout_ms: int,
    change_detection: str,
    change_signal: str,
) -> dict[str, Any]:
    observation = _stream_action_capture_iteration(
        stream,
        None,
        capture_delay_ms=0,
        observe_change=True,
        poll_strategy="adaptive",
        change_detection=change_detection,
        change_signal=change_signal,
        dirty_frame_producer=dirty_frame_producer,
        full_frame_fallback=full_frame_fallback,
        change_region_radius=change_region_radius,
        dirty_frame_producer_wait_ms=dirty_frame_producer_wait_ms,
        dirty_region_confirmation=dirty_region_confirmation,
        frame_encoding_override=frame_encoding,
        causal_action_observe=True,
        change_timeout_ms=change_timeout_ms,
    )
    observation["benchmark_arm"] = {
        "frame_encoding": frame_encoding,
        "dirty_frame_producer": dirty_frame_producer,
        "full_frame_fallback": full_frame_fallback,
        "change_region_radius": change_region_radius,
        "dirty_frame_producer_wait_ms": dirty_frame_producer_wait_ms,
        "dirty_region_confirmation": dirty_region_confirmation,
        "change_timeout_ms": change_timeout_ms,
        "change_detection": change_detection,
        "change_signal": change_signal,
        "pairing": "same_stream_command_override",
    }
    return observation


def _paired_ab_comparison(deltas: list[float]) -> dict[str, Any]:
    variant_wins = sum(1 for value in deltas if value < 0)
    baseline_wins = sum(1 for value in deltas if value > 0)
    ties = len(deltas) - variant_wins - baseline_wins
    return {
        "status": "measured" if deltas else "unavailable",
        "samples": len(deltas),
        "delta_summary_ms": _summary(deltas),
        "variant_wins": variant_wins,
        "baseline_wins": baseline_wins,
        "ties": ties,
        "variant_win_rate": variant_wins / len(deltas) if deltas else None,
        "baseline_win_rate": baseline_wins / len(deltas) if deltas else None,
    }


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
    connect_attempts: int = 3,
    connect_backoff_seconds: float = 0.25,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    name = (
        f"observation_transport_probe_envelope_{_size_label(size_bytes)}"
        if frame_encoding == "binary-envelope"
        else f"observation_transport_probe_{_size_label(size_bytes)}"
    )
    transport: ObservationStreamTransport | None = None
    observations: list[dict[str, Any]] = []
    samples: list[float] = []
    setup: dict[str, Any] = {
        "attempts": 0,
        "retry_count": 0,
        "elapsed_ms": None,
        "retry_errors": [],
    }
    try:
        transport = ObservationStreamTransport(
            base_url,
            token=token,
            connect_attempts=connect_attempts,
            connect_backoff_seconds=connect_backoff_seconds,
        )
        setup = {
            "attempts": transport.setup_attempts,
            "retry_count": max(transport.setup_attempts - 1, 0),
            "elapsed_ms": transport.setup_elapsed_ms,
            "retry_errors": transport.setup_retry_errors,
        }
        active_transport = transport
        samples, observations = _measure_observed_case(
            name=name,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            operation=lambda: active_transport.transport_probe(
                size_bytes=size_bytes,
                frame_encoding=frame_encoding,
            ),
            failures=failures,
        )
    except Exception as exc:
        failures.append(_failure(name, phase="setup", iteration=0, exc=exc))
    finally:
        if transport is not None:
            transport.close()
    result = _case_result(name, iterations, samples, failures)
    result.update(
        {
            "transport_encoding": "websocket_binary_envelope"
            if frame_encoding == "binary-envelope"
            else "websocket_json_metadata_binary_payload",
            "requested_size_bytes": size_bytes,
            "frame_encoding": frame_encoding or "json-binary",
            "setup": setup,
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
                    failures.append(_failure(name, phase="warmup", iteration=warmup_index, exc=exc))
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
    dirty_frame_producer: Literal["auto", "off"] = "auto",
    full_frame_fallback: bool | None = None,
    frame_encoding: Literal["json-binary", "binary-envelope"] | None = None,
    transport_timing: bool = True,
    causal_action_observe: bool = False,
    change_timeout_ms: int = 100,
    action: dict[str, Any] | None = None,
) -> tuple[list[float], list[dict[str, Any]]]:
    samples: list[float] = []
    observations: list[dict[str, Any]] = []
    client_kwargs: dict[str, Any] = {
        "options": OBSERVATION_SCREENSHOT_OPTIONS,
        "fps": 0.01,
        "transport_timing": transport_timing,
    }
    if frame_encoding is not None:
        client_kwargs["frame_encoding"] = frame_encoding
    try:
        with ObservationClient(
            ObservationStreamTransport(base_url, token=token),
            **client_kwargs,
        ) as stream:
            frames = None
            if transport_timing:
                stream.start(drain_initial_frame=True)
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
                        dirty_frame_producer=dirty_frame_producer,
                        full_frame_fallback=full_frame_fallback,
                        causal_action_observe=causal_action_observe,
                        change_timeout_ms=change_timeout_ms,
                        action=action,
                    )
                except Exception as exc:
                    failures.append(_failure(name, phase="warmup", iteration=warmup_index, exc=exc))
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
                        dirty_frame_producer=dirty_frame_producer,
                        full_frame_fallback=full_frame_fallback,
                        causal_action_observe=causal_action_observe,
                        change_timeout_ms=change_timeout_ms,
                        action=action,
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
    dirty_frame_producer: Literal["auto", "off"] = "auto",
    full_frame_fallback: bool | None = None,
    change_region_radius: int | None = None,
    dirty_frame_producer_wait_ms: int | None = None,
    dirty_region_confirmation: Literal["auto", "off"] = "auto",
    frame_encoding_override: Literal["json-binary", "binary-envelope"] | None = None,
    causal_action_observe: bool = False,
    change_timeout_ms: int = 100,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "actions": [action or CLICK_TOGGLE_ACTION],
        "source": "benchmark",
        "capture_delay_ms": capture_delay_ms,
    }
    if observe_change:
        payload.update(
            {
                "change_timeout_ms": change_timeout_ms,
                "poll_interval_ms": 8,
                "poll_strategy": poll_strategy,
                "change_detection": change_detection,
            }
        )
        if change_signal is not None:
            payload["change_signal"] = change_signal
        payload["dirty_frame_producer"] = dirty_frame_producer
        if dirty_frame_producer_wait_ms is not None:
            payload["dirty_frame_producer_wait_ms"] = dirty_frame_producer_wait_ms
        if dirty_region_confirmation != "auto":
            payload["dirty_region_confirmation"] = dirty_region_confirmation
        if full_frame_fallback is not None:
            payload["full_frame_fallback"] = full_frame_fallback
        if change_region_radius is not None:
            payload["change_region_radius"] = change_region_radius
        if frame_encoding_override is not None:
            payload["frame_encoding"] = frame_encoding_override
        if causal_action_observe:
            call_started = perf_counter()
            result = stream.act_and_observe(**payload)
            action_to_frame_ms = (perf_counter() - call_started) * 1000
            frame = result.frame
            request_frame_ms = 0.0
            receive_frame_ms = action_to_frame_ms
        else:
            request_started = perf_counter()
            stream.run_actions_observe_change(**payload)
            request_frame_ms = (perf_counter() - request_started) * 1000
            receive_started = perf_counter()
            frame = stream.transport.recv_frame_with_timing() if frames is None else next(frames)
            receive_frame_ms = (perf_counter() - receive_started) * 1000
    else:
        request_started = perf_counter()
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
        "full_frame_fallback": full_frame_fallback,
        "causal_action_observe": causal_action_observe,
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
        f"style='position:fixed;left:{CLICK_TOGGLE_TARGET_LEFT}px;"
        f"top:{CLICK_TOGGLE_TARGET_TOP}px;width:{CLICK_TOGGLE_TARGET_WIDTH}px;"
        f"height:{CLICK_TOGGLE_TARGET_HEIGHT}px;"
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
    _wait_for_click_toggle_page_ready()


def _open_click_toggle_beacon_page(client: DaemonClient) -> str:
    beacon_token = str(time.monotonic_ns())
    body = (
        "<!doctype html>"
        "<html style='margin:0;width:100%;height:100%;overflow:hidden;'>"
        "<body style='margin:0;width:100%;height:100%;overflow:hidden;"
        "background:#ffffff;'>"
        "<button id='target' aria-label='toggle' "
        f"style='position:fixed;left:{CLICK_TOGGLE_TARGET_LEFT}px;"
        f"top:{CLICK_TOGGLE_TARGET_TOP}px;width:{CLICK_TOGGLE_TARGET_WIDTH}px;"
        f"height:{CLICK_TOGGLE_TARGET_HEIGHT}px;"
        "border:0;background:#22c55e;color:#111827;font:32px sans-serif;'>0</button>"
        "<script>"
        "let n=0;"
        "const token="
        f"{json.dumps(beacon_token)};"
        "const t=document.getElementById('target');"
        "function paint(){"
        "t.textContent=String(n);"
        "t.style.background=(n%2)?'#ef4444':'#22c55e';"
        "}"
        "function beacon(){"
        "window.__clickBeacons=window.__clickBeacons||[];"
        "const img=new Image();"
        "img.src='/click?token='+encodeURIComponent(token)+'&n='+n+'&t='+Date.now();"
        "window.__clickBeacons.push(img);"
        "}"
        "function readyBeacon(){"
        "window.__readyBeacon=new Image();"
        "window.__readyBeacon.src='/ready?token='+encodeURIComponent(token)+'&t='+Date.now();"
        "}"
        "document.addEventListener('click',()=>{n++;paint();beacon();});"
        "paint();"
        "readyBeacon();"
        "</script>"
        "</body></html>"
    )
    _run_click_beacon_setup_step("serve_page", lambda: _serve_synthetic_page(client, body))
    _run_click_beacon_setup_step(
        "open_browser",
        lambda: client.post_json(
            "/v1/browser/open-url",
            json={
                "url": (
                    "http://127.0.0.1:8766/index.html?"
                    f"action-observe-beacon={quote(beacon_token)}"
                ),
                "wait_for_window": True,
            },
        ),
    )
    _run_click_beacon_setup_step("settle_page", _wait_for_click_toggle_page_ready)
    _run_click_beacon_setup_step(
        "wait_ready",
        lambda: _require_click_ready_count(
            client,
            beacon_token,
            expected_events=1,
            timeout_ms=CLICK_TOGGLE_READY_TIMEOUT_MS,
        ),
    )
    return beacon_token


def _run_click_beacon_setup_step(step: str, operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except Exception as exc:
        raise _ClickBeaconSetupError(step, exc) from exc


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
    _wait_for_click_toggle_page_ready()


def _wait_for_click_toggle_page_ready() -> None:
    time.sleep(CLICK_TOGGLE_PAGE_READY_SETTLE_MS / 1000)


def _serve_synthetic_page(client: DaemonClient, body: str) -> None:
    body_b64 = base64.b64encode(body.encode()).decode()
    script = (
        "import base64, pathlib, socket, subprocess, sys\n"
        "directory = pathlib.Path('/tmp/modal-computer-use-observation')\n"
        "directory.mkdir(parents=True, exist_ok=True)\n"
        "(directory / 'index.html').write_bytes(base64.b64decode(sys.argv[1]))\n"
        "sock = socket.socket()\n"
        "sock.settimeout(0.2)\n"
        "try:\n"
        "    running = sock.connect_ex(('127.0.0.1', 8766)) == 0\n"
        "finally:\n"
        "    sock.close()\n"
        "if not running:\n"
        "    log = open('/tmp/modal-computer-use-observation-http.log', 'ab')\n"
        "    subprocess.Popen(\n"
        "        [sys.executable, '-m', 'http.server', '8766', '--bind', '127.0.0.1', "
        "'--directory', str(directory)],\n"
        "        stdin=subprocess.DEVNULL,\n"
        "        stdout=log,\n"
        "        stderr=subprocess.STDOUT,\n"
        "        start_new_session=True,\n"
        "        close_fds=True,\n"
        "    )\n"
    )
    client.post_json(
        "/v1/commands/run",
        json={"command": ["python3", "-c", script, body_b64], "timeout": 5},
    )


def _read_click_beacon_count(client: DaemonClient, token: str) -> int:
    return _read_http_log_event_count(client, "click", token)


def _read_click_index_count(client: DaemonClient, token: str) -> int:
    needle = f"GET /index.html?action-observe-beacon={quote(token, safe='')}"
    return _read_http_log_needle_count(client, needle)


def _read_click_ready_count(client: DaemonClient, token: str) -> int:
    return _read_http_log_event_count(client, "ready", token)


def _probe_click_page_server(client: DaemonClient, token: str) -> dict[str, Any]:
    url = f"http://127.0.0.1:8766/index.html?server-probe={quote(token)}"
    py = (
        "import sys, urllib.request\n"
        "url = sys.argv[1]\n"
        "try:\n"
        "    with urllib.request.urlopen(url, timeout=2) as response:\n"
        "        body = response.read()\n"
        "        print('ok=true')\n"
        "        print(f'status={response.status}')\n"
        "        print(f'bytes={len(body)}')\n"
        "except Exception as exc:\n"
        "    print('ok=false')\n"
        "    print(f'error_type={type(exc).__name__}')\n"
        "    print(f'error={str(exc)[:200]}')\n"
    )
    script = f"python3 -c {shell_quote(py)} {shell_quote(url)}"
    result = client.post_json(
        "/v1/commands/run",
        json={"command": ["sh", "-lc", script], "timeout": 5},
    )
    output = result.get("output") if isinstance(result, dict) else {}
    stdout = output.get("stdout") if isinstance(output, dict) else None
    return _parse_click_target_state(str(stdout or ""))


def _read_http_log_event_count(
    client: DaemonClient,
    event: Literal["click", "ready"],
    token: str,
) -> int:
    needle = f"GET /{event}?token={quote(token, safe='')}"
    return _read_http_log_needle_count(client, needle)


def _read_http_log_needle_count(client: DaemonClient, needle: str) -> int:
    script = (
        "set -eu; "
        f"log={shell_quote(CLICK_TOGGLE_HTTP_LOG_PATH)}; "
        'if [ -f "$log" ]; then '
        f"grep -F -c {shell_quote(needle)} \"$log\" || true; "
        "else printf '0\\n'; fi"
    )
    result = client.post_json(
        "/v1/commands/run",
        json={"command": ["sh", "-lc", script], "timeout": 5},
    )
    output = result.get("output") if isinstance(result, dict) else {}
    stdout = output.get("stdout") if isinstance(output, dict) else None
    try:
        return max(int(str(stdout).strip() or "0"), 0)
    except ValueError:
        return 0


def _read_click_target_state(client: DaemonClient) -> dict[str, Any]:
    script = (
        "set -u; "
        "if ! command -v xdotool >/dev/null 2>&1; then "
        "printf 'available=false\\n'; exit 0; fi; "
        "printf 'available=true\\n'; "
        "active=$(xdotool getactivewindow 2>/dev/null || true); "
        "printf 'active_window=%s\\n' \"$active\"; "
        'if [ -n "$active" ]; then '
        "name=$(xdotool getwindowname \"$active\" 2>/dev/null || true); "
        "printf 'window_name=%s\\n' \"$name\"; "
        "xdotool getwindowgeometry --shell \"$active\" 2>/dev/null "
        " | sed 's/^/window_/'; "
        "fi; "
        "xdotool getmouselocation --shell 2>/dev/null | sed 's/^/pointer_/' || true"
    )
    result = client.post_json(
        "/v1/commands/run",
        json={"command": ["sh", "-lc", script], "timeout": 5},
    )
    output = result.get("output") if isinstance(result, dict) else {}
    stdout = output.get("stdout") if isinstance(output, dict) else None
    return _parse_click_target_state(str(stdout or ""))


def _parse_click_target_state(stdout: str) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key:
            continue
        state[key.lower()] = _parse_click_target_state_value(value)
    return state


def _parse_click_target_state_value(value: str) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _wait_for_click_beacon_count(
    client: DaemonClient,
    token: str,
    *,
    expected_events: int,
    timeout_ms: int = 500,
) -> int:
    deadline = time.monotonic() + (timeout_ms / 1000)
    count = _read_click_beacon_count(client, token)
    while count < expected_events and time.monotonic() < deadline:
        time.sleep(0.05)
        count = _read_click_beacon_count(client, token)
    return count


def _wait_for_click_ready_count(
    client: DaemonClient,
    token: str,
    *,
    expected_events: int,
    timeout_ms: int = 500,
) -> int:
    deadline = time.monotonic() + (timeout_ms / 1000)
    count = _read_click_ready_count(client, token)
    while count < expected_events and time.monotonic() < deadline:
        time.sleep(0.05)
        count = _read_click_ready_count(client, token)
    return count


def _require_click_ready_count(
    client: DaemonClient,
    token: str,
    *,
    expected_events: int,
    timeout_ms: int,
) -> int:
    count = _wait_for_click_ready_count(
        client,
        token,
        expected_events=expected_events,
        timeout_ms=timeout_ms,
    )
    if count < expected_events:
        raise RuntimeError("click benchmark page did not report ready")
    return count


def _probe_direct_action_click_beacon(
    client: DaemonClient,
    token: str,
    action: dict[str, Any],
) -> dict[str, Any]:
    before = _read_click_beacon_count(client, token)
    probe: dict[str, Any] = {"before": before}
    try:
        result = client.post_json(
            "/v1/actions/run",
            json={"actions": [dict(action)]},
        )
    except Exception as exc:
        probe["error_type"] = type(exc).__name__
        return probe
    after = _wait_for_click_beacon_count(
        client,
        token,
        expected_events=before + 1,
        timeout_ms=CLICK_TOGGLE_READY_TIMEOUT_MS,
    )
    probe["after"] = after
    probe["delta"] = after - before
    if isinstance(result, dict):
        probe["ok"] = result.get("ok")
        items = _action_result_items(result)
        if isinstance(items, list):
            probe["item_count"] = len(items)
            probe["item_ok"] = [item.get("ok") for item in items if isinstance(item, dict)]
            probe["item_error_code"] = [
                item.get("error_code") for item in items if isinstance(item, dict)
            ]
    return probe


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
        "dirty_frame_producer": metadata.get("dirty_frame_producer"),
        "dirty_frame_producer_used": metadata.get("dirty_frame_producer_used"),
        "dirty_frame_producer_fallback_reason": metadata.get(
            "dirty_frame_producer_fallback_reason"
        ),
        "dirty_frame_producer_wait_budget_ms": metadata.get(
            "dirty_frame_producer_wait_budget_ms"
        ),
        "frame_poll_skipped_reason": metadata.get("frame_poll_skipped_reason"),
        "frame_poll_budget_ms": metadata.get("frame_poll_budget_ms"),
        "frame_poll_deadline_reason": metadata.get("frame_poll_deadline_reason"),
        "dirty_region_confirmation_result": metadata.get(
            "dirty_region_confirmation_result"
        ),
        "dirty_frame_capture_region": metadata.get("dirty_frame_capture_region"),
        "dirty_frame_capture_region_source": metadata.get(
            "dirty_frame_capture_region_source"
        ),
        "dirty_frame_age_ms": metadata.get("dirty_frame_age_ms"),
        "xdamage_dirty_rect": metadata.get("xdamage_dirty_rect"),
        "xdamage_dirty_rects": metadata.get("xdamage_dirty_rects"),
        "xdamage_dirty_ratio": metadata.get("xdamage_dirty_ratio"),
        "change_stage_timing_ms": metadata.get("change_stage_timing_ms"),
        "action_observe_attribution_ms": metadata.get("action_observe_attribution_ms"),
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
    sample_rows = _observation_sample_rows(result, samples, observations)
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
            "sample_observations": sample_rows,
            "outlier_observations": [
                row for row in sample_rows if row.get("high_outlier") is True
            ],
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
            _add_dirty_frame_producer_rollups(result, observations)
            _add_change_stage_timing_rollups(result, observations)
            attribution_names = sorted(
                {
                    key
                    for item in observations
                    if isinstance(
                        (attribution := item.get("action_observe_attribution_ms")),
                        dict,
                    )
                    for key, value in attribution.items()
                    if isinstance(value, int | float)
                }
            )
            if attribution_names:
                result["action_observe_attribution_summary_ms"] = {
                    name: _summary(
                        [
                            float(attribution[name])
                            for item in observations
                            if isinstance(
                                (attribution := item.get("action_observe_attribution_ms")),
                                dict,
                            )
                            and isinstance(attribution.get(name), int | float)
                        ]
                    )
                    for name in attribution_names
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
    _add_action_observe_receive_residual_rollups(result, observations)
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


def _add_dirty_frame_producer_rollups(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    if not any(
        isinstance(item, dict) and item.get("dirty_frame_producer") is not None
        for item in observations
    ):
        return
    result["dirty_frame_producer_frames"] = sum(
        1 for item in observations if isinstance(item, dict) and item.get("dirty_frame_producer")
    )
    result["dirty_frame_producer_used_frames"] = sum(
        1
        for item in observations
        if isinstance(item, dict) and item.get("dirty_frame_producer_used")
    )
    result["dirty_frame_producer_fallback_reasons"] = sorted(
        {
            reason
            for item in observations
            if isinstance(item, dict)
            if isinstance(reason := item.get("dirty_frame_producer_fallback_reason"), str)
        }
    )
    result["frame_poll_skipped_reasons"] = sorted(
        {
            reason
            for item in observations
            if isinstance(item, dict)
            if isinstance(reason := item.get("frame_poll_skipped_reason"), str)
        }
    )
    _add_frame_poll_deadline_rollups(result, observations)
    result["dirty_region_confirmation_results"] = sorted(
        {
            reason
            for item in observations
            if isinstance(item, dict)
            if isinstance(reason := item.get("dirty_region_confirmation_result"), str)
        }
    )
    _add_dirty_region_confirmation_capture_timing_rollups(result, observations)
    _add_dirty_frame_capture_region_source_rollups(result, observations)
    dirty_frame_age_samples = [
        item["dirty_frame_age_ms"]
        for item in observations
        if isinstance(item, dict)
        if isinstance(item.get("dirty_frame_age_ms"), int | float)
    ]
    if dirty_frame_age_samples:
        result["dirty_frame_age_samples_ms"] = dirty_frame_age_samples
        result["dirty_frame_age_summary_ms"] = _summary(dirty_frame_age_samples)
    dirty_frame_producer_wait_budget_samples = [
        item["dirty_frame_producer_wait_budget_ms"]
        for item in observations
        if isinstance(item, dict)
        if isinstance(item.get("dirty_frame_producer_wait_budget_ms"), int | float)
    ]
    if dirty_frame_producer_wait_budget_samples:
        result["dirty_frame_producer_wait_budget_samples_ms"] = (
            dirty_frame_producer_wait_budget_samples
        )
        result["dirty_frame_producer_wait_budget_summary_ms"] = _summary(
            dirty_frame_producer_wait_budget_samples
        )
    result["dirty_frame_region_capture_frames"] = sum(
        1
        for item in observations
        if isinstance(item, dict) and item.get("dirty_frame_capture_region") is not None
    )
    _add_dirty_frame_capture_region_size_rollups(result, observations)


def _add_change_stage_timing_rollups(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    stage_names = sorted(
        {
            key
            for item in observations
            if isinstance(item, dict)
            and isinstance((timing := item.get("change_stage_timing_ms")), dict)
            for key, value in timing.items()
            if isinstance(value, int | float)
        }
    )
    if not stage_names:
        return
    result["change_stage_timing_summary_ms"] = {
        name: _summary(
            [
                float(timing[name])
                for item in observations
                if isinstance(item, dict)
                and isinstance((timing := item.get("change_stage_timing_ms")), dict)
                and isinstance(timing.get(name), int | float)
            ]
        )
        for name in stage_names
    }


def _add_action_observe_receive_residual_rollups(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    action_to_frame_samples = [
        timing["action_to_frame_ms"]
        for item in observations
        if isinstance(item, dict)
        and isinstance((timing := item.get("benchmark_timing_ms")), dict)
        and isinstance(timing.get("action_to_frame_ms"), int | float)
    ]
    if action_to_frame_samples:
        result["action_to_frame_samples_ms"] = action_to_frame_samples
        result["action_to_frame_summary_ms"] = _summary(action_to_frame_samples)

    receive_minus_pre_emit_samples = [
        timing["receive_frame_ms"] - stage_timing["server_pre_emit_ms"]
        for item in observations
        if isinstance(item, dict)
        and isinstance((timing := item.get("benchmark_timing_ms")), dict)
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
        if isinstance(item, dict)
        and isinstance((timing := item.get("benchmark_timing_ms")), dict)
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


def _add_dirty_frame_capture_region_source_rollups(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    sources = sorted(
        {
            source
            for item in observations
            if isinstance(item, dict)
            if isinstance(source := item.get("dirty_frame_capture_region_source"), str)
        }
    )
    if not sources:
        return
    result["dirty_frame_capture_region_sources"] = sources
    result["dirty_frame_capture_region_source_summaries"] = {
        source: _dirty_frame_capture_region_source_summary(observations, source)
        for source in sources
    }


def _dirty_frame_capture_region_source_summary(
    observations: list[Any],
    source: str,
) -> dict[str, Any]:
    rows = [
        item
        for item in observations
        if isinstance(item, dict) and item.get("dirty_frame_capture_region_source") == source
    ]
    dirty_frame_age_samples = [
        float(item["dirty_frame_age_ms"])
        for item in rows
        if isinstance(item.get("dirty_frame_age_ms"), int | float)
    ]
    result: dict[str, Any] = {
        "frames": len(rows),
        "producer_used_frames": sum(1 for item in rows if item.get("dirty_frame_producer_used")),
        "changed_frames": sum(1 for item in rows if item.get("change_detected") is True),
        "unchanged_frames": sum(1 for item in rows if item.get("unchanged") is True),
        "timeout_frames": sum(1 for item in rows if item.get("change_timeout_reached") is True),
        "fallback_reasons": sorted(
            {
                reason
                for item in rows
                if isinstance(reason := item.get("dirty_frame_producer_fallback_reason"), str)
            }
        ),
        "dirty_region_confirmation_results": sorted(
            {
                value
                for item in rows
                if isinstance(value := item.get("dirty_region_confirmation_result"), str)
            }
        ),
        "frame_poll_deadline_reasons": sorted(
            {
                reason
                for item in rows
                if isinstance(reason := item.get("frame_poll_deadline_reason"), str)
            }
        ),
    }
    if dirty_frame_age_samples:
        result["dirty_frame_age_samples_ms"] = dirty_frame_age_samples
        result["dirty_frame_age_summary_ms"] = _summary(dirty_frame_age_samples)
    _add_dirty_frame_capture_region_size_rollups(result, rows)
    _add_dirty_region_confirmation_capture_timing_rollups(result, rows)
    return result


def _add_dirty_frame_capture_region_size_rollups(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    widths: list[float] = []
    heights: list[float] = []
    areas: list[float] = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        region = item.get("dirty_frame_capture_region")
        if not isinstance(region, dict):
            continue
        width = region.get("width")
        height = region.get("height")
        if not isinstance(width, int | float) or not isinstance(height, int | float):
            continue
        widths.append(float(width))
        heights.append(float(height))
        areas.append(float(width) * float(height))
    if not areas:
        return
    result["dirty_frame_capture_region_width_summary_px"] = _summary(widths)
    result["dirty_frame_capture_region_height_summary_px"] = _summary(heights)
    result["dirty_frame_capture_region_area_summary_px"] = _summary(areas)


def _add_frame_poll_deadline_rollups(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    frame_poll_deadline_reasons = sorted(
        {
            reason
            for item in observations
            if isinstance(item, dict)
            if isinstance(reason := item.get("frame_poll_deadline_reason"), str)
        }
    )
    if frame_poll_deadline_reasons:
        result["frame_poll_deadline_reasons"] = frame_poll_deadline_reasons
        result["frame_poll_deadline_reason_summaries"] = {
            reason: _frame_poll_deadline_reason_summary(observations, reason)
            for reason in frame_poll_deadline_reasons
        }
    frame_poll_budget_samples = [
        item["frame_poll_budget_ms"]
        for item in observations
        if isinstance(item, dict)
        if isinstance(item.get("frame_poll_budget_ms"), int | float)
    ]
    if frame_poll_budget_samples:
        result["frame_poll_budget_samples_ms"] = frame_poll_budget_samples
        result["frame_poll_budget_summary_ms"] = _summary(frame_poll_budget_samples)


def _frame_poll_deadline_reason_summary(
    observations: list[Any],
    reason: str,
) -> dict[str, Any]:
    rows = [
        item
        for item in observations
        if isinstance(item, dict) and item.get("frame_poll_deadline_reason") == reason
    ]
    frame_poll_samples = [
        float(timing["frame_poll_ms"])
        for item in rows
        if isinstance((timing := item.get("change_stage_timing_ms")), dict)
        and isinstance(timing.get("frame_poll_ms"), int | float)
    ]
    result: dict[str, Any] = {
        "frames": len(rows),
        "changed_frames": sum(1 for item in rows if item.get("change_detected") is True),
        "unchanged_frames": sum(1 for item in rows if item.get("unchanged") is True),
        "timeout_frames": sum(1 for item in rows if item.get("change_timeout_reached") is True),
        "dirty_region_confirmation_results": sorted(
            {
                value
                for item in rows
                if isinstance(value := item.get("dirty_region_confirmation_result"), str)
            }
        ),
    }
    if frame_poll_samples:
        result["frame_poll_samples_ms"] = frame_poll_samples
        result["frame_poll_summary_ms"] = _summary(frame_poll_samples)
    _add_frame_poll_capture_timing_rollups(result, rows)
    _add_dirty_region_confirmation_capture_timing_rollups(result, rows)
    return result


def _add_frame_poll_capture_timing_rollups(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    rows = [
        item
        for item in observations
        if isinstance(item, dict)
        and isinstance((timing := item.get("change_stage_timing_ms")), dict)
        and isinstance(timing.get("frame_poll_capture_ms"), int | float)
        and timing["frame_poll_capture_ms"] > 0
    ]
    timing_keys = {
        "total_ms": "frame_poll_capture_ms",
        "ready_ms": "frame_poll_capture_ready_ms",
        "lock_wait_ms": "frame_poll_capture_lock_wait_ms",
        "operation_ms": "frame_poll_capture_operation_ms",
    }
    timing_summary = {
        summary_name: _summary(samples)
        for summary_name, timing_name in timing_keys.items()
        if (
            samples := [
                float(timing[timing_name])
                for item in rows
                if isinstance((timing := item.get("change_stage_timing_ms")), dict)
                and isinstance(timing.get(timing_name), int | float)
            ]
        )
    }
    if timing_summary:
        result["frame_poll_capture_timing_summary_ms"] = timing_summary


def _add_dirty_region_confirmation_capture_timing_rollups(
    result: dict[str, Any],
    observations: list[Any],
) -> None:
    rows = [
        item
        for item in observations
        if isinstance(item, dict)
        and (
            isinstance(item.get("dirty_region_confirmation_result"), str)
            or (
                isinstance((timing := item.get("change_stage_timing_ms")), dict)
                and isinstance(timing.get("dirty_region_confirmation_capture_ms"), int | float)
                and timing["dirty_region_confirmation_capture_ms"] > 0
            )
        )
    ]
    timing_keys = {
        "total_ms": "dirty_region_confirmation_capture_ms",
        "ready_ms": "dirty_region_confirmation_capture_ready_ms",
        "lock_wait_ms": "dirty_region_confirmation_capture_lock_wait_ms",
        "operation_ms": "dirty_region_confirmation_capture_operation_ms",
        "native_ms": "dirty_region_confirmation_native_ms",
    }
    timing_summary = {
        summary_name: _summary(samples)
        for summary_name, timing_name in timing_keys.items()
        if (
            samples := [
                float(timing[timing_name])
                for item in rows
                if isinstance((timing := item.get("change_stage_timing_ms")), dict)
                and isinstance(timing.get(timing_name), int | float)
            ]
        )
    }
    if timing_summary:
        result["dirty_region_confirmation_capture_timing_summary_ms"] = timing_summary


def _observation_sample_rows(
    result: dict[str, Any],
    samples: list[float],
    observations: list[Any],
) -> list[dict[str, Any]]:
    summary = result.get("summary_ms")
    outlier_indices = (
        set(summary.get("high_outlier_indices", [])) if isinstance(summary, dict) else set()
    )
    rows: list[dict[str, Any]] = []
    for index, (sample_ms, observation) in enumerate(zip(samples, observations, strict=False)):
        if not isinstance(observation, dict):
            continue
        compact = _compact_observation_sample(observation)
        compact.update(
            {
                "iteration": index,
                "sample_ms": sample_ms,
                "high_outlier": index in outlier_indices,
            }
        )
        rows.append(compact)
    return rows


def _compact_observation_sample(observation: dict[str, Any]) -> dict[str, Any]:
    benchmark_timing = observation.get("benchmark_timing_ms")
    transport_timing = observation.get("observation_transport_timing")
    server_emit_timing = (
        transport_timing.get("server_emit_timing_ms")
        if isinstance(transport_timing, dict)
        else None
    )
    client_receive_timing = (
        transport_timing.get("client_receive_timing_ms")
        if isinstance(transport_timing, dict)
        else None
    )
    return {
        "kind": observation.get("kind"),
        "unchanged": observation.get("unchanged"),
        "size_bytes": observation.get("size_bytes"),
        "metadata_size_bytes": observation.get("metadata_size_bytes"),
        "frame_encoding": observation.get("frame_encoding"),
        "dirty_rect": observation.get("dirty_rect"),
        "dirty_ratio": observation.get("dirty_ratio"),
        "patch_count": observation.get("patch_count"),
        "patch_rects": observation.get("patch_rects"),
        "patch_sizes_bytes": observation.get("patch_sizes_bytes"),
        "change_detected": observation.get("change_detected"),
        "change_timeout_reached": observation.get("change_timeout_reached"),
        "change_wait_ms": observation.get("change_wait_ms"),
        "change_signal": observation.get("change_signal"),
        "change_signal_detected": observation.get("change_signal_detected"),
        "change_signal_wait_ms": observation.get("change_signal_wait_ms"),
        "change_signal_reason": observation.get("change_signal_reason"),
        "change_detection": observation.get("change_detection"),
        "change_detection_region": observation.get("change_detection_region"),
        "full_frame_fallback": observation.get("full_frame_fallback"),
        "dirty_frame_producer": observation.get("dirty_frame_producer"),
        "dirty_frame_producer_used": observation.get("dirty_frame_producer_used"),
        "dirty_frame_producer_fallback_reason": observation.get(
            "dirty_frame_producer_fallback_reason"
        ),
        "dirty_frame_producer_wait_budget_ms": observation.get(
            "dirty_frame_producer_wait_budget_ms"
        ),
        "frame_poll_skipped_reason": observation.get("frame_poll_skipped_reason"),
        "frame_poll_budget_ms": observation.get("frame_poll_budget_ms"),
        "frame_poll_deadline_reason": observation.get("frame_poll_deadline_reason"),
        "dirty_region_confirmation_result": observation.get(
            "dirty_region_confirmation_result"
        ),
        "dirty_frame_age_ms": observation.get("dirty_frame_age_ms"),
        "dirty_frame_capture_region": observation.get("dirty_frame_capture_region"),
        "dirty_frame_capture_region_source": observation.get(
            "dirty_frame_capture_region_source"
        ),
        "xdamage_dirty_rect": observation.get("xdamage_dirty_rect"),
        "xdamage_dirty_rects": observation.get("xdamage_dirty_rects"),
        "xdamage_dirty_ratio": observation.get("xdamage_dirty_ratio"),
        "source_version": observation.get("source_version"),
        "previous_source_version": observation.get("previous_source_version"),
        "emit_version": observation.get("emit_version"),
        "delivery": observation.get("delivery"),
        "capture_backend": observation.get("capture_backend"),
        "tile_hash_backend": observation.get("tile_hash_backend"),
        "change_stage_timing_ms": observation.get("change_stage_timing_ms"),
        "action_observe_attribution_ms": observation.get("action_observe_attribution_ms"),
        "action_result": _compact_action_result(observation.get("action_result")),
        "screenshot_daemon_timing_ms": observation.get("screenshot_daemon_timing_ms"),
        "benchmark_timing_ms": benchmark_timing if isinstance(benchmark_timing, dict) else {},
        "server_emit_timing_ms": server_emit_timing
        if isinstance(server_emit_timing, dict)
        else {},
        "client_receive_timing_ms": client_receive_timing
        if isinstance(client_receive_timing, dict)
        else {},
    }


def _compact_action_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    compact: dict[str, Any] = {"ok": value.get("ok")}
    items = _action_result_items(value)
    if isinstance(items, list):
        compact["item_count"] = len(items)
        compact["item_ok"] = [item.get("ok") for item in items if isinstance(item, dict)]
        compact["item_error_code"] = [
            item.get("error_code") for item in items if isinstance(item, dict)
        ]
        input_backends = [
            output.get("input_backend")
            for item in items
            if isinstance(item, dict)
            and isinstance((output := item.get("output")), dict)
            and output.get("input_backend") is not None
        ]
        if input_backends:
            compact["input_backend"] = input_backends
    return compact


def _action_result_items(value: dict[str, Any]) -> Any:
    return value.get("items", value.get("results"))


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
    attribution = result.get("action_observe_attribution_summary_ms")
    action_to_signal_p50 = _nested_summary_value(
        attribution,
        "action_end_to_signal_detect_ms",
        "p50",
    )
    signal_to_capture_p50 = _nested_summary_value(
        attribution,
        "signal_detect_to_capture_start_ms",
        "p50",
    )
    capture_to_delta_p50 = _nested_summary_value(
        attribution,
        "capture_start_to_delta_ready_ms",
        "p50",
    )
    delta_to_pre_emit_p50 = _nested_summary_value(attribution, "delta_ready_to_pre_emit_ms", "p50")
    action_end_to_pre_emit_p50 = _nested_summary_value(
        attribution,
        "action_end_to_pre_emit_ms",
        "p50",
    )
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
    elif (
        isinstance(action_to_signal_p50, int | float)
        and isinstance(action_end_to_pre_emit_p50, int | float)
        and action_to_signal_p50 >= max(8.0, action_end_to_pre_emit_p50 * 0.5)
    ):
        bottleneck = "action_to_damage_signal"
        reason = "action end to XDamage signal is the largest attributed daemon interval"
    elif (
        isinstance(capture_to_delta_p50, int | float)
        and isinstance(action_end_to_pre_emit_p50, int | float)
        and capture_to_delta_p50 >= max(4.0, action_end_to_pre_emit_p50 * 0.35)
    ):
        bottleneck = "capture_diff_or_encode"
        reason = "capture-to-delta-ready is a material share of daemon pre-emit latency"
    result["latency_diagnosis"] = {
        "bottleneck": bottleneck,
        "reason": reason,
        "sample_stability": stability_status,
        "total_p50_ms": total_p50,
        "daemon_p50_ms": daemon_p50,
        "overhead_p50_ms": overhead_p50,
        "action_end_to_signal_detect_p50_ms": action_to_signal_p50,
        "signal_detect_to_capture_start_p50_ms": signal_to_capture_p50,
        "capture_start_to_delta_ready_p50_ms": capture_to_delta_p50,
        "delta_ready_to_pre_emit_p50_ms": delta_to_pre_emit_p50,
        "action_end_to_pre_emit_p50_ms": action_end_to_pre_emit_p50,
        "receive_minus_server_pre_emit_and_send_p50_ms": receive_wait_p50,
    }


def _summary_value(summary: Any, key: str) -> float | None:
    if not isinstance(summary, dict):
        return None
    value = summary.get(key)
    return float(value) if isinstance(value, int | float) else None


def _nested_summary_value(summary: Any, name: str, key: str) -> float | None:
    if not isinstance(summary, dict):
        return None
    return _summary_value(summary.get(name), key)
