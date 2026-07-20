from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from modal_computer_use.benchmarks.modal_v2_candidate import (
    sanitize_result_artifact,
    serialize_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote gate-passing Modal V2 candidate evidence")
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument("--raw-artifact-path", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    raw_bytes = args.raw.read_bytes()
    raw = json.loads(raw_bytes)
    preregistration = json.loads(args.preregistration.read_bytes())
    if not isinstance(raw, dict) or not isinstance(preregistration, dict):
        parser.error("raw result and preregistration must be JSON objects")
    normalizer_sha = _normalizer_sha(args.output, check=args.check)
    promoted = sanitize_result_artifact(
        raw,
        raw_bytes=raw_bytes,
        raw_artifact_path=args.raw_artifact_path,
        preregistration=preregistration,
        normalizer_sha=normalizer_sha,
    )
    rendered = serialize_json(promoted)
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            parser.error("tracked candidate artifact differs from deterministic regeneration")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


def _normalizer_sha(output: Path, *, check: bool) -> str:
    if check:
        payload = json.loads(output.read_bytes())
        if not isinstance(payload, dict):
            raise ValueError("tracked candidate artifact must be a JSON object")
        return str(payload["provenance"]["normalizer_sha"])
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for benchmark promotion")
    head = _git_output(git, "rev-parse", "HEAD")
    if _git_output(git, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("benchmark promotion requires a clean tracked worktree")
    return head


def _git_output(git: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        [git, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
