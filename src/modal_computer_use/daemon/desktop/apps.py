from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable, Sequence

from modal_computer_use.models import ActionResult

SpawnProcess = Callable[..., Awaitable[subprocess.Popen[str]]]


class X11AppController:
    def __init__(self, *, spawn: SpawnProcess) -> None:
        self._spawn = spawn

    async def launch(self, command: str, args: Sequence[str] = ()) -> ActionResult:
        try:
            process = await self._spawn(command, *args)
        except OSError as exc:
            return ActionResult(
                ok=False,
                message="failed to launch application",
                output={"command": command, "returncode": None, "error": str(exc)},
            )
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
