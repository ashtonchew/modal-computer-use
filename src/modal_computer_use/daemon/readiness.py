from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ReadinessSnapshot:
    ready: bool
    errors: list[str]
    expires_at: float


class ReadinessCache:
    def __init__(self, ttl_ms: int) -> None:
        self.ttl_seconds = max(0, ttl_ms) / 1000
        self._snapshot: ReadinessSnapshot | None = None
        self._lock = asyncio.Lock()

    async def backend_ready(self, backend: Any, *, force: bool = False) -> tuple[bool, list[str]]:
        now = time.monotonic()
        if not force:
            snapshot = self._snapshot
            if snapshot is not None and snapshot.expires_at > now:
                return snapshot.ready, list(snapshot.errors)

        async with self._lock:
            now = time.monotonic()
            if not force:
                snapshot = self._snapshot
                if snapshot is not None and snapshot.expires_at > now:
                    return snapshot.ready, list(snapshot.errors)

            ready, errors = await backend.ready()
            if ready and self.ttl_seconds > 0:
                self._snapshot = ReadinessSnapshot(
                    ready=True,
                    errors=list(errors),
                    expires_at=time.monotonic() + self.ttl_seconds,
                )
            else:
                self._snapshot = None
            return ready, list(errors)

    def invalidate(self) -> None:
        self._snapshot = None
