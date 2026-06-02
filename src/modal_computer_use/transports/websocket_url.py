from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def daemon_websocket_url(base_url: str, path: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    base_path = parts.path.rstrip("/")
    route_path = "/" + path.lstrip("/")
    return urlunsplit((scheme, parts.netloc, f"{base_path}{route_path}", parts.query, ""))
