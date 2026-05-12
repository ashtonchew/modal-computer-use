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
    from modal_computer_use.models import Point

    computer = ComputerSandbox.create(config=ComputerConfig())
    try:
        assert computer.status().ready is True
        actions = computer.actions.run(
            [
                {"type": "move", "x": 24, "y": 25},
                {"type": "cursor_position"},
                {
                    "type": "drag",
                    "path": [{"x": 24, "y": 25}, {"x": 30, "y": 35}],
                    "button": "left",
                    "duration_ms": 0,
                },
                {"type": "scroll", "direction": "down", "amount": 1, "x": 30, "y": 35},
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "move", "x": 40, "y": 45}],
                },
                {"type": "keypress", "key": "Escape"},
                {"type": "release_all"},
            ],
            continue_on_error=False,
        )
        assert actions.ok is True
        assert computer.mouse.position() == Point(x=40, y=45)
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
        command = computer.commands.run("pgrep", "-af", "x11vnc")
        assert command.ok is True
        argv = command.output["stdout"]
        assert "-passwdfile" in argv
        assert "-nopw" not in argv
        assert "-viewonly" in argv
    finally:
        computer.terminate()
        computer.detach()
