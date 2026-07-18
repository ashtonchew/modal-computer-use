from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmark_comparison import run_provider_comparison
from modal_computer_use.benchmarks import daemon_surface


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


def test_benchmark_compare_external_providers_skip_without_credentials(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        ["benchmark", "compare", "--providers", "daytona,e2b", "--iterations", "1"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["providers"]["daytona"]["status"] == "not_measured"
    assert payload["providers"]["e2b"]["status"] == "not_measured"
    assert "DAYTONA_API_KEY is not set" in captured.out
    assert "E2B_API_KEY is not set" in captured.out


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
                return b"png"

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
                return {"exit_code": 0, "stdout": "42"}

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
        "modal_computer_use.benchmark_comparison._import_provider_module",
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

        def terminate(self) -> None:
            self.terminate_calls += 1

        def detach(self) -> None:
            self.detach_calls += 1

    def fake_create(*args, **kwargs):
        computer = FakeComputer(len(computers))
        computers.append(computer)
        return computer, {"modal_cold_create_to_ready_ms": 1000.0 + computer.index}

    def fake_run_provider_comparison(**kwargs):
        seen_metadata.update(kwargs["environment_metadata"])
        return {"ok": True}

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

    assert result == {"ok": True}
    assert seen_metadata["modal_product_create_to_first_screenshot_samples_ms"] == [
        1000.0,
        1001.0,
        1002.0,
    ]
    assert len(computers) == 3
    assert [computer.terminate_calls for computer in computers] == [1, 1, 1]
    assert [computer.detach_calls for computer in computers] == [1, 1, 1]


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
