from __future__ import annotations

import ipaddress
import os

import uvicorn

from .app import create_app
from .settings import DaemonSettings, get_settings


def main() -> None:
    settings = get_settings()
    host = os.getenv("COMPUTER_USE_DAEMON_HOST")
    if not host:
        host = (
            "127.0.0.1"
            if settings.local_token or settings.allow_unauthenticated_loopback
            else "0.0.0.0"  # noqa: S104 - Modal connect-token mode must listen on port 8080.
        )
    _validate_bind(settings, host)
    port = int(os.getenv("COMPUTER_USE_DAEMON_PORT", "8080"))
    if os.getenv("COMPUTER_USE_DAEMON_HTTP_VERSION") == "2":
        _run_hypercorn_h2(settings=settings, host=host, port=port)
        return
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_config=None,
        ws_max_size=settings.max_websocket_message_bytes or None,
        ws_max_queue=4,
    )


def _run_hypercorn_h2(*, settings: DaemonSettings, host: str, port: int) -> None:
    import asyncio

    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    config = Config()
    config.bind = [f"{host}:{port}"]
    config.accesslog = None
    config.errorlog = "-"
    config.websocket_max_message_size = settings.max_websocket_message_bytes or (2**63 - 1)
    # Hypercorn accepts cleartext h2c on this bind, which keeps the daemon ready
    # for Modal h2_ports while still allowing HTTP/1.1 fallback locally.
    asyncio.run(serve(create_app(settings), config))


def _validate_bind(settings: DaemonSettings, host: str) -> None:
    has_authenticator = bool(
        settings.local_token or settings.tunnel_token or settings.require_connect_user
    )
    if not has_authenticator and not settings.allow_unauthenticated_loopback:
        raise ValueError(
            "daemon authentication is not configured; set a token, require Connect, or explicitly "
            "allow unauthenticated loopback"
        )
    if settings.allow_unauthenticated_loopback and not has_authenticator:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise ValueError("unauthenticated daemon mode must bind to a loopback address")


if __name__ == "__main__":
    main()
