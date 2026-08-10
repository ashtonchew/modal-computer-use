"""Promotion harness for the complete public full-screenshot request.

This module is benchmark-only.  Its boundary is deliberately the literal
``await computer.screenshots.full()`` call on one warm pooled SDK client per
arm.  It does not measure actions, observation streams, deltas, browser
protocols, or patch arrival.  The daemon timing header is captured alongside
the complete SDK latency so the promotion gate can distinguish capture/encode
work from transport and receipt validation.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from modal_computer_use.errors import DaemonHTTPError

_EXPECTED_PAYLOAD = {
    "format": "png",
    "quality": 90,
    "scale": 1.0,
    "show_cursor": False,
    "processing": "auto",
    "storage": "inline",
}
_MINIMUM_SAMPLES_PER_ARM = 100
_MINIMUM_WARMUP_ITERATIONS = 10
_TIMING_HEADER = "x-computer-use-timing-ms"


def _safe_failure_label(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 80:
        return "unknown"
    if not all(character.isalnum() or character in "._-" for character in value):
        return "unknown"
    return value


def _readiness_error_categories(value: object) -> str:
    if not isinstance(value, list):
        return "none"
    categories: list[str] = []
    for item in value:
        if not isinstance(item, str):
            category = "unknown"
        elif "screenshot" in item.lower():
            category = "screenshot"
        elif "input backend" in item.lower() or "xdotool" in item.lower():
            category = "input"
        elif "window" in item.lower():
            category = "windows"
        elif "missing required tools" in item.lower():
            category = "tools"
        elif "xdpyinfo" in item.lower() or "xvfb" in item.lower():
            category = "display_probe"
        elif "display lifecycle" in item.lower():
            category = "lifecycle"
        else:
            category = "unknown"
        if category not in categories:
            categories.append(category)
    return ",".join(categories) if categories else "none"


def _annotate_daemon_failure(
    error: DaemonHTTPError,
    *,
    arm: str,
    phase: str,
    sample_index: int | None,
) -> None:
    error.add_note(
        "screenshot benchmark daemon failure: "
        f"arm={arm} phase={phase} sample_index={sample_index} "
        f"status_code={error.status_code} code={_safe_failure_label(error.code)} "
        "error_type="
        f"{_safe_failure_label(error.details.get('error_type'))} "
        "readiness_errors="
        f"{_readiness_error_categories(error.details.get('errors'))}"
    )


def build_paired_schedule(
    arms: Sequence[str],
    *,
    sample_count: int,
    seed: int,
    order: str = "random",
    minimum_sample_count: int = _MINIMUM_SAMPLES_PER_ARM,
) -> list[dict[str, int | str]]:
    """Return a reproducible two-arm schedule.

    Pairing keeps each arm exposed to the same warm session drift while the
    ``random`` order prevents a fixed arm from always paying first-request or
    scheduler effects; ``alternating`` gives a fixed AB/BA protocol. The
    promotion artifact intentionally supports exactly the MSS baseline and
    X11 shared-memory candidate.
    """

    if len(arms) != 2 or len(set(arms)) != 2:
        raise ValueError("the screenshot promotion schedule requires two distinct arms")
    if (
        isinstance(minimum_sample_count, bool)
        or minimum_sample_count < 1
        or sample_count < minimum_sample_count
    ):
        raise ValueError(f"sample_count must be at least {minimum_sample_count} per arm")
    if isinstance(seed, bool) or seed <= 0:
        raise ValueError("schedule seed must be positive")
    if order not in {"random", "alternating"}:
        raise ValueError("schedule order must be random or alternating")
    rng = random.Random(seed)  # noqa: S311 - deterministic benchmark schedule.
    schedule: list[dict[str, int | str]] = []
    for sample_index in range(sample_count):
        pair = list(arms)
        if order == "random":
            rng.shuffle(pair)
        elif sample_index % 2:
            pair.reverse()
        for position, arm in enumerate(pair):
            schedule.append(
                {
                    "sequence": len(schedule),
                    "sample_index": sample_index,
                    "position": position,
                    "arm": arm,
                }
            )
    return schedule


def build_paired_random_schedule(
    arms: Sequence[str],
    *,
    sample_count: int,
    seed: int,
    order: str = "random",
    minimum_sample_count: int = _MINIMUM_SAMPLES_PER_ARM,
) -> list[dict[str, int | str]]:
    """Compatibility alias for :func:`build_paired_schedule`."""

    return build_paired_schedule(
        arms,
        sample_count=sample_count,
        seed=seed,
        order=order,
        minimum_sample_count=minimum_sample_count,
    )


async def measure_full_screenshot_arms(
    borrow_for_arm: Mapping[
        str, Callable[[], AbstractAsyncContextManager[Any]]
    ],
    *,
    sample_count: int = _MINIMUM_SAMPLES_PER_ARM,
    warmup_iterations: int = _MINIMUM_WARMUP_ITERATIONS,
    schedule_seed: int = 20260808,
    schedule_order: str = "random",
    retain_partial_evidence: bool = False,
    decode_parity: Callable[[bytes], bool] | None = None,
    expected_capture_backends: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Measure complete public SDK screenshot calls in a paired schedule.

    ``borrow_for_arm`` is entered once per arm.  Each entered object must be a
    public SDK computer exposing ``screenshots`` and its pooled ``client``. The
    wrapper around ``post_bytes_with_headers`` is observational only; the
    namespace still executes the exact public call and performs normal SDK
    response validation.
    """

    if len(borrow_for_arm) != 2:
        raise ValueError("exactly two matched screenshot arms are required")
    if isinstance(warmup_iterations, bool) or warmup_iterations < _MINIMUM_WARMUP_ITERATIONS:
        raise ValueError("warmup_iterations must be at least 10 warmup iterations")
    if decode_parity is None:
        raise ValueError("decode_parity callback is required for independent pixel parity")
    arms = tuple(borrow_for_arm)
    schedule = build_paired_schedule(
        arms, sample_count=sample_count, seed=schedule_seed, order=schedule_order
    )
    warmup_schedule = build_paired_schedule(
        arms,
        sample_count=warmup_iterations,
        seed=schedule_seed,
        order=schedule_order,
        minimum_sample_count=1,
    )
    observations: dict[str, list[dict[str, Any]]] = {arm: [] for arm in arms}
    traces: dict[str, list[dict[str, Any]]] = {arm: [] for arm in arms}
    fallback_counts: dict[str, int] = {arm: 0 for arm in arms}
    warmup_completed: dict[str, int] = {arm: 0 for arm in arms}
    contexts = {arm: borrow_for_arm[arm]() for arm in arms}
    entered: dict[str, Any] = {}
    cleanup_errors: list[dict[str, str]] = []
    reference_metadata: tuple[Any, ...] | None = None
    failure: dict[str, Any] | None = None
    phase = "enter"
    current_arm: str | None = None
    current_sample_index: int | None = None
    try:
        for arm in arms:
            current_arm = arm
            entered[arm] = await contexts[arm].__aenter__()
            _require_public_client(entered[arm])

        # Warm every session before the paired schedule using the same fixed
        # AB/BA protocol as measured calls. Warmups are excluded from
        # percentiles but completed counts are retained for auditability.
        phase = "warmup"
        for item in warmup_schedule:
            arm = str(item["arm"])
            current_arm = arm
            current_sample_index = int(item["sample_index"])
            try:
                await entered[arm].screenshots.full()
            except DaemonHTTPError as error:
                _annotate_daemon_failure(
                    error,
                    arm=arm,
                    phase="warmup",
                    sample_index=current_sample_index,
                )
                raise
            warmup_completed[arm] += 1

        phase = "sample"
        for item in schedule:
            arm = str(item["arm"])
            current_arm = arm
            current_sample_index = int(item["sample_index"])
            computer = entered[arm]
            trace: dict[str, Any] = {}
            original = computer.client.post_bytes_with_headers

            async def traced_request(
                *args: Any,
                _original=original,
                _trace=trace,
                **kwargs: Any,
            ) -> Any:
                request_started = clock()
                data, headers = await _original(*args, **kwargs)
                _trace.update(
                    {
                        "path": args[0] if args else kwargs.get("path"),
                        "request_json": kwargs.get("json"),
                        "transport_ms": max(0.0, (clock() - request_started) * 1000.0),
                        "response_headers": _safe_headers(headers),
                    }
                )
                return data, headers

            computer.client.post_bytes_with_headers = traced_request
            started = clock()
            try:
                try:
                    screenshot = await computer.screenshots.full()
                except DaemonHTTPError as error:
                    _annotate_daemon_failure(
                        error,
                        arm=arm,
                        phase="sample",
                        sample_index=int(item["sample_index"]),
                    )
                    raise
            finally:
                computer.client.post_bytes_with_headers = original
            complete_ms = max(0.0, (clock() - started) * 1000.0)
            _validate_sample(screenshot, trace)

            headers = trace["response_headers"]
            assert isinstance(headers, Mapping)
            capture_backend = headers.get("x-computer-use-capture-backend")
            if expected_capture_backends is not None:
                expected = expected_capture_backends.get(arm)
                if expected is None:
                    raise AssertionError(f"no expected capture backend registered for {arm}")
                if capture_backend != expected:
                    raise AssertionError(
                        f"{arm} used capture backend {capture_backend!r}; expected {expected!r}"
                    )
            if isinstance(capture_backend, str) and "fallback" in capture_backend:
                fallback_counts[arm] += 1

            decoded_pixel_parity = bool(decode_parity(screenshot.as_bytes()))
            if not decoded_pixel_parity:
                raise AssertionError("independent decoded-pixel parity failed")
            metadata = _screenshot_metadata(screenshot)
            metadata_parity = reference_metadata is None or metadata == reference_metadata
            if reference_metadata is None:
                reference_metadata = metadata
            if not metadata_parity:
                raise AssertionError("screenshot metadata parity failed")

            timings = _timing_metrics(headers)
            observations[arm].append(
                {
                    "sample_index": int(item["sample_index"]),
                    "status": "ok",
                    "complete_sdk_ms": round(complete_ms, 4),
                    "daemon_total_ms": timings["total_ms"],
                    "capture_ms": timings["capture_ms"],
                    "encode_ms": timings["encode_ms"],
                    "x11_shm_capture_encode_ms": timings[
                        "x11_shm_capture_encode_ms"
                    ],
                    "hash_ms": timings["hash_ms"],
                    "payload_bytes": screenshot.size_bytes,
                    "decoded_pixel_parity": decoded_pixel_parity,
                    "metadata_parity": metadata_parity,
                    "capture_backend": capture_backend,
                }
            )
            traces[arm].append(trace)
    except Exception as exc:
        if not retain_partial_evidence:
            raise
        failure = {
            "phase": phase,
            "exception_type": type(exc).__name__,
            "arm": current_arm,
            "sample_index": current_sample_index,
        }
    finally:
        for arm in reversed(arms):
            if arm not in entered:
                continue
            try:
                await contexts[arm].__aexit__(None, None, None)
            except Exception as exc:
                cleanup_errors.append({"arm": arm, "exception_type": type(exc).__name__})
        if cleanup_errors and not retain_partial_evidence:
            raise RuntimeError("screenshot benchmark cleanup failed")

    result = {
        "benchmark": "x11-shm-screenshot-promotion",
        "public_call": "await computer.screenshots.full()",
        "payload": dict(_EXPECTED_PAYLOAD),
        "sample_count_per_arm": sample_count,
        "warmup_iterations": warmup_iterations,
        "warmup_schedule": warmup_schedule,
        "warmup_completed_per_arm": warmup_completed,
        "schedule_seed": schedule_seed,
        "schedule_order": schedule_order,
        "schedule": schedule,
        "arms": {
            arm: {
                "requested_source": arm,
                "expected_backend": (
                    expected_capture_backends.get(arm, arm)
                    if expected_capture_backends is not None
                    else arm
                ),
                "observations": observations[arm],
                "transport_traces": traces[arm],
            }
            for arm in arms
        },
        "cleanup": {"errors": cleanup_errors, "succeeded": not cleanup_errors},
        "fallback_counts": fallback_counts,
    }
    if failure is not None:
        result["status"] = "rejected"
        result["failure"] = failure
    elif cleanup_errors:
        result["status"] = "rejected"
        result["failure"] = {
            "phase": "cleanup",
            "exception_type": "CleanupError",
            "arm": None,
            "sample_index": None,
        }
    else:
        result["status"] = "complete"
    return result


