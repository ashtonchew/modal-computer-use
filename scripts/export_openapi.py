from __future__ import annotations

import argparse
import json
from pathlib import Path

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "docs" / "openapi.json"


def generate_openapi_json() -> str:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=Path("/tmp/modal-computer-use-artifacts"),  # noqa: S108
            recordings_dir=Path("/tmp/modal-computer-use-recordings"),  # noqa: S108
            local_token="dev",  # noqa: S106 - schema export uses mock local settings only.
        )
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the daemon OpenAPI schema.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if docs/openapi.json is missing or stale",
    )
    args = parser.parse_args()

    generated = generate_openapi_json()
    if args.check:
        current = OPENAPI_PATH.read_text() if OPENAPI_PATH.exists() else ""
        if current != generated:
            print("docs/openapi.json is stale; run `uv run python scripts/export_openapi.py`")
            return 1
        return 0

    OPENAPI_PATH.write_text(generated)
    print(f"wrote {OPENAPI_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
