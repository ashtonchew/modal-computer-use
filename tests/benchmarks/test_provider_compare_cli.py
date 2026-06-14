from __future__ import annotations

import json

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmark_comparison import run_provider_comparison


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
