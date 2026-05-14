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
    uvicorn.run(create_app(), host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
