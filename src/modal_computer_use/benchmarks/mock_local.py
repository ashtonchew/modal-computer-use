from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from ..client import DaemonClient
from ..daemon.app import create_app
from ..daemon.settings import DaemonSettings
from ..transports.http import HTTPTransport


def _with_mock_local_client(callback: Callable[[DaemonClient], dict[str, Any]]) -> dict[str, Any]:
    with TemporaryDirectory(prefix="modal-computer-use-benchmark-") as temp_dir:
        root = Path(temp_dir)
        with redirect_stdout(StringIO()):
            app = create_app(
                DaemonSettings(
                    backend="mock",
                    artifacts_dir=root / "artifacts",
                    recordings_dir=root / "recordings",
                    trace_dir=root / "artifacts" / "traces",
                    local_token="dev",  # noqa: S106 - mock-local benchmark auth only.
                    input_rate_limit_per_sec=0,
                )
            )
            with TestClient(app, headers={"Authorization": "Bearer dev"}) as test_client:
                transport = HTTPTransport(
                    "http://testserver",
                    token="dev",  # noqa: S106 - mock-local benchmark auth only.
                    client=test_client,
                )
                client = DaemonClient(
                    "http://testserver",
                    token="dev",  # noqa: S106 - mock-local benchmark auth only.
                    transport=transport,
                )
                try:
                    return callback(client)
                finally:
                    client.close()
