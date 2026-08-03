from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_release_candidate", ROOT / "scripts" / "check_release_candidate.py"
)
assert SPEC is not None and SPEC.loader is not None
CANDIDATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CANDIDATE
SPEC.loader.exec_module(CANDIDATE)
GIT = shutil.which("git")
assert GIT is not None


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed git executable and test arguments.
        [GIT, "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path, *, unreleased: str = "") -> tuple[Path, str]:
    (tmp_path / "src" / "modal_computer_use").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "modal-computer-use"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "modal_computer_use" / "_version.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "docs" / "openapi.json").write_text(
        json.dumps({"info": {"version": "1.2.3"}}), encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## Unreleased\n\n{unreleased}\n\n"
        "## 1.2.3 - 2026-08-03\n\n- Release notes.\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Release Test")
    _git(tmp_path, "config", "user.email", "release@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "release fixture")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", commit)
    return tmp_path, commit


def test_accepts_clean_annotated_tagged_candidate(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    _git(root, "tag", "-a", "v1.2.3", "-m", "v1.2.3")

    candidate = CANDIDATE.check_release_candidate(
        root=root, tag="v1.2.3", commit=commit
    )

    assert candidate.version == "1.2.3"
    assert candidate.commit == commit


def test_rejects_lightweight_tag(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    _git(root, "tag", "v1.2.3")

    with pytest.raises(CANDIDATE.ReleaseCandidateError, match="annotated tag"):
        CANDIDATE.check_release_candidate(root=root, tag="v1.2.3", commit=commit)


def test_rejects_tag_or_commit_mismatch(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    _git(root, "tag", "-a", "v1.2.3", "-m", "v1.2.3")

    with pytest.raises(CANDIDATE.ReleaseCandidateError, match="expected release tag"):
        CANDIDATE.check_release_candidate(root=root, tag="v1.2.4", commit=commit)
    with pytest.raises(CANDIDATE.ReleaseCandidateError, match="expected checked commit"):
        CANDIDATE.check_release_candidate(root=root, tag="v1.2.3", commit="0" * 40)


def test_rejects_version_drift_and_unreleased_content(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    _git(root, "tag", "-a", "v1.2.3", "-m", "v1.2.3")
    (root / "docs" / "openapi.json").write_text(
        json.dumps({"info": {"version": "9.9.9"}}), encoding="utf-8"
    )

    with pytest.raises(CANDIDATE.ReleaseCandidateError, match="versions differ"):
        CANDIDATE.check_release_candidate(root=root, tag="v1.2.3", commit=commit)

    _git(root, "checkout", "--", "docs/openapi.json")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Not moved.\n\n"
        "## 1.2.3 - 2026-08-03\n\n- Release notes.\n",
        encoding="utf-8",
    )
    _git(root, "add", "CHANGELOG.md")
    _git(root, "commit", "-m", "retain unreleased content")
    _git(root, "tag", "-f", "-a", "v1.2.3", "-m", "v1.2.3")
    commit = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", commit)
    with pytest.raises(CANDIDATE.ReleaseCandidateError, match="Unreleased section must be empty"):
        CANDIDATE.check_release_candidate(root=root, tag="v1.2.3", commit=commit)


def test_rejects_dirty_worktree(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    _git(root, "tag", "-a", "v1.2.3", "-m", "v1.2.3")
    (root / "untracked.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(CANDIDATE.ReleaseCandidateError, match="worktree must be clean"):
        CANDIDATE.check_release_candidate(root=root, tag="v1.2.3", commit=commit)


def test_rejects_tagged_commit_that_is_not_current_origin_main(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)
    (root / "release-only.txt").write_text("not on main", encoding="utf-8")
    _git(root, "add", "release-only.txt")
    _git(root, "commit", "-m", "release-only commit")
    commit = _git(root, "rev-parse", "HEAD")
    _git(root, "tag", "-a", "v1.2.3", "-m", "v1.2.3")

    with pytest.raises(CANDIDATE.ReleaseCandidateError, match="origin/main points to"):
        CANDIDATE.check_release_candidate(root=root, tag="v1.2.3", commit=commit)


def test_rejects_undated_release_section(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    _git(root, "tag", "-a", "v1.2.3", "-m", "v1.2.3")
    changelog = root / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace(
            "## 1.2.3 - 2026-08-03", "## 1.2.3"
        ),
        encoding="utf-8",
    )
    _git(root, "add", "CHANGELOG.md")
    _git(root, "commit", "-m", "undated changelog")
    _git(root, "tag", "-f", "-a", "v1.2.3", "-m", "v1.2.3")
    commit = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", commit)

    with pytest.raises(CANDIDATE.ReleaseCandidateError, match="dated section"):
        CANDIDATE.check_release_candidate(root=root, tag="v1.2.3", commit=commit)
