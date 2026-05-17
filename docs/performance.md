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
- `screenshot_full`: full-screen screenshot latency and provider-returned encoded payload byte
  size.
- `screenshot_compressed`: scaled JPEG screenshot latency and encoded byte size.
- `move_click`: one deterministic move+click action batch.
- `move_click_sequence`: four deterministic move+click pairs that avoid same-coordinate no-op
  moves in provider SDKs that synchronize cursor movement.
- `type_100_chars`: one deterministic 100-character typing action with safe length/method
  metadata only.
- `type_1000_chars`: one deterministic 1000-character typing action with safe length/method
  metadata only.
- `recording_start_stop`: recording start and stop call latency plus safe file metadata.
- `sandbox_exec`: explicit live Modal `Sandbox.exec` comparison for the same move+click hot path,
  or `not_measured` when not requested.

The report never includes raw screenshot bytes, base64 image payloads, bearer tokens, noVNC URLs,
typed text, clipboard text, recording bytes, raw recording paths, artifact URIs, raw command
strings, stdout, stderr, or ffmpeg argv. Any failed warmup or measured iteration is included under
`failures`, partial successful samples remain in the report, and the command exits nonzero.
Typing failures are redacted against the typed payload before they are included in benchmark JSON.

For interpretation notes and one captured live run set, see:

- [Provider benchmark results interpretation](benchmark-results-interpretation.md)
- [Provider benchmark results, 2026-05-13](benchmark-results-2026-05-13.md)

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

## SDK benchmark surfaces

Use the SDK benchmark when you want one JSON report across daemon behavior and adapter
compatibility without calling model or external sandbox provider APIs:

```bash
uv run computer-use benchmark sdk --mock-local --iterations 5
```

By default this measures:

- `daemon-http`: daemon action batching, screenshots, move+click, move/click sequence,
  100-character typing, 1000-character typing, command echo, and recording start/stop.
- `openai-adapter`: OpenAI computer-action adapter normalization and native action execution against an in-process recorder.
- `anthropic-adapter`: Anthropic computer-action adapter normalization for `computer_20250124` and native action execution against an in-process recorder.
- `action-executor`: provider-neutral `ActionExecutor` execution against an in-process recorder.

Adapter cases do not call OpenAI, Anthropic, or any model API. They measure translation and
execution-path overhead only, which keeps the benchmark deterministic and credential-free.

The Modal daemon can also be measured from a freshly created Modal-backed CUA sandbox:

```bash
uv run computer-use benchmark sdk \
  --create-modal-sandbox \
  --surfaces daemon-http \
  --browser chromium \
  --gpu T4 \
  --iterations 5
```

This mode is intentionally explicit because it creates billable Modal resources. It builds a
`ComputerConfig`, passes `ResourceConfig.gpu` through to `Sandbox.create(gpu=...)`, measures cold
create-to-ready time, runs the warm daemon cases through the connect token, and terminates the
sandbox in a `finally` block. The JSON keeps the backward-compatible `daemon-http` surface key, and
records the canonical ingress label in `metadata.ingress.canonical_name`. A Modal-created sandbox
reports `modal-daemon-connect`; mock-local reports `modal-daemon-local`; caller-provided daemon
URLs report `modal-daemon-http` unless the caller labels them separately. If `--gpu` is set without
`--resource-profile`, the created sandbox uses `browser-gpu`; otherwise `--resource-profile`
controls the image/resource profile label.
Supported GPU strings and counts follow Modal's `gpu` argument, such as `T4`, `L4`, `A10`,
`L40S`, `A100`, `H100`, or `H100:2`. For GPU-specific benchmarking where Modal's H100-to-H200
upgrade would pollute attribution, use Modal's strict `H100!` string.

The raw Modal `Sandbox.exec` baseline is opt-in:

```bash
uv run computer-use benchmark sdk \
  --base-url "$COMPUTER_USE_DAEMON_URL" \
  --surfaces daemon-http,sandbox-exec \
  --sandbox-id sb-... \
  --iterations 5
```

This baseline attaches to an existing sandbox and runs a small `xdotool` command through
`Sandbox.exec`. It is useful as a transport comparison, but the daemon HTTP surface is the SDK's
normal primitive path.

Daemon HTTP entries may also include `verification` readbacks. Cursor readback checks the final
cursor position after the deterministic move/click sequence. Typing readback starts a controlled
`xev` target as a detached process and verifies that keypress events reached that target without
serializing the typed text.

