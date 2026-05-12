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

## Benchmarking

Generate a release-style benchmark report without Modal or model credentials:

```bash
uv run computer-use benchmark report --mock-local --iterations 5
```

Against a running daemon:

```bash
uv run computer-use benchmark report --base-url http://127.0.0.1:8080 --token dev --iterations 5
```

To add the live Modal `Sandbox.exec` hot-path comparison, attach to an existing sandbox
explicitly:

```bash
uv run computer-use benchmark report \
  --base-url https://connect.modal.run \
  --token "$MODAL_CONNECT_TOKEN" \
  --sandbox-id sb-... \
  --include-sandbox-exec \
  --iterations 5
```

The report emits JSON with action-batch, full screenshot, compressed screenshot, safe metadata,
structured failures, and explicit `not_measured` entries for Modal/Sandbox.exec cases unless the
live comparison is requested. It also measures a deterministic move+click action batch, optional
`Sandbox.exec` move+click latency, and recording start/stop latency without including recording
bytes, raw paths, artifact URIs, raw command strings, typed text, or clipboard text. Action hot
paths include daemon-side timing attribution when the daemon returns `timing.daemon_ms`; old
daemons without timing are reported as attribution unavailable.

Measure only the action batching hot path:

```bash
uv run computer-use benchmark action-batch --mock-local --iterations 5
```

Against a running daemon:

```bash
uv run computer-use benchmark action-batch --base-url http://127.0.0.1:8080 --token dev --iterations 5
```

The benchmark emits JSON with raw samples, summary timings, and the batch-vs-separate-call speedup.

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
