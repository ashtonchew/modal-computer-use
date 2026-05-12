from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _has_modal_auth() -> bool:
    if os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET"):
        return True
    config_path = Path(os.getenv("MODAL_CONFIG_PATH", "~/.modal.toml")).expanduser()
    return config_path.is_file()


def _skip_without_modal_auth() -> None:
    if importlib.util.find_spec("modal") is None or not _has_modal_auth():
        pytest.skip("Modal SDK or credentials are not configured")


@pytest.mark.modal
def test_modal_smoke_skipped_without_credentials() -> None:
    _skip_without_modal_auth()
    from modal_computer_use import ComputerConfig, ComputerSandbox

    computer = ComputerSandbox.create(config=ComputerConfig())
    try:
        computer.wait_until_ready(timeout=120)
        assert computer.status().ready is True
    finally:
        computer.terminate()
        computer.detach()


@pytest.mark.modal
def test_modal_novnc_view_only_smoke() -> None:
    _skip_without_modal_auth()
    if os.getenv("MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE") != "1":
        pytest.skip("Set MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 to run noVNC smoke")

    from modal_computer_use import ComputerConfig, ComputerSandbox
    from modal_computer_use.config import RuntimeConfig

    computer = ComputerSandbox.create(
        config=ComputerConfig(
            expose_vnc="view_only",
            runtime=RuntimeConfig(timeout_seconds=300, idle_timeout_seconds=120),
        ),
        tags={"computer-use.smoke": "novnc-view-only"},
    )
    try:
        computer.wait_until_ready(timeout=120)
        caps = computer.client.get_json("/v1/capabilities")
        x11vnc = computer.processes.status("x11vnc")
        novnc = computer.processes.status("novnc")

        assert caps["vnc_enabled"] is True
        assert x11vnc.status == "running"
        assert novnc.status == "running"
    finally:
        computer.terminate()
        computer.detach()
