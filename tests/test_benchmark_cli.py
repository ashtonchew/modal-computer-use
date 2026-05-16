from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import modal_computer_use.benchmark_billing as benchmark_billing
import modal_computer_use.benchmark_comparison as benchmark_comparison
from modal_computer_use import cli
from modal_computer_use.benchmark_costs import estimate_provider_cost
from modal_computer_use.benchmarks import (
    TYPE_1000_CHARS_TEXT,
    TYPE_1000_CHARS_TIMEOUT_MS,
    TYPING_BENCHMARK_TEXT,
    run_action_batch_benchmark,
    run_benchmark_report,
    run_move_click_sequence_benchmark,
    run_provider_comparison,
    run_provider_comparison_mock_local,
    run_sandbox_exec_benchmark,
    run_type_100_chars_benchmark,
    run_type_1000_chars_benchmark,
)
from modal_computer_use.errors import ModalNotInstalledError


def test_benchmark_action_batch_mock_local_outputs_json(capsys) -> None:
    exit_code = cli.main(["benchmark", "action-batch", "--mock-local", "--iterations", "2"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["benchmark"] == "action-batch"
    assert payload["mode"] == "mock-local"
    assert payload["iterations"] == 2
    assert payload["action_count"] == 5
    assert len(payload["cases"]["batch_5_actions"]["samples_ms"]) == 2
    assert len(payload["cases"]["separate_5_actions"]["samples_ms"]) == 2
    assert payload["cases"]["batch_5_actions"]["summary_ms"]["mean"] is not None
    assert payload["cases"]["separate_5_actions"]["summary_ms"]["p95"] is not None
    assert payload["cases"]["sandbox_exec"]["status"] == "not_measured"
    assert payload["comparison"]["status"] == "measured"
    assert "hello" not in captured.out
    assert "secret" not in captured.out.lower()
    assert '"text"' not in captured.out


def test_benchmark_action_batch_invalid_iterations_rejected(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "action-batch", "--mock-local", "--iterations", "0"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "must be >= 1" in captured.err


def test_benchmark_action_batch_requires_one_mode(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "action-batch"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "one of the arguments" in captured.err


def test_benchmark_compare_mock_local_outputs_json(capsys) -> None:
    exit_code = cli.main(["benchmark", "compare", "--mock-local", "--iterations", "1"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["benchmark"] == "provider-compare"
    assert payload["mode"] == "mock-local"
    assert payload["providers"]["modal-daemon"]["status"] == "ok"
    assert payload["providers"]["modal-daemon"]["cases"]["command_echo"]["status"] == "ok"
    assert payload["providers"]["modal-daemon"]["cases"]["move_click_sequence"]["status"] == "ok"
    assert payload["providers"]["modal-daemon"]["cases"]["type_1000_chars"]["status"] == "ok"
    assert payload["providers"]["openai"]["metadata"]["provider_api_calls"] is False
    assert payload["providers"]["anthropic"]["metadata"]["tool_version"] == "computer_20250124"
    assert payload["providers"]["generic"]["metadata"]["adapter"] == "ActionExecutor"
    assert payload["providers"]["openai"]["cost_estimate"]["status"] == "not_applicable"
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
    assert payload["providers"]["daytona"]["cost_estimate"]["status"] == "not_measured"
    assert payload["providers"]["e2b"]["cost_estimate"]["status"] == "not_measured"
    assert "DAYTONA_API_KEY is not set" in captured.out
    assert "E2B_API_KEY is not set" in captured.out


def test_provider_cost_estimate_is_partial_without_unknown_resource_zeroes() -> None:
    estimate = estimate_provider_cost(
        "e2b",
        provider_status="ok",
        runtime_seconds=10.0,
        metadata={"cpu_count": 2},
    )

    assert estimate["status"] == "partial"
    assert estimate["inputs"]["cpu_count"] == 2.0
    assert estimate["inputs"]["memory_gib"] is None
    assert len(estimate["components"]) == 1
    assert estimate["components"][0]["amount"] == pytest.approx(0.00028)
    assert estimate["total"]["amount"] == pytest.approx(
        sum(component["amount"] for component in estimate["components"])
    )
    assert "memory allocation was unavailable" in estimate["notes"]


def test_daytona_default_resources_produce_estimated_cost() -> None:
    estimate = estimate_provider_cost(
        "daytona",
        provider_status="ok",
        runtime_seconds=10.0,
        metadata=benchmark_comparison._daytona_default_resource_metadata(),
    )

    assert estimate["status"] == "estimated"
    assert estimate["inputs"] == {
        "duration_seconds": 10.0,
        "cpu_count": 1.0,
        "memory_gib": 1.0,
        "storage_gib": 3.0,
    }
    assert [component["resource"] for component in estimate["components"]] == [
        "cpu",
        "memory",
        "storage",
    ]
    assert estimate["total"]["amount"] == pytest.approx(
        sum(component["amount"] for component in estimate["components"])
    )


def test_modal_cost_estimate_requires_explicit_resources() -> None:
    partial = estimate_provider_cost(
        "modal-daemon",
        provider_status="ok",
        runtime_seconds=10.0,
        metadata={"environment": {"modal_cold_create_to_ready_ms": 10000}},
    )
    estimated = estimate_provider_cost(
        "modal-daemon",
        provider_status="ok",
        runtime_seconds=10.0,
        metadata={
            "environment": {
                "modal_cpu_count": 0.125,
                "modal_memory_gib": 0.125,
            }
        },
    )

    assert partial["status"] == "partial"
    assert "cpu allocation was unavailable" in partial["notes"]
    assert "memory allocation was unavailable" in partial["notes"]
    assert estimated["status"] == "estimated"
    assert estimated["inputs"]["cpu_count"] == 0.125
    assert estimated["inputs"]["memory_gib"] == 0.125
    assert estimated["total"]["amount"] == pytest.approx(
        sum(component["amount"] for component in estimated["components"])
    )


def test_modal_billing_reconciliation_filters_and_sums_tagged_rows() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
        end=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
        required_tags={
            "benchmark": "provider-compare",
            "benchmark_run_id": "provider_compare_test",
            "provider": "modal-daemon",
        },
    )

    def report_loader(start, end, resolution, tag_names):
        assert start == datetime(2026, 5, 13, 1, 0, tzinfo=UTC)
        assert end == datetime(2026, 5, 13, 2, 0, tzinfo=UTC)
        assert resolution == "h"
        assert tag_names == ["benchmark", "benchmark_run_id", "provider"]
        return [
            {
                "cost": Decimal("0.12"),
                "interval_start": datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                "tags": {
                    "benchmark": "provider-compare",
                    "benchmark_run_id": "provider_compare_test",
                    "provider": "modal-daemon",
                },
                "object_id": "secret-object-id",
            },
            {
                "cost": Decimal("9.99"),
                "interval_start": datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                "tags": {
                    "benchmark": "provider-compare",
                    "benchmark_run_id": "other",
                    "provider": "modal-daemon",
                },
            },
        ]

    result = benchmark_billing.reconcile_modal_billing(request, report_loader=report_loader)
    serialized = json.dumps(result)

    assert result["status"] == "matched"
    assert result["matched_row_count"] == 1
    assert result["row_count"] == 2
    assert result["total"]["amount"] == pytest.approx(0.12)
    assert "secret-object-id" not in serialized


def test_modal_billing_reconciliation_handles_unavailable_and_pending() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
        end=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
        required_tags={"benchmark_run_id": "provider_compare_test"},
    )

    unavailable = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: (_ for _ in ()).throw(ImportError("no modal")),
    )
    pending = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: [],
    )

    assert unavailable["status"] == "unavailable"
    assert pending["status"] == "not_available_yet"


def test_modal_billing_reconciliation_distinguishes_tag_mismatch() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 15, tzinfo=UTC),
        end=datetime(2026, 5, 13, 1, 45, tzinfo=UTC),
        required_tags={"benchmark_run_id": "provider_compare_test"},
    )

    result = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: [
            {
                "cost": Decimal("0.12"),
                "interval_start": datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                "tags": {"provider": "modal-daemon"},
            }
        ],
    )

    assert result["status"] == "no_matching_tags"
    assert result["row_count"] == 1
    assert "full intervals" in " ".join(result["notes"])


