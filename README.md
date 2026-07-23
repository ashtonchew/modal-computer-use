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

The Modal extra supports the Modal 1.5 line and requires Modal 1.5.2 or later.

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

Measure SDK-owned benchmark surfaces across the daemon HTTP path and adapter matrix:

```bash
uv run computer-use benchmark sdk --mock-local --iterations 5
```

To create a fresh Modal-backed CUA sandbox for the Modal daemon benchmark, including an optional
GPU request, use the explicit creation mode:

```bash
uv run computer-use benchmark sdk \
  --create-modal-sandbox \
  --surfaces daemon-http \
  --browser chromium \
  --gpu T4 \
  --iterations 5
```

Creation mode measures `product_create_to_first_screenshot`, runs the warm daemon benchmark through
the selected Modal ingress, tags the Modal app and sandbox with `benchmark=sdk-surfaces` plus a
generated `benchmark_run_id`, then terminates and detaches the sandbox. The deprecated
`cold_create_to_ready` JSON alias remains through the 1.1.x minor release and is removed in 1.2.0;
consumers should migrate to the canonical case now. The report records the canonical ingress label
in metadata. Passing `--gpu` defaults the created resource profile to `browser-gpu` unless
`--resource-profile` is supplied.
Use `--modal-region` to pin placement for created benchmark sandboxes when measuring latency from a
known caller location. The same creation knob is available in SDK code as
`ComputerConfig(runtime={"modal_region": "us-west"})`; leaving it unset preserves Modal's default
placement policy. SDK-owned co-located runner commands inherit this requested region, so the target
and runner use one placement decision. Targets attached without a matching `ComputerConfig` still
require an explicit runner region because the SDK cannot safely reconstruct their creation policy.
Broad selectors preserve scheduling flexibility but do not guarantee the same concrete provider
region; use a measured narrow selector when that stronger latency guarantee justifies its
availability and pricing tradeoffs.

To compare placement directly, run one fresh `daemon-transport-floor` sandbox per region:

```bash
uv run computer-use benchmark modal-region-ab --iterations 30 \
  --modal-region default --modal-region us-west --modal-region us-east \
  --caller-region-label dev-laptop-us-west
```

The region A/B command keeps ingress, daemon HTTP version, image profile, and resource knobs fixed
while varying only Modal placement. `--caller-region-label` is descriptive metadata for where the
benchmark caller or model loop ran; it does not affect Modal placement.

Turn the raw JSON into a PR-ready table with:

```bash
uv run computer-use benchmark modal-region-summary modal-region-ab-attested-h1-30x-20260526.json
```

For production loops, keep the SDK default unpinned until you have a benchmark from the same caller
environment. Then pin `runtime.modal_region` near the caller/model loop, not necessarily near the
end user. See `examples/region_colocation.py`.

To test whether moving the benchmark client into Modal lowers the receive floor further, compare the
normal external caller with an ephemeral same-region Modal runner:

```bash
uv run computer-use benchmark modal-colocated-client --iterations 30 \
  --modal-region us-west --caller-region-label dev-laptop-us-west --browser chromium \
  --surface daemon-transport-floor --surface daemon-observation-stream
```

This command creates a target sandbox, measures the selected surfaces from the current process,
then runs the same benchmark from a temporary Modal runner in the target region. Use
`daemon-transport-floor` for raw receive-floor attribution and `daemon-observation-stream` for the
experimental action-to-first-changed-frame workload. That workload measures a correlated first
visual response, not semantic application readiness or a complete model loop. Observation-stream
benchmark runs need a browser-capable target image, so pass `--browser chromium` or
`--browser firefox`. See the [Alpha visual-change observation guide](docs/experimental-visual-change-observation.md)
for the contract and limitations.

Application workloads can use `run_modal_daemon_command_with_fallback()` for the same topology.
Create the target with an explicit `runtime.modal_region`, then run the whole latency-sensitive
session as one runner command; the helper inherits the target's requested region. It rejects a
conflicting explicit runner region rather than silently losing co-location. See
`examples/modal_colocated_runner.py`.

