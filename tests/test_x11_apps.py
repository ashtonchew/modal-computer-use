from __future__ import annotations

import signal
import subprocess

import anyio

from modal_computer_use.daemon.desktop import apps as apps_module
from modal_computer_use.daemon.desktop.apps import X11AppController


class _OwnedProcess:
    def __init__(self, *, requires_kill: bool = False) -> None:
        self.pid = 123
        self.requires_kill = requires_kill
        self.running = True
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True
        if not self.requires_kill:
            self.running = False

    def kill(self) -> None:
        self.killed = True
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        if self.running:
            raise subprocess.TimeoutExpired("browser", timeout)
        return 0


def test_display_generation_invalidation_stops_owned_app_process() -> None:
    process = _OwnedProcess()

    async def spawn(*_args: object) -> _OwnedProcess:
        return process

    controller = X11AppController(spawn=spawn)  # type: ignore[arg-type]

    async def exercise() -> None:
        result = await controller.launch("chromium")
        assert result.ok is True
        await controller.invalidate_display_generation()

    anyio.run(exercise)

    assert process.terminated is True
    assert process.killed is False
    assert controller._processes == set()


def test_display_generation_invalidation_kills_owned_app_after_timeout() -> None:
    process = _OwnedProcess(requires_kill=True)

    async def spawn(*_args: object) -> _OwnedProcess:
        return process

    controller = X11AppController(spawn=spawn)  # type: ignore[arg-type]

    async def exercise() -> None:
        result = await controller.launch("chromium")
        assert result.ok is True
        await controller.invalidate_display_generation()

    anyio.run(exercise)

    assert process.terminated is True
    assert process.killed is True
    assert controller._processes == set()


def test_display_generation_invalidation_signals_owned_process_group(monkeypatch) -> None:
    process = _OwnedProcess()
    signals: list[tuple[int, int]] = []

    async def spawn(*_args: object) -> _OwnedProcess:
        return process

    def killpg(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        process.running = False

    monkeypatch.setattr(apps_module.subprocess, "Popen", _OwnedProcess)
    monkeypatch.setattr(apps_module.os, "killpg", killpg)
    controller = X11AppController(spawn=spawn)  # type: ignore[arg-type]

    async def exercise() -> None:
        result = await controller.launch("chromium")
        assert result.ok is True
        await controller.invalidate_display_generation()

    anyio.run(exercise)

    assert signals == [(process.pid, signal.SIGTERM)]
