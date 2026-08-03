# Local Development

Run the daemon locally with the mock backend:

```bash
uv sync --extra dev
COMPUTER_USE_BACKEND=mock COMPUTER_USE_LOCAL_TOKEN=dev uv run computer-use-daemon
```

The repository helper applies those mock/token defaults and forwards Ctrl-C directly to the daemon:

```bash
bash scripts/run_local_daemon.sh
```

Then attach:

```python
from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(token="dev")
try:
    computer.wait_until_ready()
finally:
    computer.detach()
```

[`examples/01_mouse_keyboard_screenshots.py`](../examples/01_mouse_keyboard_screenshots.py) is an
import-safe local walkthrough of readiness, mouse and keyboard input, screenshot capture, and client
cleanup. Run it in a second terminal after starting the daemon:

```bash
uv run python examples/01_mouse_keyboard_screenshots.py
```

Async applications can connect to the same running daemon without blocking their event loop:

```python
import asyncio

from modal_computer_use import AsyncDaemonClient


async def main() -> None:
    async with AsyncDaemonClient.local(token="dev") as computer:
        await computer.wait_until_ready()
        await computer.mouse.move(100, 120)


asyncio.run(main())
```

`AsyncDaemonClient.local()` connects to the process started above. Closing the client closes its
connections; stop the daemon from the terminal that owns it.

On Linux with X11 tools available, set `COMPUTER_USE_BACKEND=x11` and `DISPLAY=:99`.

Before submitting changes, run:

```bash
uv run ruff check .
uv run pytest
uv run mypy src
```
