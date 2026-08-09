from __future__ import annotations

import asyncio
import os
import secrets
import signal
import subprocess
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Protocol

from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ProcessStatus

from .process_environment import desktop_process_environment

_X_SERVER_STARTUP_TIMEOUT_SECONDS = 5.0
_X_SERVER_STARTUP_POLL_INTERVAL_SECONDS = 0.05


class _ManagedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class Supervisor:
    """Minimal Python process supervisor facade.

    In production this starts Xvfb/window-manager/VNC processes. In local tests the
    mock backend reports stable process status without launching OS desktop tools.
    """

    def __init__(self, settings: DaemonSettings) -> None:
        self.settings = settings
        self.started_at = datetime.now(UTC)
        self.restart_counts: dict[str, int] = {}
        self.names = ["xvfb", "window_manager", "x11vnc", "novnc"]
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.log_dir = settings.artifacts_dir / "logs"
        self.commands: dict[str, list[str]] = {}
        self.running = False

    async def start(self) -> None:
        self.running = True
        self.started_at = datetime.now(UTC)
        if self.settings.backend == "mock":
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._start_process(
            "xvfb",
            [
                "Xvfb",
                self.settings.display,
                "-screen",
                "0",
                (
                    f"{self.settings.desktop_width}x"
                    f"{self.settings.desktop_height}x{self.settings.display_depth}"
                ),
                "-nolisten",
                "tcp",
                "-dpi",
                str(self.settings.desktop_dpi),
            ],
        )
        try:
            await self._wait_for_x_server_ready()
        except Exception as startup_error:
            try:
                await self.stop()
            except Exception as cleanup_error:
                startup_error.add_note(
                    "display stack cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
            raise
        wm_command = ["openbox"] if self.settings.window_manager == "openbox" else ["startxfce4"]
        self._start_process("window_manager", wm_command)
        if self.settings.vnc_mode != "off":
            password_file = self._vnc_password_file()
            x11vnc = [
                "x11vnc",
                "-display",
                self.settings.display,
                "-localhost",
                "-forever",
                "-shared",
                "-passwdfile",
                str(password_file),
            ]
            if self.settings.vnc_mode == "view_only":
                x11vnc.append("-viewonly")
            self._start_process("x11vnc", x11vnc)
            self._start_process(
                "novnc",
                ["websockify", "--web=/usr/share/novnc/", "6080", "127.0.0.1:5900"],
            )

    async def _wait_for_x_server_ready(self) -> None:
        """Wait until the new X server accepts an authenticated client."""

        deadline = monotonic() + _X_SERVER_STARTUP_TIMEOUT_SECONDS
        while True:
            xvfb = self.processes.get("xvfb")
            if xvfb is not None and xvfb.poll() is not None:
                raise RuntimeError("Xvfb exited before accepting clients")
            if await asyncio.to_thread(self._x_server_accepts_clients):
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError("Xvfb did not accept clients before its startup deadline")
            await asyncio.sleep(min(_X_SERVER_STARTUP_POLL_INTERVAL_SECONDS, remaining))

    def _x_server_accepts_clients(self) -> bool:
        env = desktop_process_environment(display=self.settings.display)
        try:
            completed = subprocess.run(  # noqa: S603 - fixed readiness probe.
                ("xdpyinfo", "-display", self.settings.display),  # noqa: S607
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=1,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    async def stop(self) -> None:
        self.running = False
        first_error: Exception | None = None
        for name in reversed(self.names):
            process = self.processes.get(name)
            if process is None or process.poll() is not None:
                continue
            try:
                _stop_process(process)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def restart(self, name: str | None = None) -> None:
        if name:
            if name not in self.names:
                raise ValueError(f"unknown process: {name}")
            if name == "xvfb":
                for process_name in self.names:
                    self.restart_counts[process_name] = (
                        self.restart_counts.get(process_name, 0) + 1
                    )
                await self.stop()
                await self.start()
                return
            self.restart_counts[name] = self.restart_counts.get(name, 0) + 1
            process = self.processes.get(name)
            if process is not None and process.poll() is None:
                _stop_process(process)
            if self.settings.backend != "mock":
                command = self.commands.get(name)
                if command is not None:
                    self._start_process(name, command)
        else:
            for process_name in self.names:
                self.restart_counts[process_name] = self.restart_counts.get(process_name, 0) + 1
            await self.stop()
            await self.start()

    def status(self, name: str) -> ProcessStatus:
        process = self.processes.get(name)
        if self.settings.backend == "mock" and name in self.names:
            state = "running" if self.running else "stopped"
        elif process is None:
            state = "stopped" if name in self.names else "unknown"
        else:
            state = "running" if process.poll() is None else "failed"
        return ProcessStatus(
            name=name,
            status=state,
            pid=process.pid if process is not None else None,
            started_at=self.started_at,
            uptime_seconds=(datetime.now(UTC) - self.started_at).total_seconds(),
            restart_count=self.restart_counts.get(name, 0),
            exit_code=process.poll() if process is not None else None,
        )

    def statuses(self) -> dict[str, ProcessStatus]:
        return {name: self.status(name) for name in self.names}

    def logs(self, name: str, tail: int = 200) -> str:
        return self._tail(self.log_dir / f"{name}.log", tail)

    def stderr(self, name: str, tail: int = 200) -> str:
        return self._tail(self.log_dir / f"{name}.stderr.log", tail)

    def _start_process(self, name: str, command: list[str]) -> None:
        self.commands[name] = command
        existing = self.processes.get(name)
        if existing is not None and existing.poll() is None:
            return
        stdout = (self.log_dir / f"{name}.log").open("ab")
        stderr = (self.log_dir / f"{name}.stderr.log").open("ab")
        env = desktop_process_environment(display=self.settings.display)
        self.processes[name] = subprocess.Popen(  # noqa: S603
            command,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=True,
        )

    def _vnc_password_file(self) -> Path:
        secret_dir = self.settings.runtime_dir / ".secrets"
        secret_dir.mkdir(parents=True, exist_ok=True)
        secret_dir.chmod(0o700)
        password_file = secret_dir / "x11vnc.pass"
        if self.settings.vnc_password is not None or not password_file.exists():
            password = self.settings.vnc_password or secrets.token_urlsafe(24)
            password_file.write_text(password)
            password_file.chmod(0o600)
        return password_file

    @staticmethod
    def _tail(path: Path, tail: int) -> str:
        if not path.exists():
            return ""
        with path.open(encoding="utf-8", errors="replace") as handle:
            lines = deque(handle, maxlen=tail)
        return "\n".join(line.rstrip("\n") for line in lines)


def _stop_process(process: _ManagedProcess) -> None:
    if isinstance(process, subprocess.Popen):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            if process.poll() is not None:
                return
            raise
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if isinstance(process, subprocess.Popen):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                if process.poll() is not None:
                    return
                raise
        else:
            process.kill()
        process.wait(timeout=5)
