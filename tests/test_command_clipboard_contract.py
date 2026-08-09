from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from modal_computer_use.daemon.desktop.clipboard import X11ClipboardController
from modal_computer_use.daemon.desktop.process_runner import IsolatedAsyncioProcessRunner
from modal_computer_use.daemon.desktop.x11 import X11DesktopBackend
from modal_computer_use.models import ActionResult


class _FakeClipboardOwner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.alive = True

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.alive = False
        return 0

    def kill(self) -> None:
        self.alive = False


def test_failed_command_response_does_not_disclose_process_output(
    app,
    test_client,
    caplog,
) -> None:
    command_output = "command-output-sentinel-8e3c"

    async def fail_command(_command, timeout: float = 30.0) -> ActionResult:
        del timeout
        return ActionResult(
            ok=False,
            message="command failed",
            output={
                "returncode": 7,
                "stdout": command_output,
                "stderr": command_output,
            },
        )

    app.state.backend.run_command = fail_command

    response = test_client.post(
        "/v1/commands/run",
        json={"command": ["sh", "-c", "exit 7"]},
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "command_failed",
        "message": "command failed",
        "details": {"returncode": 7},
    }
    assert command_output not in response.text
    assert command_output not in caplog.text


def test_timed_out_command_cleans_process_group_before_releasing_capacity(tmp_path: Path) -> None:
    child_marker = tmp_path / "timed-out-child"
    runner = IsolatedAsyncioProcessRunner(max_active=1)
    backend = X11DesktopBackend(process_runner=runner)

    async def exercise() -> None:
        command = _command_that_spawns_delayed_marker(child_marker)
        with pytest.raises(TimeoutError):
            await backend.run_command(command, timeout=0.2)

        follow_up = await backend.run_command(
            (sys.executable, "-c", "print('capacity-released')"),
            timeout=1,
        )
        assert follow_up.output == {
            "returncode": 0,
            "stdout": "capacity-released\n",
            "stderr": "",
        }
        await asyncio.sleep(0.5)

    try:
        asyncio.run(exercise())
    finally:
        backend.close()

    assert not child_marker.exists()


def test_cancelled_command_cleans_process_group_before_releasing_capacity(
    tmp_path: Path,
) -> None:
    child_marker = tmp_path / "cancelled-child"
    parent_started = tmp_path / "parent-started"
    runner = IsolatedAsyncioProcessRunner(max_active=1)
    backend = X11DesktopBackend(process_runner=runner)

    async def exercise() -> None:
        command = _command_that_spawns_delayed_marker(
            child_marker,
            parent_started=parent_started,
        )
        task = asyncio.create_task(backend.run_command(command, timeout=10))
        await _wait_for_path(parent_started)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        follow_up = await backend.run_command(
            (sys.executable, "-c", "print('capacity-released')"),
            timeout=1,
        )
        assert follow_up.output == {
            "returncode": 0,
            "stdout": "capacity-released\n",
            "stderr": "",
        }
        await asyncio.sleep(0.5)

    try:
        asyncio.run(exercise())
    finally:
        backend.close()

    assert not child_marker.exists()


def _command_that_spawns_delayed_marker(
    child_marker: Path,
    *,
    parent_started: Path | None = None,
) -> tuple[str, ...]:
    child_source = (
        "import pathlib, time; "
        "time.sleep(0.35); "
        f"pathlib.Path({str(child_marker)!r}).write_text('leaked')"
    )
    parent_source = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen((sys.executable, '-c', {child_source!r})); "
        + (
            f"pathlib.Path({str(parent_started)!r}).write_text('started'); "
            if parent_started is not None
            else ""
        )
        + "time.sleep(10)"
    )
    return sys.executable, "-c", parent_source


async def _wait_for_path(path: Path) -> None:
    for _attempt in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("command did not start")


def test_clipboard_set_returns_while_selection_owner_remains_available() -> None:
    owners: list[_FakeClipboardOwner] = []
    state = {"value": ""}
    selection = {"value": ""}

    async def run(
        *args: str,
        input_text: str | None = None,
        capture_output: bool = True,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args == ("xclip", "-selection", "clipboard", "-o")
        return subprocess.CompletedProcess(args, 0, selection["value"], "")

    async def spawn_owner(text: str) -> _FakeClipboardOwner:
        selection["value"] = text
        owner = _FakeClipboardOwner(text)
        owners.append(owner)
        return owner

    async def get_state() -> str:
        return state["value"]

    async def set_state(text: str) -> ActionResult:
        state["value"] = text
        return ActionResult(ok=True)

    controller = X11ClipboardController(
        run=run,
        spawn_owner=spawn_owner,
        get_state=get_state,
        set_state=set_state,
        clear_state=lambda: set_state(""),
    )

    async def exercise() -> None:
        await asyncio.wait_for(controller.set("paste-later"), timeout=0.1)
        assert [(owner.alive, owner.text) for owner in owners] == [
            (True, "paste-later")
        ]
        assert await controller.get() == "paste-later"

        await asyncio.wait_for(controller.clear(), timeout=0.1)
        assert [(owner.alive, owner.text) for owner in owners] == [
            (False, "paste-later"),
            (True, ""),
        ]
        controller.close()

    asyncio.run(exercise())

    assert [(owner.alive, owner.text) for owner in owners] == [
        (False, "paste-later"),
        (False, ""),
    ]


def test_failed_clipboard_response_does_not_disclose_text(app, test_client, caplog) -> None:
    clipboard_text = "clipboard-sentinel-c67e"

    async def fail_clipboard(_text: str) -> ActionResult:
        return ActionResult(
            ok=False,
            message=f"could not copy {clipboard_text}",
            output={"clipboard": clipboard_text},
        )

    app.state.backend.clipboard_set = fail_clipboard

    response = test_client.put(
        "/v1/clipboard/text",
        json={"text": clipboard_text},
    )

    assert response.status_code == 400
    assert clipboard_text not in response.text
    assert clipboard_text not in caplog.text
