from __future__ import annotations

import time
from shlex import quote as shell_quote
from typing import Any
from urllib.parse import quote

from ..client import DaemonClient
from ..observations import ObservationClient
from ..transports import ObservationStreamTransport
from .measurement import _case_result, _measure_observed_case, _summary
from .surface_result import _surface_result

OBSERVATION_SCREENSHOT_OPTIONS = {"format": "png", "show_cursor": False}


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
