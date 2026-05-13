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

Compare benchmark surfaces across the Modal daemon, adapter matrix, and optional live providers:

```bash
uv run computer-use benchmark compare --mock-local --iterations 5
uv run computer-use benchmark compare --providers daytona,e2b --iterations 5
uv run computer-use benchmark compare --providers daytona,e2b --env-file .env --iterations 5
```

The default comparison runs the Modal daemon plus OpenAI, Anthropic, and generic adapter
normalization/execution without calling provider APIs. Daytona and E2B live runs are credential
gated and report `not_measured` when `DAYTONA_API_KEY` or `E2B_API_KEY` is absent. Install pinned
provider extras with `uv sync --extra bench-daytona --extra bench-e2b` before live provider runs.
Provider-live comparison loads a local `.env` from the current working directory when present, or
an explicit dotenv file passed with `--env-file`. Already exported environment variables take
precedence over `.env` values. Only the documented Daytona/E2B benchmark keys are imported from
dotenv files; unrelated variables such as Modal auth/config are ignored. Keep real keys in
untracked `.env` files; use `.env.example` for the non-secret key names.
By default, Daytona uses `daytona.create()` with Daytona's default Computer Use-capable snapshot,
and E2B uses the default `desktop` template. Set `DAYTONA_SNAPSHOT` or `E2B_TEMPLATE` only when
you intentionally want to benchmark a custom prebuilt baseline.
Live provider reports separate cold create-to-ready timing from warm screenshot, action, typing,
and command cases that reuse a ready sandbox. The warm primitive set includes single move/click,
deterministic multi-click sequence, 100-character typing, 1000-character typing, screenshot, and
command echo cases. Provider reports also include `cost_estimate` metadata based on public pricing
rates and measured sandbox wall-clock runtime; this is an approximation for comparison, not an
actual billing statement.
Default Daytona runs estimate cost from provider-returned resources or documented default sandbox
resources. Modal default runs remain partial unless CPU and memory are explicitly configured.
For Modal runs whose billed Modal object was created with attribution tags, `benchmark compare` can
also attach delayed Modal billing report reconciliation without replacing `cost_estimate`:

```bash
uv run computer-use benchmark compare --base-url "$COMPUTER_USE_DAEMON_URL" \
  --providers modal-daemon \
  --modal-billing-reconcile \
  --modal-billing-start 2026-05-13T01:00:00Z \
  --modal-billing-end 2026-05-13T02:00:00Z \
  --modal-billing-tag benchmark=provider-compare \
  --modal-billing-tag benchmark_run_id=provider_compare_abc123 \
  --modal-billing-tag provider=modal-daemon
```

Modal billing reports can lag and are bucketed by full reporting intervals, so short runs may report
`not_available_yet` until the relevant interval is closed and collected, or `no_matching_tags` when
rows exist but the requested tags do not match. The reconciliation output is additive
`billing_reconciliation` metadata from Modal's billing report API; it is useful telemetry for a
tagged run, but still separate from invoices, credits, discounts, and account-level billing
adjustments. For strongest attribution, use an isolated Modal App or verify that the tags you filter
on appear in `workspace_billing_report` rows for that run.
When supported, provider reports include readback proof metadata for final cursor position and typed
keypress delivery. These proof probes use provider-native computer-use APIs for actuation and avoid
serializing typed text.

See [docs/release-checklist.md](docs/release-checklist.md) for the release verification checklist,
including benchmark regeneration and boundary scans.

## Observability

The daemon writes structured JSON logs with secret redaction. Optional OpenTelemetry is off by
default; set `COMPUTER_USE_OTEL_ENABLED=true` in an environment that already installs
`opentelemetry-api` to emit bounded spans for SDK requests, daemon routes, action execution,
artifact write/sync, and trace replay.

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

For Volume-backed artifacts, mount a Modal Volume v2 at `storage.artifacts_dir` and set
`StorageConfig(persist_artifacts=True)`. In that verified path, `computer.artifacts.sync()` runs
`sync <artifacts_dir>` inside the sandbox so files are visible through Modal Volume APIs before
termination. Already-mounted Modal readers must reload their Volume view before observing
committed changes, and concurrent writes to the same paths should be avoided. Modal Volume v1 is
not a supported immediate-sync target for this package.

For owner-scoped listing and conservative stale-sandbox cleanup, use the optional manager:

```python
from modal_computer_use import ComputerSandboxManager

manager = ComputerSandboxManager()
plan = manager.cleanup_expired(ttl_seconds=3600, owner="alice")  # dry-run by default
```

Cleanup uses safe creation metadata tags and skips sandboxes whose creation time is missing or
malformed unless you terminate them explicitly by sandbox ID.

For filesystem snapshots, use Modal's directory snapshot flow:
`computer.snapshot_directory("/home/desktop/artifacts")`, then create a fresh normal sandbox and
call `computer.mount_image("/home/desktop/artifacts", snapshot_image)`. Directory snapshots are
not durable storage and should not be used as the whole desktop base image.

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

Trace replay can validate a run without contacting a daemon, or replay supported normalized
actions into an explicit target:

```bash
uv run computer-use trace replay artifacts/traces/actions.ndjson --dry-run
uv run computer-use trace replay artifacts/traces/actions.ndjson --base-url http://127.0.0.1:8080 --token dev
```

Real replay skips redacted typed text, stops on action failure by default, and redacts screenshot
bytes/base64 in emitted JSON while preserving safe artifact references and metadata.

Provider-shaped screenshot helpers are available for user-owned loops:

```python
from modal_computer_use.adapters.openai import openai_computer_call_output

shot = computer.screenshots.full()
input_item = openai_computer_call_output(shot, call_id="call_123")
```

The helpers are pure conversions from native `Screenshot`/`ActionResult` models. They do not call
provider APIs or import provider SDKs.

## Security Defaults

- No unauthenticated public control endpoint in Modal mode; `/healthz` and `/readyz`
  remain unauthenticated probe endpoints and still reject query-string tokens.
- Local bearer auth via `COMPUTER_USE_LOCAL_TOKEN`.
- Query-string connect tokens are rejected by default.
- Artifacts are path-safe and relative to the artifact root.
- Logs redact typed text, clipboard text, tokens, screenshot bytes, noVNC URLs, and artifact bytes.
