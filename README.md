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
  --image-profile browser \
  --include-sandbox-exec \
  --iterations 5
```

The report emits JSON with action-batch, full screenshot, compressed screenshot, safe metadata,
structured failures, and explicit `not_measured` entries for Modal/Sandbox.exec cases unless the
live comparison is requested. It also measures a deterministic move+click action batch,
100-character typing latency, optional `Sandbox.exec` move+click latency, and recording
start/stop latency without including recording bytes, raw paths, artifact URIs, raw command
strings, typed text, or clipboard text. Action hot paths include daemon-side timing attribution
when the daemon returns `timing.daemon_ms`; old daemons without timing are reported as
attribution unavailable. Reported URLs strip userinfo, query strings, and fragments before they
are written to JSON.

Measure only the action batching hot path:

```bash
uv run computer-use benchmark action-batch --mock-local --iterations 5
```

Against a running daemon:

```bash
uv run computer-use benchmark action-batch --base-url http://127.0.0.1:8080 --token dev --iterations 5
```

The benchmark emits JSON with raw samples, summary timings, and the batch-vs-separate-call speedup.

See [docs/release-checklist.md](docs/release-checklist.md) for the release verification checklist,
including benchmark regeneration and boundary scans.

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
Browser-heavy workloads can opt into `ResourceConfig(profile="browser")` with
`BrowserConfig(prewarm=True)`, or `profile="browser-gpu"` with an explicit `gpu` value after
measurement shows rendering is the bottleneck. See `examples/browser_profile.py`.

To reuse an existing run-scoped sandbox, use an explicit reuse policy:

```python
computer = ComputerSandbox.attach_or_create(
    run_id="support-ticket-123",
    config=ComputerConfig(),
    reuse="by_run_id",  # "by_run_id", "by_name", or "never"
)
```

Existing sandboxes with a `computer-use.config_hash` tag must match the requested config hash or
`attach_or_create` raises `ConfigConflictError` by default. Ambiguous run ID matches raise
`SandboxAmbiguousError` instead of selecting one arbitrarily.

For owner-scoped listing and conservative stale-sandbox cleanup, use the optional manager:

```python
from modal_computer_use import ComputerSandboxManager

manager = ComputerSandboxManager()
plan = manager.cleanup_expired(ttl_seconds=3600, owner="alice")  # dry-run by default
```

Cleanup uses safe creation metadata tags and skips sandboxes whose creation time is missing or
malformed unless you terminate them explicitly by sandbox ID.

## Provider Adapters

Adapters normalize provider-returned actions into the core action schema. They do not call provider APIs:

```python
from modal_computer_use.adapters.anthropic import AnthropicAdapter

adapter = AnthropicAdapter(computer, tool_version="computer_20250124")
adapter.apply({"action": "mouse_move", "coordinate": [500, 300]})
```

Unknown provider actions fail closed by default. Pass an explicit `CoordinateSpace` when the model
saw a resized screenshot; adapters never silently scale coordinates.
When tracing is enabled, adapter actions preserve redacted provider provenance alongside the
native action that the daemon executed.

Provider-shaped screenshot helpers are available for user-owned loops:

```python
from modal_computer_use.adapters.openai import openai_computer_call_output

shot = computer.screenshots.full()
input_item = openai_computer_call_output(shot, call_id="call_123")
```

The helpers are pure conversions from native `Screenshot`/`ActionResult` models. They do not call
provider APIs or import provider SDKs.

## Security Defaults

- No unauthenticated public daemon endpoint in Modal mode.
- Local bearer auth via `COMPUTER_USE_LOCAL_TOKEN`.
- Query-string connect tokens are rejected by default.
- Artifacts are path-safe and relative to the artifact root.
- Logs redact typed text, clipboard text, tokens, screenshot bytes, noVNC URLs, and artifact bytes.
