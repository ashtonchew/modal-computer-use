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


@pytest.mark.modal
def test_modal_smoke_skipped_without_credentials() -> None:
    if importlib.util.find_spec("modal") is None or not _has_modal_auth():
        pytest.skip("Modal SDK or credentials are not configured")
    from modal_computer_use import ComputerConfig, ComputerSandbox

    computer = ComputerSandbox.create(config=ComputerConfig())
    try:
        computer.wait_until_ready(timeout=120)
        assert computer.status().ready is True
    finally:
        computer.terminate()
        computer.detach()