The daemon HTTP surface includes additive `cost_estimate` metadata when the benchmark has Modal
resource metadata. Cost estimates use public Modal rates, measured sandbox wall-clock runtime
including warmup, and safe resource assumptions when available. They are approximate metadata, not
actual billing data. Unknown CPU or memory produces `partial`, `unknown`, or `not_measured` status
instead of a fake zero-cost total. Adapter-only and `sandbox-exec` surfaces return
`not_applicable` because they do not create resources in this benchmark.

Modal runs can also attach a separate `billing_reconciliation` object when the billed Modal
object is tagged and the caller provides a Modal billing report window. This uses
`modal.billing.workspace_billing_report` with requested tag names, filters rows by required benchmark
tags, and sums only matched row costs. It intentionally does not overwrite `cost_estimate`: the
estimate is immediate public-rate context, while reconciliation is delayed Modal-reported
billing telemetry for the tag/window. Reconciliation can return `matched`, `not_available_yet`,
`no_matching_tags`, `not_measured`, `unavailable`, or `failed`. Short benchmark runs often need a
later query because Modal billing report data is delayed and reported in full intervals. Modal rounds
partial `start` values down to the interval boundary and excludes partial `end` intervals; requested
tag keys can be absent when they were not in use, and tag changes apply to the whole reported
interval. Saved reconciliation metadata omits raw billing rows, object ids, URLs, tokens,
stdout/stderr, and daemon payloads.
For strongest attribution, run the benchmark in an isolated Modal App and pass the benchmark tags as
`app_tags` to `ComputerSandbox.create(...)`, because Modal billing reports surface tags from the
billed Modal object. Sandbox tags remain useful for operational lookup/debugging, but they should not
be the only billing attribution mechanism.

Keep SDK benchmark surfaces fair:

- Name the ingress explicitly: `modal-daemon-local`, `modal-daemon-connect`,
  `modal-daemon-tunnel`, `daytona-toolbox-http`, or `e2b-desktop-sdk`.
- Separate cold create, readiness, action, screenshot, stream, command, and cleanup costs.
- Compare deterministic SDK primitives before comparing model-driven task completion.
- Treat public-rate `cost_estimate` values as approximate context, not billing truth.
- Treat screenshot byte summaries as daemon-returned payload size.
- Do not include noVNC stream URLs, bearer tokens, typed text, screenshot bytes, stdout,
  stderr, artifact URIs, or recording paths in saved reports.
- Treat adapter timings as normalization/execution-path overhead, not provider API latency.

### Mock-local baseline, 2026-05-16

This baseline was captured from the repository root on `main` after PR #14 merged:

```bash
uv run computer-use benchmark sdk --mock-local --iterations 10 \
  --output benchmark-sdk-mock-local-2026-05-16.json
```

The run used mock-local mode, created no Modal sandbox, requested no GPU, and made no provider API
calls. Treat these numbers as a local regression baseline, not a Modal infrastructure benchmark.

| Surface | Case | Mean ms | p95 ms | Notes |
| --- | --- | ---: | ---: | --- |
| `daemon-http` | `batch_5_actions` | 207.21 | 209.48 | Includes mock daemon execution and SDK round trip. |
| `daemon-http` | `separate_5_actions` | 5.78 | 8.56 | Five separate daemon action requests. |
| `daemon-http` | `move_click` | 105.74 | 107.69 | One move and one click. |
| `daemon-http` | `move_click_sequence` | 716.90 | 721.49 | Four move/click pairs. |
| `daemon-http` | `screenshot_full` | 6.30 | 6.62 | Inline 1440x900 mock PNG, 5,965 bytes. |
| `daemon-http` | `command_echo` | 0.92 | 1.07 | Mock command route. |
| `daemon-http` | `recording_start` | 2.01 | 2.39 | Mock recording start. |
| `daemon-http` | `recording_stop` | 3.06 | 4.12 | Mock recording stop. |
| `daemon-http` | `type_100_chars` | 2.61 | 3.67 | Redacted text payload; `xdotool` method metadata only. |
| `daemon-http` | `type_1000_chars` | 1.58 | 2.34 | Redacted text payload; `xdotool` method metadata only. |
| `openai-adapter` | `adapter_matrix` | 0.045 | 0.057 | Normalization/execution only; no OpenAI API call. |
| `anthropic-adapter` | `adapter_matrix` | 0.046 | 0.050 | Normalization/execution only; no Anthropic API call. |
| `action-executor` | `adapter_matrix` | 0.021 | 0.022 | Provider-neutral in-process execution. |

