from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modal_computer_use.benchmarks.provider_results import (
    sanitize_modal_observation_input,
    sanitize_modal_optimized_input,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate allowlisted Modal inputs for the combined provider report"
    )
    parser.add_argument("raw_optimized", type=Path)
    parser.add_argument("raw_observation", type=Path)
    parser.add_argument("optimized_output", type=Path)
    parser.add_argument("observation_output", type=Path)
    parser.add_argument("--evidence-harness-sha", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    pairs: tuple[tuple[Path, Path, Callable[..., dict[str, Any]]], ...] = (
        (args.raw_optimized, args.optimized_output, sanitize_modal_optimized_input),
        (args.raw_observation, args.observation_output, sanitize_modal_observation_input),
    )
    for raw_path, output_path, sanitizer in pairs:
        raw_bytes = raw_path.read_bytes()
        raw_payload = json.loads(raw_bytes)
        if not isinstance(raw_payload, dict):
            parser.error(f"{raw_path} must contain a JSON object")
        sanitized = sanitizer(
            raw_payload,
            raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            evidence_harness_sha=args.evidence_harness_sha,
        )
        rendered = f"{json.dumps(sanitized, indent=2, sort_keys=True)}\n"
        if args.check:
            if not output_path.is_file() or output_path.read_text(encoding="utf-8") != rendered:
                parser.error(f"{output_path} differs from generated output")
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
