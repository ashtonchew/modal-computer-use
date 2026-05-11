from __future__ import annotations

import json
from pathlib import Path

from .models import TraceEntry


class TraceWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: TraceEntry) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")


def load_trace(path: str | Path) -> list[TraceEntry]:
    trace_path = Path(path)
    if not trace_path.exists():
        return []
    return [
        TraceEntry.model_validate(json.loads(line))
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
