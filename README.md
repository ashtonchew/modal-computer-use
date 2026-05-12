# modal-computer-use

`modal-computer-use` is a daemon-first primitive layer that turns a Modal Sandbox into a remotely controllable Linux desktop. It exposes typed Python APIs for mouse, keyboard, screenshots, recordings, windows, artifacts, action batches, and optional provider adapters.

It is not an autonomous agent framework. Model loops belong in examples or user code.

## Install

For repository development:

```bash
uv sync --extra dev
```

For Modal creation APIs:

```bash
uv sync --extra modal
```

Downstream projects can install the published package with `uv add modal-computer-use`.

## Local Daemon Quickstart

```bash
COMPUTER_USE_BACKEND=mock COMPUTER_USE_LOCAL_TOKEN=dev uv run computer-use-daemon
```

```python
from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(base_url="http://127.0.0.1:8080", token="dev")
computer.wait_until_ready()
computer.mouse.move(100, 120)
shot = computer.screenshots.full(show_cursor=True)
print(shot.width, shot.height, shot.sha256)
```

## Modal Quickstart

```python
from modal_computer_use import ComputerConfig, ComputerSandbox

computer = ComputerSandbox.create(config=ComputerConfig())
computer.wait_until_ready()
computer.browser.open_url("https://example.com")
shot = computer.screenshots.full()
computer.terminate()
computer.detach()
```

The Modal path uses Sandbox Connect Tokens for daemon access on port `8080`. noVNC is off by default and must be explicitly enabled.

## Provider Adapters

Adapters normalize provider-returned actions into the core action schema. They do not call provider APIs:

```python
from modal_computer_use.adapters.anthropic import AnthropicAdapter

adapter = AnthropicAdapter(computer, tool_version="computer_20250124")
adapter.apply({"action": "mouse_move", "coordinate": [500, 300]})
```

## Security Defaults

- No unauthenticated public daemon endpoint in Modal mode.
- Local bearer auth via `COMPUTER_USE_LOCAL_TOKEN`.
- Query-string connect tokens are rejected by default.
- Artifacts are path-safe and relative to the artifact root.
- Logs redact typed text, clipboard text, tokens, screenshot bytes, noVNC URLs, and artifact bytes.