def test_modal_billing_reconciliation_treats_modal_missing_as_unavailable() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
        end=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
        required_tags={"benchmark_run_id": "provider_compare_test"},
    )

    result = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: (_ for _ in ()).throw(
            ModalNotInstalledError("modal not installed")
        ),
    )

    assert result["status"] == "unavailable"


def test_benchmark_compare_modal_billing_tag_must_be_key_value(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "compare",
                "--mock-local",
                "--providers",
                "modal-daemon",
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


def test_benchmark_compare_modal_billing_default_end_resolves_during_reconciliation(
    monkeypatch,
) -> None:
    seen = {}

    def fake_reconcile(metadata):
        seen["request"] = metadata["modal_billing_reconciliation"]
        return {"status": "not_available_yet"}

    monkeypatch.setattr(
        benchmark_comparison,
        "reconcile_modal_billing_from_metadata",
        fake_reconcile,
    )

    payload = run_provider_comparison_mock_local(
        providers=["modal-daemon"],
        iterations=1,
        environment_metadata={
            "modal_billing_reconciliation": benchmark_billing.modal_billing_reconciliation_request(
                start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                end=None,
                required_tags={"benchmark_run_id": "provider_compare_test"},
            )
        },
    )

    assert payload["providers"]["modal-daemon"]["billing_reconciliation"]["status"] == (
        "not_available_yet"
    )
    assert seen["request"]["end"] is None


def test_modal_daemon_provider_attaches_billing_reconciliation_separately(monkeypatch) -> None:
    reconciliation = {
        "status": "matched",
        "source": "modal.billing.workspace_billing_report",
        "total": {"amount": 0.01, "unit": "report_window"},
    }
    monkeypatch.setattr(
        benchmark_comparison,
        "reconcile_modal_billing_from_metadata",
        lambda metadata: reconciliation,
    )

    payload = run_provider_comparison_mock_local(
        providers=["modal-daemon"],
        iterations=1,
        environment_metadata={
            "modal_billing_reconciliation": {"start": "2026-05-13T01:00:00Z"},
        },
    )

    provider = payload["providers"]["modal-daemon"]
    assert provider["billing_reconciliation"] == reconciliation
    assert provider["cost_estimate"]["status"] == "unknown"


def test_benchmark_compare_live_external_providers_loads_cwd_dotenv(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    tmp_path.joinpath(".env").write_text(
        "DAYTONA_API_KEY=daytona-from-dotenv\nE2B_API_KEY=e2b-from-dotenv\n",
        encoding="utf-8",
    )

    def missing_sdk(*args, **kwargs):
        raise ImportError("missing sdk")

    monkeypatch.setattr(benchmark_comparison, "_import_provider_module", missing_sdk)

    exit_code = cli.main(
        ["benchmark", "compare", "--providers", "daytona,e2b", "--iterations", "1"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    serialized = json.dumps(payload)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["providers"]["daytona"]["status"] == "unavailable"
    assert payload["providers"]["e2b"]["status"] == "unavailable"
    assert "DAYTONA_API_KEY is not set" not in serialized
    assert "E2B_API_KEY is not set" not in serialized
    assert "daytona-from-dotenv" not in serialized
    assert "e2b-from-dotenv" not in serialized


def test_benchmark_compare_env_file_does_not_override_existing_env(
    monkeypatch, tmp_path, capsys
) -> None:
    env_file = tmp_path / "provider.env"
    env_file.write_text("E2B_API_KEY=e2b-from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("E2B_API_KEY", "e2b-from-shell")

    def missing_sdk(*args, **kwargs):
        raise ImportError("missing sdk")

    monkeypatch.setattr(benchmark_comparison, "_import_provider_module", missing_sdk)

    exit_code = cli.main(
        [
            "benchmark",
            "compare",
            "--providers",
            "e2b",
            "--iterations",
            "1",
            "--env-file",
            str(env_file),
        ]
    )

    captured = capsys.readouterr()
    serialized = captured.out
    assert exit_code == 0
    assert "install the bench-e2b extra" in serialized
    assert os.environ["E2B_API_KEY"] == "e2b-from-shell"
    assert "e2b-from-shell" not in serialized
    assert "e2b-from-dotenv" not in serialized


def test_benchmark_compare_env_file_loads_only_provider_keys(monkeypatch, tmp_path, capsys) -> None:
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "E2B_API_KEY=e2b-from-dotenv\nMODAL_CONFIG_PATH=/tmp/should-not-load\nE2B_TEMPLATE=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_TEMPLATE", raising=False)
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)

    def missing_sdk(*args, **kwargs):
        raise ImportError("missing sdk")

    monkeypatch.setattr(benchmark_comparison, "_import_provider_module", missing_sdk)

    exit_code = cli.main(
        [
            "benchmark",
            "compare",
            "--providers",
            "e2b",
            "--iterations",
            "1",
            "--env-file",
            str(env_file),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "install the bench-e2b extra" in captured.out
    assert os.environ["E2B_API_KEY"] == "e2b-from-dotenv"
    assert "E2B_TEMPLATE" not in os.environ
    assert "MODAL_CONFIG_PATH" not in os.environ
    assert "/tmp/should-not-load" not in captured.out


def test_benchmark_compare_env_file_must_exist(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "compare",
                "--providers",
                "e2b",
                "--env-file",
                "/tmp/does-not-exist-provider.env",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--env-file must point to an existing file" in captured.err


def test_benchmark_compare_invalid_comma_provider_uses_argparse_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "compare", "--providers", "e2b,nope"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid provider: nope" in captured.err


def test_benchmark_compare_mock_local_never_loads_live_external_providers(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DAYTONA_API_KEY", "daytona-secret")
    monkeypatch.setenv("E2B_API_KEY", "e2b-secret")

    def fail_import(*args, **kwargs):
        raise AssertionError("mock-local should not import live provider SDKs")

    monkeypatch.setattr(benchmark_comparison, "_import_provider_module", fail_import)

    exit_code = cli.main(
        [
            "benchmark",
            "compare",
            "--mock-local",
            "--providers",
            "daytona,e2b",
            "--iterations",
            "1",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    serialized = json.dumps(payload)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["providers"]["daytona"]["status"] == "not_measured"
    assert payload["providers"]["e2b"]["status"] == "not_measured"
    assert "disabled in mock-local mode" in serialized
    assert "secret" not in serialized


def test_benchmark_compare_mock_local_preserves_modal_exec_runner() -> None:
    class FakeProcess:
        returncode = 0

        def wait(self) -> None:
            return None

    calls = []

    def run(command: tuple[str, ...], timeout: int) -> FakeProcess:
        calls.append((command, timeout))
        return FakeProcess()

    payload = run_provider_comparison_mock_local(
        providers=["modal-exec"],
        iterations=1,
        sandbox_exec_runner=run,
    )

    assert payload["ok"] is True
    assert payload["providers"]["modal-exec"]["status"] == "ok"
    assert payload["providers"]["modal-exec"]["cases"]["sandbox_exec_move_click"]["status"] == "ok"
    assert len(calls) == 2


def test_benchmark_compare_live_external_providers_skip_without_sdks(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DAYTONA_API_KEY", "daytona-secret")
    monkeypatch.setenv("E2B_API_KEY", "e2b-secret")

    def missing_sdk(*args, **kwargs):
        raise ImportError("missing sdk")

    monkeypatch.setattr(benchmark_comparison, "_import_provider_module", missing_sdk)

    payload = run_provider_comparison(providers=["daytona", "e2b"], iterations=1)
    serialized = json.dumps(payload)
    assert payload["ok"] is True
    assert payload["providers"]["daytona"]["status"] == "unavailable"
    assert payload["providers"]["e2b"]["status"] == "unavailable"
    assert "install the bench-daytona extra" in serialized
    assert "install the bench-e2b extra" in serialized
    assert "secret" not in serialized


def test_benchmark_compare_redacts_nested_provider_observations() -> None:
    observation = benchmark_comparison._safe_provider_observation(
        {
            "meta": {
                "token": "secret-token",
                "stdout": f"secret stdout {TYPING_BENCHMARK_TEXT}",
                "clientSecret": "client-secret",
                "accessKey": "access-secret",
                "streamUrl": "https://user:secret@example.test/vnc?token=secret",
            },
            "url": "https://user:secret@example.test/vnc?token=secret",
            "note": f"typed: {TYPING_BENCHMARK_TEXT}",
        }
    )

    serialized = json.dumps(observation)
    assert "secret-token" not in serialized
    assert "secret stdout" not in serialized
    assert "client-secret" not in serialized
    assert "access-secret" not in serialized
    assert "user:secret" not in serialized
    assert "token=secret" not in serialized
    assert TYPING_BENCHMARK_TEXT not in serialized
    assert observation["url"]["redacted"] is True
    assert observation["meta"]["token"]["redacted"] is True
    assert "[redacted typed text]" in serialized


def test_benchmark_compare_safe_provider_metadata_removes_url_credentials() -> None:
    value = benchmark_comparison._safe_provider_metadata_value(
        "https://user:secret@example.test/vnc?token=secret#frag"
    )

    assert value == "https://example.test"


def test_benchmark_compare_provider_failure_redacts_url_paths(monkeypatch) -> None:
    def fail_provider(**kwargs):
        raise RuntimeError(
            "stream https://user:secret@example.com/sandbox/token/vnc?password=secret"
        )

    monkeypatch.setattr(benchmark_comparison, "_run_adapter_provider", fail_provider)

    payload = run_provider_comparison(providers=["openai"], iterations=1)
    serialized = json.dumps(payload)

    assert payload["ok"] is False
    assert "user:secret" not in serialized
    assert "sandbox/token/vnc" not in serialized
    assert "password=secret" not in serialized
    assert "https://example.com/[redacted-url]" in serialized


def test_benchmark_compare_daytona_live_uses_computer_use_and_deletes(monkeypatch) -> None:
    sandboxes = []
    create_args = []

    class FakeDaytonaConfig:
        def __init__(self, *, api_key: str, target: str | None = None):
            assert api_key == "daytona-secret"
            assert target == "us"

    class FakeMouse:
        def __init__(self, calls: list[str]):
            self._calls = calls

        def move(self, x: int, y: int) -> None:
            self._calls.append(f"move:{x}:{y}")

        def click(self, x: int, y: int) -> None:
            self._calls.append(f"click:{x}:{y}")

        def get_position(self):
            return type("MousePositionResponse", (), {"x": 16, "y": 128})()

    class FakeKeyboard:
        def __init__(self, calls: list[str]):
            self._calls = calls

        def type(self, text: str) -> None:
            self._calls.append(f"type:{len(text)}")

    class FakeScreenshot:
        def __init__(self, calls: list[str]):
            self._calls = calls

        def take_full_screen(self):
            self._calls.append("screenshot")
            return type(
                "ScreenshotResponse",
                (),
                {"size_bytes": None, "screenshot": "base64-png"},
            )()

    class FakeComputerUse:
        def __init__(self, calls: list[str]):
            self._calls = calls
            self.mouse = FakeMouse(calls)
            self.keyboard = FakeKeyboard(calls)
            self.screenshot = FakeScreenshot(calls)

        def start(self) -> None:
            self._calls.append("computer_use_start")

        def stop(self) -> None:
            self._calls.append("computer_use_stop")

        def get_status(self) -> dict[str, str]:
            return {"desktop": "ready"}

    class FakeProcess:
        def __init__(self, calls: list[str]):
            self._calls = calls

        def exec(self, command: str, *, timeout: int):
            if command == "sh -lc 'printf 42'":
                assert timeout == 30
                self._calls.append("command")
                return {"exit_code": 0}
            if "rm -f " in command:
                return {"exit_code": 0, "result": ""}
            if "grep -c" in command:
                return {
                    "exit_code": 0,
                    "result": f"keypress_count={len(benchmark_comparison.TYPE_READBACK_TEXT)}\n",
                }
            raise AssertionError(command)

    class FakeSandbox:
        def __init__(self):
            self.calls: list[str] = []
            self.cpu = 1
            self.memory = 1
            self.disk = 3
            self.computer_use = FakeComputerUse(self.calls)
            self.process = FakeProcess(self.calls)
            sandboxes.append(self)

        def stop(self) -> None:
            self.calls.append("sandbox_stop")

        def delete(self) -> None:
            self.calls.append("sandbox_delete")

    class FakeDaytona:
        def __init__(self, config: FakeDaytonaConfig):
            self.config = config

        def create(self, *args) -> FakeSandbox:
            create_args.append(args)
            return FakeSandbox()

        def delete(self, sandbox: FakeSandbox) -> None:
            sandbox.calls.append("client_delete")

    class FakeDaytonaModule:
        Daytona = FakeDaytona
        DaytonaConfig = FakeDaytonaConfig

    monkeypatch.setenv("DAYTONA_API_KEY", "daytona-secret")
    monkeypatch.setenv("DAYTONA_TARGET", "us")
    monkeypatch.setattr(
        benchmark_comparison,
        "_import_provider_module",
        lambda *args: FakeDaytonaModule,
    )

    payload = run_provider_comparison(
        providers=["daytona"],
        iterations=1,
        warmup_iterations=0,
    )
    serialized = json.dumps(payload)

    assert payload["ok"] is True
    assert payload["providers"]["daytona"]["status"] == "ok"
    assert payload["providers"]["daytona"]["metadata"]["sandbox_source"] == "default_snapshot"
    assert payload["providers"]["daytona"]["metadata"]["target"] == "us"
    assert {
        "cold_create_to_ready",
        "screenshot_full",
        "move_click",
        "move_click_sequence",
        "type_100_chars",
        "type_1000_chars",
        "command_echo",
    } <= set(payload["providers"]["daytona"]["cases"])
    assert payload["providers"]["daytona"]["metadata"]["cpu_count"] == 1
    assert payload["providers"]["daytona"]["metadata"]["memory_gib"] == 1
    assert payload["providers"]["daytona"]["metadata"]["storage_gib"] == 3
    assert payload["providers"]["daytona"]["metadata"]["cpu_count_source"] == (
        "provider_sandbox_metadata"
    )
    assert payload["providers"]["daytona"]["cost_estimate"]["status"] == "estimated"
    assert payload["providers"]["daytona"]["cost_estimate"]["total"]["amount"] > 0
    assert len(sandboxes) == 2
    assert create_args == [(), ()]
    assert sandboxes[0].calls == [
        "computer_use_start",
        "screenshot",
        "computer_use_stop",
        "client_delete",
    ]
    assert sandboxes[1].calls.count("screenshot") == 2
    assert "move:24:24" in sandboxes[1].calls
    assert "click:24:24" in sandboxes[1].calls
    assert "move:16:16" in sandboxes[1].calls
    assert "click:128:128" in sandboxes[1].calls
    assert "type:100" in sandboxes[1].calls
    assert "type:1000" in sandboxes[1].calls
    assert "click:40:60" in sandboxes[1].calls
    assert f"type:{len(benchmark_comparison.TYPE_READBACK_TEXT)}" in sandboxes[1].calls
    assert "command" in sandboxes[1].calls
    assert sandboxes[1].calls[-2:] == ["computer_use_stop", "client_delete"]
    assert payload["providers"]["daytona"]["verification"]["cursor_position"]["status"] == "ok"
    assert payload["providers"]["daytona"]["verification"]["type_text"]["status"] == "ok"
    assert TYPING_BENCHMARK_TEXT not in serialized
    assert TYPE_1000_CHARS_TEXT not in serialized
    assert benchmark_comparison.TYPE_READBACK_TEXT not in serialized


def test_benchmark_compare_e2b_live_reuses_ready_sandbox_and_uses_python_kwargs(
    monkeypatch,
) -> None:
    sandboxes = []
    create_kwargs = []

    class FakeCommands:
        def __init__(self, calls: list[str]):
            self._calls = calls

        def run(self, command: str, *, timeout: int):
            if command == "sh -lc 'printf 42'":
                assert timeout == 30
                self._calls.append("command")
                return {"exit_code": 0}
            if "xdotool getmouselocation" in command:
                return {"exit_code": 0, "stdout": "X=16\nY=128\n"}
            if "rm -f " in command:
                return {"exit_code": 0, "stdout": ""}
            if "grep -c" in command:
                return {
                    "exit_code": 0,
                    "stdout": f"keypress_count={len(benchmark_comparison.TYPE_READBACK_TEXT)}\n",
                }
            if command == "xdotool key Return ctrl+d":
                return {"exit_code": 0, "stdout": ""}
            raise AssertionError(command)

    class FakeSandbox:
        def __init__(self):
            self.calls: list[str] = []
            self.commands = FakeCommands(self.calls)
            sandboxes.append(self)

        @classmethod
        def create(cls, **kwargs):
            create_kwargs.append(kwargs)
            return cls()

        def screenshot(self) -> bytes:
            self.calls.append("screenshot")
            return b"png"

        def move_mouse(self, x: int, y: int) -> None:
            self.calls.append(f"move:{x}:{y}")

        def left_click(self, x: int | None = None, y: int | None = None) -> None:
            self.calls.append(f"click:{x}:{y}")

        def write(self, text: str) -> None:
            self.calls.append(f"type:{len(text)}")

        def delete(self) -> None:
            self.calls.append("delete")

    class FakeE2BModule:
        Sandbox = FakeSandbox

    monkeypatch.setenv("E2B_API_KEY", "e2b-secret")
    monkeypatch.setattr(
        benchmark_comparison,
        "_import_provider_module",
        lambda *args: FakeE2BModule,
    )

    payload = run_provider_comparison(
        providers=["e2b"],
        iterations=1,
        warmup_iterations=0,
    )
    serialized = json.dumps(payload)

    assert payload["ok"] is True
    assert payload["providers"]["e2b"]["status"] == "ok"
    assert payload["providers"]["e2b"]["metadata"]["template_source"] == "default_desktop"
    assert payload["providers"]["e2b"]["cost_estimate"]["status"] == "estimated"
    assert payload["providers"]["e2b"]["cost_estimate"]["components"][0]["resource"] == "cpu"
    assert payload["providers"]["e2b"]["cost_estimate"]["components"][1]["resource"] == "memory"
    assert payload["providers"]["e2b"]["cost_estimate"]["total"]["amount"] > 0
    assert create_kwargs == [
        {"resolution": (1024, 768), "dpi": 96, "display": ":0", "timeout": 300},
        {"resolution": (1024, 768), "dpi": 96, "display": ":0", "timeout": 300},
    ]
    assert len(sandboxes) == 2
    assert sandboxes[0].calls == ["screenshot", "delete"]
    assert sandboxes[1].calls == [
        "screenshot",
        "screenshot",
        "move:24:24",
        "click:None:None",
        "move:16:16",
        "click:None:None",
        "move:128:16",
        "click:None:None",
        "move:128:128",
        "click:None:None",
        "move:16:128",
        "click:None:None",
        "type:100",
        "type:1000",
        "command",
        f"type:{len(benchmark_comparison.TYPE_READBACK_TEXT)}",
        "delete",
    ]
    assert payload["providers"]["e2b"]["verification"]["cursor_position"]["status"] == "ok"
    assert payload["providers"]["e2b"]["verification"]["type_text"]["status"] == "ok"
    assert TYPING_BENCHMARK_TEXT not in serialized
    assert TYPE_1000_CHARS_TEXT not in serialized
    assert benchmark_comparison.TYPE_READBACK_TEXT not in serialized


def test_benchmark_compare_e2b_move_click_avoids_noop_synced_move() -> None:
    class FakeSandbox:
        def __init__(self):
            self.calls: list[str] = []

        def move_mouse(self, x: int, y: int) -> None:
            self.calls.append(f"move:{x}:{y}")

        def left_click(self) -> None:
            self.calls.append("click")

    class FakeSandboxClass:
        @classmethod
        def create(cls, **kwargs):
            return FakeSandbox()

    class FakeE2BModule:
        Sandbox = FakeSandboxClass

    benchmark = benchmark_comparison._E2BLiveBenchmark(FakeE2BModule, template=None)
    sandbox = FakeSandbox()

    benchmark.move_click(sandbox)
    benchmark.move_click(sandbox)

    assert sandbox.calls == ["move:24:24", "click", "move:25:25", "click"]


def test_benchmark_compare_e2b_move_click_sequence_avoids_repeated_synced_moves() -> None:
    class FakeSandbox:
        def __init__(self):
            self.calls: list[str] = []

        def move_mouse(self, x: int, y: int) -> None:
            self.calls.append(f"move:{x}:{y}")

        def left_click(self) -> None:
            self.calls.append("click")

    class FakeSandboxClass:
        @classmethod
        def create(cls, **kwargs):
            return FakeSandbox()

    class FakeE2BModule:
        Sandbox = FakeSandboxClass

    benchmark = benchmark_comparison._E2BLiveBenchmark(FakeE2BModule, template=None)
    sandbox = FakeSandbox()

    benchmark.move_click_sequence(sandbox)
    benchmark.move_click_sequence(sandbox)

    expected = [
        "move:16:16",
        "click",
        "move:128:16",
        "click",
        "move:128:128",
        "click",
        "move:16:128",
        "click",
    ]
    assert sandbox.calls[:8] == expected
    assert sandbox.calls[8:] == expected


def test_benchmark_compare_e2b_create_type_error_is_not_silently_retried(
    monkeypatch,
) -> None:
    calls = []

    class FakeSandbox:
        @classmethod
        def create(cls, **kwargs):
            calls.append(kwargs)
            raise TypeError("internal provider type error")

    class FakeE2BModule:
        Sandbox = FakeSandbox

    monkeypatch.setenv("E2B_API_KEY", "e2b-secret")
    monkeypatch.setattr(
        benchmark_comparison,
        "_import_provider_module",
        lambda *args: FakeE2BModule,
    )

    payload = run_provider_comparison(
        providers=["e2b"],
        iterations=1,
        warmup_iterations=0,
    )
    serialized = json.dumps(payload)

    assert payload["ok"] is False
    assert payload["providers"]["e2b"]["status"] == "failed"
    assert len(calls) == 2
    assert calls[0] == {"resolution": (1024, 768), "dpi": 96, "display": ":0", "timeout": 300}
    assert "internal provider type error" in serialized


def test_benchmark_compare_e2b_cleanup_failure_is_reported(monkeypatch) -> None:
    class FakeCommands:
        def run(self, command: str, *, timeout: int):
            return {"exit_code": 0}

    class FakeSandbox:
        commands = FakeCommands()

        @classmethod
        def create(cls, **kwargs):
            return cls()

        def screenshot(self) -> bytes:
            return b"png"

        def move_mouse(self, x: int, y: int) -> None:
            return None

        def left_click(self, x: int, y: int) -> None:
            return None

        def write(self, text: str) -> None:
            return None

        def delete(self) -> None:
            raise RuntimeError("delete failed https://user:secret@example.com/vnc?token=secret")

    class FakeE2BModule:
        Sandbox = FakeSandbox

    monkeypatch.setenv("E2B_API_KEY", "e2b-secret")
    monkeypatch.setattr(
        benchmark_comparison,
        "_import_provider_module",
        lambda *args: FakeE2BModule,
    )

    payload = run_provider_comparison(
        providers=["e2b"],
        iterations=1,
        warmup_iterations=0,
    )
    serialized = json.dumps(payload)

    assert payload["ok"] is False
    assert payload["providers"]["e2b"]["status"] == "failed"
    assert payload["providers"]["e2b"]["cases"]["cleanup"]["status"] == "failed"
    assert "delete failed" in serialized
    assert "user:secret" not in serialized
    assert "token=secret" not in serialized


def test_benchmark_compare_requires_modal_daemon_target(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "compare", "--provider", "modal-daemon"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert (
        "modal-daemon comparison requires --mock-local, --base-url, or --create-modal-sandbox"
    ) in captured.err


def test_benchmark_compare_structures_and_redacts_provider_failures(monkeypatch) -> None:
    def fail_provider(**kwargs):
        raise RuntimeError(
            f"Authorization: Bearer secret-token {TYPING_BENCHMARK_TEXT} "
            "https://user:secret@example.com/vnc.html?password=secret "
            '{"apiKey":"secret","clientSecret":"secret"}'
        )

    monkeypatch.setattr(benchmark_comparison, "_run_adapter_provider", fail_provider)

    payload = run_provider_comparison(providers=["openai"], iterations=1)
    serialized = json.dumps(payload)

    assert payload["ok"] is False
    assert payload["providers"]["openai"]["status"] == "failed"
    assert "secret-token" not in serialized
    assert "user:secret" not in serialized
    assert TYPING_BENCHMARK_TEXT not in serialized
    assert "password=secret" not in serialized
    assert '"secret"' not in serialized
    assert "[redacted typed text]" in serialized


def test_benchmark_action_batch_failure_is_structured(monkeypatch, capsys) -> None:
    def fail(*, iterations: int):
        return {
            "ok": False,
            "benchmark": "action-batch",
            "iterations": iterations,
            "action_count": 5,
            "cases": {},
            "comparison": {"status": "not_available"},
            "failures": [
                {
                    "case": "batch_5_actions",
                    "phase": "measure",
                    "iteration": 0,
                    "type": "RuntimeError",
                    "message": "boom",
                }
            ],
        }

    monkeypatch.setattr(cli, "run_action_batch_benchmark_mock_local", fail)

    exit_code = cli.main(["benchmark", "action-batch", "--mock-local", "--iterations", "1"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["failures"][0]["case"] == "batch_5_actions"
    assert payload["failures"][0]["message"] == "boom"


def test_benchmark_action_batch_non_ok_response_is_structured() -> None:
    class FailingClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            return {"ok": False, "results": []}

    payload = run_action_batch_benchmark(
        client=FailingClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["cases"]["batch_5_actions"]["status"] == "failed"
    assert payload["failures"][0]["case"] == "batch_5_actions"
    assert payload["failures"][0]["phase"] == "measure"
    assert payload["failures"][0]["type"] == "RuntimeError"


def test_benchmark_action_batch_redacts_reported_base_url_credentials() -> None:
    class SuccessfulClient:
        base_url = "https://user:secret@example.com:443/daemon?token=secret#frag"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}]}

    payload = run_action_batch_benchmark(
        client=SuccessfulClient(),
        mode="http",
        iterations=1,
        base_url=SuccessfulClient.base_url,
        warmup_iterations=0,
    )

    serialized = json.dumps(payload)
    assert payload["base_url"] == "https://example.com:443/daemon"
    assert "secret" not in serialized
    assert "token" not in serialized


def test_benchmark_action_batch_records_daemon_timing_attribution() -> None:
    class TimedClient:
        base_url = "http://testserver"
        calls = 0

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            self.calls += 1
            return {
                "ok": True,
                "results": [{"ok": True}],
                "timing": {"daemon_ms": float(self.calls)},
            }

    payload = run_action_batch_benchmark(
        client=TimedClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is True
    batch = payload["cases"]["batch_5_actions"]
    separate = payload["cases"]["separate_5_actions"]
    assert batch["attribution"]["status"] == "measured"
    assert batch["daemon_samples_ms"] == [1.0]
    assert batch["daemon_summary_ms"]["mean"] == 1.0
    assert batch["overhead_summary_ms"]["mean"] is not None
    assert separate["attribution"]["status"] == "measured"
    assert separate["daemon_samples_ms"] == [20.0]


def test_benchmark_action_batch_missing_timing_is_unavailable_not_failure() -> None:
    class OldClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}]}

    payload = run_action_batch_benchmark(
        client=OldClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is True
    assert payload["cases"]["batch_5_actions"]["attribution"] == {
        "status": "unavailable",
        "reason": "daemon response did not include timing.daemon_ms",
    }
    assert payload["cases"]["batch_5_actions"]["daemon_samples_ms"] == []
    assert payload["failures"] == []


def test_benchmark_action_batch_malformed_timing_is_structured_failure() -> None:
    class MalformedTimingClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}], "timing": {"daemon_ms": "fast"}}

    payload = run_action_batch_benchmark(
        client=MalformedTimingClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["cases"]["batch_5_actions"]["status"] == "failed"
    assert payload["failures"][0]["case"] == "batch_5_actions"
    assert payload["failures"][0]["type"] == "RuntimeError"
    assert payload["failures"][0]["message"] == "daemon action timing.daemon_ms was malformed"


def test_type_100_chars_benchmark_uses_safe_metadata_and_attribution() -> None:
    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            assert json["actions"][0]["type"] == "type"
            assert json["actions"][0]["text"] == TYPING_BENCHMARK_TEXT
            return {
                "ok": True,
                "results": [{"ok": True, "output": {"length": 100, "method": "xdotool"}}],
                "timing": {"daemon_ms": 12.5},
            }

    payload = run_type_100_chars_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["request"] == {"character_count": 100, "method": "xdotool"}
    assert payload["daemon_samples_ms"] == [12.5]
    assert payload["attribution"]["status"] == "measured"
    serialized = json.dumps(payload)
    assert TYPING_BENCHMARK_TEXT not in serialized
    assert '"text"' not in serialized


def test_type_100_chars_missing_timing_is_unavailable_not_failure() -> None:
    class OldClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}]}

    payload = run_type_100_chars_benchmark(
        client=OldClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["attribution"] == {
        "status": "unavailable",
        "reason": "daemon response did not include timing.daemon_ms",
    }
    assert payload["daemon_samples_ms"] == []
    assert payload["failures"] == []


def test_type_100_chars_malformed_timing_is_structured_failure() -> None:
    class MalformedTimingClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}], "timing": {"daemon_ms": "fast"}}

    payload = run_type_100_chars_benchmark(
        client=MalformedTimingClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "failed"
    assert payload["failures"][0]["case"] == "type_100_chars"
    assert payload["failures"][0]["message"] == "daemon action timing.daemon_ms was malformed"


def test_type_100_chars_failure_does_not_leak_typed_payload(monkeypatch) -> None:
    sentinel = "_".join(["SENTINEL", "TYPED", "PAYLOAD", "NO", "LEAK"])

    class FailingTypeClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            raise RuntimeError(f"backend echoed {sentinel}")

    monkeypatch.setattr("modal_computer_use.benchmarks.TYPING_BENCHMARK_TEXT", sentinel)

    payload = run_type_100_chars_benchmark(
        client=FailingTypeClient(),
        iterations=1,
        warmup_iterations=0,
    )

    serialized = json.dumps(payload)
    assert payload["status"] == "failed"
    assert payload["failures"][0]["message"] == "backend echoed [redacted typed text]"
    assert sentinel not in serialized
    assert '"text"' not in serialized


def test_type_1000_chars_benchmark_uses_safe_metadata_and_attribution() -> None:
    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            assert json["actions"][0]["type"] == "type"
            assert json["actions"][0]["text"] == TYPE_1000_CHARS_TEXT
            assert json["actions"][0]["method"] == "xdotool"
            assert json["actions"][0]["timeout_ms"] == TYPE_1000_CHARS_TIMEOUT_MS
            return {
                "ok": True,
                "results": [{"ok": True, "output": {"length": 1000, "method": "xdotool"}}],
                "timing": {"daemon_ms": 125.0},
            }

    payload = run_type_1000_chars_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["request"] == {
        "character_count": 1000,
        "method": "xdotool",
        "timeout_ms": TYPE_1000_CHARS_TIMEOUT_MS,
    }
    assert payload["daemon_samples_ms"] == [125.0]
    assert payload["attribution"]["status"] == "measured"
    serialized = json.dumps(payload)
    assert TYPE_1000_CHARS_TEXT not in serialized
    assert '"text"' not in serialized


def test_type_1000_chars_failure_does_not_leak_typed_payload(monkeypatch) -> None:
    sentinel = "_".join(["SENTINEL", "TYPED", "PAYLOAD", "1000", "NO", "LEAK"])

    class FailingTypeClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            raise RuntimeError(f"backend echoed {sentinel}")

    monkeypatch.setattr("modal_computer_use.benchmarks.TYPE_1000_CHARS_TEXT", sentinel)

    payload = run_type_1000_chars_benchmark(
        client=FailingTypeClient(),
        iterations=1,
        warmup_iterations=0,
    )

    serialized = json.dumps(payload)
    assert payload["status"] == "failed"
    assert payload["failures"][0]["message"] == "backend echoed [redacted typed text]"
    assert sentinel not in serialized
    assert '"text"' not in serialized


def test_type_1000_chars_failed_daemon_response_keeps_safe_error_detail() -> None:
    class FailedDaemonClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {
                "ok": False,
                "results": [
                    {
                        "index": 0,
                        "type": "type",
                        "ok": False,
                        "error_code": "timeout",
                        "error": "action timed out after 30000 ms",
                    }
                ],
                "timing": {"daemon_ms": 10100.0},
            }

    payload = run_type_1000_chars_benchmark(
        client=FailedDaemonClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "failed"
    assert payload["failures"][0]["message"] == (
        "daemon action response was not ok: result[0] timeout: action timed out after 30000 ms"
    )


def test_move_click_sequence_benchmark_uses_safe_metadata_and_attribution() -> None:
    seen_actions = []

    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            seen_actions.extend(json["actions"])
            return {
                "ok": True,
                "results": [{"ok": True} for _action in json["actions"]],
                "timing": {"daemon_ms": 18.0},
            }

    payload = run_move_click_sequence_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["action_count"] == 8
    assert payload["actions"] == [
        {"type": "move"},
        {"type": "click", "button": "left"},
        {"type": "move"},
        {"type": "click", "button": "left"},
        {"type": "move"},
        {"type": "click", "button": "left"},
        {"type": "move"},
        {"type": "click", "button": "left"},
    ]
    assert payload["daemon_samples_ms"] == [18.0]
    assert payload["attribution"]["status"] == "measured"
    serialized = json.dumps(payload)
    assert '"x"' not in serialized
    assert '"y"' not in serialized
    assert [action["type"] for action in seen_actions] == [
        "move",
        "click",
        "move",
        "click",
        "move",
        "click",
        "move",
        "click",
    ]


def test_benchmark_report_mock_local_outputs_release_report(capsys) -> None:
    exit_code = cli.main(["benchmark", "report", "--mock-local", "--iterations", "2"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["mode"] == "mock-local"
    assert payload["iterations"] == 2
    assert payload["warmup_iterations"] == 1
    assert payload["package_version"]
    assert payload["generated_at"]
    assert payload["metadata"]["version"]["daemon_version"]
    assert payload["metadata"]["capabilities"]["image_profile"]
    assert len(payload["benchmarks"]["action_batch"]["cases"]["batch_5_actions"]["samples_ms"]) == 2
    assert len(payload["benchmarks"]["screenshot_full"]["samples_ms"]) == 2
    assert len(payload["benchmarks"]["screenshot_compressed"]["samples_ms"]) == 2
    assert len(payload["benchmarks"]["move_click"]["samples_ms"]) == 2
    assert payload["benchmarks"]["move_click"]["summary_ms"]["mean"] is not None
    assert payload["benchmarks"]["move_click"]["attribution"]["status"] == "measured"
    assert len(payload["benchmarks"]["move_click"]["daemon_samples_ms"]) == 2
    assert payload["benchmarks"]["move_click"]["daemon_summary_ms"]["mean"] is not None
    assert payload["benchmarks"]["move_click"]["overhead_summary_ms"]["mean"] is not None
    assert payload["benchmarks"]["move_click"]["action_count"] == 2
    move_sequence = payload["benchmarks"]["move_click_sequence"]
    assert move_sequence["status"] == "ok"
    assert len(move_sequence["samples_ms"]) == 2
    assert move_sequence["summary_ms"]["mean"] is not None
    assert move_sequence["daemon_samples_ms"]
    assert move_sequence["action_count"] == 8
    typing = payload["benchmarks"]["type_100_chars"]
    assert typing["status"] == "ok"
    assert len(typing["samples_ms"]) == 2
    assert typing["summary_ms"]["mean"] is not None
    assert typing["daemon_samples_ms"]
    assert typing["daemon_summary_ms"]["mean"] is not None
    assert typing["overhead_summary_ms"]["mean"] is not None
    assert typing["attribution"]["status"] == "measured"
    assert typing["request"] == {"character_count": 100, "method": "xdotool"}
    typing_1000 = payload["benchmarks"]["type_1000_chars"]
    assert typing_1000["status"] == "ok"
    assert len(typing_1000["samples_ms"]) == 2
    assert typing_1000["request"] == {
        "character_count": 1000,
        "method": "xdotool",
        "timeout_ms": TYPE_1000_CHARS_TIMEOUT_MS,
    }
    assert (
        payload["benchmarks"]["action_batch"]["cases"]["separate_5_actions"]["attribution"][
            "status"
        ]
        == "measured"
    )
    assert (
        len(
            payload["benchmarks"]["action_batch"]["cases"]["separate_5_actions"][
                "daemon_samples_ms"
            ]
        )
        == 2
    )
    recording = payload["benchmarks"]["recording_start_stop"]
    assert len(recording["start_samples_ms"]) == 2
    assert len(recording["stop_samples_ms"]) == 2
    assert recording["start_summary_ms"]["mean"] is not None
    assert recording["stop_summary_ms"]["p95"] is not None
    assert recording["last_result"]["status"] == "stopped"
    assert recording["last_result"]["format"] == "mp4"
    assert recording["last_result"]["size_bytes"] > 0
    assert recording["last_result"]["duration_seconds"] is not None
    assert payload["benchmarks"]["screenshot_full"]["last_result"]["size_bytes"] > 0
    assert payload["benchmarks"]["screenshot_compressed"]["summary_bytes"]["mean"] is not None
    assert payload["benchmarks"]["sandbox_exec"]["status"] == "not_measured"
    assert payload["benchmarks"]["cold_create_to_ready"]["status"] == "not_measured"
    assert payload["failures"] == []
    assert TYPING_BENCHMARK_TEXT not in captured.out
    assert "data_base64" not in captured.out
    assert '"bytes"' not in captured.out
    assert '"path"' not in captured.out
    assert "artifact://" not in captured.out
    assert "mock recording" not in captured.out
    assert "hello" not in captured.out
    assert "clipboard text" not in captured.out.lower()
    assert "dev-token" not in captured.out
    assert "Bearer" not in captured.out
    assert "novnc" not in captured.out.lower()


def test_benchmark_report_writes_output_and_prints_json(tmp_path, capsys) -> None:
    output = tmp_path / "benchmark-report.json"

    exit_code = cli.main(
        [
            "benchmark",
            "report",
            "--mock-local",
            "--iterations",
            "1",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    written = json.loads(output.read_text())
    assert exit_code == 0
    assert printed["ok"] is True
    assert written == printed


def test_benchmark_report_invalid_iterations_rejected(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "report", "--mock-local", "--iterations", "0"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "must be >= 1" in captured.err


def test_benchmark_report_failure_is_structured(monkeypatch, capsys) -> None:
    def fail(*, iterations: int):
        return {
            "ok": False,
            "generated_at": "2026-05-11T00:00:00+00:00",
            "package_version": "0.0.0",
            "mode": "mock-local",
            "base_url": "http://testserver",
            "iterations": iterations,
            "warmup_iterations": 1,
            "metadata": {},
            "benchmarks": {
                "action_batch": {"status": "ok"},
                "screenshot_full": {
                    "status": "failed",
                    "samples_ms": [],
                    "summary_ms": {},
                    "failures": [
                        {
                            "case": "screenshot_full",
                            "phase": "measure",
                            "iteration": 0,
                            "type": "RuntimeError",
                            "message": "boom",
                        }
                    ],
                },
            },
            "failures": [
                {
                    "benchmark": "screenshot_full",
                    "case": "screenshot_full",
                    "phase": "measure",
                    "iteration": 0,
                    "type": "RuntimeError",
                    "message": "boom",
                }
            ],
        }

    monkeypatch.setattr(cli, "run_benchmark_report_mock_local", fail)

    exit_code = cli.main(["benchmark", "report", "--mock-local", "--iterations", "1"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["failures"][0]["benchmark"] == "screenshot_full"
    assert payload["failures"][0]["message"] == "boom"


def test_benchmark_report_screenshot_failure_is_structured() -> None:
    class FailingClient:
        base_url = "http://testserver"
        recording_id = "rec_test"

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "standard"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10}
            if path == "/v1/recordings":
                return {"id": self.recording_id}
            if path == f"/v1/recordings/{self.recording_id}/stop":
                return {"status": "stopped", "format": "mp4", "size_bytes": 100}
            raise AssertionError(path)

    payload = run_benchmark_report(
        client=FailingClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["benchmarks"]["action_batch"]["status"] == "ok"
    assert payload["benchmarks"]["screenshot_full"]["status"] == "failed"
    assert payload["failures"][0]["benchmark"] == "screenshot_full"
    assert payload["failures"][0]["type"] == "RuntimeError"


def test_benchmark_report_redacts_reported_base_url_credentials() -> None:
    class SuccessfulClient:
        base_url = "https://user:secret@example.com/connect?token=secret"
        recording_id = "rec_test"

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "standard"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10, "height": 10, "size_bytes": 100}
            if path == "/v1/recordings":
                return {"id": self.recording_id}
            if path == f"/v1/recordings/{self.recording_id}/stop":
                return {"status": "stopped", "format": "mp4", "size_bytes": 100}
            raise AssertionError(path)

    payload = run_benchmark_report(
        client=SuccessfulClient(),
        mode="http",
        iterations=1,
        base_url=SuccessfulClient.base_url,
        warmup_iterations=0,
    )

    serialized = json.dumps(payload)
    assert payload["base_url"] == "https://example.com/connect"
    assert "secret" not in serialized
    assert "token" not in serialized


def test_benchmark_report_move_click_failure_is_structured() -> None:
    class FailingMoveClient:
        base_url = "http://testserver"
        recording_id = "rec_test"

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "standard"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                if len(json["actions"]) == 2:
                    return {"ok": False, "results": []}
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10, "height": 10, "size_bytes": 100}
            if path == "/v1/recordings":
                return {"id": self.recording_id}
            if path == f"/v1/recordings/{self.recording_id}/stop":
                return {"status": "stopped", "format": "mp4", "size_bytes": 100}
            raise AssertionError(path)

    payload = run_benchmark_report(
        client=FailingMoveClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["benchmarks"]["move_click"]["status"] == "failed"
    assert payload["failures"][0]["benchmark"] == "move_click"
    assert payload["failures"][0]["case"] == "move_click"


def test_benchmark_report_recording_failure_is_structured() -> None:
    class FailingRecordingClient:
        base_url = "http://testserver"

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "standard"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10, "height": 10, "size_bytes": 100}
            if path == "/v1/recordings":
                return {"id": "rec_test"}
            if path == "/v1/recordings/rec_test/stop":
                return {"status": "failed", "format": "mp4", "size_bytes": 0}
            raise AssertionError(path)

    payload = run_benchmark_report(
        client=FailingRecordingClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["benchmarks"]["recording_start_stop"]["status"] == "failed"
    assert payload["failures"][0]["benchmark"] == "recording_start_stop"
    assert payload["failures"][0]["case"] == "recording_stop"
    assert payload["failures"][0]["type"] == "RuntimeError"
    assert payload["failures"][0]["message"] == "daemon recording status was failed"


def test_benchmark_report_include_sandbox_exec_requires_sandbox_id(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "report",
                "--base-url",
                "http://127.0.0.1:8080",
                "--include-sandbox-exec",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--include-sandbox-exec requires --sandbox-id" in captured.err


def test_benchmark_report_mock_local_cannot_include_sandbox_exec(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "report",
                "--mock-local",
                "--include-sandbox-exec",
                "--sandbox-id",
                "sb-123",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--include-sandbox-exec requires --base-url" in captured.err


def test_sandbox_exec_benchmark_success_uses_safe_metadata() -> None:
    class FakeProcess:
        returncode = 0

        def wait(self) -> None:
            return None

    calls = []

    def run(command: tuple[str, ...], timeout: int) -> FakeProcess:
        calls.append((command, timeout))
        return FakeProcess()

    payload = run_sandbox_exec_benchmark(
        iterations=2,
        warmup_iterations=1,
        runner=run,
    )

    assert payload["status"] == "ok"
    assert payload["successful_iterations"] == 2
    assert len(payload["samples_ms"]) == 2
    assert payload["summary_ms"]["mean"] is not None
    assert payload["command"] == {
        "tool": "xdotool",
        "action_count": 2,
        "actions": [{"type": "move"}, {"type": "click", "button": "left"}],
        "timeout_seconds": 10,
    }
    assert calls[0][0][0] == "sh"
    serialized = json.dumps(payload)
    assert "xdotool mousemove" not in serialized
    assert "stderr" not in serialized.lower()
    assert "stdout" not in serialized.lower()
    assert "/" not in serialized


def test_sandbox_exec_benchmark_nonzero_exit_is_structured() -> None:
    class FakeProcess:
        returncode = 2

        def wait(self) -> None:
            return None

    payload = run_sandbox_exec_benchmark(
        iterations=1,
        warmup_iterations=0,
        runner=lambda command, timeout: FakeProcess(),
    )

    assert payload["status"] == "failed"
    assert payload["successful_iterations"] == 0
    assert payload["failures"][0]["case"] == "sandbox_exec_move_click"
    assert payload["failures"][0]["code"] == "sandbox_exec_nonzero_exit"
    assert payload["failures"][0]["message"] == "Sandbox.exec command exited nonzero"


def test_sandbox_exec_benchmark_missing_tool_is_structured() -> None:
    class FakeProcess:
        returncode = 127

        def wait(self) -> None:
            return None

    payload = run_sandbox_exec_benchmark(
        iterations=1,
        warmup_iterations=0,
        runner=lambda command, timeout: FakeProcess(),
    )

    assert payload["status"] == "failed"
    assert payload["failures"][0]["code"] == "sandbox_exec_missing_tool"
    assert payload["failures"][0]["message"] == (
        "Sandbox.exec command could not find xdotool in the sandbox"
    )


def test_benchmark_report_live_sandbox_exec_setup_failure_is_reported(monkeypatch, capsys) -> None:
    def missing_modal(sandbox_id: str):
        raise ModalNotInstalledError("modal extra missing")

    monkeypatch.setattr(cli, "modal_sandbox_exec_runner_from_id", missing_modal)

    class SuccessfulClient:
        base_url = "http://daemon.example"
        recording_id = "rec_test"

        def close(self) -> None:
            return None

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "browser"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10, "height": 10, "size_bytes": 100}
            if path == "/v1/recordings":
                return {"id": self.recording_id}
            if path == f"/v1/recordings/{self.recording_id}/stop":
                return {"status": "stopped", "format": "mp4", "size_bytes": 100}
            raise AssertionError(path)

    monkeypatch.setattr(cli, "DaemonClient", lambda *args, **kwargs: SuccessfulClient())

    exit_code = cli.main(
        [
            "benchmark",
            "report",
            "--base-url",
            "http://daemon.example",
            "--token",
            "dev-token",
            "--include-sandbox-exec",
            "--sandbox-id",
            "sb-123",
            "--iterations",
            "1",
            "--modal-region",
            "us-east-1",
            "--browser",
            "chromium",
            "--image-profile",
            "browser",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["benchmarks"]["sandbox_exec"]["status"] == "failed"
    assert payload["benchmarks"]["sandbox_exec"]["failures"][0]["code"] == "modal_not_installed"
    assert payload["failures"][-1]["benchmark"] == "sandbox_exec"
    assert payload["metadata"]["environment"] == {
        "browser": "chromium",
        "image_profile": "browser",
        "modal_region": "us-east-1",
    }
    assert "dev-token" not in captured.out
    assert "Bearer" not in captured.out
    assert "novnc" not in captured.out.lower()
    assert "stderr" not in captured.out.lower()
    assert "stdout" not in captured.out.lower()


def test_benchmark_compare_can_create_gpu_modal_sandbox(monkeypatch, capsys) -> None:
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

    def fake_run_provider_comparison(**kwargs):
        environment = kwargs["environment_metadata"]
        return {
            "ok": True,
            "benchmark": "provider-compare",
            "mode": kwargs["mode"],
            "providers": {
                "modal-daemon": {
                    "metadata": {"environment": environment},
                    "status": "ok",
                    "cases": {},
                    "failures": [],
                }
            },
            "failures": [],
        }

    monkeypatch.setattr(cli.ComputerSandbox, "create", staticmethod(fake_create))
    monkeypatch.setattr(cli, "run_provider_comparison", fake_run_provider_comparison)
    monkeypatch.setattr(cli, "new_run_id", lambda: "provider_compare_test")

    exit_code = cli.main(
        [
            "benchmark",
            "compare",
            "--create-modal-sandbox",
            "--providers",
            "modal-daemon",
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
        "benchmark": "provider-compare",
        "benchmark_run_id": "provider_compare_test",
    }
    assert created["tags"] == {
        "benchmark": "provider-compare",
        "benchmark_run_id": "provider_compare_test",
        "provider": "modal-daemon",
    }
    assert config.resources.profile == "browser-gpu"
    assert config.resources.gpu == "T4"
    assert config.resources.cpu == 2
    assert config.resources.memory_mib == 4096
    assert config.browser.kind == "chromium"
    assert config.browser.gpu_mode is None
    environment = payload["providers"]["modal-daemon"]["metadata"]["environment"]
    assert environment["gpu"] == "T4"
    assert environment["modal_cpu_count"] == 2
    assert environment["modal_memory_gib"] == 4
    assert environment["modal_sandbox_id"] == "sb-gpu"
    assert environment["modal_cold_create_to_ready_ms"] > 0
    assert closed == ["terminate", "detach"]


def test_benchmark_compare_create_modal_sandbox_requires_modal_daemon(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "compare",
                "--create-modal-sandbox",
                "--providers",
                "openai",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--create-modal-sandbox requires provider modal-daemon" in captured.err
