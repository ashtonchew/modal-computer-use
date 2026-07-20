from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from modal_computer_use.benchmarks.modal_optimized_frontier import (
    sanitize_result_artifact,
    serialize_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sanitize a complete Modal optimized-frontier result"
    )
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
        raise ValueError("raw result and preregistration must be objects")
    promoted = sanitize_result_artifact(
        raw,
        raw_bytes=raw_bytes,
        raw_artifact_path=args.raw_artifact_path,
        preregistration=preregistration,
        normalizer_sha=_git_output("rev-parse", "HEAD"),
    )
    expected = serialize_json(promoted)
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != expected:
            raise RuntimeError("tracked optimized-frontier artifact is not reproducible")
        print(json.dumps({"status": "verified", "output": str(args.output)}))
        return 0
    if args.output.parts[:1] != ("benchmark-data",):
        raise ValueError("promoted output must be under benchmark-data")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(expected, encoding="utf-8")
    print(json.dumps({"status": "sanitized", "output": str(args.output)}))
    return 0


def _git_output(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required")
    return subprocess.run(  # noqa: S603
        [git, *args], check=True, capture_output=True, text=True, timeout=10
    ).stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
