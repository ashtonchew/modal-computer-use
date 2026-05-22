from __future__ import annotations

import os

import uvicorn

from .app import create_app


def main() -> None:
    host = os.getenv("COMPUTER_USE_DAEMON_HOST")
    if not host:
        host = (
            "127.0.0.1"
            if os.getenv("COMPUTER_USE_LOCAL_TOKEN")
            else "0.0.0.0"  # noqa: S104 - Modal connect-token mode must listen on port 8080.
        )
    port = int(os.getenv("COMPUTER_USE_DAEMON_PORT", "8080"))
    if os.getenv("COMPUTER_USE_DAEMON_HTTP_VERSION") == "2":
        _run_hypercorn_h2(host=host, port=port)
        return
    uvicorn.run(create_app(), host=host, port=port, log_config=None)


def _run_hypercorn_h2(*, host: str, port: int) -> None:
    import asyncio

    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    config = Config()
    config.bind = [f"{host}:{port}"]
    config.accesslog = None
    config.errorlog = "-"
    # Hypercorn accepts cleartext h2c on this bind, which keeps the daemon ready
    # for Modal h2_ports while still allowing HTTP/1.1 fallback locally.
    asyncio.run(serve(create_app(), config))


if __name__ == "__main__":
    main()
