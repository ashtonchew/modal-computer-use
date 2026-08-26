from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..client import DaemonClient
from .common import BenchmarkMode, BenchmarkSurface, _with_mock_local_client


def run_sdk_surface_benchmark(
    *,
    surfaces: list[BenchmarkSurface] | None = None,
    iterations: int,
    client: DaemonClient | None = None,
    mode: BenchmarkMode = "http",
    base_url: str | None = None,
    warmup_iterations: int = 1,
    sandbox_exec_runner: Callable[[tuple[str, ...], int], object] | None = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
    billing_reconciliation_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .surfaces import run_sdk_surface_benchmark as run_surfaces

    return run_surfaces(
        surfaces=surfaces,
        iterations=iterations,
        client=client,
        mode=mode,
        base_url=base_url,
        warmup_iterations=warmup_iterations,
        sandbox_exec_runner=sandbox_exec_runner,
        sandbox_exec_setup_failure=sandbox_exec_setup_failure,
        environment_metadata=environment_metadata,
        billing_reconciliation_request=billing_reconciliation_request,
    )

def run_sdk_surface_benchmark_mock_local(
    *,
    surfaces: list[BenchmarkSurface] | None = None,
    iterations: int,
    sandbox_exec_runner: Callable[[tuple[str, ...], int], object] | None = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
    billing_reconciliation_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _with_mock_local_client(
        lambda client: run_sdk_surface_benchmark(
            surfaces=surfaces,
            client=client,
            mode="mock-local",
            iterations=iterations,
            base_url="http://testserver",
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=environment_metadata,
            billing_reconciliation_request=billing_reconciliation_request,
        )
    )
