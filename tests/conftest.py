from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from modal_computer_use.client import DaemonClient
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.sandbox import ComputerSandbox
from modal_computer_use.transports.http import HTTPTransport

_MODAL_LIVE_OPT_IN = "MODAL_COMPUTER_USE_RUN_LIVE_TESTS"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep credentialed Modal smoke tests non-billable by default."""
    if os.getenv(_MODAL_LIVE_OPT_IN) == "1":
        return
    skip_live = pytest.mark.skip(
        reason=f"set {_MODAL_LIVE_OPT_IN}=1 in a protected environment to run live Modal tests"
    )
    for item in items:
        if item.get_closest_marker("modal") is not None:
            item.add_marker(skip_live)


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
