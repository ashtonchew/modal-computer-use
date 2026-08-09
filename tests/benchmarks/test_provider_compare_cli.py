from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from modal_computer_use import cli
from modal_computer_use.benchmark_comparison import run_provider_comparison
from modal_computer_use.benchmarks import daemon_surface, measurement
from modal_computer_use.benchmarks import lifecycle as benchmark_lifecycle
from modal_computer_use.benchmarks.constants import DEFAULT_PROVIDER_COMPARISON_ITERATIONS
from modal_computer_use.benchmarks.provider_comparison import (
    comparison,
    daytona,
    e2b,
    live,
    payloads,
    provider_sdk,
    results,
)
from modal_computer_use.daemon import budget_policy
from modal_computer_use.daemon.input_rate_limit import InputTokenBucket


def test_e2b_benchmark_session_outlives_publishable_warm_matrix() -> None:
    create_kwargs: dict[str, object] = {}

    class FakeSandbox:
        @staticmethod
        def create(**kwargs):
            create_kwargs.update(kwargs)
            return object()

    driver = e2b.E2BDriver(SimpleNamespace(Sandbox=FakeSandbox), template=None)

    driver.create_lifecycle_session()

    assert (
        create_kwargs["timeout"]
        == e2b.E2B_BENCHMARK_SESSION_TIMEOUT_SECONDS
        == 3600
    )


