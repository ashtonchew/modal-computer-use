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
    assert (
        payload["surfaces"]["daemon-http"]["metadata"]["ingress"]["canonical_name"]
        == "modal-daemon-local"
    )
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


def test_daemon_ingress_metadata_identifies_modal_tunnel() -> None:
    ingress = benchmark_daemon_surface._daemon_ingress_metadata(
        mode="http",
        base_url="https://example.r5.modal.host",
    )

    assert ingress["canonical_name"] == "modal-daemon-tunnel"
    assert ingress["kind"] == "modal-encrypted-tunnel"


def test_daemon_ingress_metadata_prefers_explicit_modal_ingress() -> None:
    ingress = benchmark_daemon_surface._daemon_ingress_metadata(
        mode="http",
        base_url="https://example.r5.modal.host",
        environment_metadata={"modal_ingress": "connect"},
    )

    assert ingress["canonical_name"] == "modal-daemon-connect"
    assert ingress["kind"] == "modal-connect-token"


def test_daemon_ingress_metadata_names_h2_attested_tunnel() -> None:
    ingress = benchmark_daemon_surface._daemon_ingress_metadata(
        mode="http",
        base_url="https://example.r5.modal.host",
        environment_metadata={
            "modal_ingress": "attested-tunnel",
            "daemon_http_version": "2",
        },
    )

    assert ingress["canonical_name"] == "modal-daemon-attested-h2-tunnel"
    assert ingress["kind"] == "modal-attested-encrypted-h2-tunnel"


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
    assert surface["cost_status"] == {
        "estimate": "unknown",
        "billing_reconciliation": "matched",
    }

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


