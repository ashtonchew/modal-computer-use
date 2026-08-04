from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATED_RELEASE_HEADING = re.compile(r"^## (?P<version>\S+) - \d{4}-\d{2}-\d{2}$", re.MULTILINE)


class ReleaseCandidateError(ValueError):
    """Raised when source state is not ready to publish."""


@dataclass(frozen=True)
class ReleaseCandidate:
    version: str
    tag: str
    commit: str


def _git(root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise ReleaseCandidateError("git executable is required")
    result = subprocess.run(  # noqa: S603 - fixed git command and validated arguments.
        [git, "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise ReleaseCandidateError(detail)
    return result.stdout.strip()


def _runtime_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in targets
        ):
            continue
        value = statement.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    raise ReleaseCandidateError(f"{path}: __version__ must be a string literal")


def _section(markdown: str, heading: str) -> str:
    marker = f"## {heading}"
    start = markdown.find(marker)
    if start < 0:
        raise ReleaseCandidateError(f"CHANGELOG.md is missing {marker!r}")
    content_start = markdown.find("\n", start)
    if content_start < 0:
        return ""
    next_heading = markdown.find("\n## ", content_start)
    if next_heading < 0:
        next_heading = len(markdown)
    return markdown[content_start:next_heading].strip()


def check_release_candidate(
    *,
    root: Path,
    tag: str,
    commit: str | None = None,
    main_ref: str = "refs/remotes/origin/main",
) -> ReleaseCandidate:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]
    expected_tag = f"v{project_version}"
    if tag != expected_tag:
        raise ReleaseCandidateError(f"expected release tag {expected_tag!r}, found {tag!r}")

    runtime_version = _runtime_version(root / "src" / "modal_computer_use" / "_version.py")
    openapi_version = json.loads((root / "docs" / "openapi.json").read_text(encoding="utf-8"))[
        "info"
    ]["version"]
    versions = {
        "pyproject.toml": project_version,
        "src/modal_computer_use/_version.py": runtime_version,
        "docs/openapi.json": openapi_version,
    }
    if len(set(versions.values())) != 1:
        raise ReleaseCandidateError(f"release versions differ: {versions!r}")

    tag_ref = f"refs/tags/{tag}"
    object_type = _git(root, "cat-file", "-t", tag_ref)
    if object_type != "tag":
        raise ReleaseCandidateError(f"{tag!r} must be an annotated tag, found {object_type!r}")
    tagged_commit = _git(root, "rev-parse", f"{tag_ref}^{{commit}}")
    expected_commit = commit or _git(root, "rev-parse", "HEAD")
    if tagged_commit != expected_commit:
        raise ReleaseCandidateError(
            f"{tag!r} points to {tagged_commit}, expected checked commit {expected_commit}"
        )
    main_commit = _git(root, "rev-parse", f"{main_ref}^{{commit}}")
    if tagged_commit != main_commit:
        raise ReleaseCandidateError(
            f"{tag!r} points to {tagged_commit}, but {main_ref} points to {main_commit}"
        )
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise ReleaseCandidateError("release candidate worktree must be clean")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if _section(changelog, "Unreleased"):
        raise ReleaseCandidateError("CHANGELOG.md Unreleased section must be empty")
    dated_versions = {match.group("version") for match in DATED_RELEASE_HEADING.finditer(changelog)}
    if project_version not in dated_versions:
        raise ReleaseCandidateError(
            f"CHANGELOG.md needs a dated section for version {project_version}"
        )

    return ReleaseCandidate(version=project_version, tag=tag, commit=tagged_commit)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate source state for a tagged release.")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit")
    parser.add_argument("--main-ref", default="refs/remotes/origin/main")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        candidate = check_release_candidate(
            root=args.root,
            tag=args.tag,
            commit=args.commit,
            main_ref=args.main_ref,
        )
    except (KeyError, OSError, ReleaseCandidateError, SyntaxError, tomllib.TOMLDecodeError) as exc:
        parser.error(str(exc))
    print(f"validated {candidate.tag} at {candidate.commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
