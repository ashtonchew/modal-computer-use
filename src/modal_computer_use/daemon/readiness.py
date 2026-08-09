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
    backend_generation: int | None


def _backend_generation(backend: Any) -> int | None:
    generation = getattr(backend, "readiness_generation", None)
    return generation if isinstance(generation, int) else None


class ReadinessCache:
    def __init__(self, ttl_ms: int) -> None:
        self.ttl_seconds = max(0, ttl_ms) / 1000
        self._snapshot: ReadinessSnapshot | None = None
        self._lock = asyncio.Lock()
        self._generation = 0

    async def backend_ready(self, backend: Any, *, force: bool = False) -> tuple[bool, list[str]]:
        now = time.monotonic()
        if not force:
            snapshot = self._snapshot
            if (
                snapshot is not None
                and snapshot.expires_at > now
                and snapshot.backend_generation == _backend_generation(backend)
            ):
                return snapshot.ready, list(snapshot.errors)

        async with self._lock:
            now = time.monotonic()
            if not force:
                snapshot = self._snapshot
                if (
                    snapshot is not None
                    and snapshot.expires_at > now
                    and snapshot.backend_generation == _backend_generation(backend)
                ):
                    return snapshot.ready, list(snapshot.errors)

            generation = self._generation
            backend_generation = _backend_generation(backend)
            ready, errors = await backend.ready()
            current_backend_generation = _backend_generation(backend)
            if (
                ready
                and self.ttl_seconds > 0
                and generation == self._generation
                and backend_generation == current_backend_generation
            ):
                self._snapshot = ReadinessSnapshot(
                    ready=True,
                    errors=list(errors),
                    expires_at=time.monotonic() + self.ttl_seconds,
                    backend_generation=current_backend_generation,
                )
            else:
                self._snapshot = None
            return ready, list(errors)

    def mark_ready(self, backend: Any) -> None:
        if self.ttl_seconds <= 0:
            return
        self._snapshot = ReadinessSnapshot(
            ready=True,
            errors=[],
            expires_at=time.monotonic() + self.ttl_seconds,
            backend_generation=_backend_generation(backend),
        )

    def invalidate(self) -> None:
        """Discard readiness proven against an earlier desktop generation."""

        self._generation += 1
        self._snapshot = None
