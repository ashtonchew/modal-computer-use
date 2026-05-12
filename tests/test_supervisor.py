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
