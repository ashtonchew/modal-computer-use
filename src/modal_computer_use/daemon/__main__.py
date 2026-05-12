from __future__ import annotations

import uvicorn

from .app import create_app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, log_config=None)  # noqa: S104


if __name__ == "__main__":
    main()
