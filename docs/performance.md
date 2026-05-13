# Performance

Most latency in a computer-use loop comes from network round trips and screenshot encoding. This page lists the knobs that matter.

## Batch actions

The daemon's `/v1/actions/run` route accepts an ordered list of actions and executes them under one input lock. Each action is one HTTP round trip when sent individually; a 10-action batch is one. For multi-step interactions the model returns in a single turn, batch them.

Batch limits live behind `COMPUTER_USE_MAX_BATCH_ACTIONS` (default `50`). The daemon validates the whole batch before running anything and returns per-action results. Use `continue_on_error=True` if a flaky action should not abort the rest.

Action batch responses include `timing.daemon_ms`, a daemon-side elapsed time for the batch route.
Benchmark reports use it to split total SDK round-trip latency from daemon execution time and
derive client/network/transport overhead. Older daemons that do not return timing are reported as
`attribution.status="unavailable"` rather than failed.

## Benchmark report

Use the release report command to measure current daemon hot paths without model credentials:

```bash
uv run computer-use benchmark report --mock-local --iterations 5
```

Against a running daemon:

```bash
uv run computer-use benchmark report \
  --base-url http://127.0.0.1:8080 \
  --token dev \
  --iterations 5
```

To compare the daemon hot path with live Modal `Sandbox.exec`, pass an existing sandbox ID
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

This mode attaches to the supplied sandbox only; it does not create Modal resources. The
`Sandbox.exec` case runs a safe `xdotool` move+click command and records timing, action count,
tool name, and structured failures. It does not print raw shell strings, stdout, stderr, tokens,
or noVNC URLs. Missing Modal SDK, failed attach, missing desktop tool, timeout, and nonzero exit
are reported distinctly, and the command exits nonzero once the comparison is explicitly
requested and fails.

The report emits JSON with top-level run metadata, safe daemon version/capability metadata,
benchmark entries keyed by name, raw millisecond samples, timing summaries, screenshot byte-size
summaries, structured failures, and explicit `not_measured` entries for future Modal/Sandbox.exec
cases unless live comparison is requested. Use `--output benchmark-report.json` to also write the
same JSON to disk. The reported `base_url` strips URL userinfo, query strings, and fragments so
tokens are not copied into saved reports.

Benchmark status fields are deliberately explicit:

- `ok`: measured successfully.
- `failed`: requested but one or more warmup or measured iterations failed.
- `not_measured`: not attempted in this report mode, usually because it would create or attach to
  Modal resources unless explicitly requested.
- `unsupported`: known unsupported case.
- `unavailable`: compatible daemon metadata was missing, such as older daemons without
  `timing.daemon_ms`.

For action hot paths, `samples_ms` is total client-side elapsed time. When the daemon returns
`timing.daemon_ms`, reports also include `daemon_samples_ms`, `daemon_summary_ms`,
`overhead_samples_ms`, and `overhead_summary_ms`. For the separate-call action benchmark, each
daemon sample is the sum of daemon timings for the five calls in that measured iteration.

The current report includes:

- `action_batch`: one five-action batch request compared with five separate action requests.
- `screenshot_full`: full-screen PNG screenshot latency and encoded byte size.
- `screenshot_compressed`: scaled JPEG screenshot latency and encoded byte size.
- `move_click`: one deterministic move+click action batch.
- `type_100_chars`: one deterministic 100-character typing action with safe length/method
  metadata only.
- `recording_start_stop`: recording start and stop call latency plus safe file metadata.
- `sandbox_exec`: explicit live Modal `Sandbox.exec` comparison for the same move+click hot path,
  or `not_measured` when not requested.

The report never includes raw screenshot bytes, base64 image payloads, bearer tokens, noVNC URLs,
typed text, clipboard text, recording bytes, raw recording paths, artifact URIs, raw command
strings, stdout, stderr, or ffmpeg argv. Any failed warmup or measured iteration is included under
`failures`, partial successful samples remain in the report, and the command exits nonzero.
Typing failures are redacted against the typed payload before they are included in benchmark JSON.

## Benchmark action batching

Use the benchmark CLI to measure the daemon hot path without model credentials:

```bash
uv run computer-use benchmark action-batch --mock-local --iterations 5
```

Against a running daemon:

```bash
uv run computer-use benchmark action-batch \
  --base-url http://127.0.0.1:8080 \
  --token dev \
  --iterations 5
```

The command emits JSON with metadata, raw millisecond samples, summary timings, and a batch-vs-separate-call comparison. It measures one request containing five safe actions against five separate action requests. The `Sandbox.exec` comparison is reported as `not_measured` in this version; no Modal Sandbox is created by the benchmark.

The benchmark performs one warmup iteration per case before measuring. Any daemon error or exception is included under `failures`, and the command exits nonzero. The built-in action set avoids text-entry and clipboard actions so benchmark output does not include typed or clipboard text.

## Provider comparison

Use the comparison benchmark when you want one JSON report across Modal daemon behavior,
adapter compatibility, and optional live Daytona/E2B runs:

```bash
uv run computer-use benchmark compare --mock-local --iterations 5
```

By default this measures:

- `modal-daemon`: daemon action batching, screenshots, move+click, 100-character typing, and recording start/stop.
- `openai`: OpenAI computer-action adapter normalization and native action execution against an in-process recorder.
- `anthropic`: Anthropic computer-action adapter normalization for `computer_20250124` and native action execution against an in-process recorder.
- `generic`: provider-neutral `ActionExecutor` execution against an in-process recorder.

Adapter cases do not call OpenAI, Anthropic, or any model API. They measure translation and
execution-path overhead only, which keeps the benchmark deterministic and credential-free.

