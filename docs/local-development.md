# Local Development

Run the daemon locally with the mock backend:

```bash
uv sync --extra dev
COMPUTER_USE_BACKEND=mock COMPUTER_USE_LOCAL_TOKEN=dev uv run computer-use-daemon
```

Then attach:

```python
from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(token="dev")
computer.wait_until_ready()
```

On Linux with X11 tools available, set `COMPUTER_USE_BACKEND=x11` and `DISPLAY=:99`.

Before submitting changes, run:

```bash
uv run ruff check .
uv run pytest
uv run mypy src
```
