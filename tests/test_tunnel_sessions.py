from __future__ import annotations

import pytest

from modal_computer_use.daemon.tunnel_sessions import (
    TunnelSessionLimitError,
    TunnelSessionStore,
)


def test_tunnel_sessions_expire_and_are_pruned_on_access() -> None:
    store = TunnelSessionStore()
    token, expires_at = store.mint(60, now=100.0)

    assert expires_at == 160.0
    assert store.validate(token, now=159.0) is True
    assert store.validate(token, now=160.0) is False
    assert len(store) == 0


def test_tunnel_sessions_are_unlimited_by_default() -> None:
    store = TunnelSessionStore()

    for _ in range(256):
        store.mint(60, now=100.0)

    assert len(store) == 256


def test_optional_tunnel_session_limit_rejects_without_evicting_active_token() -> None:
    store = TunnelSessionStore(max_sessions=1)
    token, _ = store.mint(60, now=100.0)

    with pytest.raises(TunnelSessionLimitError):
        store.mint(60, now=101.0)

    assert store.validate(token, now=101.0) is True
