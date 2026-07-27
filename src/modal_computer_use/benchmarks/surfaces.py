from __future__ import annotations

from typing import Any, Literal

from .._version import __version__
from ..client import DaemonClient
from ..hot_session import HotSessionClient
from ..transports import HotSessionTransport
from . import core
from .adapter_surface import _run_adapter_surface
from .constants import (
    DEFAULT_SDK_BENCHMARK_SURFACES,
    TYPING_BENCHMARK_DELAY_MS,
    TYPING_BENCHMARK_METHOD,
    BenchmarkSurface,
)
from .daemon_surface import _run_daemon_http_surface
from .hot_session_surface import _run_daemon_hot_session_surface
from .observation_surface import _run_daemon_observation_surface
from .surface_result import _surface_not_measured, _surface_result
from .transport_floor_surface import _run_daemon_transport_floor_surface

SurfaceBenchmarkMode = Literal["mock-local", "http"]


def run_sdk_surface_benchmark(
    *,
    surfaces: list[BenchmarkSurface] | None = None,
    iterations: int,
    client: DaemonClient | None = None,
    mode: SurfaceBenchmarkMode = "mock-local",
    base_url: str | None = None,
    warmup_iterations: int = 1,
    sandbox_exec_runner: Any = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
    observation_cases: list[str] | None = None,
    typing_method: str = TYPING_BENCHMARK_METHOD,
    typing_delay_ms: int = TYPING_BENCHMARK_DELAY_MS,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    selected = surfaces or list(DEFAULT_SDK_BENCHMARK_SURFACES)
    failures: list[dict[str, Any]] = []
    surface_results: dict[str, Any] = {}
    for surface in selected:
        try:
            result = _run_surface(
                surface,
                client=client,
                mode=mode,
                base_url=base_url,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                sandbox_exec_runner=sandbox_exec_runner,
                sandbox_exec_setup_failure=sandbox_exec_setup_failure,
                environment_metadata=environment_metadata,
                observation_cases=observation_cases,
                typing_method=typing_method,
                typing_delay_ms=typing_delay_ms,
            )
        except Exception as exc:
            result = _surface_result(
                surface,
                cases={
                    "setup": core._case_result(
                        "setup",
                        iterations,
                        [],
                        [
                            core._failure(
                                "setup",
                                phase="setup",
                                iteration=0,
                                exc=exc,
                                redacted_text=core.ADAPTER_BENCHMARK_TEXT,
                            )
                        ],
                    )
                },
            )
        surface_results[surface] = result
        failures.extend(core._benchmark_failures(surface, result.get("failures", [])))
    return {
        "ok": not failures,
        "benchmark": "sdk-surfaces",
        "generated_at": core.datetime.now(core.UTC).isoformat(),
        "package_version": __version__,
        "mode": mode,
        "base_url": core._safe_base_url(base_url),
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "metadata": {
            "environment": {
                key: value
                for key, value in (environment_metadata or {}).items()
                if value is not None
            },
            "surfaces": selected,
        },
        "surfaces": surface_results,
        "failures": failures,
    }

def run_sdk_surface_benchmark_mock_local(
    *,
    surfaces: list[BenchmarkSurface] | None = None,
    iterations: int,
    warmup_iterations: int = 1,
    sandbox_exec_runner: Any = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
    observation_cases: list[str] | None = None,
    typing_method: str = TYPING_BENCHMARK_METHOD,
    typing_delay_ms: int = TYPING_BENCHMARK_DELAY_MS,
) -> dict[str, Any]:
    return core._with_mock_local_client(
        lambda client: run_sdk_surface_benchmark(
            surfaces=surfaces,
            client=client,
            mode="mock-local",
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=environment_metadata,
            observation_cases=observation_cases,
            typing_method=typing_method,
            typing_delay_ms=typing_delay_ms,
        )
    )

def _run_surface(
    surface: BenchmarkSurface,
    *,
    client: DaemonClient | None,
    mode: SurfaceBenchmarkMode,
    base_url: str | None,
    iterations: int,
    warmup_iterations: int,
    sandbox_exec_runner: Any,
    sandbox_exec_setup_failure: dict[str, Any] | None,
    environment_metadata: dict[str, Any] | None,
    observation_cases: list[str] | None,
    typing_method: str,
    typing_delay_ms: int,
) -> dict[str, Any]:
    if surface == "daemon-http":
        return _run_daemon_http_surface(
            client=client,
            mode=mode,
            base_url=base_url,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            environment_metadata=environment_metadata,
            typing_method=typing_method,
            typing_delay_ms=typing_delay_ms,
        )
    if surface == "daemon-hot-session":
        if client is None or mode == "mock-local":
            return _surface_not_measured(
                "modal-daemon-hot-session",
                "daemon hot-session benchmark requires a reachable daemon websocket URL",
            )
        hot_session = HotSessionClient(
            HotSessionTransport(
                client.base_url,
                token=client.transport.token,
            )
        )
        try:
            return _run_daemon_hot_session_surface(
                hot_session=hot_session,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                environment_metadata=environment_metadata,
            )
        finally:
            hot_session.close()
    if surface == "daemon-observation-stream":
        if client is None or mode == "mock-local":
            return _surface_not_measured(
                "modal-daemon-observation-stream",
                "daemon observation stream benchmark requires a reachable daemon websocket URL",
            )
        return _run_daemon_observation_surface(
            base_url=client.base_url,
            token=client.transport.token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            environment_metadata=environment_metadata,
            observation_cases=observation_cases,
        )
    if surface == "daemon-transport-floor":
        if client is None or mode == "mock-local":
            return _surface_not_measured(
                "modal-daemon-transport-floor",
                "daemon transport floor benchmark requires a reachable daemon URL",
            )
        return _run_daemon_transport_floor_surface(
            base_url=client.base_url,
            token=client.transport.token,
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            environment_metadata=environment_metadata,
        )
    if surface == "sandbox-exec":
        return _surface_result(
            surface,
            cases={
                "sandbox_exec_move_click": core.run_sandbox_exec_benchmark(
                    iterations=iterations,
                    warmup_iterations=warmup_iterations,
                    runner=sandbox_exec_runner,
                    setup_failure=sandbox_exec_setup_failure,
                )
            },
            metadata={"transport": "Modal Sandbox.exec"},
            runtime_seconds=None,
        )
    if surface in {"openai-adapter", "anthropic-adapter", "action-executor"}:
        return _run_adapter_surface(
            surface=surface,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        )
    return _surface_not_measured(str(surface), "unknown benchmark surface")
