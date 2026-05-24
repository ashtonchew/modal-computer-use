from __future__ import annotations

import time
from shlex import quote as shell_quote
from time import perf_counter
from typing import Any
from urllib.parse import quote

from ..client import DaemonClient
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
            )
        ),
        "observation_action_click_fused_raw": _run_observation_action_click_fused_raw_benchmark(
            client=client,
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
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    name = "observation_action_click_observe_change"
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
    return result


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
                    _stream_action_capture_iteration(
                        stream,
                        frames,
                        capture_delay_ms=capture_delay_ms,
                        observe_change=observe_change,
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
) -> dict[str, Any]:
    request_started = perf_counter()
    payload = {
        "actions": [CLICK_TOGGLE_ACTION],
        "source": "benchmark",
        "capture_delay_ms": capture_delay_ms,
    }
    if observe_change:
        payload.update({"change_timeout_ms": 100, "poll_interval_ms": 8})
        stream.run_actions_observe_change(**payload)
    else:
        stream.run_actions_capture(**payload)
    request_frame_ms = (perf_counter() - request_started) * 1000

    receive_started = perf_counter()
    frame = next(frames)
    receive_frame_ms = (perf_counter() - receive_started) * 1000

    observation = _frame_observation(frame)
    action_result = observation.get("action_result")
    observation["benchmark_timing_ms"] = {
        "mutation_ms": 0.0,
        "capture_delay_ms": capture_delay_ms,
        "observe_change": observe_change,
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


def _collect_first_frame(base_url: str, token: str | None) -> dict[str, Any]:
    with ObservationClient(
        ObservationStreamTransport(base_url, token=token),
        options=OBSERVATION_SCREENSHOT_OPTIONS,
        fps=30,
        max_frames=1,
    ) as stream:
        return _frame_observation(next(stream.frames()))


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
        "trigger": metadata.get("trigger"),
        "action_result": metadata.get("action_result"),
        "change_detected": metadata.get("change_detected"),
        "change_attempts": metadata.get("change_attempts"),
        "change_wait_ms": metadata.get("change_wait_ms"),
        "change_timeout_reached": metadata.get("change_timeout_reached"),
        "screenshot_daemon_timing_ms": timing if isinstance(timing, dict) else {},
    }


def _add_frame_observations(
    result: dict[str, Any],
    samples: list[float],
    observations: list[Any],
) -> None:
    result.update(
        {
            "request": OBSERVATION_SCREENSHOT_OPTIONS,
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
    changed_count = sum(1 for item in observations if item.get("unchanged") is False)
    if observations:
        result["changed_frames"] = changed_count
        result["unchanged_frames"] = len(observations) - changed_count
        result["changed_frame_ratio"] = changed_count / len(observations)
        change_detected_count = sum(1 for item in observations if item.get("change_detected"))
        if any(item.get("change_detected") is not None for item in observations):
            result["change_detected_frames"] = change_detected_count
            result["change_detected_ratio"] = change_detected_count / len(observations)
            result["change_timeout_frames"] = sum(
                1 for item in observations if item.get("change_timeout_reached")
            )
            change_wait_samples = [
                item["change_wait_ms"]
                for item in observations
                if isinstance(item.get("change_wait_ms"), int | float)
            ]
            if change_wait_samples:
                result["change_wait_samples_ms"] = change_wait_samples
                result["change_wait_summary_ms"] = _summary(change_wait_samples)
    action_to_frame_samples = [
        timing["action_to_frame_ms"]
        for item in observations
        if isinstance((timing := item.get("benchmark_timing_ms")), dict)
        and isinstance(timing.get("action_to_frame_ms"), int | float)
    ]
    if action_to_frame_samples:
        result["action_to_frame_samples_ms"] = action_to_frame_samples
        result["action_to_frame_summary_ms"] = _summary(action_to_frame_samples)
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
