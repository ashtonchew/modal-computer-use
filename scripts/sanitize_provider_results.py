from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from modal_computer_use.benchmarks.provider_results import (
    MINIMUM_ELIGIBLE_SOURCE_SHA,
    build_provider_results,
    render_provider_results_json,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a secret-safe combined provider results artifact"
    )
    parser.add_argument("provider", type=Path, help="sanitized provider-default artifact")
    parser.add_argument("modal_optimized", type=Path, help="ignored raw Modal optimized artifact")
    parser.add_argument(
        "modal_observation", type=Path, help="ignored raw Modal observation artifact"
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--expected-harness-commit", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    _verify_source_revision(args.source_sha)

    paths = (args.provider, args.modal_optimized, args.modal_observation)
    raw_bytes = tuple(path.read_bytes() for path in paths)
    payloads = tuple(json.loads(item) for item in raw_bytes)
    if not all(isinstance(item, dict) for item in payloads):
        raise ValueError("all provider result inputs must be JSON objects")
    result = build_provider_results(
        payloads[0],
        payloads[1],
        payloads[2],
        input_sha256=tuple(hashlib.sha256(item).hexdigest() for item in raw_bytes),
        source_sha=args.source_sha,
        expected_harness_commit=args.expected_harness_commit,
    )
    rendered = render_provider_results_json(result)
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            parser.error("combined provider results artifact differs from generated output")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


def _verify_source_revision(source_sha: str) -> None:
    if _git_output("rev-parse", "HEAD") != source_sha:
        raise RuntimeError("source SHA must match HEAD")
    try:
        _git_output(
            "merge-base",
            "--is-ancestor",
            MINIMUM_ELIGIBLE_SOURCE_SHA,
            source_sha,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "source SHA is older than or unrelated to the minimum eligible source"
        ) from exc
    if _git_output("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("provider results require a clean tracked worktree")


def _git_output(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to verify provider results provenance")
    return subprocess.run(  # noqa: S603
        [git, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
