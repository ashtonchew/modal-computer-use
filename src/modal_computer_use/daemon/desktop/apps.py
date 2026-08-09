from __future__ import annotations

import asyncio
import errno
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from modal_computer_use.models import ActionResult

SpawnProcess = Callable[..., Awaitable[subprocess.Popen[str]]]


class _ManagedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class X11AppController:
    def __init__(self, *, spawn: SpawnProcess) -> None:
        self._spawn = spawn
        self._processes: set[subprocess.Popen[str]] = set()

    async def launch(self, command: str, args: Sequence[str] = ()) -> ActionResult:
        try:
            process = await self._spawn(command, *args)
        except OSError as exc:
            if exc.errno == errno.E2BIG:
                raise
            return ActionResult(
                ok=False,
                message="failed to launch application",
                output={"command": command, "returncode": None, "error": str(exc)},
            )
        if process.poll() is None:
            self._processes.add(process)
        await asyncio.sleep(0.2)
        returncode = process.poll()
        return ActionResult(
            ok=returncode in (None, 0),
            message=None if returncode in (None, 0) else "application exited immediately",
            output={
                "command": command,
                "args": list(args),
                "pid": process.pid,
                "returncode": returncode,
            },
        )

    async def invalidate_display_generation(self) -> None:
        """Stop app processes that own clients on the outgoing X display."""
        await asyncio.to_thread(self.close)

    def close(self) -> None:
        """Stop every process launched through this controller."""
        processes = tuple(self._processes)
        self._processes.clear()
        first_error: Exception | None = None
        for process in processes:
            try:
                _stop_owned_process(process)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def _stop_owned_process(process: _ManagedProcess) -> None:
    if process.poll() is not None:
        return
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
        process.wait(timeout=2)
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
        process.wait(timeout=2)
