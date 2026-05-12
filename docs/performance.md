# Performance

Most latency in a computer-use loop comes from network round trips and screenshot encoding. This page lists the knobs that matter.

## Batch actions

The daemon's `/v1/actions/run` route accepts an ordered list of actions and executes them under one input lock. Each action is one HTTP round trip when sent individually; a 10-action batch is one. For multi-step interactions the model returns in a single turn, batch them.

Batch limits live behind `COMPUTER_USE_MAX_BATCH_ACTIONS` (default `50`). The daemon validates the whole batch before running anything and returns per-action results. Use `continue_on_error=True` if a flaky action should not abort the rest.

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

The report emits JSON with top-level run metadata, safe daemon version/capability metadata,
benchmark entries keyed by name, raw millisecond samples, timing summaries, screenshot byte-size
summaries, structured failures, and explicit `not_measured` entries for future Modal/Sandbox.exec
or recording cases. Use `--output benchmark-report.json` to also write the same JSON to disk.

The current report includes:

- `action_batch`: one five-action batch request compared with five separate action requests.
- `screenshot_full`: full-screen PNG screenshot latency and encoded byte size.
- `screenshot_compressed`: scaled JPEG screenshot latency and encoded byte size.

The report never includes raw screenshot bytes, base64 image payloads, bearer tokens, noVNC URLs,
typed text, or clipboard text. Any failed warmup or measured iteration is included under
`failures`, partial successful samples remain in the report, and the command exits nonzero.

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

## Image profile

`COMPUTER_USE_IMAGE_PROFILE` is a label reported by `/v1/capabilities`. The image you build for a Modal Sandbox should match it:

- `standard`: minimum desktop stack.
- `browser`: adds a browser and prewarm-friendly cache directories.
- `browser-gpu`: adds GPU-accelerated rendering for browser-heavy workloads.

Pick `browser-gpu` only when the agent is rendering 3D, video, or heavy WebGL; otherwise the GPU sits idle and costs money.

## Post-action delay

`COMPUTER_USE_POST_ACTION_DELAY_MS` (default `100`) inserts a sleep after every action so the desktop has time to settle. Lower it for headless workflows where you do not screenshot between actions; raise it if the next action consistently runs against a half-rendered UI.

## When in doubt

Leave the defaults. They work for the common cases. Profile first, tune second.
