from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

_PERSONAL_HOME_RE = re.compile(r"/" + r"Users/[A-Za-z0-9._-]+/")
_MODAL_SANDBOX_ID_RE = re.compile(r"\bsb" + r"-[A-Za-z0-9]{16,}\b")
_MODAL_RUN_ID_RE = re.compile(r"\brun" + r"_[0-9a-fA-F]{16,32}\b")
_MODAL_ENDPOINT_RE = re.compile(
    r"https?://(?:[^/\s\"']+\.modal\.host|connect\.modal\.run)(?=[/?#\s\"']|$)",
    re.IGNORECASE,
)
_ENDPOINT_KEYS = frozenset(
    {
        "base_url",
        "connect_url",
        "daemon_base_url",
        "endpoint",
        "endpoint_url",
        "no_vnc_url",
        "tunnel_url",
        "vnc_url",
    }
)


def tracked_paths(root: Path = ROOT) -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to inspect tracked repository files")
    result = subprocess.run(  # noqa: S603
        [git, "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]


def _walk_json(value: Any, location: str = "$") -> Iterator[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}"
            yield child_location, str(key), child
            yield from _walk_json(child, child_location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{location}[{index}]")


def find_violations(root: Path, paths: Iterable[Path]) -> list[str]:
    violations: list[str] = []
    for relative in paths:
        path = root / relative
        if not path.is_file():
            continue
        if (
            relative.parent == Path()
            and relative.name.startswith("benchmark")
            and relative.suffix.lower() == ".json"
        ):
            violations.append(f"root benchmark output: {relative}")

        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for label, pattern in (
            ("personal home path", _PERSONAL_HOME_RE),
            ("Modal Sandbox ID", _MODAL_SANDBOX_ID_RE),
            ("Modal run ID", _MODAL_RUN_ID_RE),
        ):
            for match in pattern.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                violations.append(f"{label}: {relative}:{line}")

        if not relative.parts or relative.parts[0] != "benchmark-data":
            continue
        if relative.suffix.lower() != ".json":
            continue
        try:
            payload = json.loads(source)
        except json.JSONDecodeError as error:
            violations.append(f"invalid benchmark JSON: {relative}:{error.lineno}")
            continue
        for location, key, value in _walk_json(payload):
            if key.lower() in _ENDPOINT_KEYS and isinstance(value, str) and value:
                violations.append(f"benchmark endpoint field: {relative}:{location}")
            if isinstance(value, str) and _MODAL_ENDPOINT_RE.search(value):
                violations.append(f"Modal endpoint value: {relative}:{location}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject tracked local benchmark output and private runtime metadata"
    )
    parser.parse_args()
    violations = find_violations(ROOT, tracked_paths(ROOT))
    if violations:
        for violation in violations:
            print(violation)
        return 1
    print("Repository hygiene scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
