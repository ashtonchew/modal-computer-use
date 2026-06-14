from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Callable
from contextlib import suppress
from importlib import metadata as importlib_metadata
from typing import Any, Literal

from . import benchmarks as core
from ._version import __version__
from .adapters.anthropic import AnthropicAdapter
from .adapters.generic import ActionExecutor
from .adapters.openai import OpenAIAdapter
from .benchmark_costs import estimate_provider_cost
from .benchmarks import ComparisonProvider
from .benchmarks.surfaces import run_sdk_surface_benchmark
from .client import DaemonClient
from .models import ActionBatchResult, ActionItemResult

ProviderMode = Literal["mock-local", "http", "provider-live"]
TYPE_READBACK_TEXT = "mcu-readback-0123456789"
TYPE_READBACK_FILE = "/tmp/modal-computer-use-type-readback-xev.log"  # noqa: S108
TYPE_READBACK_PID_FILE = "/tmp/modal-computer-use-type-readback-xev.pid"  # noqa: S108
TYPE_READBACK_TITLE = "mcu-type-readback"
TYPE_READBACK_FOCUS_X = 40
TYPE_READBACK_FOCUS_Y = 60


def run_provider_comparison(
    *,
    providers: list[ComparisonProvider] | None = None,
    iterations: int,
    client: DaemonClient | None = None,
    mode: ProviderMode = "provider-live",
    base_url: str | None = None,
    warmup_iterations: int = 1,
    sandbox_exec_runner: Any = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    selected = providers or list(core.DEFAULT_COMPARE_PROVIDERS)
    failures: list[dict[str, Any]] = []
    provider_results: dict[str, Any] = {}
    for provider in selected:
        try:
            result = _run_provider(
                provider,
                client=client,
                mode=mode,
                base_url=base_url,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                sandbox_exec_runner=sandbox_exec_runner,
                sandbox_exec_setup_failure=sandbox_exec_setup_failure,
                environment_metadata=environment_metadata,
            )
        except Exception as exc:
            result = _provider_result(
                provider,
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
                                redacted_text=core.PROVIDER_BENCHMARK_TEXT,
                            )
                        ],
                    )
                },
            )
        provider_results[provider] = result
        failures.extend(core._benchmark_failures(provider, result.get("failures", [])))
    return {
        "ok": not failures,
        "benchmark": "provider-compare",
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
            "providers": selected,
        },
        "providers": provider_results,
        "failures": failures,
    }


