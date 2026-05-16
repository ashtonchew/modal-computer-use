from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import modal_computer_use.benchmarks.billing as benchmark_billing
import modal_computer_use.benchmarks.daemon_surface as benchmark_daemon_surface
import modal_computer_use.benchmarks.surfaces as benchmark_surfaces
from modal_computer_use import cli
from modal_computer_use.benchmarks import (
    TYPING_BENCHMARK_TEXT,
    run_sdk_surface_benchmark,
    run_sdk_surface_benchmark_mock_local,
)


def test_benchmark_sdk_mock_local_outputs_json(capsys) -> None:
    exit_code = cli.main(["benchmark", "sdk", "--mock-local", "--iterations", "1"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["benchmark"] == "sdk-surfaces"
    assert payload["mode"] == "mock-local"
    assert payload["surfaces"]["daemon-http"]["status"] == "ok"
    assert payload["surfaces"]["daemon-http"]["cases"]["command_echo"]["status"] == "ok"
    assert payload["surfaces"]["daemon-http"]["cases"]["move_click_sequence"]["status"] == "ok"
    assert payload["surfaces"]["daemon-http"]["cases"]["type_1000_chars"]["status"] == "ok"
    assert payload["surfaces"]["openai-adapter"]["metadata"]["provider_api_calls"] is False
    assert (
        payload["surfaces"]["anthropic-adapter"]["metadata"]["tool_version"]
        == "computer_20250124"
    )
    assert payload["surfaces"]["action-executor"]["metadata"]["executor"] == "ActionExecutor"
    assert payload["surfaces"]["openai-adapter"]["cost_estimate"]["status"] == "not_applicable"
    assert "0123456789" not in captured.out
    assert '"text"' not in captured.out
    assert "Bearer" not in captured.out

def test_benchmark_sdk_modal_billing_tag_must_be_key_value(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "sdk",
                "--mock-local",
                "--surfaces",
                "daemon-http",
                "--modal-billing-reconcile",
                "--modal-billing-start",
                "2026-05-13T01:00:00Z",
                "--modal-billing-tag",
                "not-a-pair",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--modal-billing-tag must be key=value" in captured.err

def test_benchmark_sdk_modal_billing_default_end_resolves_during_reconciliation(
    monkeypatch,
) -> None:
    seen = {}

    def fake_reconcile(metadata):
        seen["request"] = metadata["modal_billing_reconciliation"]
        return {"status": "not_available_yet"}

    monkeypatch.setattr(
        benchmark_daemon_surface,
        "reconcile_modal_billing_from_metadata",
        fake_reconcile,
    )

    payload = run_sdk_surface_benchmark_mock_local(
        surfaces=["daemon-http"],
        iterations=1,
        environment_metadata={
            "modal_billing_reconciliation": benchmark_billing.modal_billing_reconciliation_request(
                start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                end=None,
                required_tags={"benchmark_run_id": "sdk_surface_test"},
            )
        },
    )

    assert payload["surfaces"]["daemon-http"]["billing_reconciliation"]["status"] == (
        "not_available_yet"
    )
    assert seen["request"]["end"] is None

def test_daemon_http_surface_attaches_billing_reconciliation_separately(monkeypatch) -> None:
    reconciliation = {
        "status": "matched",
        "source": "modal.billing.workspace_billing_report",
        "total": {"amount": 0.01, "unit": "report_window"},
    }
    monkeypatch.setattr(
        benchmark_daemon_surface,
        "reconcile_modal_billing_from_metadata",
        lambda metadata: reconciliation,
    )

    payload = run_sdk_surface_benchmark_mock_local(
        surfaces=["daemon-http"],
        iterations=1,
        environment_metadata={
            "modal_billing_reconciliation": {"start": "2026-05-13T01:00:00Z"},
        },
    )

    surface = payload["surfaces"]["daemon-http"]
    assert surface["billing_reconciliation"] == reconciliation
    assert surface["cost_estimate"]["status"] == "unknown"

def test_benchmark_sdk_invalid_comma_surface_uses_argparse_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "sdk", "--surfaces", "daemon-http,nope"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid benchmark surface: nope" in captured.err

def test_benchmark_sdk_mock_local_preserves_sandbox_exec_runner() -> None:
    class FakeProcess:
        returncode = 0

        def wait(self) -> None:
            return None

    calls = []

    def run(command: tuple[str, ...], timeout: int) -> FakeProcess:
        calls.append((command, timeout))
        return FakeProcess()

    payload = run_sdk_surface_benchmark_mock_local(
        surfaces=["sandbox-exec"],
        iterations=1,
        sandbox_exec_runner=run,
    )

    assert payload["ok"] is True
    assert payload["surfaces"]["sandbox-exec"]["status"] == "ok"
    assert (
        payload["surfaces"]["sandbox-exec"]["cases"]["sandbox_exec_move_click"]["status"]
        == "ok"
    )
    assert len(calls) == 2

def test_benchmark_sdk_requires_daemon_http_target(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "sdk", "--surface", "daemon-http"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert (
        "daemon-http surface benchmark requires --mock-local, --base-url, "
        "or --create-modal-sandbox"
    ) in captured.err

def test_benchmark_sdk_structures_and_redacts_surface_failures(monkeypatch) -> None:
    def fail_surface(**kwargs):
        raise RuntimeError(
            f"Authorization: Bearer secret-token {TYPING_BENCHMARK_TEXT} "
            "https://user:secret@example.com/vnc.html?password=secret "
            '{"apiKey":"secret","clientSecret":"secret"}'
        )

    monkeypatch.setattr(benchmark_surfaces, "_run_adapter_surface", fail_surface)

    payload = run_sdk_surface_benchmark(surfaces=["openai-adapter"], iterations=1)
    serialized = json.dumps(payload)

    assert payload["ok"] is False
    assert payload["surfaces"]["openai-adapter"]["status"] == "failed"
    assert "secret-token" not in serialized
    assert "user:secret" not in serialized
    assert TYPING_BENCHMARK_TEXT not in serialized
    assert "password=secret" not in serialized
    assert '"secret"' not in serialized
    assert "[redacted typed text]" in serialized

def test_benchmark_sdk_can_create_gpu_modal_sandbox(monkeypatch, capsys) -> None:
    created: dict[str, object] = {}
    closed: list[str] = []

    class CreatedComputer:
        client = object()

        def metadata(self):
            return SimpleNamespace(sandbox_id="sb-gpu")

        def terminate(self) -> None:
            closed.append("terminate")

        def detach(self) -> None:
            closed.append("detach")

    def fake_create(**kwargs):
        created.update(kwargs)
        return CreatedComputer()

    def fake_run_sdk_surface_benchmark(**kwargs):
        environment = kwargs["environment_metadata"]
        return {
            "ok": True,
            "benchmark": "sdk-surfaces",
            "mode": kwargs["mode"],
            "surfaces": {
                "daemon-http": {
                    "metadata": {"environment": environment},
                    "status": "ok",
                    "cases": {},
                    "failures": [],
                }
            },
            "failures": [],
        }

    monkeypatch.setattr(cli.ComputerSandbox, "create", staticmethod(fake_create))
    monkeypatch.setattr(cli, "run_sdk_surface_benchmark", fake_run_sdk_surface_benchmark)
    monkeypatch.setattr(cli, "new_run_id", lambda: "sdk_surface_test")

    exit_code = cli.main(
        [
            "benchmark",
            "sdk",
            "--create-modal-sandbox",
            "--surfaces",
            "daemon-http",
            "--gpu",
            "T4",
            "--browser",
            "chromium",
            "--modal-cpu",
            "2",
            "--modal-memory-mib",
            "4096",
            "--iterations",
            "1",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    config = created["config"]
    assert exit_code == 0
    assert created["app_tags"] == {
        "benchmark": "sdk-surfaces",
        "benchmark_run_id": "sdk_surface_test",
    }
    assert created["tags"] == {
        "benchmark": "sdk-surfaces",
        "benchmark_run_id": "sdk_surface_test",
        "surface": "daemon-http",
    }
    assert config.resources.profile == "browser-gpu"
    assert config.resources.gpu == "T4"
    assert config.resources.cpu == 2
    assert config.resources.memory_mib == 4096
    assert config.browser.kind == "chromium"
    assert config.browser.gpu_mode is None
    environment = payload["surfaces"]["daemon-http"]["metadata"]["environment"]
    assert environment["gpu"] == "T4"
    assert environment["modal_cpu_count"] == 2
    assert environment["modal_memory_gib"] == 4
    assert environment["modal_sandbox_id"] == "sb-gpu"
    assert environment["modal_cold_create_to_ready_ms"] > 0
    assert closed == ["terminate", "detach"]

def test_benchmark_sdk_create_modal_sandbox_requires_daemon_http(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "sdk",
                "--create-modal-sandbox",
                "--surfaces",
                "openai-adapter",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--create-modal-sandbox requires surface daemon-http" in captured.err
