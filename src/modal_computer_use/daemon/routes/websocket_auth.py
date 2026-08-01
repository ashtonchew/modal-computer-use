from __future__ import annotations

import ipaddress
import json
import secrets
from contextlib import suppress

from fastapi import WebSocket


def daemon_websocket_auth_error(websocket: WebSocket) -> str | None:
    settings = websocket.app.state.settings
    if settings.reject_query_tokens and "_modal_connect_token" in websocket.query_params:
        return "query_token_rejected"
    if _has_valid_tunnel_token(websocket):
        return None
    if settings.local_token:
        if not _is_loopback_websocket(websocket):
            return "local_token_requires_loopback"
        if not secrets.compare_digest(
            websocket.headers.get("authorization", ""),
            f"Bearer {settings.local_token}",
        ):
            return "unauthorized"
        return None
    if settings.require_connect_user:
        if not _is_trusted_connect_proxy(
            websocket,
            trust_private=settings.trust_private_connect_proxy,
        ):
            return "connect_token_required"
        raw = websocket.headers.get("x-verified-user-data")
        if not raw:
            return "connect_token_required"
        with suppress(json.JSONDecodeError):
            metadata = json.loads(raw)
            if isinstance(metadata, dict) and metadata.get("sdk") == "modal-computer-use":
                return None
        return "invalid_verified_user_data"
    if settings.tunnel_token:
        return "unauthorized"
    if settings.allow_unauthenticated_loopback:
        return None if _is_loopback_websocket(websocket) else "loopback_required"
    return "authentication_required"
def _is_loopback_websocket(websocket: WebSocket) -> bool:
    host = websocket.client.host if websocket.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_trusted_connect_proxy(websocket: WebSocket, *, trust_private: bool) -> bool:
    host = websocket.client.host if websocket.client else ""
    if host == "testclient":
        return True
    if host == "localhost":
        return trust_private
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return trust_private
    return trust_private and (address.is_private or address.is_link_local)


def _has_valid_tunnel_token(websocket: WebSocket) -> bool:
    token = _bearer_token(websocket)
    if not token:
        return False
    settings = websocket.app.state.settings
    if settings.tunnel_token and secrets.compare_digest(token, settings.tunnel_token):
        return True
    return websocket.app.state.tunnel_sessions.validate(token)


def _bearer_token(websocket: WebSocket) -> str | None:
    value = websocket.headers.get("authorization", "")
    prefix = "Bearer "
    if not value.startswith(prefix):
        return None
    token = value[len(prefix) :].strip()
    return token or None