def _run_provider(
    provider: ComparisonProvider,
    *,
    client: DaemonClient | None,
    mode: ProviderMode,
    base_url: str | None,
    iterations: int,
    warmup_iterations: int,
    sandbox_exec_runner: Any,
    sandbox_exec_setup_failure: dict[str, Any] | None,
    environment_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if provider == "modal-daemon":
        return _run_modal_daemon_provider(
            client=client,
            mode=mode,
            base_url=base_url,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            environment_metadata=environment_metadata,
        )
    if provider == "modal-exec":
        surface_payload = run_sdk_surface_benchmark(
            surfaces=["sandbox-exec"],
            mode="mock-local" if mode == "mock-local" else "http",
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=environment_metadata,
        )
        return _provider_from_surface("modal-exec", surface_payload["surfaces"]["sandbox-exec"])
    if provider in {"openai", "anthropic", "generic"}:
        return _run_adapter_provider(
            provider=provider,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        )
    if provider == "daytona":
        if mode == "mock-local":
            return _provider_not_measured(
                provider,
                "live Daytona benchmarks are disabled in mock-local mode",
            )
        return _run_daytona_provider(iterations=iterations, warmup_iterations=warmup_iterations)
    if provider == "e2b":
        if mode == "mock-local":
            return _provider_not_measured(
                provider,
                "live E2B benchmarks are disabled in mock-local mode",
            )
        return _run_e2b_provider(iterations=iterations, warmup_iterations=warmup_iterations)
    return _provider_not_measured(str(provider), "unknown provider")


def _run_modal_daemon_provider(
    *,
    client: DaemonClient | None,
    mode: str,
    base_url: str | None,
    iterations: int,
    warmup_iterations: int,
    environment_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if client is None:
        return _provider_not_measured(
            "modal-daemon",
            "modal-daemon comparison requires --mock-local or --base-url",
        )
    surface_payload = run_sdk_surface_benchmark(
        surfaces=["daemon-http"],
        client=client,
        mode="mock-local" if mode == "mock-local" else "http",
        iterations=iterations,
        base_url=base_url,
        warmup_iterations=warmup_iterations,
        environment_metadata=environment_metadata,
    )
    surface = surface_payload["surfaces"]["daemon-http"]
    return _provider_from_surface("modal-daemon", surface)


def _provider_from_surface(provider: str, surface: dict[str, Any]) -> dict[str, Any]:
    result = {
        "status": surface.get("status", "not_measured"),
        "provider": provider,
        "metadata": {
            **_dict_value(surface.get("metadata")),
            "canonical_source": "benchmark sdk surface",
            "provider_surface": surface.get("surface"),
        },
        "cases": _dict_value(surface.get("cases")),
        "failures": list(surface.get("failures", [])),
    }
    for key in ("verification", "billing_reconciliation", "cost_estimate"):
        if key in surface:
            result[key] = surface[key]
    return result


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _run_adapter_provider(
    *,
    provider: ComparisonProvider,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    benchmark = _AdapterProviderBenchmark(provider)
    samples, observations = core._measure_observed_case(
        name=f"{provider}_adapter_matrix",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run,
        failures=failures,
        redacted_text=core.PROVIDER_BENCHMARK_TEXT,
    )
    case = core._case_result(f"{provider}_adapter_matrix", iterations, samples, failures)
    case.update(
        {
            "actions": observations[-1]["actions"] if observations else benchmark.safe_actions,
            "action_count": len(benchmark.safe_actions),
            "last_result": observations[-1] if observations else None,
        }
    )
    return _provider_result(
        provider,
        cases={"adapter_matrix": case},
        metadata=benchmark.metadata,
        runtime_seconds=None,
    )


class _AdapterProviderBenchmark:
    def __init__(self, provider: ComparisonProvider) -> None:
        self.provider = provider
        self.metadata = self._metadata()
        self.safe_actions = [core._safe_action_metadata(action) for action in self._actions()]

    def run(self) -> dict[str, Any]:
        computer = _BenchmarkRecordingComputer()
        actions = self._actions()
        start = time.perf_counter()
        if self.provider == "openai":
            OpenAIAdapter(computer).apply_many(actions)
        elif self.provider == "anthropic":
            AnthropicAdapter(computer, tool_version="computer_20250124").apply_many(actions)
        elif self.provider == "generic":
            ActionExecutor(computer).apply_many(actions)
        else:
            raise RuntimeError(f"unsupported adapter provider: {self.provider}")
        elapsed_ms = (time.perf_counter() - start) * 1000
        run = computer.actions.runs[-1]
        return {
            "elapsed_ms": elapsed_ms,
            "source": run["source"],
            "actions": [core._safe_action_metadata(action) for action in run["actions"]],
        }

    def _actions(self) -> list[dict[str, Any]]:
        if self.provider == "openai":
            return [
                {"type": "move", "x": 10, "y": 20},
                {"type": "click", "x": 10, "y": 20, "button": "left"},
                {"type": "type", "text": core.PROVIDER_BENCHMARK_TEXT},
                {"type": "wait", "duration_ms": 0},
            ]
        if self.provider == "anthropic":
            return [
                {"action": "mouse_move", "coordinate": [10, 20]},
                {"action": "left_click", "coordinate": [10, 20]},
                {"action": "type", "text": core.PROVIDER_BENCHMARK_TEXT},
                {"action": "wait", "duration_ms": 0},
            ]
        return [
            {"type": "move", "x": 10, "y": 20},
            {"type": "click", "x": 10, "y": 20, "button": "left"},
            {"type": "type", "text": core.PROVIDER_BENCHMARK_TEXT},
            {"type": "wait", "duration_ms": 0},
        ]

    def _metadata(self) -> dict[str, Any]:
        if self.provider == "anthropic":
            return {
                "adapter": "AnthropicAdapter",
                "tool_version": "computer_20250124",
                "provider_api_calls": False,
            }
        if self.provider == "openai":
            return {"adapter": "OpenAIAdapter", "provider_api_calls": False}
        return {"adapter": "ActionExecutor", "provider_api_calls": False}


class _BenchmarkRecordingActions:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    def run(
        self,
        actions: list[Any],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        source: str = "sdk",
    ) -> ActionBatchResult:
        dumped = [action.model_dump(mode="json") for action in actions]
        self.runs.append(
            {
                "actions": dumped,
                "continue_on_error": continue_on_error,
                "screenshot_after": screenshot_after,
                "source": source,
            }
        )
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(index=index, type=action["type"], ok=True)
                for index, action in enumerate(dumped)
            ],
        )

    def apply(self, action: Any) -> Any:
        return self.run([action]).results[0]


class _BenchmarkRecordingComputer:
    def __init__(self) -> None:
        self.actions = _BenchmarkRecordingActions()


def _run_daytona_provider(*, iterations: int, warmup_iterations: int) -> dict[str, Any]:
    provider = "daytona"
    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        return _provider_not_measured(provider, "DAYTONA_API_KEY is not set")
    try:
        daytona_module = _import_provider_module("daytona", "Daytona")
    except ImportError:
        return _provider_unavailable(
            provider,
            "install the bench-daytona extra to run Daytona benchmarks",
        )

    snapshot = os.environ.get("DAYTONA_SNAPSHOT")
    metadata = {
        "sdk_package": "daytona",
        "sdk_version": _package_version("daytona"),
        "target": _safe_provider_metadata_value(os.environ.get("DAYTONA_TARGET")),
        "api_url": core._safe_base_url(os.environ.get("DAYTONA_API_URL")),
        "sandbox_source": "snapshot" if snapshot else "default_snapshot",
        "snapshot": _safe_provider_metadata_value(snapshot),
        "startup_model": "managed_sandbox_snapshot" if snapshot else "managed_default_snapshot",
        "uses_snapshot_or_template": True,
        "readiness_contract": (
            "daytona.create -> computer_use.start -> first non-empty full-screen screenshot"
        ),
        "setup_included": True,
        "ingress_included": False,
        "first_observation_api": "computer_use.screenshot.take_full_screen",
    }
    if not snapshot:
        metadata.update(_daytona_default_resource_metadata())
    benchmark = _DaytonaLiveBenchmark(
        daytona_module,
        api_key=api_key,
        api_url=os.environ.get("DAYTONA_API_URL"),
        target=os.environ.get("DAYTONA_TARGET"),
        snapshot=snapshot,
    )
    return _run_live_provider_cases(
        provider=provider,
        benchmark=benchmark,
        cold_cases=("cold_create_to_ready",),
        warm_cases=(
            "screenshot_full",
            "move_click",
            "move_click_sequence",
            "type_100_chars",
            "type_1000_chars",
            "command_echo",
        ),
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        metadata=metadata,
    )


def _run_e2b_provider(*, iterations: int, warmup_iterations: int) -> dict[str, Any]:
    provider = "e2b"
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        return _provider_not_measured(provider, "E2B_API_KEY is not set")
    try:
        e2b_module = _import_provider_module("e2b_desktop", "Sandbox")
    except ImportError:
        return _provider_unavailable(provider, "install the bench-e2b extra to run E2B benchmarks")

    template = os.environ.get("E2B_TEMPLATE")
    metadata = {
        "sdk_package": "e2b-desktop",
        "sdk_version": _package_version("e2b-desktop"),
        "template": _safe_provider_metadata_value(template),
        "template_source": "explicit" if template else "default_desktop",
        "startup_model": "desktop_template_snapshot",
        "uses_snapshot_or_template": True,
        "readiness_contract": "Sandbox.create -> first non-empty screenshot",
        "setup_included": True,
        "ingress_included": False,
        "first_observation_api": "Sandbox.screenshot",
        "resolution": "1024x768",
        "dpi": 96,
        "display": ":0",
        "cpu_count": 2,
        "cpu_count_source": "public_default_desktop_pricing",
        "memory_gib": 1,
        "memory_gib_source": "public_default_desktop_pricing",
    }
    benchmark = _E2BLiveBenchmark(e2b_module, template=template)
    return _run_live_provider_cases(
        provider=provider,
        benchmark=benchmark,
        cold_cases=("cold_create_to_ready",),
        warm_cases=(
            "screenshot_full",
            "move_click",
            "move_click_sequence",
            "type_100_chars",
            "type_1000_chars",
            "command_echo",
        ),
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        metadata=metadata,
    )


class _DaytonaLiveBenchmark:
    def __init__(
        self,
        daytona_module: Any,
        *,
        api_key: str,
        api_url: str | None,
        target: str | None,
        snapshot: str | None,
    ) -> None:
        self._module = daytona_module
        self._snapshot = snapshot
        self.cleanup_errors: list[tuple[str, Exception]] = []
        config_cls = getattr(daytona_module, "DaytonaConfig", None)
        client_cls = daytona_module.Daytona
        if config_cls is None:
            self._client = client_cls()
        else:
            config_kwargs = {"api_key": api_key}
            if api_url:
                config_kwargs["api_url"] = api_url
            if target:
                config_kwargs["target"] = target
            self._client = client_cls(config_cls(**config_kwargs))

    def cold_create_to_ready(self) -> dict[str, Any]:
        sandbox = self.create_ready_session()
        try:
            return self._status(sandbox)
        finally:
            self.cleanup_session(sandbox)

    def create_ready_session(self) -> Any:
        sandbox = self._create_sandbox()
        try:
            computer_use = _computer_use(sandbox)
            _call_first_available(computer_use, ("start",))
            _wait_for_provider_screenshot_ready(self.screenshot_full, sandbox)
            return sandbox
        except Exception:
            self.cleanup_session(sandbox)
            raise

    def cleanup_session(self, sandbox: Any) -> None:
        with suppress(Exception):
            computer_use = _computer_use(sandbox)
            _call_first_available(computer_use, ("stop",))
        client_delete_error: Exception | None = None
        try:
            self._client.delete(sandbox)
            return
        except Exception as exc:
            client_delete_error = exc
        cleanup_errors = _cleanup_provider_sandbox(sandbox)
        if cleanup_errors:
            if client_delete_error is not None:
                self.cleanup_errors.append(("client.delete", client_delete_error))
            self.cleanup_errors.extend(cleanup_errors)

    def screenshot_full(self, sandbox: Any) -> dict[str, Any]:
        screenshot = _call_first_available(
            _computer_use(sandbox).screenshot,
            ("take_full_screen", "full_screen", "take"),
        )
        size_bytes = _provider_payload_size(screenshot)
        if size_bytes <= 0:
            raise RuntimeError("Daytona screenshot was empty")
        return {"size_bytes": size_bytes}

    def move_click(self, sandbox: Any) -> dict[str, Any]:
        mouse = _computer_use(sandbox).mouse
        _call_first_available(mouse, ("move", "move_to"), 24, 24)
        _call_first_available(mouse, ("click", "left_click"), 24, 24)
        return {"action_count": 2}

    def move_click_sequence(self, sandbox: Any) -> dict[str, Any]:
        mouse = _computer_use(sandbox).mouse
        for action in core.MOVE_CLICK_SEQUENCE_ACTIONS:
            if action["type"] == "move":
                _call_first_available(mouse, ("move", "move_to"), action["x"], action["y"])
            elif action["type"] == "click":
                _call_first_available(
                    mouse,
                    ("click", "left_click"),
                    action["x"],
                    action["y"],
                )
        return {"action_count": len(core.MOVE_CLICK_SEQUENCE_ACTIONS)}

    def type_100_chars(self, sandbox: Any) -> dict[str, Any]:
        keyboard = _computer_use(sandbox).keyboard
        _call_first_available(keyboard, ("type", "write"), core.PROVIDER_BENCHMARK_TEXT)
        return {"character_count": len(core.PROVIDER_BENCHMARK_TEXT), "method": "provider_default"}

    def type_1000_chars(self, sandbox: Any) -> dict[str, Any]:
        keyboard = _computer_use(sandbox).keyboard
        _call_first_available(keyboard, ("type", "write"), core.TYPE_1000_CHARS_TEXT)
        return {"character_count": len(core.TYPE_1000_CHARS_TEXT), "method": "provider_default"}

    def command_echo(self, sandbox: Any) -> dict[str, Any]:
        process = sandbox.process
        result = process.exec("sh -lc 'printf 42'", timeout=30)
        exit_code = _provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("Daytona command exited nonzero")
        return {"exit_code": exit_code}

    def _create_sandbox(self) -> Any:
        create = self._client.create
        if self._snapshot:
            params_cls = getattr(self._module, "CreateSandboxFromSnapshotParams", None)
            if params_cls is None:
                raise RuntimeError("Daytona SDK did not expose snapshot creation params")
            return create(params_cls(snapshot=self._snapshot))
        return create()

    def _status(self, sandbox: Any) -> dict[str, Any]:
        computer_use = _computer_use(sandbox)
        status_method = getattr(computer_use, "get_status", None)
        if callable(status_method):
            return {"status": "ready", "computer_use": _safe_provider_observation(status_method())}
        return {"status": "ready"}

    def resource_metadata(self, sandbox: Any) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        cpu = _provider_numeric_attr(sandbox, ("cpu", "cpu_count"))
        memory = _provider_numeric_attr(sandbox, ("memory", "memory_gib"))
        disk = _provider_numeric_attr(sandbox, ("disk", "storage_gib"))
        if cpu is not None:
            metadata["cpu_count"] = cpu
            metadata["cpu_count_source"] = "provider_sandbox_metadata"
        if memory is not None:
            metadata["memory_gib"] = memory
            metadata["memory_gib_source"] = "provider_sandbox_metadata"
        if disk is not None:
            metadata["storage_gib"] = disk
            metadata["storage_gib_source"] = "provider_sandbox_metadata"
        return metadata

    def verify_readbacks(self, sandbox: Any) -> dict[str, Any]:
        def run_command(command: str, timeout: int) -> str:
            return self._run_command(sandbox, command, timeout=timeout)

        def focus_target() -> None:
            _call_first_available(
                _computer_use(sandbox).mouse,
                ("click", "left_click"),
                TYPE_READBACK_FOCUS_X,
                TYPE_READBACK_FOCUS_Y,
            )

        def type_text(text: str) -> Any:
            return _call_first_available(
                _computer_use(sandbox).keyboard,
                ("type", "write"),
                text,
            )

        return {
            "cursor_position": _verification_step(
                lambda: _verify_daytona_cursor_position(sandbox),
                redacted_text=None,
            ),
            "type_text": _verification_step(
                lambda: _verify_provider_type_readback(
                    type_text=type_text,
                    focus_target=focus_target,
                    run_command=run_command,
                ),
                redacted_text=TYPE_READBACK_TEXT,
            ),
        }

    def _run_command(self, sandbox: Any, command: str, *, timeout: int) -> str:
        result = sandbox.process.exec(f"sh -lc {shlex.quote(command)}", timeout=timeout)
        exit_code = _provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("provider readback command exited nonzero")
        return _provider_stdout(result)


class _E2BLiveBenchmark:
    def __init__(self, e2b_module: Any, *, template: str | None) -> None:
        self._sandbox_cls = e2b_module.Sandbox
        self._template = template
        self.cleanup_errors: list[tuple[str, Exception]] = []
        self._move_click_count = 0

    def cold_create_to_ready(self) -> dict[str, Any]:
        sandbox = self.create_ready_session()
        try:
            return {"status": "ready"}
        finally:
            self.cleanup_session(sandbox)

    def create_ready_session(self) -> Any:
        sandbox = self._create_sandbox()
        try:
            _wait_for_provider_screenshot_ready(self.screenshot_full, sandbox)
            return sandbox
        except Exception:
            self.cleanup_session(sandbox)
            raise

    def cleanup_session(self, sandbox: Any) -> None:
        self.cleanup_errors.extend(_cleanup_provider_sandbox(sandbox))

    def screenshot_full(self, sandbox: Any) -> dict[str, Any]:
        screenshot = sandbox.screenshot()
        size_bytes = _provider_payload_size(screenshot)
        if size_bytes <= 0:
            raise RuntimeError("E2B screenshot was empty")
        return {"size_bytes": size_bytes}

    def move_click(self, sandbox: Any) -> dict[str, Any]:
        offset = self._move_click_count % 2
        self._move_click_count += 1
        _call_first_available(sandbox, ("move_mouse", "moveMouse"), 24 + offset, 24 + offset)
        _call_first_available(sandbox, ("left_click", "leftClick"))
        return {"action_count": 2}

    def move_click_sequence(self, sandbox: Any) -> dict[str, Any]:
        for action in core.MOVE_CLICK_SEQUENCE_ACTIONS:
            if action["type"] == "move":
                _call_first_available(
                    sandbox,
                    ("move_mouse", "moveMouse"),
                    action["x"],
                    action["y"],
                )
            elif action["type"] == "click":
                _call_first_available(sandbox, ("left_click", "leftClick"))
        return {"action_count": len(core.MOVE_CLICK_SEQUENCE_ACTIONS)}

    def type_100_chars(self, sandbox: Any) -> dict[str, Any]:
        _call_first_available(sandbox, ("write", "type"), core.PROVIDER_BENCHMARK_TEXT)
        return {"character_count": len(core.PROVIDER_BENCHMARK_TEXT), "method": "provider_default"}

    def type_1000_chars(self, sandbox: Any) -> dict[str, Any]:
        _call_first_available(sandbox, ("write", "type"), core.TYPE_1000_CHARS_TEXT)
        return {"character_count": len(core.TYPE_1000_CHARS_TEXT), "method": "provider_default"}

    def command_echo(self, sandbox: Any) -> dict[str, Any]:
        commands = getattr(sandbox, "commands", None)
        if commands is None:
            raise RuntimeError("E2B sandbox did not expose commands")
        run = getattr(commands, "run", None)
        if not callable(run):
            raise RuntimeError("E2B sandbox commands did not expose run")
        try:
            result = run("sh -lc 'printf 42'", timeout=30)
        except TypeError:
            result = run("sh -lc 'printf 42'")
        exit_code = _provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("E2B command exited nonzero")
        return {"exit_code": exit_code}

    def _create_sandbox(self) -> Any:
        create = self._sandbox_cls.create
        create_kwargs: dict[str, Any] = {
            "resolution": (1024, 768),
            "dpi": 96,
            "display": ":0",
            "timeout": 300,
        }
        if self._template:
            create_kwargs["template"] = self._template
        return create(**create_kwargs)

    def verify_readbacks(self, sandbox: Any) -> dict[str, Any]:
        def run_command(command: str, timeout: int) -> str:
            return self._run_command(sandbox, command, timeout=timeout)

        def type_text(text: str) -> Any:
            return _call_first_available(sandbox, ("write", "type"), text)

        return {
            "cursor_position": _verification_step(
                lambda: _verify_provider_cursor_position(run_command),
                redacted_text=None,
            ),
            "type_text": _verification_step(
                lambda: _verify_provider_type_readback(
                    type_text=type_text,
                    run_command=run_command,
                ),
                redacted_text=TYPE_READBACK_TEXT,
            ),
        }

    def _run_command(self, sandbox: Any, command: str, *, timeout: int) -> str:
        commands = getattr(sandbox, "commands", None)
        if commands is None:
            raise RuntimeError("E2B sandbox did not expose commands")
        run = getattr(commands, "run", None)
        if not callable(run):
            raise RuntimeError("E2B sandbox commands did not expose run")
        try:
            result = run(command, timeout=timeout)
        except TypeError:
            result = run(command)
        exit_code = _provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("provider readback command exited nonzero")
        return _provider_stdout(result)


def _run_live_provider_cases(
    *,
    provider: str,
    benchmark: Any,
    cold_cases: tuple[str, ...],
    warm_cases: tuple[str, ...],
    iterations: int,
    warmup_iterations: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    results: dict[str, Any] = {}
    measured_runtime_seconds = 0.0
    warm_cleanup_failed = False
    verification: dict[str, Any] | None = None
    for case in cold_cases:
        operation = getattr(benchmark, case)
        result_name = _provider_cold_case_name(case)
        case_start = time.perf_counter()
        samples, observations = core._measure_observed_case(
            name=result_name,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            operation=operation,
            failures=failures,
            redacted_text=_provider_case_redacted_text(result_name),
        )
        measured_runtime_seconds += time.perf_counter() - case_start
        result = core._case_result(result_name, iterations, samples, failures)
        result["last_result"] = (
            _safe_provider_observation(observations[-1]) if observations else None
        )
        _annotate_product_readiness_case(result, metadata)
        results[result_name] = result
        if result_name != case:
            results[case] = {
                **result,
                "name": case,
                "canonical_case": result_name,
                "deprecated": True,
            }
    if warm_cases:
        sandbox: Any | None = None
        warm_start = time.perf_counter()
        try:
            sandbox = benchmark.create_ready_session()
        except Exception as exc:
            measured_runtime_seconds += time.perf_counter() - warm_start
            for case in warm_cases:
                failure = core._failure(
                    case,
                    phase="setup",
                    iteration=0,
                    exc=exc,
                    redacted_text=_provider_case_redacted_text(case),
                )
                results[case] = core._case_result(case, iterations, [], [failure])
        else:
            try:
                resource_metadata = getattr(benchmark, "resource_metadata", None)
                if callable(resource_metadata):
                    metadata = _merge_provider_resource_metadata(
                        metadata, resource_metadata(sandbox)
                    )
                for case in warm_cases:
                    operation = getattr(benchmark, case)
                    samples, observations = core._measure_observed_case(
                        name=case,
                        iterations=iterations,
                        warmup_iterations=warmup_iterations,
                        operation=_bind_provider_operation(operation, sandbox),
                        failures=failures,
                        redacted_text=_provider_case_redacted_text(case),
                    )
                    result = core._case_result(case, iterations, samples, failures)
                    result["last_result"] = (
                        _safe_provider_observation(observations[-1]) if observations else None
                    )
                    results[case] = result
            finally:
                verifier = getattr(benchmark, "verify_readbacks", None)
                if callable(verifier):
                    try:
                        verification = _safe_provider_observation(verifier(sandbox))
                    except Exception as exc:
                        verification = {
                            "status": "failed",
                            "message": core._redact_text(str(exc), TYPE_READBACK_TEXT),
                        }
                before_cleanup_errors = len(getattr(benchmark, "cleanup_errors", []))
                benchmark.cleanup_session(sandbox)
                warm_cleanup_failed = (
                    len(getattr(benchmark, "cleanup_errors", [])) > before_cleanup_errors
                )
                measured_runtime_seconds += time.perf_counter() - warm_start
    cleanup_case = _provider_cleanup_case(benchmark)
    if cleanup_case is not None:
        results["cleanup"] = cleanup_case
    if warm_cleanup_failed:
        metadata = dict(metadata)
        metadata["cost_notes"] = ["cleanup failed; leaked resources may incur unmeasured cost"]
    return _provider_result(
        provider,
        cases=results,
        metadata=metadata,
        runtime_seconds=measured_runtime_seconds,
        verification=verification,
    )


def _bind_provider_operation(operation: Callable[[Any], Any], sandbox: Any) -> Callable[[], Any]:
    def run() -> Any:
        return operation(sandbox)

    return run


def _wait_for_provider_screenshot_ready(
    screenshot_operation: Callable[[Any], dict[str, Any]],
    sandbox: Any,
    *,
    attempts: int = 5,
    delay_seconds: float = 1.0,
) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            screenshot_operation(sandbox)
            return
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
    if last_error is not None:
        raise last_error


def _provider_result(
    provider: str,
    *,
    cases: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    runtime_seconds: float | None = None,
    verification: dict[str, Any] | None = None,
    billing_reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    measured = False
    failed = False
    for case_name, case in cases.items():
        if not isinstance(case, dict):
            continue
        failures.extend(core._benchmark_failures(case_name, case.get("failures", [])))
        status = case.get("status")
        failed = failed or status == "failed"
        measured = measured or status == "ok"
    if failed:
        status = "failed"
    elif measured:
        status = "ok"
    else:
        statuses = {case.get("status") for case in cases.values() if isinstance(case, dict)}
        status = "unavailable" if "unavailable" in statuses else "not_measured"
    safe_metadata = metadata or {}
    result = {
        "status": status,
        "provider": provider,
        "metadata": safe_metadata,
        "cases": cases,
        "failures": failures,
    }
    if verification is not None:
        result["verification"] = verification
    if billing_reconciliation is not None:
        result["billing_reconciliation"] = billing_reconciliation
    result["cost_estimate"] = estimate_provider_cost(
        provider,
        provider_status=status,
        runtime_seconds=runtime_seconds,
        metadata=safe_metadata,
    )
    return result


def _verify_daytona_cursor_position(sandbox: Any) -> dict[str, Any]:
    expected = _expected_sequence_cursor_position()
    observed = _provider_point_xy(
        _call_first_available(_computer_use(sandbox).mouse, ("get_position", "position"))
    )
    ok = observed == expected
    return {
        "status": "ok" if ok else "failed",
        "expected": {"x": expected[0], "y": expected[1]},
        "observed": {"x": observed[0], "y": observed[1]} if observed is not None else None,
        "method": "computer_use.mouse.get_position",
    }


def _verify_provider_cursor_position(run_command: Callable[[str, int], str]) -> dict[str, Any]:
    expected = _expected_sequence_cursor_position()
    output = run_command("xdotool getmouselocation --shell", 10)
    observed = _parse_xdotool_position(output)
    ok = observed == expected
    return {
        "status": "ok" if ok else "failed",
        "expected": {"x": expected[0], "y": expected[1]},
        "observed": {"x": observed[0], "y": observed[1]} if observed is not None else None,
    }


def _verification_step(
    operation: Callable[[], dict[str, Any]],
    *,
    redacted_text: str | None,
) -> dict[str, Any]:
    try:
        return operation()
    except Exception as exc:
        return {
            "status": "failed",
            "message": core._redact_text(str(exc), redacted_text),
        }


def _verify_provider_type_readback(
    *,
    type_text: Callable[[str], Any],
    focus_target: Callable[[], Any] | None = None,
    run_command: Callable[[str, int], str],
) -> dict[str, Any]:
    setup = _run_type_readback_setup(run_command)
    if setup["status"] != "ready":
        return setup
    if focus_target is not None:
        focus_target()
    type_text(TYPE_READBACK_TEXT)
    return _read_type_readback_file(run_command)


def _run_type_readback_setup(run_command: Callable[[str, int], str]) -> dict[str, Any]:
    output = run_command(_type_readback_setup_command(), 15)
    if output.startswith("unsupported:"):
        return {
            "status": "unsupported",
            "reason": output.strip(),
        }
    return {"status": "ready"}


def _read_type_readback_file(run_command: Callable[[str, int], str]) -> dict[str, Any]:
    output = run_command(_type_readback_result_command(), 10)
    observed = _parse_key_value_output(output)
    expected_count = len(TYPE_READBACK_TEXT)
    observed_count = _int_or_none(observed.get("keypress_count"))
    ok = observed_count is not None and observed_count >= expected_count
    return {
        "status": "ok" if ok else "failed",
        "expected": {"minimum_keypress_count": expected_count},
        "observed": {
            "keypress_count": observed_count,
        },
    }


def _type_readback_setup_command() -> str:
    target = shlex.quote(TYPE_READBACK_FILE)
    pid_file = shlex.quote(TYPE_READBACK_PID_FILE)
    title = shlex.quote(TYPE_READBACK_TITLE)
    launcher = shlex.quote(
        "import os, subprocess, sys; "
        "env = os.environ.copy(); "
        "env['DISPLAY'] = env.get('DISPLAY') or ':0'; "
        "out = open(sys.argv[1], 'wb'); "
        "process = subprocess.Popen("
        "['xev', '-event', 'keyboard', '-name', sys.argv[3], '-geometry', '220x120+0+0'], "
        "stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, "
        "env=env, start_new_session=True"
        "); "
        "open(sys.argv[2], 'w').write(str(process.pid))"
    )
    return (
        "export DISPLAY=${DISPLAY:-:0}; "
        "if ! command -v xev >/dev/null 2>&1; then "
        "printf 'unsupported:no-xev\\n'; exit 0; fi; "
        "python_bin=$(command -v python3 || command -v python || true); "
        "if [ -z \"$python_bin\" ]; then printf 'unsupported:no-python\\n'; exit 0; fi; "
        f"rm -f {target} {pid_file}; "
        f"\"$python_bin\" -c {launcher} {target} {pid_file} {title}; "
        "sleep 0.5; "
        "printf 'ready=1\\n'"
    )


def _type_readback_result_command() -> str:
    target = shlex.quote(TYPE_READBACK_FILE)
    pid_file = shlex.quote(TYPE_READBACK_PID_FILE)
    return (
        "sleep 0.2; "
        f"if [ -f {pid_file} ]; then kill $(cat {pid_file}) >/dev/null 2>&1 || true; fi; "
        f"if [ ! -f {target} ]; then printf 'missing=1\\n'; exit 0; fi; "
        f"count=$(grep -c 'KeyPress event' {target} 2>/dev/null || true); "
        "printf 'keypress_count=%s\\n' \"$count\""
    )


def _expected_sequence_cursor_position() -> tuple[int, int]:
    for action in reversed(core.MOVE_CLICK_SEQUENCE_ACTIONS):
        if action["type"] == "move":
            return int(action["x"]), int(action["y"])
    raise RuntimeError("move/click sequence did not include a move action")


def _parse_xdotool_position(output: str) -> tuple[int, int] | None:
    values = _parse_key_value_output(output)
    x = _int_or_none(values.get("X"))
    y = _int_or_none(values.get("Y"))
    if x is None or y is None:
        return None
    return x, y


def _provider_point_xy(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict):
        x = _int_or_none(value.get("x"))
        y = _int_or_none(value.get("y"))
    else:
        x = _int_or_none(getattr(value, "x", None))
        y = _int_or_none(getattr(value, "y", None))
    if x is None or y is None:
        return None
    return x, y


def _provider_numeric_attr(value: Any, names: tuple[str, ...]) -> float | None:
    for name in names:
        raw_value = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        parsed = _float_or_none(raw_value)
        if parsed is not None:
            return parsed
    return None


def _parse_key_value_output(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _provider_not_measured(provider: str, reason: str) -> dict[str, Any]:
    return _provider_result(
        provider,
        cases={"setup": {"status": "not_measured", "reason": reason, "failures": []}},
    )


def _provider_unavailable(provider: str, reason: str) -> dict[str, Any]:
    return _provider_result(
        provider,
        cases={"setup": {"status": "unavailable", "reason": reason, "failures": []}},
    )


def _provider_case_redacted_text(case: str) -> str | None:
    if case == "type_1000_chars":
        return core.TYPE_1000_CHARS_TEXT
    return core.PROVIDER_BENCHMARK_TEXT


def _provider_cold_case_name(case: str) -> str:
    if case == "cold_create_to_ready":
        return "product_create_to_first_screenshot"
    return case


def _annotate_product_readiness_case(
    case: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    case["definition"] = "provider product create call to first successful full-screen screenshot"
    for key in (
        "startup_model",
        "uses_snapshot_or_template",
        "readiness_contract",
        "setup_included",
        "ingress_included",
        "first_observation_api",
    ):
        if key in metadata:
            case[key] = metadata[key]


def _daytona_default_resource_metadata() -> dict[str, Any]:
    return {
        "cpu_count": 1,
        "cpu_count_source": "public_default_sandbox_resources",
        "memory_gib": 1,
        "memory_gib_source": "public_default_sandbox_resources",
        "storage_gib": 3,
        "storage_gib_source": "public_default_sandbox_resources",
        "cost_notes": [
            "Daytona default sandbox resources are documented as 1 vCPU, 1 GiB memory, "
            "and 3 GiB disk; storage estimate does not account for account-level free allowance"
        ],
    }


def _merge_provider_resource_metadata(
    metadata: dict[str, Any], resource_metadata: dict[str, Any]
) -> dict[str, Any]:
    if not resource_metadata:
        return metadata
    merged = {**metadata, **resource_metadata}
    if any(
        resource_metadata.get(key) == "provider_sandbox_metadata"
        for key in ("cpu_count_source", "memory_gib_source", "storage_gib_source")
    ):
        merged.pop("cost_notes", None)
    return merged


def _safe_provider_observation(observation: Any) -> dict[str, Any] | None:
    if observation is None:
        return None
    if not isinstance(observation, dict):
        return {"type": type(observation).__name__}
    redacted = _redact_provider_value(observation)
    return redacted if isinstance(redacted, dict) else {"value": redacted}


def _redact_provider_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_provider_key(key):
        return _redaction_marker(value)
    if isinstance(value, dict):
        return {
            item_key: _redact_provider_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_provider_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_provider_value(item) for item in value]
    if isinstance(value, str):
        return _safe_provider_metadata_value(value)
    return value


def _is_sensitive_provider_key(key: str) -> bool:
    normalized = _normalize_provider_key(key)
    return normalized in {
        "stdout",
        "stderr",
        "text",
        "bytes",
        "data",
        "data_base64",
        "secret",
        "client_secret",
        "access_key",
        "secret_key",
        "private_key",
        "credential",
        "credentials",
        "url",
        "auth_key",
        "authorization",
        "bearer",
        "api_key",
        "token",
        "password",
    } or normalized.endswith(("_token", "_url", "_uri", "_secret", "_key"))


def _normalize_provider_key(key: str) -> str:
    underscored = re.sub(r"(?<!^)(?=[A-Z])", "_", key)
    return underscored.lower().replace("-", "_")


def _safe_provider_metadata_value(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith(("http://", "https://")):
        return core._safe_url_origin(value)
    return core._redact_text(value, core.PROVIDER_BENCHMARK_TEXT)


def _import_provider_module(module: str, *fromlist: str) -> Any:
    return __import__(module, fromlist=list(fromlist))


def _package_version(package: str) -> str | None:
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return None


def _cleanup_provider_sandbox(sandbox: Any) -> list[tuple[str, Exception]]:
    errors: list[tuple[str, Exception]] = []
    for method_name in ("delete", "kill", "stop"):
        method = getattr(sandbox, method_name, None)
        if not callable(method):
            continue
        try:
            method()
            return []
        except Exception as exc:
            errors.append((method_name, exc))
        try:
            method(force=True)
            return []
        except Exception as exc:
            errors.append((f"{method_name}(force=True)", exc))
    return errors


def _provider_cleanup_case(benchmark: Any) -> dict[str, Any] | None:
    errors = getattr(benchmark, "cleanup_errors", [])
    if not errors:
        return None
    failures = []
    for index, (method, exc) in enumerate(errors):
        failure = core._failure(
            "cleanup",
            phase="cleanup",
            iteration=index,
            exc=exc,
            redacted_text=core.PROVIDER_BENCHMARK_TEXT,
        )
        failure["method"] = method
        failures.append(failure)
    return core._case_result("cleanup", len(failures), [], failures)


def _computer_use(sandbox: Any) -> Any:
    computer_use = getattr(sandbox, "computer_use", None)
    if computer_use is None:
        computer_use = getattr(sandbox, "computerUse", None)
    if computer_use is None:
        raise RuntimeError("provider sandbox did not expose computer use")
    return computer_use


def _call_first_available(target: Any, names: tuple[str, ...], *args: Any) -> Any:
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            return method(*args)
    raise RuntimeError(f"provider object did not expose any of: {', '.join(names)}")


def _provider_payload_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes | bytearray):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if hasattr(value, "read"):
        current = value.read()
        return _provider_payload_size(current)
    if hasattr(value, "size_bytes"):
        size = value.size_bytes
        if size is not None:
            return int(size) if isinstance(size, int | float) else 0
    if hasattr(value, "bytes"):
        payload = value.bytes
        if payload is not None:
            return _provider_payload_size(payload)
    if hasattr(value, "data"):
        payload = value.data
        if payload is not None:
            return _provider_payload_size(payload)
    if hasattr(value, "image"):
        payload = value.image
        if payload is not None:
            return _provider_payload_size(payload)
    if hasattr(value, "screenshot"):
        payload = value.screenshot
        if payload is not None:
            return _provider_payload_size(payload)
    if hasattr(value, "image_base64"):
        payload = value.image_base64
        if payload is not None:
            return _provider_payload_size(payload)
    if hasattr(value, "base64"):
        payload = value.base64
        if payload is not None:
            return _provider_payload_size(payload)
    return 0


def _provider_exit_code(result: Any) -> int | None:
    for attr in ("exit_code", "return_code", "code"):
        value = getattr(result, attr, None)
        if value is not None:
            return int(value)
    if isinstance(result, dict):
        for key in ("exit_code", "return_code", "code"):
            value = result.get(key)
            if value is not None:
                return int(value)
    return 0


def _provider_stdout(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("stdout", "result", "output"):
            value = result.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = value.get("stdout") or value.get("result")
                if isinstance(nested, str):
                    return nested
    for attr in ("stdout", "result", "output"):
        value = getattr(result, attr, None)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("stdout") or value.get("result")
            if isinstance(nested, str):
                return nested
    return ""


def _redaction_marker(value: Any) -> dict[str, Any]:
    marker: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        marker["length"] = len(value)
    elif isinstance(value, bytes | bytearray):
        marker["size_bytes"] = len(value)
    elif isinstance(value, list | tuple | dict):
        marker["items"] = len(value)
    return marker