### Modal no-GPU baseline, 2026-05-17

This baseline was captured from the repository root on `main` against a real Modal-backed browser
sandbox without requesting a Modal GPU:

```bash
uv run computer-use benchmark sdk \
  --create-modal-sandbox \
  --surfaces daemon-http \
  --browser chromium \
  --resource-profile browser \
  --iterations 10 \
  --output benchmark-sdk-modal-nogpu-2026-05-17-rerun.json
```

The run created one `browser` resource-profile sandbox, prewarmed Chromium, and measured the
`daemon-http` surface through `https://connect.modal.run`. It exited with `ok=true`; the earlier
same-day no-GPU run hit one 30-second HTTP read timeout and should be treated as a transient partial
sample rather than the baseline.

Environment metadata:

- `modal_cold_create_to_ready_ms`: `10042.95`
- `resource_profile`: `browser`
- `browser`: `chromium`
- `modal_run_id`: `run_d414387310d04d82`
- `modal_sandbox_id`: `sb-lRBMLiEuXSwlbXnkWJIZzG`

| Surface | Case | Status | Mean ms | p95 ms | Notes |
| --- | --- | --- | ---: | ---: | --- |
| `daemon-http` | `cold_create_to_ready` | `ok` | 10042.95 | 10042.95 | Live sandbox create through daemon readiness. |
| `daemon-http` | `batch_5_actions` | `ok` | 1045.23 | 1051.08 | One five-action daemon request. |
| `daemon-http` | `separate_5_actions` | `ok` | 3633.46 | 3884.08 | Five separate daemon action requests. |
| `daemon-http` | `move_click` | `ok` | 1007.74 | 1065.80 | One move and one click. |
| `daemon-http` | `move_click_sequence` | `ok` | 2473.91 | 2592.68 | Four move/click pairs. |
| `daemon-http` | `screenshot_full` | `ok` | 909.97 | 964.66 | Inline 1440x900 PNG, 76,477 bytes. |
| `daemon-http` | `command_echo` | `ok` | 786.35 | 822.37 | Shell command through daemon route. |
| `daemon-http` | `browser_render_metrics` | `ok` | 2085.74 | 2190.63 | Chromium loads `https://example.com`. |
| `daemon-http` | `recording_start` | `ok` | 490.60 | 560.11 | Live recording start. |
| `daemon-http` | `recording_stop` | `ok` | 390.60 | 464.69 | Live recording stop. |
| `daemon-http` | `type_100_chars` | `ok` | 1715.85 | 1906.02 | Redacted text payload; `xdotool` method metadata only. |
| `daemon-http` | `type_1000_chars` | `ok` | 7465.19 | 7560.72 | Redacted text payload; `xdotool` method metadata only. |

### Normalized SDK defaults, 2026-05-17

These runs were captured after the SDK defaults changed to `1024x768 @ 96 DPI` and
`COMPUTER_USE_POST_ACTION_DELAY_MS=0`. Both commands used 10 measured iterations and one warmup
iteration:

```bash
uv run computer-use benchmark sdk --mock-local --surfaces daemon-http --iterations 10 \
  --output benchmark-sdk-mock-local-2026-05-17-defaults.json

uv run computer-use benchmark sdk --create-modal-sandbox --surfaces daemon-http --iterations 10 \
  --output benchmark-sdk-modal-connect-1024x768-2026-05-17.json
```

The `daemon-http` key remains for CLI compatibility, but the recorded canonical labels are
`modal-daemon-local` and `modal-daemon-connect`.

