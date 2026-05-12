from __future__ import annotations

from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.daemon.supervisor import Supervisor


def test_supervisor_restarts_only_named_process(tmp_path) -> None:
    supervisor = Supervisor(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    starts: list[str] = []

    def fake_start(name: str, command: list[str]) -> None:
        starts.append(name)
        supervisor.commands[name] = command

    supervisor._start_process = fake_start  # type: ignore[method-assign]
    supervisor.commands = {
        "xvfb": ["Xvfb"],
        "window_manager": ["openbox"],
    }

    import anyio

    anyio.run(supervisor.restart, "xvfb")

    assert starts == ["xvfb"]
    assert supervisor.restart_counts["xvfb"] == 1
    assert "window_manager" not in supervisor.restart_counts


def test_supervisor_uses_server_side_view_only_vnc(tmp_path) -> None:
    supervisor = Supervisor(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
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
    assert (tmp_path / "artifacts" / ".secrets" / "x11vnc.pass").read_text() == "test-password"
    assert password_file.endswith("x11vnc.pass")
