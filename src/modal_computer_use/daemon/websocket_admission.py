from __future__ import annotations

import asyncio
from collections import Counter
from typing import Literal

ConnectionKind = Literal["hot", "observation"]


class WebSocketAdmission:
    def __init__(self, *, hot_limit: int, observation_limit: int) -> None:
        self._limits = {"hot": hot_limit, "observation": observation_limit}
        self._active: Counter[ConnectionKind] = Counter()
        self._lock = asyncio.Lock()

    async def acquire(self, kind: ConnectionKind) -> bool:
        async with self._lock:
            limit = self._limits[kind]
            if limit and self._active[kind] >= limit:
                return False
            self._active[kind] += 1
            return True

    async def release(self, kind: ConnectionKind) -> None:
        async with self._lock:
            if self._active[kind] > 1:
                self._active[kind] -= 1
            else:
                self._active.pop(kind, None)

    def active(self, kind: ConnectionKind) -> int:
        return self._active[kind]
