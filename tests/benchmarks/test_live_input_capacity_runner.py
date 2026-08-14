from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("modal") is None:
    pytest.skip("Modal benchmark runner requires the optional modal extra", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts" / "run_input_capacity_gate.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("live_input_capacity_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_capacity_runner_requires_explicit_authorization() -> None:
    runner = _load_runner()

    with pytest.raises(PermissionError, match="--authorize"):
        runner.require_live_authorization(False, credential_probe=lambda: True)


def test_capacity_runner_accepts_environment_or_authenticated_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)

    runner.require_live_authorization(True, credential_probe=lambda: True)
    with pytest.raises(PermissionError, match="credentials or an authenticated Modal profile"):
        runner.require_live_authorization(True, credential_probe=lambda: False)

    monkeypatch.setenv("MODAL_TOKEN_ID", "present")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "present")
    runner.require_live_authorization(True, credential_probe=lambda: False)


def test_capacity_runner_samples_only_processes_in_the_target_cgroup() -> None:
    runner = _load_runner()

    assert "/proc/self/cgroup" in runner._RESOURCE_SAMPLE_SCRIPT
    assert '(root / "cgroup").read_text' in runner._RESOURCE_SAMPLE_SCRIPT
    assert "!= membership" in runner._RESOURCE_SAMPLE_SCRIPT