| Canonical label | Case | Mean ms | Notes |
| --- | --- | ---: | --- |
| `modal-daemon-local` | `batch_5_actions` | 1.23 | In-process daemon HTTP path. |
| `modal-daemon-local` | `separate_5_actions` | 5.50 | Five daemon requests; no Modal transport. |
| `modal-daemon-local` | `move_click` | 1.16 | One move and click. |
| `modal-daemon-local` | `move_click_sequence` | 1.60 | Four move/click pairs. |
| `modal-daemon-local` | `screenshot_full` | 4.60 | Inline 1024x768 PNG, 4,051 bytes. |
| `modal-daemon-local` | `command_echo` | 0.80 | Mock command route. |
| `modal-daemon-local` | `type_100_chars` | 1.14 | Mock typing path. |
| `modal-daemon-local` | `type_1000_chars` | 1.16 | Mock typing path. |
| `modal-daemon-connect` | `cold_create_to_ready` | 12192.81 | Live sandbox create through daemon readiness. |
| `modal-daemon-connect` | `batch_5_actions` | 961.06 | One five-action daemon request through Modal Connect. |
| `modal-daemon-connect` | `separate_5_actions` | 3900.72 | Five daemon requests through Modal Connect. |
| `modal-daemon-connect` | `move_click` | 968.44 | One move and click through Modal Connect. |
| `modal-daemon-connect` | `move_click_sequence` | 1661.29 | Four move/click pairs through Modal Connect. |
| `modal-daemon-connect` | `screenshot_full` | 1176.07 | Inline 1024x768 PNG, 257,992 bytes. |
| `modal-daemon-connect` | `command_echo` | 552.56 | Shell command through daemon route. |
| `modal-daemon-connect` | `type_100_chars` | 1558.96 | Redacted text payload; `xdotool` method metadata only. |
| `modal-daemon-connect` | `type_1000_chars` | 7512.75 | Redacted text payload; `xdotool` method metadata only. |

The normalized live run still shows large per-request Modal Connect overhead. One batched
five-action request averaged `961.06ms`, while five separate action requests averaged `3900.72ms`.
Use batched actions for model turns, and use `modal-daemon-local` or a future
`modal-daemon-tunnel` run when isolating daemon implementation cost from hosted ingress cost.

## Screenshot storage modes

`screenshots.full(...)` and `screenshots.region(...)` accept a `storage` mode:

- `inline` returns base64 in the HTTP response. Fast for small screenshots; expensive once the image is over a few hundred kilobytes.
- `artifact` writes to disk and returns a `Screenshot` with `artifact_uri`. Cheaper to ship around, slower first hit.
- `auto` (default) picks based on size.

For agent loops that call screenshot every few actions, `auto` is usually the right answer.

For PNG screenshots at native scale, the daemon compares the native `maim` PNG with its Pillow
RGB re-encode and returns the smaller valid payload. This keeps simple/paletted desktops compact
without regressing desktops where the RGB re-encode compresses better than the native capture.

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

Use `BrowserConfig(open_url_on_start="https://...")` when a workload always starts on the same
page and you want startup to pay both browser creation and first navigation. Use
`BrowserConfig(launch_args=[...])` for browser-owned flags such as viewport/device-scale tuning.

## Image profile

`COMPUTER_USE_IMAGE_PROFILE` is a label reported by `/v1/capabilities`. The image you build for a Modal Sandbox should match it:

- `standard`: minimum desktop stack.
- `browser`: adds a browser and prewarm-friendly cache directories.
- `browser-gpu`: adds GPU-accelerated rendering for browser-heavy workloads.

Pick `browser-gpu` only when the agent is rendering 3D, video, or heavy WebGL; otherwise the GPU sits idle and costs money.

GPU is never enabled implicitly. Set both `ResourceConfig(profile="browser-gpu")` and a concrete
`gpu` value, such as `"T4"`, when you want Modal to request a GPU.
Browser GPU launch stays in `auto` mode by default because forcing a graphics backend can regress or
hang on some Linux/X11 stacks. Use `BrowserConfig(gpu_mode="chromium-vulkan")` only for measured
Chromium Vulkan/ANGLE experiments, or `gpu_mode="off"` to force software rendering.

`browser.render_metrics(url)` runs a Chromium-only synthetic page-load probe through the DevTools
Protocol and returns Navigation Timing, paint timing, and WebGL renderer metadata. Use it before
treating a GPU allocation as a browser-rendering improvement.

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

`COMPUTER_USE_POST_ACTION_DELAY_MS` defaults to `0`. The SDK primitive layer should execute the
requested action and return immediately; observation-loop settle policy belongs in the caller,
example, or benchmark profile. Set a nonzero delay only for screenshot-driven agent loops that need
a short settle period before the next screenshot, or use explicit `wait` actions when the caller
knows the condition it is waiting for.

## When in doubt

Leave the defaults. They work for the common cases. Profile first, tune second.
