from __future__ import annotations

import os
import re
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
from .client import DaemonClient
from .models import ActionBatchResult, ActionItemResult

ProviderMode = Literal["mock-local", "http", "provider-live"]


def run_provider_comparison(
    *,
    providers: list[core.ComparisonProvider] | None = None,
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
    provider: core.ComparisonProvider,
    *,
    client: DaemonClient | None,
    mode: ProviderMode,
    base_url: str | None,
    iterations: int,
    warmup_iterations: int,
    sandbox_exec_runner: Any,
    sandbox_exec_setup_failure: dict[str, Any] | None,
) -> dict[str, Any]:
    if provider == "modal-daemon":
        return _run_modal_daemon_provider(
            client=client,
            mode=mode,
            base_url=base_url,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        )
    if provider == "modal-exec":
        return _provider_result(
            provider,
            cases={
                "sandbox_exec_move_click": core.run_sandbox_exec_benchmark(
                    iterations=iterations,
                    warmup_iterations=warmup_iterations,
                    runner=sandbox_exec_runner,
                    setup_failure=sandbox_exec_setup_failure,
                )
            },
            metadata={"transport": "Modal Sandbox.exec"},
        )
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
) -> dict[str, Any]:
    if client is None:
        return _provider_not_measured(
            "modal-daemon",
            "modal-daemon comparison requires --mock-local or --base-url",
        )
    action_batch = core.run_action_batch_benchmark(
        client=client,
        mode="mock-local" if mode == "mock-local" else "http",
        iterations=iterations,
        base_url=base_url,
        warmup_iterations=warmup_iterations,
    )
    cases = {
        "action_batch": core._report_action_batch(action_batch),
        "screenshot_full": core.run_screenshot_benchmark(
            client=client,
            name="screenshot_full",
            request={"format": "png", "storage": "inline", "show_cursor": False},
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "move_click": core.run_move_click_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "type_100_chars": core.run_type_100_chars_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "recording_start_stop": core.run_recording_start_stop_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "cold_create_to_ready": core._future_benchmark(
            "not_measured",
            "cold Modal Sandbox creation is measured by a live orchestration runner, "
            "not this daemon target",
        ),
        "warm_attach_to_health": core._future_benchmark(
            "not_measured",
            "warm attach requires Modal orchestration metadata",
        ),
    }
    return _provider_result(
        "modal-daemon",
        cases=cases,
        metadata={"transport": "daemon-http", "base_url": core._safe_base_url(base_url)},
    )


def _run_adapter_provider(
    *,
    provider: core.ComparisonProvider,
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
    )


class _AdapterProviderBenchmark:
    def __init__(self, provider: core.ComparisonProvider) -> None:
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
    }
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
        warm_cases=("screenshot_full", "move_click", "type_100_chars", "command_echo"),
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
        "resolution": "1024x768",
        "dpi": 96,
        "display": ":0",
    }
    benchmark = _E2BLiveBenchmark(e2b_module, template=template)
    return _run_live_provider_cases(
        provider=provider,
        benchmark=benchmark,
        cold_cases=("cold_create_to_ready",),
        warm_cases=("screenshot_full", "move_click", "type_100_chars", "command_echo"),
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

    def type_100_chars(self, sandbox: Any) -> dict[str, Any]:
        keyboard = _computer_use(sandbox).keyboard
        _call_first_available(keyboard, ("type", "write"), core.PROVIDER_BENCHMARK_TEXT)
        return {"character_count": len(core.PROVIDER_BENCHMARK_TEXT)}

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


class _E2BLiveBenchmark:
    def __init__(self, e2b_module: Any, *, template: str | None) -> None:
        self._sandbox_cls = e2b_module.Sandbox
        self._template = template
        self.cleanup_errors: list[tuple[str, Exception]] = []

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
        _call_first_available(sandbox, ("move_mouse", "moveMouse"), 24, 24)
        _call_first_available(sandbox, ("left_click", "leftClick"), 24, 24)
        return {"action_count": 2}

    def type_100_chars(self, sandbox: Any) -> dict[str, Any]:
        _call_first_available(sandbox, ("write", "type"), core.PROVIDER_BENCHMARK_TEXT)
        return {"character_count": len(core.PROVIDER_BENCHMARK_TEXT)}

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
    for case in cold_cases:
        operation = getattr(benchmark, case)
        samples, observations = core._measure_observed_case(
            name=case,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            operation=operation,
            failures=failures,
            redacted_text=core.PROVIDER_BENCHMARK_TEXT,
        )
        result = core._case_result(case, iterations, samples, failures)
        result["last_result"] = (
            _safe_provider_observation(observations[-1]) if observations else None
        )
        results[case] = result
    if warm_cases:
        sandbox: Any | None = None
        try:
            sandbox = benchmark.create_ready_session()
        except Exception as exc:
            for case in warm_cases:
                failure = core._failure(
                    case,
                    phase="setup",
                    iteration=0,
                    exc=exc,
                    redacted_text=core.PROVIDER_BENCHMARK_TEXT,
                )
                results[case] = core._case_result(case, iterations, [], [failure])
        else:
            try:
                for case in warm_cases:
                    operation = getattr(benchmark, case)
                    samples, observations = core._measure_observed_case(
                        name=case,
                        iterations=iterations,
                        warmup_iterations=warmup_iterations,
                        operation=_bind_provider_operation(operation, sandbox),
                        failures=failures,
                        redacted_text=core.PROVIDER_BENCHMARK_TEXT,
                    )
                    result = core._case_result(case, iterations, samples, failures)
                    result["last_result"] = (
                        _safe_provider_observation(observations[-1]) if observations else None
                    )
                    results[case] = result
            finally:
                benchmark.cleanup_session(sandbox)
    cleanup_case = _provider_cleanup_case(benchmark)
    if cleanup_case is not None:
        results["cleanup"] = cleanup_case
    return _provider_result(provider, cases=results, metadata=metadata)


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
    return {
        "status": status,
        "provider": provider,
        "metadata": metadata or {},
        "cases": cases,
        "failures": failures,
    }


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


def _redaction_marker(value: Any) -> dict[str, Any]:
    marker: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        marker["length"] = len(value)
    elif isinstance(value, bytes | bytearray):
        marker["size_bytes"] = len(value)
    elif isinstance(value, list | tuple | dict):
        marker["items"] = len(value)
    return marker