The default SDK benchmark runs daemon HTTP plus OpenAI, Anthropic, and generic action-executor
adapter normalization/execution without calling provider APIs. The raw Modal `Sandbox.exec`
surface is opt-in with `--surface sandbox-exec --sandbox-id <id>` because it attaches to a live
sandbox and is a transport baseline, not the SDK's recommended hot path.
For Modal runs whose billed Modal object was created with attribution tags, `benchmark sdk` can
also attach delayed Modal billing report reconciliation without replacing `cost_estimate`:

```bash
uv run computer-use benchmark sdk --base-url "$COMPUTER_USE_DAEMON_URL" \
  --surfaces daemon-http \
  --modal-billing-reconcile \
  --modal-billing-start 2026-05-13T01:00:00Z \
  --modal-billing-end 2026-05-13T02:00:00Z \
  --modal-billing-tag benchmark=sdk-surfaces \
  --modal-billing-tag benchmark_run_id=sdk_surface_abc123 \
  --modal-billing-tag surface=daemon-http
```

Modal billing reports can lag and are bucketed by full reporting intervals, so short runs may report
`not_available_yet` until the relevant interval is closed and collected, or `no_matching_tags` when
rows exist but the requested tags do not match. The reconciliation output is additive
`billing_reconciliation` metadata from Modal's billing report API; it is useful telemetry for a
tagged run, but still separate from invoices, credits, discounts, and account-level billing
adjustments. For strongest attribution, pass benchmark tags as `app_tags` to
`ComputerSandbox.create(...)` and use an isolated benchmark `app_name`, because Modal billing reports
surface tags from the billed Modal object. Sandbox tags are still useful for lookup/debugging, but
should not be the only billing attribution mechanism.
When supported, daemon HTTP reports include readback proof metadata for final cursor position and
typed keypress delivery. These proof probes use daemon computer-use APIs for actuation and avoid
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
measurement shows rendering is the bottleneck. Browser GPU launch stays in autodetect mode by
default; use `BrowserConfig(gpu_mode="chromium-vulkan" | "off")` only when benchmarking driver
behavior. See `examples/browser_profile.py`.

The inline Image builder remains the default. To publish revision-tagged standard, Firefox, and
Chromium Images from a clean commit, run:

```bash
uv run python scripts/publish_modal_images.py --environment prod
```

Select a published Image with `ImageConfig(source="named", revision="<full-git-sha>")`. Named
selection never falls back to an inline build. Select a prior revision or restore the default
`ImageConfig()` to roll back. The publisher never replaces an existing revision tag. A retry keeps
published variants and publishes only missing variants. Serialize publishers for the same
Environment because Modal does not provide an atomic create-only publish operation.

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

Wrap a raw Volume in `ModalVolumeMount` to opt into `read_only=True` or a Volume `sub_path` for one
mount. Raw Volume values retain their existing behavior. Call `computer.reload_volumes(timeout=55)`
to block until every mounted Volume reloads or Modal raises a timeout.

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

adapter = AnthropicAdapter(
    computer,
    tool_version="computer_20251124",
    enable_zoom=True,
)
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

See the runnable [OpenAI](examples/03_openai_computer_loop.py) and
[Anthropic](examples/anthropic_message_server.py) loops. These examples use the current provider
request formats. They set execution limits, keep tool results in order, and return screenshots
after graphical actions. The [computer-use landscape](research/computer-use-landscape.md) compares
model providers, browser-agent frameworks, browser infrastructure, and desktop infrastructure.

## Security Defaults

- No unauthenticated public control endpoint in Modal mode; `/healthz` and `/readyz`
  remain unauthenticated probe endpoints and still reject query-string tokens.
- Local bearer auth via `COMPUTER_USE_LOCAL_TOKEN`.
- Query-string connect tokens are rejected by default.
- Artifacts are path-safe and relative to the artifact root.
- Logs redact typed text, clipboard text, tokens, screenshot bytes, noVNC URLs, and artifact bytes.
