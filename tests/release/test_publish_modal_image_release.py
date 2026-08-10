from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "publish_modal_image_release.py"
SPEC = importlib.util.spec_from_file_location("publish_modal_image_release", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)

REVISION = "a" * 40


def test_release_cli_passes_explicit_operator_inputs_to_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "standard.json"
    captured: list[object] = []
    record = SimpleNamespace(to_dict=lambda: {"modal_image_object_id": "im-release"})
    monkeypatch.setattr(SCRIPT, "_git_state", lambda: (REVISION, True))
    monkeypatch.setattr(
        SCRIPT,
        "publish_image_release",
        lambda spec: captured.append(spec) or record,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--logical-release",
            "2.0.0",
            "--variant",
            "standard",
            "--environment",
            "prod",
            "--manifest",
            str(manifest_path),
            "--image-builder-version",
            "2025.06",
        ],
    )

    assert SCRIPT.main() == 0

    release_spec = captured[0]
    assert release_spec.source_revision == REVISION
    assert release_spec.logical_release == "2.0.0"
    assert release_spec.image_variant == "standard"
    assert release_spec.environment_name == "prod"
    assert release_spec.manifest_path == manifest_path
    assert release_spec.expected_image_builder_version == "2025.06"
    assert json.loads(capsys.readouterr().out) == {
        "modal_image_object_id": "im-release"
    }


def test_release_cli_fails_before_publication_from_a_dirty_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SCRIPT, "_git_state", lambda: (REVISION, False))
    monkeypatch.setattr(
        SCRIPT,
        "publish_image_release",
        lambda spec: pytest.fail("publication must not start from a dirty worktree"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--logical-release",
            "2.0.0",
            "--variant",
            "standard",
            "--environment",
            "prod",
            "--manifest",
            "dist/image-releases/standard.json",
            "--image-builder-version",
            "2025.06",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        SCRIPT.main()

    assert exc_info.value.code == 2
