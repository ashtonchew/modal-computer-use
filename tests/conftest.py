from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from modal_computer_use.client import DaemonClient
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.sandbox import ComputerSandbox
from modal_computer_use.transports.http import HTTPTransport


@pytest.fixture()
def app(tmp_path):
    return create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
        )
    )


@pytest.fixture()
def test_client(app):
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        yield client


@pytest.fixture()
def computer(test_client):
    transport = HTTPTransport("http://testserver", token="dev", client=test_client)
    return ComputerSandbox(DaemonClient("http://testserver", token="dev", transport=transport))