def test_benchmark_compare_mock_local_outputs_json(capsys) -> None:
    exit_code = cli.main(["benchmark", "compare", "--mock-local", "--iterations", "1"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["benchmark"] == "provider-compare"
    assert payload["mode"] == "mock-local"
    assert payload["providers"]["modal-daemon"]["status"] == "ok"
    assert (
        payload["providers"]["modal-daemon"]["metadata"]["canonical_source"]
        == "benchmark sdk surface"
    )
    assert payload["providers"]["modal-daemon"]["metadata"]["provider_surface"] == "daemon-http"
    assert (
        payload["providers"]["modal-daemon"]["metadata"]["ingress"]["canonical_name"]
        == "modal-daemon-local"
    )
    assert payload["providers"]["modal-daemon"]["cases"]["command_echo"]["status"] == "ok"
    assert payload["providers"]["modal-daemon"]["cases"]["screenshot_full"]["status"] == "ok"
    assert payload["providers"]["modal-daemon"]["cases"]["type_100_chars"]["request"] == {
        "character_count": 100,
        "method": "auto",
        "delay_ms": 10,
    }
    assert payload["providers"]["modal-daemon"]["cases"]["type_1000_chars"]["request"] == {
        "character_count": 1000,
        "method": "auto",
        "delay_ms": 10,
    }
    coordinate_click = payload["providers"]["modal-daemon"]["cases"]["coordinate_click"]
    coordinate_sequence = payload["providers"]["modal-daemon"]["cases"][
        "coordinate_click_sequence"
    ]
    assert coordinate_click["semantic"] == "coordinate_click"
    assert coordinate_click["benchmark_semantics"] == "coordinate-click-v1"
    assert coordinate_click["logical_action_count"] == 1
    assert coordinate_click["provider_action_count"] == 1
    assert coordinate_sequence["semantic"] == "coordinate_click_sequence"
    assert coordinate_sequence["benchmark_semantics"] == "coordinate-click-v1"
    assert coordinate_sequence["logical_action_count"] == 4
    assert coordinate_sequence["provider_action_count"] == 4
    assert coordinate_sequence["native_batch"] is True
    assert (
        payload["providers"]["modal-daemon"]["cases"]["click_then_screenshot"]["status"] == "ok"
    )
    assert (
        payload["providers"]["modal-daemon"]["cases"]["screenshot_full"]["comparison_role"]
        == "canonical_fast_path"
    )
    assert (
        payload["providers"]["modal-daemon"]["cases"]["screenshot_full_structured"][
            "comparison_role"
        ]
        == "structured_compatibility_path"
    )
    assert (
        payload["providers"]["modal-daemon"]["cases"]["cold_create_to_ready"]["status"]
        == "not_measured"
    )
    assert (
        payload["providers"]["modal-daemon"]["cases"]["product_create_to_first_screenshot"][
            "status"
        ]
        == "not_measured"
    )
    assert payload["providers"]["openai"]["metadata"]["provider_api_calls"] is False
    assert payload["providers"]["anthropic"]["metadata"]["tool_version"] == "computer_20250124"
    assert payload["providers"]["generic"]["metadata"]["adapter"] == "ActionExecutor"
    assert "0123456789" not in captured.out
    assert '"text"' not in captured.out
    assert "Bearer" not in captured.out


def test_benchmark_compare_defaults_to_publishable_sample_count(monkeypatch) -> None:
    seen: dict[str, int] = {}

    def fake_benchmark_compare(args) -> int:
        seen["iterations"] = args.iterations
        return 0

    monkeypatch.setattr(cli, "_benchmark_compare", fake_benchmark_compare)

    assert cli.main(["benchmark", "compare", "--mock-local"]) == 0
    assert seen["iterations"] == DEFAULT_PROVIDER_COMPARISON_ITERATIONS == 30


def test_adapter_providers_project_sdk_surfaces_to_provider_schema() -> None:
    payload = run_provider_comparison(
        providers=["openai", "anthropic", "generic"],
        iterations=1,
        warmup_iterations=0,
    )

    expected_adapters = {
        "openai": "OpenAIAdapter",
        "anthropic": "AnthropicAdapter",
        "generic": "ActionExecutor",
    }
    for provider_name, adapter_name in expected_adapters.items():
        provider = payload["providers"][provider_name]
        assert provider["provider"] == provider_name
        assert "surface" not in provider
        assert provider["status"] == "ok"
        assert provider["metadata"]["adapter"] == adapter_name
        assert provider["metadata"]["target_kind"] == "adapter"
        assert provider["cases"]["adapter_matrix"]["status"] == "ok"
        assert "name" not in provider["cases"]["adapter_matrix"]
        assert provider["cost_estimate"]["notes"] == [
            "provider comparison does not create billable provider resources"
        ]


def test_benchmark_compare_external_providers_skip_without_credentials(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("TZAFON_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        [
            "benchmark",
            "compare",
            "--providers",
            "daytona,e2b,tzafon",
            "--iterations",
            "1",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["providers"]["daytona"]["status"] == "not_measured"
    assert payload["providers"]["e2b"]["status"] == "not_measured"
    assert payload["providers"]["tzafon"]["status"] == "not_measured"
    assert "DAYTONA_API_KEY is not set" in captured.out
    assert "E2B_API_KEY is not set" in captured.out
    assert "TZAFON_API_KEY is not set" in captured.out


def test_provider_compare_cli_accepts_tzafon() -> None:
    args = SimpleNamespace(providers="tzafon", provider=[])

    assert cli._compare_providers(args) == ["tzafon"]
    assert cli._has_live_external_provider(["tzafon"]) is True


def test_provider_compare_parser_accepts_repeated_tzafon_provider(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.delenv("TZAFON_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        ["benchmark", "compare", "--provider", "tzafon", "--iterations", "1"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["providers"]["tzafon"]["status"] == "not_measured"


def test_provider_compare_subprocess_backend_reaches_created_modal_config(
    monkeypatch, capsys
) -> None:
    seen: dict[str, object] = {}

    def fake_created_modal_comparison(args, **kwargs):
        seen["config"] = cli._modal_benchmark_config(args, run_id="compare-test")
        return {
            "ok": True,
            "benchmark": "provider-compare",
            "metadata": {"environment": cli._benchmark_environment_metadata(args)},
            "providers": {},
            "failures": [],
        }

    monkeypatch.setattr(
        cli,
        "_benchmark_compare_created_modal_sandbox",
        fake_created_modal_comparison,
    )

    exit_code = cli.main(
        [
            "benchmark",
            "compare",
            "--providers",
            "modal-daemon",
            "--create-modal-sandbox",
            "--subprocess-backend",
            "threaded",
            "--token",
            "benchmark-secret-token",
            "--iterations",
            "1",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert seen["config"].actions.subprocess_backend == "threaded"
    assert payload["metadata"]["environment"]["subprocess_backend"] == "threaded"
    assert "benchmark-secret-token" not in captured.out


def test_provider_compare_created_modal_uses_public_computer_config_defaults(
    monkeypatch, capsys
) -> None:
    seen: dict[str, object] = {}

    def fake_created_modal_comparison(args, **kwargs):
        seen["config"] = cli._modal_benchmark_config(args, run_id="compare-defaults")
        seen["environment"] = cli._benchmark_environment_metadata(args)
        return {
            "ok": True,
            "benchmark": "provider-compare",
            "metadata": {"environment": seen["environment"]},
            "providers": {},
            "failures": [],
        }

    monkeypatch.setattr(
        cli,
        "_benchmark_compare_created_modal_sandbox",
        fake_created_modal_comparison,
    )

    exit_code = cli.main(
        [
            "benchmark",
            "compare",
            "--providers",
            "modal-daemon",
            "--create-modal-sandbox",
            "--iterations",
            "1",
        ]
    )

    assert exit_code == 0
    config = seen["config"]
    defaults = cli.ComputerConfig(run_id="compare-defaults")
    assert config.resources == defaults.resources
    assert config.browser == defaults.browser
    assert config.actions.input_rate_limit_per_sec == 200
    assert config.actions.input_rate_limit_burst == 400
    assert seen["environment"]["resource_profile"] == "standard"
    assert seen["environment"]["browser"] is None
    assert seen["environment"]["input_rate_limit_per_sec"] == 200
    assert seen["environment"]["input_rate_limit_burst"] == 400
    assert seen["environment"]["action_case_pacing_ms"] is None
    capsys.readouterr()


def test_provider_compare_custom_rate_disables_default_pacing_metadata(
    monkeypatch, capsys
) -> None:
    seen: dict[str, object] = {}

    def fake_created_modal_comparison(args, **kwargs):
        del kwargs
        seen.update(cli._benchmark_environment_metadata(args))
        return {
            "ok": True,
            "benchmark": "provider-compare",
            "metadata": {"environment": dict(seen)},
            "providers": {},
            "failures": [],
        }

    monkeypatch.setattr(
        cli,
        "_benchmark_compare_created_modal_sandbox",
        fake_created_modal_comparison,
    )

    exit_code = cli.main(
        [
            "benchmark",
            "compare",
            "--providers",
            "modal-daemon",
            "--create-modal-sandbox",
            "--input-rate-limit-per-sec",
            "0",
            "--iterations",
            "1",
        ]
    )

    assert exit_code == 0
    assert seen["input_rate_limit_per_sec"] == 0
    assert seen["action_case_pacing_ms"] is None
    capsys.readouterr()


def test_live_modal_provider_paces_action_cases_outside_case_timers(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(comparison.time, "sleep", sleeps.append)

    payload = cli._with_mock_local_client(
        lambda client: comparison.run_provider(
            "modal-daemon",
            client=client,
            mode="http",
            base_url="http://testserver",
            iterations=3,
            warmup_iterations=1,
            sandbox_exec_runner=None,
            sandbox_exec_setup_failure=None,
            environment_metadata={"action_case_pacing_ms": 1050},
            modal_action_pacing_seconds=1.05,
        )
    )

    assert payload["status"] == "ok"
    assert sleeps == [1.05] * 36
    assert "timeout_ms" not in payload["cases"]["type_100_chars"]["request"]
    assert "timeout_ms" not in payload["cases"]["type_1000_chars"]["request"]


def test_modal_default_token_bucket_does_not_require_sample_pacing(monkeypatch) -> None:
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(measurement.time, "perf_counter", lambda: clock.now)
    state = SimpleNamespace(
        settings=SimpleNamespace(
            input_rate_limit_per_sec=500,
            input_rate_limit_burst=1_000,
            max_actions=None,
            max_idle_seconds=None,
        ),
        action_count=0,
        input_token_bucket=InputTokenBucket(
            refill_rate=500,
            capacity=1_000,
            clock=lambda: clock.now,
        ),
        last_activity_at=0.0,
    )
    policy = budget_policy.BudgetPolicy(state)

    def eight_action_sequence() -> dict[str, str]:
        for _ in range(8):
            policy.reserve_action()
            clock.now += 0.001
        return {"status": "ok"}

    failures: list[dict[str, object]] = []
    samples, _ = measurement._measure_observed_case(
        name="coordinate_click_sequence",
        iterations=3,
        warmup_iterations=1,
        operation=eight_action_sequence,
        failures=failures,
        before_iteration=None,
    )

    assert failures == []
    assert samples == pytest.approx([8.0, 8.0, 8.0])
    assert max(samples) < 1000


def test_provider_default_documented_command_omits_modal_tuning() -> None:
    command = (
        Path("docs/benchmarking.md")
        .read_text(encoding="utf-8")
        .split("## Run the provider-default comparison", 1)[1]
        .split("Provider-default means", 1)[0]
    )

    for option in (
        "--browser",
        "--resource-profile",
        "--input-rate-limit-per-sec",
        "--subprocess-backend",
    ):
        assert option not in command


def test_provider_compare_env_file_whitelists_tzafon_key(
    monkeypatch, tmp_path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TZAFON_API_KEY=tzafon-test-secret\n"
        "NOT_A_BENCHMARK_SECRET=must-not-load\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TZAFON_API_KEY", raising=False)
    monkeypatch.delenv("NOT_A_BENCHMARK_SECRET", raising=False)

    cli._load_benchmark_env_file(env_file)

    assert __import__("os").environ["TZAFON_API_KEY"] == "tzafon-test-secret"
    assert "NOT_A_BENCHMARK_SECRET" not in __import__("os").environ


def test_provider_comparison_labels_product_readiness_case(monkeypatch) -> None:
    class FakeComputerUse:
        class Mouse:
            def move(self, x, y) -> None:
                return None

            def click(self, x, y) -> None:
                return None

            def get_position(self):
                return {"x": 16, "y": 128}

        class Keyboard:
            def type(self, text) -> None:
                return None

        class Screenshot:
            def take_full_screen(self):
                buffer = BytesIO()
                Image.new("RGB", (2, 3), color="red").save(buffer, format="PNG")
                return buffer.getvalue()

        mouse = Mouse()
        keyboard = Keyboard()
        screenshot = Screenshot()

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class FakeSandbox:
        computer_use = FakeComputerUse()

        class Process:
            def exec(self, command, timeout=30):
                return {"exit_code": 0, "stdout": "42\n"}

        process = Process()

    class FakeDaytona:
        def __init__(self, config=None):
            self.config = config

        def create(self):
            return FakeSandbox()

        def delete(self, sandbox):
            return None

    class FakeModule:
        Daytona = FakeDaytona
        DaytonaConfig = None

    monkeypatch.setenv("DAYTONA_API_KEY", "test")
    monkeypatch.setattr(
        daytona,
        "import_provider_module",
        lambda *args: FakeModule,
    )

    payload = run_provider_comparison(
        providers=["daytona"],
        iterations=1,
        mode="provider-live",
        warmup_iterations=0,
    )

    cases = payload["providers"]["daytona"]["cases"]
    product_case = cases["product_create_to_first_screenshot"]
    assert product_case["status"] == "ok"
    assert product_case["readiness_contract"].startswith("daytona.create")
    assert product_case["uses_snapshot_or_template"] is True
    assert product_case["first_observation_api"] == "computer_use.screenshot.take_full_screen"
    assert cases["cold_create_to_ready"]["canonical_case"] == "product_create_to_first_screenshot"
    assert cases["cold_create_to_ready"]["deprecated"] is True
    command_case = cases["command_nonlogin_shell_echo"]
    assert command_case["benchmark_semantics"] == "shell-command-echo-v2"
    assert command_case["shell_mode"] == "non_login"
    assert command_case["command"] == {
        "argv": ["sh", "-c", "printf '42\\n'"],
        "timeout_seconds": 30,
        "transport_shape": "command_string",
    }


def test_modal_product_readiness_uses_all_orchestration_samples() -> None:
    case = daemon_surface._modal_product_create_to_first_screenshot_case(
        {
            "modal_product_create_to_first_screenshot_samples_ms": [1200.0, 1100.0, 1000.0]
        }
    )

    assert case["iterations"] == 3
    assert case["successful_iterations"] == 3
    assert case["samples_ms"] == [1200.0, 1100.0, 1000.0]


def test_created_modal_comparison_collects_one_cold_sample_per_iteration(monkeypatch) -> None:
    computers = []
    seen_metadata = {}

    class FakeComputer:
        def __init__(self, index: int) -> None:
            self.index = index
            self.client = SimpleNamespace(base_url="https://example.invalid")
            self.terminate_calls = 0
            self.detach_calls = 0

        def terminate(self, *, wait: bool = False) -> None:
            assert wait is True
            self.terminate_calls += 1

        def detach(self) -> None:
            self.detach_calls += 1

    def fake_create(*args, **kwargs):
        computer = FakeComputer(len(computers))
        computers.append(computer)
        return computer, {
            "modal_cold_create_to_ready_ms": 1000.0 + computer.index,
            "action_case_pacing_ms": 1050,
        }

    def fake_run_provider_comparison(**kwargs):
        seen_metadata.update(kwargs["environment_metadata"])
        assert kwargs["modal_action_pacing_seconds"] == 1.05
        assert kwargs["client"] is computers[-1].client
        assert [computer.terminate_calls for computer in computers] == [1, 1, 1, 0]
        assert [computer.detach_calls for computer in computers] == [1, 1, 1, 0]
        return {
            "ok": True,
            "providers": {
                "modal-daemon": {
                    "status": "ok",
                    "metadata": {"environment": dict(kwargs["environment_metadata"])},
                    "cases": {},
                    "failures": [],
                }
            },
            "failures": [],
        }

    monkeypatch.setattr(cli, "new_run_id", lambda: f"run-{len(computers)}")
    monkeypatch.setattr(cli, "_modal_benchmark_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_create_modal_benchmark_computer", fake_create)
    monkeypatch.setattr(cli, "run_provider_comparison", fake_run_provider_comparison)

    result = cli._benchmark_compare_created_modal_sandbox(
        SimpleNamespace(iterations=3, app_name="test-app", name=None),
        providers=["modal-daemon"],
        sandbox_exec_runner=None,
        sandbox_exec_setup_failure=None,
    )

    assert result["ok"] is True
    assert len(seen_metadata["modal_product_create_to_first_screenshot_samples_ms"]) == 3
    assert seen_metadata["modal_product_create_to_first_screenshot_expected_samples"] == 3
    assert len(computers) == 4
    assert [computer.terminate_calls for computer in computers] == [1, 1, 1, 1]
    assert [computer.detach_calls for computer in computers] == [1, 1, 1, 1]
    assert (
        result["providers"]["modal-daemon"]["cost_estimate"]["inputs"]["duration_seconds"]
        > 0
    )


def test_provider_compare_modal_exec_uses_sdk_sandbox_exec_surface() -> None:
    class FakeProcess:
        returncode = 0

        def wait(self) -> None:
            return None

    calls = []

    def run(command: tuple[str, ...], timeout: int) -> FakeProcess:
        calls.append((command, timeout))
        return FakeProcess()

    payload = run_provider_comparison(
        providers=["modal-exec"],
        iterations=1,
        mode="provider-live",
        sandbox_exec_runner=run,
    )

    provider = payload["providers"]["modal-exec"]
    assert payload["ok"] is True
    assert provider["status"] == "ok"
    assert provider["metadata"]["canonical_source"] == "benchmark sdk surface"
    assert provider["metadata"]["provider_surface"] == "sandbox-exec"
    assert provider["cases"]["sandbox_exec_move_click"]["status"] == "ok"
    assert len(calls) == 2


def test_benchmark_compare_env_file_must_exist(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "compare",
                "--providers",
                "daytona",
                "--env-file",
                "/tmp/missing-provider-benchmark-env",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--env-file must point to an existing file" in captured.err


def test_lifecycle_measurement_excludes_cleanup_time(monkeypatch) -> None:
    ticks = iter([0.0, 1.0, 11.0])
    cleaned = []
    monkeypatch.setattr(benchmark_lifecycle.time, "perf_counter", lambda: next(ticks))

    measurement = benchmark_lifecycle.measure_create_to_first_observation(
        name="product_create_to_first_screenshot",
        iterations=1,
        warmup_iterations=0,
        create=lambda: "sandbox",
        observe=lambda sandbox: {"sandbox": sandbox},
        cleanup=lambda sandbox: cleaned.append(sandbox) or [],
    )

    assert measurement.samples_ms == [1000.0]
    assert measurement.completed_runtime_seconds == 11.0
    assert cleaned == ["sandbox"]


def test_lifecycle_cleanup_runs_after_observation_failure() -> None:
    cleaned = []

    measurement = benchmark_lifecycle.measure_create_to_first_observation(
        name="product_create_to_first_screenshot",
        iterations=1,
        warmup_iterations=0,
        create=lambda: "sandbox",
        observe=lambda _sandbox: (_ for _ in ()).throw(RuntimeError("not ready")),
        cleanup=lambda sandbox: cleaned.append(sandbox) or [],
    )

    assert measurement.samples_ms == []
    assert measurement.failures[0]["message"] == "not ready"
    assert cleaned == ["sandbox"]


def test_lifecycle_cleanup_exception_is_recorded_without_replacing_timing() -> None:
    measurement = benchmark_lifecycle.measure_create_to_first_observation(
        name="product_create_to_first_screenshot",
        iterations=1,
        warmup_iterations=0,
        create=lambda: "sandbox",
        observe=lambda _sandbox: {"status": "ready"},
        cleanup=lambda _sandbox: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    assert len(measurement.samples_ms) == 1
    assert measurement.failures == []
    assert measurement.cleanup_errors[0][0] == "cleanup"


def test_provider_case_failures_are_not_copied_to_later_cases() -> None:
    class Benchmark:
        def create_lifecycle_session(self):
            return object()

        def observe_first_screenshot(self, _sandbox):
            return {"status": "ready"}

        def cleanup_session(self, _sandbox):
            return []

        def first(self, _sandbox):
            raise RuntimeError("first failed")

        def second(self, _sandbox):
            return {"status": "ok"}

    result = live.run_product_provider_cases(
        provider="daytona",
        driver=Benchmark(),
        cold_cases=(),
        warm_cases=("first", "second"),
        iterations=1,
        warmup_iterations=0,
        metadata={},
    )

    assert result["cases"]["first"]["status"] == "failed"
    assert result["cases"]["second"]["status"] == "ok"
    assert result["cases"]["second"]["failures"] == []
    assert len(result["failures"]) == 1


def test_live_provider_runs_verification_before_cleanup_after_case_failure() -> None:
    events: list[str] = []

    class Benchmark:
        def create_lifecycle_session(self):
            events.append("create")
            return object()

        def observe_first_screenshot(self, _sandbox):
            events.append("observe")
            return {"status": "ready"}

        def first(self, _sandbox):
            events.append("first")
            raise RuntimeError("first failed")

        def second(self, _sandbox):
            events.append("second")
            return {"status": "ok"}

        def verify_readbacks(self, _sandbox):
            events.append("verify")
            return {"cursor_position": {"status": "ok"}}

        def cleanup_session(self, _sandbox):
            events.append("cleanup")
            return []

    result = live.run_product_provider_cases(
        provider="daytona",
        driver=Benchmark(),
        cold_cases=(),
        warm_cases=("first", "second"),
        iterations=1,
        warmup_iterations=0,
        metadata={},
    )

    assert events == ["create", "observe", "first", "second", "verify", "cleanup"]
    assert result["cases"]["first"]["status"] == "failed"
    assert result["cases"]["second"]["status"] == "ok"
    assert result["verification"]["cursor_position"]["status"] == "ok"


def test_provider_cleanup_tries_force_before_next_method() -> None:
    calls: list[tuple[str, bool]] = []

    class Sandbox:
        def delete(self, *, force: bool = False) -> None:
            calls.append(("delete", force))
            raise RuntimeError("delete failed")

        def kill(self, *, force: bool = False) -> None:
            calls.append(("kill", force))
            if not force:
                raise RuntimeError("kill failed")

    errors = live.cleanup_provider_sandbox(Sandbox())

    assert errors == []
    assert calls == [
        ("delete", False),
        ("delete", True),
        ("kill", False),
        ("kill", True),
    ]


def test_provider_runtime_and_cleanup_postprocessing_updates_comparison() -> None:
    payload = {
        "ok": True,
        "providers": {
            "modal-daemon": {
                "status": "ok",
                "metadata": {"environment": {"cpu": 2.0, "memory_mib": 2048}},
                "cases": {},
                "failures": [],
            }
        },
        "failures": [],
    }

    results.record_provider_runtime(
        payload,
        provider="modal-daemon",
        runtime_seconds=12.5,
    )
    results.record_provider_cleanup_errors(
        payload,
        provider="modal-daemon",
        errors=[("terminate", RuntimeError("cleanup failed"))],
    )

    provider = payload["providers"]["modal-daemon"]
    assert provider["metadata"]["environment"]["measured_resource_runtime_seconds"] == 12.5
    assert provider["cost_estimate"]["inputs"]["duration_seconds"] == 12.5
    assert provider["status"] == "failed"
    assert provider["cases"]["cleanup"]["status"] == "failed"
    assert payload["ok"] is False
    assert payload["failures"][0]["benchmark"] == "modal-daemon"


def test_provider_observation_redacts_nested_secrets_and_preserves_safe_shape() -> None:
    observation = {
        "status": "ready",
        "nested": {
            "accessToken": "credential-value",
            "endpointUrl": "https://user:password@example.com/path?token=secret",
            "count": 3,
        },
    }

    safe = provider_sdk.sanitize_provider_observation(observation)

    assert safe == {
        "status": "ready",
        "nested": {
            "accessToken": {"redacted": True, "length": 16},
            "endpointUrl": {"redacted": True, "length": 51},
            "count": 3,
        },
    }


def test_modal_cleanup_detaches_even_when_terminate_fails() -> None:
    calls = []

    class Client:
        def close(self) -> None:
            calls.append("close")

    class Computer:
        client = Client()

        def terminate(self, *, wait: bool = False) -> None:
            assert wait is True
            calls.append("terminate")
            raise RuntimeError("terminate failed")

        def detach(self) -> None:
            calls.append("detach")

    errors = cli._cleanup_modal_benchmark_computer(Computer())

    assert calls == ["terminate", "detach", "close"]
    assert [method for method, _exc in errors] == ["terminate"]


def test_provider_verification_failure_fails_provider_and_top_level(monkeypatch) -> None:
    def fake_provider(*args, **kwargs):
        return results.build_provider_result(
            "daytona",
            cases={"screenshot_full": {"status": "ok", "failures": []}},
            verification={"cursor_position": {"status": "failed"}},
        )

    monkeypatch.setattr(comparison, "run_provider", fake_provider)

    payload = run_provider_comparison(providers=["daytona"], iterations=1)

    assert payload["ok"] is False
    assert payload["providers"]["daytona"]["status"] == "failed"
    assert payload["failures"][0]["case"] == "verification.cursor_position"


def test_deprecated_cold_alias_does_not_duplicate_failures() -> None:
    failure = {
        "case": "product_create_to_first_screenshot",
        "phase": "measure",
        "iteration": 0,
        "type": "RuntimeError",
        "message": "failed",
    }
    canonical = {"status": "failed", "failures": [failure]}
    alias = {
        **canonical,
        "deprecated": True,
        "canonical_case": "product_create_to_first_screenshot",
    }

    result = results.build_provider_result(
        "daytona",
        cases={
            "product_create_to_first_screenshot": canonical,
            "cold_create_to_ready": alias,
        },
    )

    assert len(result["failures"]) == 1


def test_provider_payload_metadata_distinguishes_base64_transport_from_png_bytes() -> None:
    buffer = BytesIO()
    Image.new("RGB", (2, 3), color="red").save(buffer, format="PNG")
    png = buffer.getvalue()
    response = SimpleNamespace(
        screenshot=SimpleNamespace(base64_string=__import__("base64").b64encode(png).decode())
    )

    metadata = payloads.describe_screenshot_payload(response)

    assert metadata["source"].endswith("screenshot.base64_string")
    assert metadata["transport_encoding"] == "base64_string"
    assert metadata["transport_size_bytes"] > metadata["decoded_size_bytes"]
    assert metadata["decoded_size_bytes"] == len(png)
    assert metadata["format"] == "png"
    assert metadata["width"] == 2
    assert metadata["height"] == 3


def test_provider_payload_rejects_image_with_truncated_pixel_data() -> None:
    buffer = BytesIO()
    Image.new("RGB", (64, 64), color="red").save(buffer, format="PNG")
    truncated_png = buffer.getvalue()[:64]

    with pytest.raises(ValueError, match="fully decoded"):
        payloads.describe_screenshot_payload(truncated_png)


def test_provider_screenshot_validation_rejects_non_image_text() -> None:
    metadata = payloads.describe_screenshot_payload("not-an-image")

    with pytest.raises(RuntimeError, match="decoded image"):
        payloads.validated_screenshot_size(metadata, provider="Example")


def test_modal_product_readiness_rejects_partial_sample_metadata() -> None:
    case = daemon_surface._modal_product_create_to_first_screenshot_case(
        {
            "modal_product_create_to_first_screenshot_samples_ms": [1000.0, 900.0],
            "modal_product_create_to_first_screenshot_expected_samples": 3,
        },
    )

    assert case["status"] == "failed"
    assert case["successful_iterations"] == 2
    assert "expected 3 lifecycle samples" in case["failures"][0]["message"]
