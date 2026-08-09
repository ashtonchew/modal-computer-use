from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.daemon.supervisor import Supervisor


class _StubbornProcess:
    pid = 123

    def __init__(self) -> None:
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return 0 if self.killed else None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if not self.killed:
            raise subprocess.TimeoutExpired("Xvfb", timeout)
        return 0


class _FailingProcess:
    pid = 456

    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.events.append(f"terminate:{self.name}")
        raise RuntimeError(f"{self.name} stop failed")

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.events.append(f"wait:{self.name}")
        return 0


class _RecordingProcess:
    pid = 789

    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name
        self.stopped = False

    def poll(self) -> int | None:
        return 0 if self.stopped else None

    def terminate(self) -> None:
        self.events.append(f"terminate:{self.name}")
        self.stopped = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.events.append(f"wait:{self.name}")
        return 0


@pytest.mark.parametrize(
    ("vnc_mode", "expected_starts"),
    [
        ("off", ["xvfb", "window_manager"]),
        ("view_only", ["xvfb", "window_manager", "x11vnc", "novnc"]),
    ],
)
def test_supervisor_restarts_full_display_stack_for_named_xvfb(
    tmp_path,
    vnc_mode: str,
    expected_starts: list[str],
) -> None:
    supervisor = Supervisor(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
            vnc_mode=vnc_mode,
        )
    )
    starts: list[str] = []

    def fake_start(name: str, command: list[str]) -> None:
        starts.append(name)
        supervisor.commands[name] = command

    supervisor._start_process = fake_start  # type: ignore[method-assign]
    supervisor.commands = {"xvfb": ["Xvfb"], "window_manager": ["openbox"]}

    import anyio

    anyio.run(supervisor.restart, "xvfb")

    assert starts == expected_starts
    assert supervisor.restart_counts["xvfb"] == 1
    assert supervisor.restart_counts["window_manager"] == 1


def test_supervisor_reaps_a_process_after_kill_before_returning(tmp_path) -> None:
    import anyio

    supervisor = Supervisor(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
            vnc_mode="off",
        )
    )
    process = _StubbornProcess()
    supervisor.running = True
    supervisor.processes["xvfb"] = process  # type: ignore[assignment]

    anyio.run(supervisor.stop)

    assert process.killed is True
    assert process.wait_calls == 2


def test_supervisor_stop_attempts_all_processes_and_preserves_first_error(tmp_path) -> None:
    import anyio

    supervisor = Supervisor(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
            vnc_mode="off",
        )
    )
    events: list[str] = []
    supervisor.running = True
    supervisor.processes["xvfb"] = _RecordingProcess(events, "xvfb")  # type: ignore[assignment]
    supervisor.processes["window_manager"] = _FailingProcess(
        events,
        "window_manager",
    )  # type: ignore[assignment]
    starts: list[str] = []

    def fake_start(name: str, command: list[str]) -> None:
        del command
        starts.append(name)

    supervisor._start_process = fake_start  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="window_manager stop failed"):
        anyio.run(supervisor.restart, "xvfb")

    assert events == [
        "terminate:window_manager",
        "terminate:xvfb",
        "wait:xvfb",
    ]
    assert supervisor.running is False
    assert starts == []


def test_supervisor_uses_server_side_view_only_vnc(tmp_path) -> None:
    supervisor = Supervisor(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
            vnc_mode="view_only",
        )
    )
    commands: dict[str, list[str]] = {}

    def fake_start(name: str, command: list[str]) -> None:
        commands[name] = command
        supervisor.commands[name] = command

    supervisor._start_process = fake_start  # type: ignore[method-assign]

    import anyio

    anyio.run(supervisor.start)

    assert "-viewonly" in commands["x11vnc"]
    assert "-nopw" not in commands["x11vnc"]
    assert "-passwdfile" in commands["x11vnc"]
    assert commands["x11vnc"][:5] == ["x11vnc", "-display", ":99", "-localhost", "-forever"]
    assert commands["novnc"] == ["websockify", "--web=/usr/share/novnc/", "6080", "127.0.0.1:5900"]


def test_supervisor_vnc_off_does_not_start_vnc_processes(tmp_path) -> None:
    supervisor = Supervisor(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
            vnc_mode="off",
        )
    )
    commands: dict[str, list[str]] = {}

    def fake_start(name: str, command: list[str]) -> None:
        commands[name] = command

    supervisor._start_process = fake_start  # type: ignore[method-assign]

    import anyio

    anyio.run(supervisor.start)

    assert "x11vnc" not in commands
    assert "novnc" not in commands


def test_supervisor_control_vnc_requires_password_and_allows_input(tmp_path) -> None:
    supervisor = Supervisor(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
            vnc_mode="control",
            vnc_password="test-password",
        )
    )
    commands: dict[str, list[str]] = {}

    def fake_start(name: str, command: list[str]) -> None:
        commands[name] = command

    supervisor._start_process = fake_start  # type: ignore[method-assign]

    import anyio

    anyio.run(supervisor.start)

    x11vnc = commands["x11vnc"]
    assert "-viewonly" not in x11vnc
    assert "-nopw" not in x11vnc
    password_file = x11vnc[x11vnc.index("-passwdfile") + 1]
    assert (tmp_path / "artifacts" / ".secrets" / "x11vnc.pass").exists() is False
    assert password_file.startswith(str(tmp_path / "runtime" / ".secrets"))
    assert password_file.endswith("x11vnc.pass")
    assert Path(password_file).read_text() == "test-password"
