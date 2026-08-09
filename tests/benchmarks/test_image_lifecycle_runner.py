from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from modal_computer_use.image import ImageCanaryRecord, ImageReleaseRecord

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts" / "run_modal_image_lifecycle_benchmark.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("image_lifecycle_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _release_record() -> ImageReleaseRecord:
    revision = "a" * 40
    return ImageReleaseRecord(
        schema_version=1,
        logical_release="2.0.0",
        source_revision=revision,
        image_variant="standard",
        image_name="modal-computer-use-standard",
        image_tag=revision,
        image_reference=f"modal-computer-use-standard:{revision}",
        workspace_name="test-workspace",
        environment_name="test-environment",
        modal_image_object_id="im-managed",
        pyproject_sha256="b" * 64,
        uv_lock_sha256="c" * 64,
        image_builder_version="2025.06",
        uv_version="0.12.3",
        modal_sdk_version="1.5.3",
        build_app_name="modal-computer-use-image-builds",
        canary=ImageCanaryRecord(
            status="passed",
            checks=(
                "healthz",
                "readyz",
                "version",
                "capabilities",
                "image_object_id",
                "browser",
                "screenshot",
                "cleanup",
            ),
            checked_at="2026-08-08T20:00:00Z",
        ),
        published_at="2026-08-08T20:01:00Z",
    )


def test_image_lifecycle_runner_pilot_uses_fixed_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    output = tmp_path / "benchmark-results" / "image-lifecycle" / "pilot.json"
    manifest = tmp_path / "standard.json"
    captured: list[object] = []
    artifact = {"status": "complete", "benchmark": "modal-image-lifecycle"}
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_git_state", lambda: ("a" * 40, True))
    monkeypatch.setattr(runner, "load_image_release_record", lambda path: _release_record())
    monkeypatch.setattr(
        runner,
        "run_modal_image_lifecycle",
        lambda spec: captured.append(spec) or artifact,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RUNNER_PATH),
            "pilot",
            "--source-sha",
            "a" * 40,
            "--manifest",
            str(manifest),
            "--region",
            "us-west-2",
            "--max-estimated-cost-usd",
            "20",
            "--caller-label",
            "test-external-caller",
            "--output",
            str(output),
        ],
    )

    assert runner.main() == 0

    benchmark_spec = captured[0]
    assert benchmark_spec.run_kind == "pilot"
    assert benchmark_spec.samples_per_arm == 2
    assert benchmark_spec.warmup_pairs == 1
    assert benchmark_spec.cpu == 1.0
    assert benchmark_spec.memory_mib == 2048
    assert benchmark_spec.max_estimated_cost_usd == 20.0
    assert benchmark_spec.caller_label == "test-external-caller"
    assert json.loads(output.read_text()) == artifact


def test_image_lifecycle_runner_primary_requires_matching_complete_pilot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    root = tmp_path / "benchmark-results" / "image-lifecycle"
    root.mkdir(parents=True)
    pilot = root / "pilot.json"
    pilot.write_text(json.dumps({"status": "rejected"}), encoding="utf-8")
    output = root / "primary.json"
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_git_state", lambda: ("a" * 40, True))
    monkeypatch.setattr(runner, "load_image_release_record", lambda path: _release_record())
    monkeypatch.setattr(
        runner,
        "run_modal_image_lifecycle",
        lambda spec: pytest.fail("primary run must not start"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RUNNER_PATH),
            "primary",
            "--source-sha",
            "a" * 40,
            "--manifest",
            str(tmp_path / "standard.json"),
            "--region",
            "us-west-2",
            "--max-estimated-cost-usd",
            "20",
            "--caller-label",
            "test-external-caller",
            "--pilot-result",
            str(pilot),
            "--output",
            str(output),
        ],
    )

    with pytest.raises(RuntimeError, match="complete pilot"):
        runner.main()
    assert not output.exists()
