from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from check_release_bundle import distribution_paths

ReleaseState = Literal["missing", "draft", "immutable"]
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")


class GitHubReleaseError(ValueError):
    """Raised when a GitHub Release cannot be published or resumed safely."""


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)  # noqa: S603


def _executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise GitHubReleaseError(f"{name} executable is required")
    return path


def remote_tag_commit(
    tag: str,
    *,
    remote: str = "origin",
    runner: Runner = _run,
) -> str:
    result = runner(
        [
            _executable("git"),
            "ls-remote",
            "--tags",
            remote,
            f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}",
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git ls-remote failed"
        raise GitHubReleaseError(detail)

    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise GitHubReleaseError(f"unexpected git ls-remote output: {line!r}")
        object_id, ref = fields
        if not GIT_OBJECT_ID.fullmatch(object_id):
            raise GitHubReleaseError(f"invalid Git object ID for {ref!r}: {object_id!r}")
        refs[ref] = object_id

    tag_ref = f"refs/tags/{tag}"
    peeled_ref = f"{tag_ref}^{{}}"
    if tag_ref not in refs:
        raise GitHubReleaseError(f"remote tag {tag!r} does not exist")
    if peeled_ref not in refs:
        raise GitHubReleaseError(f"remote tag {tag!r} is not annotated")
    return refs[peeled_ref]


def verify_remote_tag(
    tag: str,
    expected_commit: str,
    *,
    remote: str = "origin",
    runner: Runner = _run,
) -> None:
    actual_commit = remote_tag_commit(tag, remote=remote, runner=runner)
    if actual_commit != expected_commit:
        raise GitHubReleaseError(
            f"remote tag {tag!r} points to {actual_commit}, expected {expected_commit}"
        )


def query_release(tag: str, *, runner: Runner = _run) -> dict[str, Any] | None:
    result = runner(
        [
            _executable("gh"),
            "release",
            "view",
            tag,
            "--json",
            "tagName,isDraft,isImmutable,assets",
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "gh release view failed"
        if "release not found" in detail.lower():
            return None
        raise GitHubReleaseError(detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubReleaseError("gh release view returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GitHubReleaseError("gh release view must return a JSON object")
    return payload


def classify_release(payload: dict[str, Any] | None, *, tag: str) -> ReleaseState:
    if payload is None:
        return "missing"
    if payload.get("tagName") != tag:
        raise GitHubReleaseError(f"release tag does not match {tag!r}")
    is_draft = payload.get("isDraft")
    is_immutable = payload.get("isImmutable")
    if not isinstance(is_draft, bool) or not isinstance(is_immutable, bool):
        raise GitHubReleaseError("release state fields must be booleans")
    if is_draft and not is_immutable:
        return "draft"
    if not is_draft and is_immutable:
        return "immutable"
    raise GitHubReleaseError("release must be a mutable draft or an immutable publication")


def expected_asset_names(*, distributions: Path, checksums: Path) -> list[str]:
    wheel, sdist = distribution_paths(distributions)
    if not checksums.is_file():
        raise GitHubReleaseError(f"checksum file does not exist: {checksums}")
    return sorted((wheel.name, sdist.name, checksums.name))


def verify_asset_names(payload: dict[str, Any], *, expected: list[str]) -> None:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise GitHubReleaseError("release assets must be a list")
    actual: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise GitHubReleaseError("each release asset must have a string name")
        actual.append(asset["name"])
    if sorted(actual) != expected:
        raise GitHubReleaseError(
            f"release assets differ: expected {expected}, found {sorted(actual)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate GitHub Release publication state.")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    tag_parser = subparsers.add_parser("tag", help="Verify the current remote annotated tag.")
    tag_parser.add_argument("--tag", required=True)
    tag_parser.add_argument("--expected-commit", required=True)
    tag_parser.add_argument("--remote", default="origin")

    state_parser = subparsers.add_parser("state", help="Classify release publication state.")
    state_parser.add_argument("--tag", required=True)

    assets_parser = subparsers.add_parser("assets", help="Verify the complete release asset set.")
    assets_parser.add_argument("--tag", required=True)
    assets_parser.add_argument("--distributions", required=True, type=Path)
    assets_parser.add_argument("--checksums", required=True, type=Path)

    args = parser.parse_args()
    try:
        if args.mode == "tag":
            verify_remote_tag(
                args.tag,
                args.expected_commit,
                remote=args.remote,
            )
            print(f"verified remote tag {args.tag} at {args.expected_commit}")
        elif args.mode == "state":
            print(classify_release(query_release(args.tag), tag=args.tag))
        else:
            payload = query_release(args.tag)
            if payload is None:
                raise GitHubReleaseError(f"release {args.tag!r} does not exist")
            verify_asset_names(
                payload,
                expected=expected_asset_names(
                    distributions=args.distributions,
                    checksums=args.checksums,
                ),
            )
            print(f"verified exact release assets for {args.tag}")
    except (GitHubReleaseError, OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