def _require_public_client(computer: Any) -> None:
    if not hasattr(computer, "screenshots") or not hasattr(computer, "client"):
        raise TypeError("borrowed object must expose screenshots and client")


def _validate_sample(screenshot: Any, trace: Mapping[str, Any]) -> None:
    """Reject any sample that is not the canonical full PNG contract."""

    if trace.get("path") != "/v1/screenshots/full/raw":
        raise AssertionError("public full screenshot used an unexpected route")
    if trace.get("request_json") != _EXPECTED_PAYLOAD:
        raise AssertionError("public full screenshot payload changed")
    if getattr(screenshot, "format", None) != "png":
        raise AssertionError("full screenshot format is not PNG")
    if screenshot.width != 1024 or screenshot.height != 768:
        raise AssertionError("full screenshot dimensions are not 1024x768")
    if screenshot.cursor_visible:
        raise AssertionError("full screenshot cursor must be hidden")
    headers = trace.get("response_headers")
    if not isinstance(headers, Mapping):
        raise AssertionError("full screenshot response headers were not captured")
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "image/png":
        raise AssertionError("full screenshot content type is not image/png")
    for key, expected in (("x-computer-use-width", 1024), ("x-computer-use-height", 768)):
        try:
            actual = int(headers.get(key, -1))
        except (TypeError, ValueError) as exc:
            raise AssertionError(f"full screenshot {key} mismatch") from exc
        if actual != expected:
            raise AssertionError(f"full screenshot {key} mismatch")
    try:
        payload_size = int(headers.get("x-computer-use-size-bytes", -1))
    except (TypeError, ValueError) as exc:
        raise AssertionError("full screenshot payload size is invalid") from exc
    if payload_size != screenshot.size_bytes or payload_size != len(screenshot.as_bytes()):
        raise AssertionError("full screenshot payload size mismatch")
    header_sha = headers.get("x-computer-use-sha256")
    computed_sha = hashlib.sha256(screenshot.as_bytes()).hexdigest()
    if header_sha != computed_sha or screenshot.sha256 != computed_sha:
        raise AssertionError("full screenshot SHA-256 mismatch")
    try:
        cursor_position = json.loads(headers.get("x-computer-use-cursor-position"))
        cursor_x = int(cursor_position["x"])
        cursor_y = int(cursor_position["y"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AssertionError("full screenshot cursor position is invalid") from exc
    if not (0 <= cursor_x < 1024 and 0 <= cursor_y < 768):
        raise AssertionError("full screenshot cursor position is out of bounds")
    _timing_metrics(headers)


def _timing_metrics(headers: Mapping[str, Any]) -> dict[str, float | None]:
    timing_header = headers.get(_TIMING_HEADER)
    try:
        timings = json.loads(timing_header)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AssertionError("full screenshot timing header is invalid") from exc
    if not isinstance(timings, dict):
        raise AssertionError("full screenshot timing header is invalid")
    for key in ("hash_ms", "total_ms"):
        value = timings.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise AssertionError(f"full screenshot timing header missing valid {key}")
    stages: dict[str, float | None] = {}
    for key in ("capture_ms", "encode_ms", "x11_shm_capture_encode_ms"):
        value = timings.get(key)
        if value is None:
            stages[key] = None
        elif (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise AssertionError(f"full screenshot timing header has invalid {key}")
        else:
            stages[key] = float(value)
    if stages["x11_shm_capture_encode_ms"] is None:
        if stages["capture_ms"] is None or stages["encode_ms"] is None:
            raise AssertionError("full screenshot timing header is missing capture stages")
    elif stages["capture_ms"] is not None or stages["encode_ms"] is not None:
        raise AssertionError("full screenshot timing header mixed fused and split stages")
    return {
        "hash_ms": float(timings["hash_ms"]),
        "total_ms": float(timings["total_ms"]),
        **stages,
    }


def _screenshot_metadata(screenshot: Any) -> tuple[Any, ...]:
    coordinate_space = screenshot.coordinate_space
    model_dump = getattr(coordinate_space, "model_dump", None)
    coordinate = model_dump(mode="json") if callable(model_dump) else coordinate_space
    return (
        getattr(screenshot, "format", None),
        screenshot.width,
        screenshot.height,
        screenshot.cursor_visible,
        (screenshot.cursor_position.x, screenshot.cursor_position.y),
        coordinate,
    )


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str | int | float | None]:
    """Keep contract/timing metadata while excluding body/token headers."""

    names = (
        "content-type",
        "x-computer-use-width",
        "x-computer-use-height",
        "x-computer-use-size-bytes",
        "x-computer-use-sha256",
        _TIMING_HEADER,
        "x-computer-use-capture-backend",
        "x-computer-use-cursor-visible",
        "x-computer-use-cursor-position",
    )
    result: dict[str, str | int | float | None] = {}
    for name in names:
        value = next((value for key, value in headers.items() if key.lower() == name), None)
        result[name] = value
    return result


def write_artifact(result: Mapping[str, Any], path: Path) -> None:
    """Write JSON without screenshot bytes or bearer metadata."""

    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(
        "Import measure_full_screenshot_arms from a Modal benchmark runner; "
        "no default borrow factory is safe."
    )