def test_benchmark_sdk_requires_daemon_hot_session_target(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "sdk", "--surface", "daemon-hot-session"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert (
        "daemon-hot-session surface benchmark requires --base-url "
        "or --create-modal-sandbox"
    ) in captured.err


def test_benchmark_sdk_requires_daemon_transport_floor_target(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "sdk", "--surface", "daemon-transport-floor"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert (
        "daemon-transport-floor surface benchmark requires --base-url "
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
        class Client:
            base_url = "https://daemon.example.modal.host"

            def post_bytes_with_headers(self, path, *, json):
                assert path == "/v1/screenshots/full/raw"
                assert json == {"format": "png", "show_cursor": False}
                return b"png", {
                    "x-computer-use-width": "1024",
                    "x-computer-use-height": "768",
                    "x-computer-use-capture-backend": "mss",
                }

        client = Client()

        def metadata(self):
            return SimpleNamespace(sandbox_id="sb-gpu")

        def terminate(self, *, wait: bool = False) -> None:
            created["terminate_wait"] = wait
            closed.append("terminate")

        def wait_until_ready(self, *, timeout: float) -> None:
            assert timeout > 0

        def detach(self) -> None:
            closed.append("detach")

    def fake_create(**kwargs):
        created.update(kwargs)
        return CreatedComputer()

    def fake_attach(**kwargs):
        assert kwargs["sandbox_id"] == "sb-gpu"
        assert kwargs["ingress"] == "attested-tunnel"
        assert kwargs["http2"] is True
        assert kwargs["wait"] is True
        return CreatedComputer()

    def fake_run_sdk_surface_benchmark(**kwargs):
        environment = kwargs["environment_metadata"]
        assert kwargs["base_url"] == "https://daemon.example.modal.host"
        return {
            "ok": True,
            "benchmark": "sdk-surfaces",
            "mode": kwargs["mode"],
            "surfaces": {
                "daemon-http": {
                    "metadata": {
                        "environment": environment,
                        "ingress": {
                            "canonical_name": "modal-daemon-attested-h2-tunnel",
                            "kind": "modal-attested-encrypted-h2-tunnel",
                        },
                    },
                        "status": "ok",
                        "cases": {},
                        "failures": [],
                        "cost_estimate": {"status": "unknown"},
                        "cost_status": {
                            "estimate": "unknown",
                            "billing_reconciliation": "not_requested",
                        },
                    }
            },
            "failures": [],
        }

    monkeypatch.setattr(cli.ComputerSandbox, "create", staticmethod(fake_create))
    monkeypatch.setattr(cli.ComputerSandbox, "attach", staticmethod(fake_attach))
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
            "--daemon-http-version",
            "2",
            "--subprocess-backend",
            "isolated-asyncio",
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
    assert config.ingress == "attested-tunnel"
    assert config.network.daemon_http_version == "2"
    assert config.actions.input_rate_limit_per_sec == 0
    assert config.actions.subprocess_backend == "isolated-asyncio"
    assert config.browser.kind == "chromium"
    assert config.browser.gpu_mode is None
    environment = payload["surfaces"]["daemon-http"]["metadata"]["environment"]
    ingress = payload["surfaces"]["daemon-http"]["metadata"]["ingress"]
    assert ingress["canonical_name"] == "modal-daemon-attested-h2-tunnel"
    assert ingress["kind"] == "modal-attested-encrypted-h2-tunnel"
    assert environment["gpu"] == "T4"
    assert environment["modal_ingress"] == "attested-tunnel"
    assert environment["daemon_http_version"] == "2"
    assert environment["input_rate_limit_per_sec"] == 0
    assert environment["subprocess_backend"] == "isolated-asyncio"
    assert environment["modal_cpu_count"] == 2
    assert environment["modal_memory_gib"] == 4
    assert environment["modal_sandbox_id"] == "sb-gpu"
    assert environment["modal_cold_create_to_ready_ms"] > 0
    assert environment["modal_resource_lifetime_ms"] > 0
    assert environment["cost_duration_policy"] == (
        "measured_resource_lifetime_including_creation_benchmark_and_teardown"
    )
    assert payload["surfaces"]["daemon-http"]["cost_status"]["estimate"] == "unknown"
    assert payload["shared_resource_cost_estimate"]["status"] == "estimated"
    assert payload["cost_status"] == {"shared_resource_estimate": "estimated"}
    assert created["terminate_wait"] is True
    assert environment["modal_cold_create_to_ready_definition"].startswith("create wait=False")
    assert environment["startup_model"] == "modal_sandbox_image_daemon_start"
    assert environment["uses_snapshot_or_template"] is False
    assert environment["readiness_contract"].startswith("ComputerSandbox.create")
    assert environment["setup_included"] is True
    assert environment["ingress_included"] is True
    assert environment["first_observation_api"] == "/v1/screenshots/full/raw"
    assert environment["modal_create_return_ms"] >= 0
    assert environment["modal_connect_ready_ms"] >= environment["modal_create_return_ms"]
    assert environment["modal_final_ingress_ready_ms"] >= environment["modal_connect_ready_ms"]
    assert (
        environment["modal_first_raw_screenshot_ms"]
        == environment["modal_cold_create_to_ready_ms"]
    )
    assert environment["modal_first_raw_screenshot_size_bytes"] == 3
    assert environment["modal_first_raw_screenshot_width"] == 1024
    assert environment["modal_first_raw_screenshot_height"] == 768
    assert environment["modal_first_raw_screenshot_capture_backend"] == "mss"
    assert closed == ["detach", "terminate", "detach"]

def test_benchmark_modal_ingress_ab_compares_tokens_on_same_sandbox(monkeypatch, capsys) -> None:
    created: dict[str, object] = {}
    closed: list[str] = []
    benchmark_calls: list[dict[str, object]] = []

    class RawClient:
        base_url = "https://daemon.example.modal.host"

    class FakeSandbox:
        def create_connect_token(self, *, user_metadata: dict[str, str]):
            assert user_metadata["benchmark"] == "modal-ingress-ab"
            return SimpleNamespace(url="https://connect.example", token="connect-token")

    class CreatedComputer:
        client = RawClient()
        _sandbox = FakeSandbox()

        def metadata(self):
            return SimpleNamespace(sandbox_id="sb-ab")

        def terminate(self) -> None:
            closed.append("terminate")

        def detach(self) -> None:
            closed.append("detach")

    class FakeDaemonClient:
        def __init__(self, base_url: str, *, token: str | None = None) -> None:
            self.base_url = base_url
            self.token = token

        def post_json(self, path: str):
            assert path == "/v1/session/tunnel-authorize"
            assert self.base_url == "https://connect.example"
            assert self.token == "connect-token"  # noqa: S105
            return {"token": "minted-token"}

        def close(self) -> None:
            closed.append(f"close:{self.token}")

    def fake_create(**kwargs):
        created.update(kwargs)
        return CreatedComputer()

    def fake_run_sdk_surface_benchmark(**kwargs):
        benchmark_calls.append(kwargs)
        role = kwargs["environment_metadata"]["modal_ingress_ab_role"]
        mean = 100.0 if role == "raw-static-token" else 110.0
        return {
            "ok": True,
            "surfaces": {
                "daemon-http": {
                    "cases": {
                        "move_click": {
                            "summary_ms": {"mean": mean},
                            "daemon_summary_ms": {"mean": 40.0},
                            "overhead_summary_ms": {"mean": mean - 40.0},
                        }
                    },
                    "failures": [],
                }
            },
            "failures": [],
        }

    monkeypatch.setattr(cli.ComputerSandbox, "create", staticmethod(fake_create))
    monkeypatch.setattr(cli, "DaemonClient", FakeDaemonClient)
    monkeypatch.setattr(cli, "run_sdk_surface_benchmark", fake_run_sdk_surface_benchmark)
    monkeypatch.setattr(cli, "new_run_id", lambda: "modal_ingress_ab_test")

    exit_code = cli.main(
        [
            "benchmark",
            "modal-ingress-ab",
            "--iterations",
            "1",
            "--modal-cpu",
            "2",
            "--subprocess-backend",
            "threaded",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    config = created["config"]
    assert exit_code == 0
    assert config.ingress == "tunnel"
    assert config.resources.cpu == 2
    assert config.actions.subprocess_backend == "threaded"
    assert len(benchmark_calls) == 2
    assert benchmark_calls[0]["base_url"] == benchmark_calls[1]["base_url"]
    assert benchmark_calls[0]["environment_metadata"]["modal_ingress_ab_role"] == (
        "raw-static-token"
    )
    assert benchmark_calls[1]["environment_metadata"]["modal_ingress_ab_role"] == (
        "attested-minted-token"
    )
    assert all(
        call["environment_metadata"]["subprocess_backend"] == "threaded"
        for call in benchmark_calls
    )
    assert payload["benchmark"] == "modal-ingress-ab"
    assert payload["comparison"]["move_click"]["delta_ms"] == 10.0
    assert "connect-token" not in captured.out
    assert "minted-token" not in captured.out
    assert closed == ["close:connect-token", "close:minted-token", "terminate", "detach"]


def test_benchmark_modal_region_ab_compares_transport_floor_by_region(
    monkeypatch,
    capsys,
) -> None:
    created: list[dict[str, object]] = []
    closed: list[str] = []
    benchmark_calls: list[dict[str, object]] = []

    class RawClient:
        def __init__(self, region_label: str) -> None:
            self.base_url = f"https://{region_label}.daemon.example.modal.host"

    class CreatedComputer:
        def __init__(self, region_label: str) -> None:
            self.region_label = region_label
            self.client = RawClient(region_label)

        def metadata(self):
            return SimpleNamespace(sandbox_id=f"sb-{self.region_label}")

        def terminate(self) -> None:
            closed.append(f"terminate:{self.region_label}")

        def detach(self) -> None:
            closed.append(f"detach:{self.region_label}")

    def fake_create(**kwargs):
        created.append(kwargs)
        config = kwargs["config"]
        region_label = config.runtime.modal_region or "default"
        return CreatedComputer(region_label)

    def fake_run_sdk_surface_benchmark(**kwargs):
        benchmark_calls.append(kwargs)
        region_label = kwargs["environment_metadata"]["modal_region_label"]
        p50 = 100.0 if region_label == "default" else 50.0
        return {
            "ok": True,
            "surfaces": {
                "daemon-transport-floor": {
                    "transport_floor_summary": {
                        "encodings": {
                            "http_binary": {
                                "cases": [
                                    {"requested_size_bytes": 0, "p50_ms": p50 + 20.0},
                                    {
                                        "requested_size_bytes": 250 * 1024,
                                        "p50_ms": p50 + 30.0,
                                    },
                                ]
                            },
                            "websocket_binary_envelope": {
                                "cases": [
                                    {"requested_size_bytes": 0, "p50_ms": p50},
                                    {
                                        "requested_size_bytes": 250 * 1024,
                                        "p50_ms": p50 + 40.0,
                                    },
                                ]
                            },
                            "websocket_json_metadata_binary_payload": {
                                "cases": [
                                    {"requested_size_bytes": 0, "p50_ms": p50 + 10.0},
                                    {
                                        "requested_size_bytes": 250 * 1024,
                                        "p50_ms": p50 + 50.0,
                                    },
                                ]
                            },
                        },
                        "fastest_floor_case": {
                            "case": "transport_floor_websocket_binary_envelope_0b",
                            "requested_size_bytes": 0,
                            "p50_ms": p50,
                            "inlier_mean_ms": p50 - 1.0,
                            "outlier_count": 0,
                            "transport_encoding": "websocket_binary_envelope",
                        },
                    }
                }
            },
            "failures": [],
        }

    monkeypatch.setattr(cli.ComputerSandbox, "create", staticmethod(fake_create))
    monkeypatch.setattr(cli, "run_sdk_surface_benchmark", fake_run_sdk_surface_benchmark)
    monkeypatch.setattr(cli, "new_run_id", lambda: "modal_region_ab_test")

    exit_code = cli.main(
        [
            "benchmark",
            "modal-region-ab",
            "--modal-region",
            "default",
            "--modal-region",
            "us-west",
            "--iterations",
            "1",
            "--modal-ingress",
            "attested-tunnel",
            "--caller-region-label",
            "dev-laptop-us-west",
            "--name",
            "region-ab",
            "--subprocess-backend",
            "isolated-asyncio",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    configs = [call["config"] for call in created]
    assert exit_code == 0
    assert [config.runtime.modal_region for config in configs] == [None, "us-west"]
    assert [config.ingress for config in configs] == ["attested-tunnel", "attested-tunnel"]
    assert [config.actions.subprocess_backend for config in configs] == [
        "isolated-asyncio",
        "isolated-asyncio",
    ]
    assert [call["name"] for call in created] == ["region-ab-default", "region-ab-us-west"]
    assert created[0]["tags"] == {
        "benchmark": "modal-region-ab",
        "benchmark_run_id": "modal_region_ab_test",
        "surface": "daemon-transport-floor",
    }
    assert len(benchmark_calls) == 2
    assert benchmark_calls[0]["surfaces"] == ["daemon-transport-floor"]
    assert benchmark_calls[1]["surfaces"] == ["daemon-transport-floor"]
    assert [
        call["environment_metadata"]["subprocess_backend"] for call in benchmark_calls
    ] == ["isolated-asyncio", "isolated-asyncio"]
    assert payload["benchmark"] == "modal-region-ab"
    assert payload["metadata"]["regions"] == ["default", "us-west"]
    assert payload["metadata"]["caller_region_label"] == "dev-laptop-us-west"
    assert benchmark_calls[0]["environment_metadata"]["caller_region_label"] == (
        "dev-laptop-us-west"
    )
    assert payload["comparison"]["fastest_region"] == "us-west"
    assert payload["comparison"]["regions"]["default"]["delta_vs_fastest_ms"] == 50.0
    assert payload["comparison"]["regions"]["default"]["fastest_floor_encoding"] == (
        "websocket_binary_envelope"
    )
    assert payload["comparison"]["regions"]["us-west"]["ratio_vs_fastest"] == 1.0
    assert closed == [
        "terminate:default",
        "detach:default",
        "terminate:us-west",
        "detach:us-west",
    ]


def test_benchmark_modal_region_ab_rejects_empty_region() -> None:
    with pytest.raises(SystemExit):
        cli.main(["benchmark", "modal-region-ab", "--modal-region", " "])


def test_benchmark_modal_region_summary_outputs_markdown(tmp_path, capsys) -> None:
    artifact = tmp_path / "modal-region-ab.json"
    artifact.write_text(
        json.dumps(
            {
                "benchmark": "modal-region-ab",
                "metadata": {
                    "modal_ingress": "attested-tunnel",
                    "daemon_http_version": "1.1",
                },
                "comparison": {
                    "fastest_region": "us-west",
                    "fastest_floor_p50_ms": 51.4,
                    "regions": {
                        "default": {
                            "fastest_floor_p50_ms": 97.3,
                            "delta_vs_fastest_ms": 45.9,
                            "fastest_floor_encoding": "websocket_binary_envelope",
                            "http_binary_0b_p50_ms": 120.0,
                            "websocket_binary_envelope_0b_p50_ms": 97.3,
                            "websocket_json_metadata_binary_payload_0b_p50_ms": 110.0,
                            "websocket_binary_envelope_250kb_p50_ms": 180.0,
                        },
                        "us-west": {
                            "fastest_floor_p50_ms": 51.4,
                            "delta_vs_fastest_ms": 0.0,
                            "fastest_floor_encoding": "websocket_binary_envelope",
                            "http_binary_0b_p50_ms": 70.0,
                            "websocket_binary_envelope_0b_p50_ms": 51.4,
                            "websocket_json_metadata_binary_payload_0b_p50_ms": 60.0,
                            "websocket_binary_envelope_250kb_p50_ms": 100.0,
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli.main(["benchmark", "modal-region-summary", str(artifact)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "### Modal region benchmark (attested-tunnel, HTTP/1.1)" in captured.out
    assert "| default | 97.3ms | 45.9ms |" in captured.out
    assert "Fastest region: `us-west` at `51.4ms` p50." in captured.out


def test_benchmark_modal_colocated_client_compares_external_and_runner(
    monkeypatch,
    capsys,
) -> None:
    calls: list[object] = []

    def fake_run_modal_colocated_client_benchmark(config):
        calls.append(config)
        target_config = config.target_config_factory("modal_colocated_test-target")
        assert target_config.runtime.modal_region == "us-west"
        assert target_config.actions.input_rate_limit_per_sec == 0
        assert target_config.actions.subprocess_backend == "threaded"
        return {
            "ok": True,
            "benchmark": "modal-colocated-client",
            "metadata": {"surfaces": config.surfaces},
            "comparison": {
                "external_fastest_floor_p50_ms": 30.0,
                "colocated_fastest_floor_p50_ms": 12.0,
                "delta_ms": -18.0,
                "ratio_vs_external": 0.4,
            },
        }

    monkeypatch.setattr(
        cli,
        "run_modal_colocated_client_benchmark",
        fake_run_modal_colocated_client_benchmark,
    )

    exit_code = cli.main(
        [
            "benchmark",
            "modal-colocated-client",
            "--modal-region",
            "us-west",
            "--caller-region-label",
            "dev-laptop-us-west",
            "--iterations",
            "1",
            "--name",
            "colocated",
            "--browser",
            "chromium",
            "--surface",
            "daemon-transport-floor",
            "--surface",
            "daemon-observation-stream",
            "--observation-case",
            "observation_action_click_act_and_observe_auto_signal_production",
            "--runner-paths",
            "inherited,connect",
            "--runner-path",
            "target-loopback",
            "--subprocess-backend",
            "threaded",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    config = calls[0]
    assert config.name == "colocated"
    assert config.modal_region == "us-west"
    assert config.surfaces == ["daemon-transport-floor", "daemon-observation-stream"]
    assert config.observation_cases == [
        "observation_action_click_act_and_observe_auto_signal_production"
    ]
    assert config.runner_paths == ["inherited", "connect", "target-loopback"]
    assert payload["benchmark"] == "modal-colocated-client"
    assert payload["comparison"] == {
        "external_fastest_floor_p50_ms": 30.0,
        "colocated_fastest_floor_p50_ms": 12.0,
        "delta_ms": -18.0,
        "ratio_vs_external": 0.4,
    }


def test_benchmark_modal_colocated_client_observation_profile(monkeypatch, capsys) -> None:
    calls: list[object] = []

    def fake_run_modal_colocated_client_benchmark(config):
        calls.append(config)
        return {
            "ok": True,
            "benchmark": "modal-colocated-client",
            "metadata": {"surfaces": config.surfaces},
            "comparison": {},
        }

    monkeypatch.setattr(
        cli,
        "run_modal_colocated_client_benchmark",
        fake_run_modal_colocated_client_benchmark,
    )

    exit_code = cli.main(
        [
            "benchmark",
            "modal-colocated-client",
            "--modal-region",
            "us-west",
            "--browser",
            "chromium",
            "--surface",
            "daemon-observation-stream",
            "--observation-profile",
            "causal-action-observe-diagnostic",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["benchmark"] == "modal-colocated-client"
    assert calls[0].observation_cases == [
        "observation_transport_probe_0b",
        "observation_transport_probe_5kb",
        "observation_transport_probe_50kb",
        "observation_transport_probe_250kb",
        "observation_action_click_act_and_observe_sdk_default_production",
        "observation_action_click_act_and_observe_sdk_default_timeout_200ms_production",
        "observation_action_click_act_and_observe_click_beacon_production",
        "observation_action_click_act_and_observe_click_target_state_production",
        "observation_action_click_act_and_observe_lower_click_target_state_production",
        "observation_action_click_act_and_observe_auto_signal_production",
        "observation_action_click_act_and_observe_auto_signal_binary_envelope_production",
        "observation_action_click_act_and_observe_auto_region_production",
        "observation_action_click_act_and_observe_auto_region_binary_envelope_production",
        "observation_action_click_act_and_observe_paired_envelope_ab_production",
        "observation_action_click_act_and_observe_paired_dirty_producer_ab_production",
        "observation_action_click_act_and_observe_paired_full_frame_fallback_ab_production",
        "observation_action_click_act_and_observe_paired_region_radius_ab_production",
        "observation_action_click_act_and_observe_paired_regional_producer_wait_ab_production",
        "observation_action_click_act_and_observe_paired_dirty_region_confirmation_ab_production",
        "observation_action_click_act_and_observe_paired_confirmation_off_producer_wait_ab_production",
        "observation_action_click_act_and_observe_paired_timeout_ab_production",
    ]


def test_benchmark_modal_colocated_observation_requires_browser(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "modal-colocated-client",
                "--modal-region",
                "us-west",
                "--surface",
                "daemon-observation-stream",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "daemon-observation-stream requires a browser-capable target" in captured.err


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