Live external providers are explicit:

```bash
uv sync --extra bench-daytona --extra bench-e2b
uv run computer-use benchmark compare --providers daytona,e2b --iterations 5
uv run computer-use benchmark compare --providers daytona,e2b --env-file .env --iterations 5
```

Daytona runs require `DAYTONA_API_KEY`; `DAYTONA_API_URL` and `DAYTONA_TARGET` are reported as
safe metadata when present. E2B runs require `E2B_API_KEY`. Missing credentials produce
`not_measured` provider entries rather than crashes. Missing optional SDK packages produce
`unavailable` entries.

For local development, provider-live comparison reads a current-working-directory `.env` file when
one exists, or the file passed with `--env-file`. Dotenv values never override already exported
environment variables, so shell and CI secrets remain authoritative. Modal SDK authentication stays
separate; keep using `~/.modal.toml`, `MODAL_CONFIG_PATH`, or Modal token environment variables for
Modal itself.

The default live baselines use each provider's documented out-of-box desktop surface: Daytona calls
`daytona.create()` with no create params so the default Computer Use-capable snapshot is used, and
E2B creates the default `desktop` sandbox template at `1024x768`, DPI `96`, display `:0`. Set
`DAYTONA_SNAPSHOT` or `E2B_TEMPLATE` only for a named custom prebuilt baseline, and compare those
results separately from out-of-box provider results. Daytona custom images are not used by default
because Daytona documents VNC and Computer Use as requiring the default image unless the custom
image installs the required desktop/VNC/X11 packages.

Provider-live comparisons split lifecycle cost from warm primitive cost. `cold_create_to_ready`
creates a fresh provider sandbox, starts or verifies the desktop computer-use surface where the
SDK exposes one, records that cold readiness timing, and deletes the sandbox. Screenshot,
move/click, typing, and command cases then run on one separate ready sandbox per provider so their
samples do not include sandbox creation. Cleanup is attempted once after the warm primitive cases.

Keep comparisons fair:

- Pin SDK versions through the benchmark extras instead of using floating latest packages.
- Separate cold create, readiness, action, screenshot, stream, command, and cleanup costs.
- Compare deterministic SDK primitives before comparing model-driven task completion.
- Do not include noVNC stream URLs, provider API keys, typed text, screenshot bytes, stdout,
  stderr, artifact URIs, or recording paths in saved reports.
- Treat Daytona/E2B provider timing as total SDK/provider round-trip timing unless their SDKs
  expose daemon-internal timing; only this package's daemon currently reports `timing.daemon_ms`.

## Screenshot storage modes

`screenshots.full(...)` and `screenshots.region(...)` accept a `storage` mode:

- `inline` returns base64 in the HTTP response. Fast for small screenshots; expensive once the image is over a few hundred kilobytes.
- `artifact` writes to disk and returns a `Screenshot` with `artifact_uri`. Cheaper to ship around, slower first hit.
- `auto` (default) picks based on size.

For agent loops that call screenshot every few actions, `auto` is usually the right answer.

## Screenshot processing location

`COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION` controls where resize and re-encode happen:

- `daemon` keeps the work inside the sandbox.
- `client` ships raw bytes to the SDK and processes there.
- `auto` picks based on pixel count.

`client` cuts in-sandbox CPU at the cost of more bytes on the wire. Pick `client` if your sandbox is CPU-bound and your control plane is not; otherwise leave it on `auto`.

## Browser prewarm

If your agent always opens a browser, set `COMPUTER_USE_BROWSER_PREWARM=true`. The daemon launches the configured browser at boot so the first `browser.open_url` does not pay startup cost.
Use `examples/browser_profile.py` for an SDK-level pattern. Prewarm is optional and can be disabled
with `BrowserConfig(prewarm=False)` for deterministic tests.

## Image profile

`COMPUTER_USE_IMAGE_PROFILE` is a label reported by `/v1/capabilities`. The image you build for a Modal Sandbox should match it:

- `standard`: minimum desktop stack.
- `browser`: adds a browser and prewarm-friendly cache directories.
- `browser-gpu`: adds GPU-accelerated rendering for browser-heavy workloads.

Pick `browser-gpu` only when the agent is rendering 3D, video, or heavy WebGL; otherwise the GPU sits idle and costs money.

GPU is never enabled implicitly. Set both `ResourceConfig(profile="browser-gpu")` and a concrete
`gpu` value, such as `"T4"`, when you want Modal to request a GPU.

## Warm Pools

Warm pools are a production orchestration pattern, not core lifecycle behavior. Keep ready sandbox
IDs in your own queue, claim only entries with enough TTL left, and health-check `/readyz` before
handing a sandbox to work. See `examples/04_warm_pool.py` for a minimal pattern.

## Filesystem Snapshots

`ComputerSandbox.snapshot_directory(path)` delegates to Modal's documented
`Sandbox.snapshot_directory(path)` and returns a reusable Modal Image for that directory. Restore
by creating a fresh normal computer-use sandbox and calling `computer.mount_image(path, image)`.
Treat snapshots as filesystem/app state first; do not assume they preserve GUI memory state or
browser sessions. Modal documents directory snapshots as retained for 30 days after last creation
or use, so use Volumes or external storage for durable artifacts. See `examples/snapshot_filesystem.py`.

## Post-action delay

`COMPUTER_USE_POST_ACTION_DELAY_MS` (default `100`) inserts a sleep after every action so the desktop has time to settle. Lower it for headless workflows where you do not screenshot between actions; raise it if the next action consistently runs against a half-rendered UI.

## When in doubt

Leave the defaults. They work for the common cases. Profile first, tune second.
