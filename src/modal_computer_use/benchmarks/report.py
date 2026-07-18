from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .._version import __version__
from ..client import DaemonClient
from .action_batch import run_action_batch_benchmark
from .constants import BenchmarkMode
from .hot_paths import (
    run_move_click_benchmark,
    run_move_click_sequence_benchmark,
    run_recording_start_stop_benchmark,
    run_screenshot_benchmark,
    run_type_100_chars_benchmark,
    run_type_1000_chars_benchmark,
)
from .measurement import _named_case_comparison
from .metadata import (
    _benchmark_failures,
    _collect_metadata,
    _future_benchmark,
    _report_action_batch,
)
from .mock_local import _with_mock_local_client
from .safety import _safe_base_url
from .sandbox_exec import run_sandbox_exec_benchmark


def run_benchmark_report(
    *,
    client: DaemonClient,
    mode: BenchmarkMode,
    iterations: int,
    base_url: str | None = None,
    warmup_iterations: int = 1,
    include_sandbox_exec: bool = False,
    sandbox_exec_runner: Callable[[tuple[str, ...], int], object] | None = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    metadata = _collect_metadata(client, failures)
    if environment_metadata:
        metadata["environment"] = {
            key: value for key, value in environment_metadata.items() if value is not None
        }
    action_batch = run_action_batch_benchmark(
        client=client,
        mode=mode,
        iterations=iterations,
        base_url=base_url,
        warmup_iterations=warmup_iterations,
    )
    screenshot_full = run_screenshot_benchmark(
        client=client,
        name="screenshot_full",
        request={"format": "png", "show_cursor": False},
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        raw=True,
    )
    screenshot_full_structured = run_screenshot_benchmark(
        client=client,
        name="screenshot_full_structured",
        request={"format": "png", "storage": "inline", "show_cursor": False},
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    screenshot_compressed = run_screenshot_benchmark(
        client=client,
        name="screenshot_compressed",
        request={
            "format": "jpeg",
            "quality": 60,
            "scale": 0.5,
            "storage": "inline",
            "show_cursor": False,
        },
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    move_click = run_move_click_benchmark(
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    move_click_sequence = run_move_click_sequence_benchmark(
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    type_100_chars = run_type_100_chars_benchmark(
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    type_1000_chars = run_type_1000_chars_benchmark(
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    recording_start_stop = run_recording_start_stop_benchmark(
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    if include_sandbox_exec:
        sandbox_exec = run_sandbox_exec_benchmark(
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            runner=sandbox_exec_runner,
            setup_failure=sandbox_exec_setup_failure,
        )
    else:
        sandbox_exec = _future_benchmark(
            "not_measured",
            "Sandbox.exec comparison requires explicit --include-sandbox-exec live mode",
        )
    benchmarks = {
        "action_batch": _report_action_batch(action_batch),
        "screenshot_full": screenshot_full,
        "screenshot_full_structured": screenshot_full_structured,
        "screenshot_compressed": screenshot_compressed,
        "move_click": move_click,
        "move_click_sequence": move_click_sequence,
        "type_100_chars": type_100_chars,
        "type_1000_chars": type_1000_chars,
        "recording_start_stop": recording_start_stop,
        "sandbox_exec": sandbox_exec,
        "product_create_to_first_screenshot": _future_benchmark(
            "not_measured",
            "cold Modal Sandbox creation is outside mock-local and live-daemon benchmark modes",
        ),
        "cold_create_to_ready": {
            **_future_benchmark(
                "not_measured",
                "cold Modal Sandbox creation is outside mock-local and live-daemon benchmark modes",
            ),
            "canonical_case": "product_create_to_first_screenshot",
            "deprecated": True,
            "removal_version": "1.2.0",
        },
        "warm_attach_to_health": _future_benchmark(
            "not_measured",
            "warm attach requires Modal orchestration and is outside this report mode",
        ),
    }
    failures.extend(_benchmark_failures("action_batch", action_batch.get("failures", [])))
    failures.extend(_benchmark_failures("screenshot_full", screenshot_full.get("failures", [])))
    failures.extend(
        _benchmark_failures(
            "screenshot_full_structured",
            screenshot_full_structured.get("failures", []),
        )
    )
    failures.extend(
        _benchmark_failures("screenshot_compressed", screenshot_compressed.get("failures", []))
    )
    failures.extend(_benchmark_failures("move_click", move_click.get("failures", [])))
    failures.extend(
        _benchmark_failures("move_click_sequence", move_click_sequence.get("failures", []))
    )
    failures.extend(_benchmark_failures("type_100_chars", type_100_chars.get("failures", [])))
    failures.extend(_benchmark_failures("type_1000_chars", type_1000_chars.get("failures", [])))
    failures.extend(
        _benchmark_failures("recording_start_stop", recording_start_stop.get("failures", []))
    )
    if include_sandbox_exec:
        failures.extend(_benchmark_failures("sandbox_exec", sandbox_exec.get("failures", [])))
        if sandbox_exec.get("status") in {"ok", "failed"}:
            sandbox_exec["comparison"] = _named_case_comparison(
                "daemon_move_click",
                move_click,
                "sandbox_exec_move_click",
                sandbox_exec,
            )
    ok = not failures
    return {
        "ok": ok,
        "generated_at": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "mode": mode,
        "base_url": _safe_base_url(base_url),
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "metadata": metadata,
        "benchmarks": benchmarks,
        "failures": failures,
    }

def run_benchmark_report_mock_local(*, iterations: int) -> dict[str, Any]:
    return _with_mock_local_client(
        lambda client: run_benchmark_report(
            client=client,
            mode="mock-local",
            iterations=iterations,
            base_url="http://testserver",
        )
    )
