from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts" / "run_external_action_frame_benchmark.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("external_action_frame_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_authorization_is_required_before_live_dependencies() -> None:
    runner = _load_runner()

    with pytest.raises(PermissionError, match="--authorize"):
        runner.require_live_authorization(False)


def test_output_collisions_are_rejected_before_live_run(tmp_path) -> None:
    runner = _load_runner()
    output = tmp_path / "result.json"
    output.write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError):
        runner._ensure_outputs_available(output, None)
    with pytest.raises(ValueError, match="different paths"):
        runner._ensure_outputs_available(output, output)


def test_load_environment_does_not_override_existing_values(tmp_path, monkeypatch) -> None:
    runner = _load_runner()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DAYTONA_API_KEY=daytona-secret\nE2B_API_KEY=e2b-secret\nUNRELATED=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAYTONA_API_KEY", "already-present")

    loaded = runner.load_benchmark_environment(env_file)

    assert loaded == {"E2B_API_KEY": "loaded", "DAYTONA_API_KEY": "preserved"}
    assert os.environ["DAYTONA_API_KEY"] == "already-present"
    assert os.environ["E2B_API_KEY"] == "e2b-secret"
    assert "UNRELATED" not in os.environ


def test_inventory_ids_are_normalized_without_printing_objects() -> None:
    runner = _load_runner()

    values = [
        {"id": "sandbox-a"},
        SimpleNamespace(id="sandbox-b"),
        {"sandbox_id": "sandbox-c"},
        {"id": ""},
        {"name": "no-id"},
    ]

    assert runner.extract_resource_ids(values) == {"sandbox-a", "sandbox-b", "sandbox-c"}


def test_cleanup_verification_is_fail_closed() -> None:
    runner = _load_runner()

    assert runner.verify_cleanup({"a"}, set()) == {
        "status": "clean",
        "survivors": 0,
    }
    assert runner.verify_cleanup(set(), {"orphan"}) == {
        "status": "survivors",
        "survivors": 1,
    }
    assert runner.verify_cleanup(None, set()) == {
        "status": "unverifiable",
        "survivors": None,
    }


def test_tracked_payload_rejects_clean_claim_without_observed_inventories() -> None:
    runner = _load_runner()

    payload = runner.build_tracked_payload(
        {
            "providers": {
                provider: {
                    "status": "ok",
                    "cases": {
                        "action_to_immediate_frame": {
                            "status": "ok",
                            "successful_iterations": 30,
                        }
                    },
                }
                for provider in runner.EXTERNAL_PROVIDERS
            }
        },
        source_sha="a" * 40,
        evidence_date="2026-08-11",
        cleanup={
            provider: {"status": "clean", "survivors": 0}
            for provider in runner.EXTERNAL_PROVIDERS
        },
        iterations=30,
    )

    assert payload["status"] == "rejected"


def test_build_tracked_payload_keeps_failures_and_excludes_ids() -> None:
    runner = _load_runner()
    compare = {
        "benchmark": "provider-compare",
        "ok": False,
        "providers": {
            "daytona": {
                "status": "failed",
                "metadata": {"sdk_package": "daytona", "sdk_version": "0.175.0"},
                "cases": {
                    "action_to_immediate_frame": {
                        "status": "failed",
                        "iterations": 3,
                        "successful_iterations": 2,
                        "samples_ms": [1.0, 2.0],
                        "summary_ms": {"p50": 1.5, "p95": 1.95},
                        "failures": [
                            {
                                "case": "action_to_immediate_frame",
                                "phase": "measure",
                                "iteration": 2,
                                "type": "TimeoutError",
                                "message": "secret should be removed",
                            }
                        ],
                    }
                },
            }
        },
    }

    payload = runner.build_tracked_payload(
        compare,
        source_sha="a" * 40,
        evidence_date="2026-08-11",
        cleanup={"daytona": {"status": "survivors", "survivors": 1}},
        iterations=3,
        warmup_iterations=1,
    )
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "rejected"
    assert payload["providers"]["daytona"]["cleanup"] == {
        "status": "survivors",
        "survivors": 1,
    }
    assert payload["providers"]["daytona"]["source_sha"] == "a" * 40
    assert payload["cleanup"]["source_sha"] == "a" * 40
    assert payload["cleanup"]["providers"]["daytona"] == {
        "status": "survivors",
        "survivors": 1,
    }
    assert payload["providers"]["daytona"]["failures"] == [
        {"phase": "measure", "category": "TimeoutError", "iteration": 2}
    ]
    assert "sandbox-a" not in encoded
    assert "secret should be removed" not in encoded
    assert "message" not in encoded


def test_run_command_uses_only_external_action_frame_providers(tmp_path) -> None:
    runner = _load_runner()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DAYTONA_API_KEY=daytona-secret\nE2B_API_KEY=e2b-secret\nTZAFON_API_KEY=tzafon-secret\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_execute(command: list[str]) -> tuple[int, str, str]:
        calls.append(command)
        return 0, json.dumps({"ok": True, "providers": {}}), ""

    runner.execute_compare = fake_execute
    result = runner.run_benchmark(
        authorize=True,
        env_file=env_file,
        output_path=tmp_path / "result.json",
        iterations=3,
        warmup_iterations=1,
        source_sha="a" * 40,
        source_verifier=lambda _source: None,
        inventory={
            name: runner.StaticInventory(name, before=set(), after=set())
            for name in runner.EXTERNAL_PROVIDERS
        },
    )

    command = calls[0]
    assert command[:2] == [sys.executable, "-m"]
    assert command[2] == "modal_computer_use.cli"
    assert "--case" in command
    assert command[command.index("--case") + 1] == "action-to-immediate-frame"
    assert command[command.index("--providers") + 1] == "daytona,e2b,tzafon"
    assert "modal-daemon" not in command
    assert result["source_sha"] == "a" * 40
    assert (tmp_path / "result.json").is_file()
