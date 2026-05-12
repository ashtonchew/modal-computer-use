from __future__ import annotations

import importlib.util
import os

import pytest


@pytest.mark.modal
def test_modal_smoke_skipped_without_credentials() -> None:
    if (
        importlib.util.find_spec("modal") is None
        or not os.getenv("MODAL_TOKEN_ID")
        or not os.getenv("MODAL_TOKEN_SECRET")
    ):
        pytest.skip("Modal SDK or credentials are not configured")
    from modal_computer_use import ComputerConfig, ComputerSandbox

    computer = ComputerSandbox.create(config=ComputerConfig())
    try:
        computer.wait_until_ready(timeout=120)
        assert computer.status().ready is True
    finally:
        computer.terminate()
        computer.detach()
