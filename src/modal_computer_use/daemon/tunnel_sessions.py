from __future__ import annotations

import secrets
import time


class TunnelSessionLimitError(RuntimeError):
    pass


class TunnelSessionStore:
    def __init__(self, *, max_sessions: int = 0) -> None:
        self.max_sessions = max_sessions
        self._sessions: dict[str, float] = {}

    def validate(self, token: str, *, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        expires_at = self._sessions.get(token)
        if expires_at is None:
            return False
        if expires_at <= current_time:
            self._sessions.pop(token, None)
            return False
        return True

    def mint(self, ttl_seconds: int, *, now: float | None = None) -> tuple[str, float]:
        current_time = time.time() if now is None else now
        self.prune_expired(now=current_time)
        if self.max_sessions and len(self._sessions) >= self.max_sessions:
            raise TunnelSessionLimitError
        token = secrets.token_urlsafe(32)
        expires_at = current_time + ttl_seconds
        self._sessions[token] = expires_at
        return token, expires_at

    def prune_expired(self, *, now: float | None = None) -> None:
        current_time = time.time() if now is None else now
        for token, expires_at in tuple(self._sessions.items()):
            if expires_at <= current_time:
                self._sessions.pop(token, None)

    def __len__(self) -> int:
        return len(self._sessions)
