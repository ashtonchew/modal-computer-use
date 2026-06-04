# Performance

Most latency in a computer-use loop comes from network round trips and screenshot encoding. This page lists the knobs that matter.

## Batch actions

The daemon's `/v1/actions/run` route accepts an ordered list of actions and executes them under one input lock. Each action is one HTTP round trip when sent individually; a 10-action batch is one. For multi-step interactions the model returns in a single turn, batch them.

Batch limits live behind `COMPUTER_USE_MAX_BATCH_ACTIONS` (default `50`). The daemon validates the whole batch before running anything and returns per-action results. Use `continue_on_error=True` if a flaky action should not abort the rest.

Action batch responses include `timing.daemon_ms`, a daemon-side elapsed time for the batch route.
Benchmark reports use it to split total SDK round-trip latency from daemon execution time and
derive client/network/transport overhead. Older daemons that do not return timing are reported as
`attribution.status="unavailable"` rather than failed.

## Pointer input hot path

The X11 daemon supports `COMPUTER_USE_INPUT_BACKEND=auto|xtest|xdotool`.

- `auto` is the default. It probes the XTest extension and uses a persistent X connection for
  pointer actions when available, otherwise it falls back to `xdotool`.
- `xtest` requires XTest and fails readiness if the extension cannot be opened. Use it for
  benchmark runs that must prove the persistent backend is active.
- `xdotool` preserves the older subprocess-backed behavior and remains the compatibility fallback.

The XTest backend currently covers mouse movement, click, button down/up, scroll, and drag movement.
Keyboard input still uses the existing keyboard path because text, layouts, modifiers, and clipboard
restore semantics need separate treatment. Action benchmark observations include `input_backend`
when a measured action used a pointer backend, and summarized cases include `input_backends`.

## Screenshot hot paths

Use the raw binary screenshot routes for latency-sensitive observation loops:

```bash
POST /v1/screenshots/full/raw
POST /v1/actions/run/raw-screenshot
```

The fused action route is the canonical model-loop path because it executes the requested actions
and returns the post-action screenshot in one HTTP request. That avoids the extra network round
trip of `POST /v1/actions/run` followed by `POST /v1/screenshots/full/raw`.

For tighter interactive loops, use the hot-session WebSocket:

```text
GET /v1/session/hot
```

The hot session keeps one authenticated daemon connection open and accepts ordered JSON control
frames for `run_actions`, `run_raw_screenshot`, and `screenshot_raw`. Raw observations are returned
as a JSON metadata frame followed by one binary image frame. This is the SDK's lowest-overhead
control path because it avoids opening a fresh request for every primitive while reusing the same
daemon batch and screenshot execution code as the REST routes.

Use the hot session when the caller owns a tight loop and can keep a connection open. Keep the REST
routes for broad compatibility, simple one-shot calls, idempotency-key workflows, and environments
where WebSocket egress is unavailable. On Modal, WebSockets are RFC 6455 and are not WebSockets over
HTTP/2, so the hot-session win comes from persistent session reuse rather than HTTP/2 multiplexing.

For continuous observation, use the observation stream instead of polling screenshots:

```text
GET /v1/observations/stream
```

The observation stream is a passive, server-pushed WebSocket. The daemon protocol supports the raw
`json-binary` shape, which sends one JSON metadata frame followed by one binary image frame for
changed observations, and the atomic `binary-envelope` shape, which wraps metadata and payload in
one binary WebSocket message. The SDK facade requests `binary-envelope` by default because causal
action-observe loops should not depend on split-message metadata/payload alignment. Raw daemon
WebSocket clients can still use `json-binary` for broad compatibility. The stream sends periodic
keyframes, can send small dirty-region patch frames, and can suppress unchanged observations to
metadata-only frames. That keeps idle observation loops from repeatedly transferring the same
screenshot bytes while still letting clients recover by requesting or waiting for a keyframe.
Actions still run on the REST or hot-session control paths; separating observation bytes from
control messages prevents large frames from blocking input, cancellation, or health checks.

For default PNG, cursor-hidden observations, the daemon uses a raw MSS capture path before PNG
encoding. It hashes raw RGB bytes to suppress unchanged frames without encoding a PNG, and uses
64x64 tile hashes to choose dirty regions for changed frames. Sparse changes can be emitted as
multiple lossless PNG patch rectangles when that saves enough pixels over one bounding rectangle.
When the native `xxhash` wheel is available, tile hashes use XXH3; otherwise the daemon falls back
to BLAKE2b. Frame metadata includes `tile_hash_backend`, `source_version`, `emit_version`,
`delivery`, and patch fields such as `patch_count` and `patch_rects` so benchmark artifacts show
which path ran. Large dirty regions still fall back to keyframes. Cursor-visible, scaled, JPEG, and
WebP streams use the encoded screenshot fallback path.

The SDK facade is:

```python
with computer.observation_stream(fps=5, options={"format": "png"}) as stream:
    for frame in stream.frames():
        ...
```

The SDK defaults observation streams to lossless PNG with the cursor hidden. Advanced callers can
tune `tile_size`, `delta_max_ratio`, `delta_mode`, `keyframe_interval`, `max_patch_rects`, and
`multi_rect_min_savings`; keep the default 64-pixel tiles unless measurements show a workload
benefits from finer patch locality. Human viewing remains a noVNC concern. The observation stream
does not switch to lossy video or preview codecs by default because agent observations need
full-fidelity UI pixels.

For action-causal observation loops, keep the stream open and request a frame immediately after the
action:

```python
with computer.observation_stream(fps=0.01) as stream:
    frames = stream.frames()
    first = next(frames)
    computer.actions.run([{"type": "click", "x": 100, "y": 100}])
    stream.request_frame()
    observed = next(frames)
```

`request_frame()` sends a `capture_now` stream op. It bypasses the passive FPS tick, so benchmarks
can attribute action-to-frame latency without accidentally measuring scheduler sleep. The passive
FPS loop remains useful for background visual monitoring.

When the action and observation are both owned by the same stream session, prefer
`run_actions_capture()`:

```python
with computer.observation_stream(fps=0.01) as stream:
    frames = stream.frames()
    first = next(frames)
    stream.run_actions_capture(actions=[{"type": "click", "x": 100, "y": 100}])
    observed = next(frames)
```

`run_actions_capture()` sends the action batch and emits the next observation frame from one
WebSocket operation. It keeps the same keyframe, patch, unchanged-frame, and screenshot-budget
behavior as the observation stream, but avoids the extra remote wait from running the action over
REST and then sending `capture_now`. Use this path for tight SDK-owned loops that already maintain
an observation stream. Keep fused raw screenshots for one-shot action-then-observe turns that do
not need stream state or delta frames.

The operation defaults to immediate capture after the action batch. If the target application needs
a paint boundary before the screenshot, pass an explicit `capture_delay_ms` or include an explicit
`wait` action in the batch. The SDK does not add an implicit post-action delay.

When correctness depends on observing the next visual change rather than taking the first possible
post-action screenshot, use `run_actions_observe_change()`:

```python
with computer.observation_stream(fps=0.01) as stream:
    frames = stream.frames()
    first = next(frames)
    stream.run_actions_observe_change(
        actions=[{"type": "click", "x": 100, "y": 100}],
        change_timeout_ms=100,
        poll_interval_ms=8,
        poll_strategy="adaptive",
        change_detection="auto_region",
    )
    observed = next(frames)
```

This operation runs the action batch once, waits for a paint signal, then verifies raw screen state
inside the daemon until the stream's source screenshot hash changes or the timeout is reached. It
emits exactly one stream frame, preserving the same patch/keyframe/unchanged semantics as the rest
of the observation stream. Frame metadata includes `change_detected`, `change_attempts`,
`change_wait_ms`, and `change_timeout_reached` so callers can distinguish a real painted change
from a no-op or timeout. Use this for paint-aware GUI loops instead of guessing a fixed post-action
delay.

The change detector supports two optional optimizations:

- `poll_strategy="adaptive"` captures immediately, then backs off up to `poll_interval_ms`.
  This avoids a tight fixed poll loop when the UI needs a frame boundary to paint.
- `change_detection="auto_region"` captures a small region around the last pointer action before
  falling back to the full stream frame. Use `change_detection_region` for an explicit region, or
  `change_detection="full"` when keyboard or global UI changes are expected.
- `change_signal="auto"` is the default. It uses a persistent X11 DAMAGE watcher as an event-driven
  paint signal when the X server and image support it, then falls back to the same polling path when
  unavailable. Use `change_signal="poll"` to disable XDamage, or `change_signal="xdamage"` to force
  the XDamage probe for benchmarking. XDamage only decides when to try the final observation frame;
  the daemon still verifies the screenshot hash before reporting a detected change, and the emitted
  keyframe/patch/unchanged payload still comes from the normal raw screenshot and tile-diff path.
  The stream arms the watcher after any region baseline capture and immediately before running the
  action, then resets it after a detected event. That keeps stale damage from a prior capture from
  satisfying the next action-observe turn.

`ObservationClient.act_and_observe()` uses the same route with `change_detection="auto"` by
default. The SDK resolves that policy from the last non-wait action: pointer-local actions such as
clicks, moves, and drags use `auto_region`, and explicit `change_detection_region` also opts into
region detection. Keyboard-only and global actions stay on full-frame detection unless the caller
opts into a region.

Observe-change frames include `change_stage_timing_ms` for attribution. It records daemon-side
signal preparation, region baseline capture, action batch wall time, explicit capture delay, signal
wait, region polling, final frame polling/capture, and total server time before the frame is
emitted. The client benchmark combines this with `request_frame_ms` and `receive_frame_ms`; the
derived `receive_minus_server_pre_emit` bucket includes websocket send, network transit, client
receive, and local scheduling because those happen after frame metadata has already been produced.

For raw PNG, no-cursor observation streams, the daemon also starts a dirty-frame producer before
`run_actions_observe_change` executes the action. The producer uses XDamage as an event-driven
wakeup, captures the latest raw frame in the background, and lets the observe-change path consume
that already-captured frame if its source hash is newer than the baseline. XDamage remains a hint,
not truth: unchanged hashes, unavailable XDamage, unsupported stream options, or producer misses
fall back to the synchronous capture/poll path. Producer metadata appears as
`dirty_frame_producer`, `dirty_frame_producer_used`, `dirty_frame_age_ms`,
`dirty_frame_producer_fallback_reason`, `dirty_frame_producer_wait_budget_ms`,
`dirty_frame_capture_region_source`, and the `dirty_producer_*` stage timings. Regional producer
captures keep a short wait cap so a slow producer cannot consume the whole deadline before regional
confirmation runs; even short regional observe-change timeouts reserve the fallback window instead
of giving the whole deadline to the producer. Full-frame XDamage producer captures can use the
caller's timeout minus the fallback reserve, because their fallback is another full-frame check
rather than regional confirmation.
If the producer returns a verified unchanged regional or full-frame capture after consuming the
observe-change deadline, the daemon preserves the caller's timeout contract and skips the final
full-frame poll. Those frames are emitted as timeout/unchanged observations with
`frame_poll_skipped_reason="deadline_exhausted_after_dirty_producer"` and a specific producer
fallback reason such as `producer_same_region` or `producer_same_frame`. A remaining frame-poll
fallback after those verified unchanged producer frames is capped to one short full-frame truth
check with `frame_poll_deadline_reason="after_unchanged_dirty_producer"`. A longer remaining
frame-poll fallback means the route did not have a verified producer frame it could safely emit.
When the dirty producer misses but still has an action-derived capture region, the daemon performs
one bounded regional confirmation before escalating to full-frame polling. That confirmation uses
the same native tile-diff gate as producer patches, so it can emit a verified changed patch or a
verified unchanged frame without trusting XDamage as truth. Confirmation frames record
`dirty_region_confirmation_result` and the `dirty_region_confirmation_*` stage timings; if a changed
regional confirmation avoids full-frame polling, `frame_poll_skipped_reason` is
`dirty_region_confirmation_changed`.
If confirmation is unchanged and the route still needs a full-frame truth check, that fallback uses
one full-frame capture instead of waiting out the caller's full observe-change timeout.
Those frames record `frame_poll_budget_ms` and
`frame_poll_deadline_reason="after_unchanged_dirty_region_confirmation"`.
Confirmation capture timing is split into ready, input-lock wait, backend operation, total capture,
and native tile-diff timings so live tails can distinguish scheduler/lock delay from backend
capture work. Benchmark summaries also expose
`dirty_region_confirmation_capture_timing_summary_ms`, including grouped summaries under
`frame_poll_deadline_reason_summaries` when confirmation is followed by a bounded full-frame truth
check.
Benchmarks include a sibling `*_production_sync` case with `dirty_frame_producer="off"` so producer
changes can be compared against the synchronous path in the same target sandbox. Treat the producer
as a latency attribution and correctness-preserving pipeline step; it removes the route-level frame
poll when successful, but full-screen capture can still remain on the critical path after the
damage event.

When `change_detection="region"` or `"auto_region"` resolves a region, that action-derived region
is the preferred dirty-frame capture hint. Otherwise, if XDamage reports a dirty rectangle and the
stream has a current raw full-frame baseline, the producer can use the XDamage rectangle as a
secondary capture hint. The dirty-frame producer keeps separate persistent DAMAGE watchers for
those two behaviors: a `NonEmpty` watcher for action-region wakeups, and a `DeltaRectangles`
watcher for XDamage rectangle hints. Report level is chosen when each DAMAGE object is created, so
the producer does not destroy and recreate a watcher just to switch modes on a hot path. When
available, the rectangle watcher fetches the accumulated XFixes damage region after subtracting
damage. The resulting rectangle metadata appears as `xdamage_dirty_rect`, `xdamage_dirty_rects`,
and `xdamage_dirty_ratio`. Those fields are diagnostic hints only; emitted patches still come from
captured pixels and tile/source-hash verification.

If a dirty capture region can be expanded to the stream tile grid, the daemon hashes only the
captured region, overlays those tile hashes onto the previous full-frame tile map, and emits a
full-coordinate patch directly from the regional raw pixels. That avoids both full-screen X11
capture and full-frame raw reconstruction on the hot path. These frames set
`source_hash_kind="tile-fingerprint"` because `source_sha256` identifies the ordered full-frame
tile map rather than a freshly reconstructed RGB buffer. Client composition semantics remain
full-frame and lossless because `dirty_rect` and patch coordinates stay in stream coordinates.

The region-native optimization is intentionally narrow: stream-level screenshot regions, missing
baselines, keyframe turns, unsupported raw options, oversized tile-aligned regions, or non-native
regional captures fall back to the full-frame producer/poll path. If a previous region-native patch
made the cached full raw frame stale, the daemon will not use it for reconstruction; it either keeps
advancing through tile-native regional patches or falls back to a fresh full-frame capture. Frame
metadata records the selected `dirty_frame_capture_region`, and stage timings include
`dirty_region_native_ms` and `dirty_region_reconstruct_ms`.

Benchmarks can also enable `transport_timing=true` on the observation stream. In that mode the
daemon sends one small `transport_timing` control message immediately after each frame payload. The
control message records server-side metadata send, payload send, and total emit timing. The
benchmark receive path records client-side metadata wait, JSON parse, payload wait,
transport-timing wait, and frame construction. Normal SDK streams leave this off so production
frames keep the stable metadata-then-binary shape without an extra control message.

`run_actions_observe_change` frames also include `action_observe_attribution_ms`. This is a derived
diagnostic map for the causal action-observe turn, separate from raw stage timings. It records
request-to-action, action end to XDamage signal, signal to capture start, capture to delta-ready,
delta-ready to pre-emit, and action end to pre-emit intervals when those boundaries are available.
Interpret it as a decision aid:

- if `action_end_to_signal_detect_ms` dominates, the next lever is action-to-paint/XDamage behavior
  or keeping a producer/watch loop armed outside the request path.
- if `capture_start_to_delta_ready_ms` dominates, the next lever is native capture/diff/encode work.
- if `delta_ready_to_pre_emit_ms` grows, the next lever is daemon orchestration before WebSocket
  send.
- if benchmark receive-minus-server-pre-emit dominates, the next lever is caller placement,
  co-located runners, or transport/client receive behavior.

The observation benchmark also includes `observation_transport_probe_*` cases. These send synthetic
in-memory binary payloads over the same observation WebSocket without screenshot capture or X11
work. Use them to distinguish a fixed tunnel/WebSocket scheduling floor from payload-size transfer
cost before changing the screenshot or delta protocol.

Observation benchmark artifacts preserve one compact `sample_observations` row per measured
iteration and an `outlier_observations` subset keyed by the summary's high-outlier indices. Use
those rows for tail diagnosis because aggregate p50/p95 summaries can identify the likely stage
class, but they cannot prove whether a specific slow iteration came from XDamage wait, dirty-frame
producer fallback, capture/diff work, WebSocket receive, or browser paint behavior. The rows keep
timing, dirty-region source, fallback reason, patch, XDamage, and transport metadata; they do not
store screenshot payload bytes.
Dirty-producer rollups also include frame-poll deadline reasons, frame-poll budgets, and
`frame_poll_deadline_reason_summaries` so post-confirmation fallback timing can be compared against
changed, unchanged, and timeout outcomes without hand-parsing every compact observation row.
They also include dirty-producer hit/fallback rollups and
`dirty_frame_capture_region_source_summaries` so producer hit rate, fallback reason, and
confirmation outcomes can be compared by action-region versus XDamage-region hints.
Use `observation_action_click_act_and_observe_paired_dirty_producer_xdamage_ab_production` when the
paired dirty-producer artifact needs full-frame XDamage signal coverage without action-region
capture.

Live observation benchmark runs can be narrowed to the cases under investigation:

```bash
uv run computer-use benchmark modal-colocated-client \
  --modal-region us-west \
  --surface daemon-transport-floor \
  --surface daemon-observation-stream \
  --browser chromium \
  --iterations 10 \
  --observation-profile causal-action-observe-diagnostic \
  --output modal-action-observe-diagnostics-us-west-browser-10x-YYYYMMDD.json
```

Use this for diagnostic PRs. The profile includes 0B/5KB/50KB/250KB transport probes plus the
production causal action-observe cases for `auto_signal` and `auto_region`, each measured in the raw
metadata-then-binary frame shape and the single-message binary-envelope shape. Production
comparisons keep `transport_timing=false` so the measurement does not add extra control frames. The
SDK-default production case uses binary-envelope unless the caller explicitly opts into
`json-binary`. The full observation surface intentionally covers older ablations and synthetic delta
cases, so it is too broad for a quick action-observe regression check.

Use the observation stream for long-lived visual feedback loops. Use fused raw screenshots for
single action-then-observe turns, because their one-shot latency remains easier to attribute and
does not require stream setup. Observation stream frames count against the screenshot budget. Patch
frames include `kind="patch"`, `dirty_rect`, `dirty_ratio`, `previous_seq`, and a binary patch image.
Clients should apply patches only when their previous frame sequence matches; otherwise request or
wait for a keyframe.

For one-shot turns that need a paint-aware wait but do not maintain an observation stream, use
`actions.run_and_observe_change_screenshot_bytes(...)` or
`POST /v1/actions/run/observe-change/raw-screenshot`. With `change_signal="auto"`, the daemon uses
XDamage as the paint signal when available, then captures one final binary screenshot. If XDamage is
unavailable, or if callers set `change_signal="poll"`, the route falls back to source-hash polling.
This route is intentionally a one-shot binary response, not a replacement for stream patch/delta
state.

For no-cursor screenshots, the X11 daemon prefers an in-process MSS capture path. Native raw PNG
screenshots use MSS PNG bytes directly. JPEG, WebP, and scaled screenshots use MSS pixel capture
plus in-memory Pillow encoding, avoiding the slower subprocess/temp-file/decode path. Cursor-visible
screenshots still use the desktop screenshot tool fallback because the MSS path does not compose the
cursor into the image.

The JSON screenshot routes remain compatibility routes:

```bash
POST /v1/screenshots/full
POST /v1/actions/run
```

They return structured JSON and base64 image payloads, which is convenient but materially slower
and larger on the wire than the raw binary routes. Benchmarks should label these separately from
raw primitive latency.

Screenshot responses include `x-computer-use-capture-backend` and
`x-computer-use-timing-ms` headers on raw routes. Benchmark results also record the client-observed
HTTP protocol version when available, so HTTP/2 runs can be verified from the artifact instead of
inferred from configuration.

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
- `screenshot_full_raw`: full-screen screenshot latency through the binary image response path.
  This avoids JSON/base64 transport overhead and is the fairer comparison to provider SDKs that
  already return screenshot bytes.
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

Add `daemon-hot-session` when measuring a reachable daemon WebSocket:

```bash
uv run computer-use benchmark sdk \
  --base-url http://127.0.0.1:8080 \
  --token dev \
  --surfaces daemon-hot-session \
  --iterations 10
```

The hot-session surface reports `screenshot_full_raw`, `move_click`, `click_screenshot_raw`, and
`move_click_sequence` over the persistent session. It intentionally does not run in `--mock-local`
mode because FastAPI's in-process test client is not the SDK's network/WebSocket stack. Use a local
daemon or a Modal-created sandbox for real transport attribution.

Add `daemon-observation-stream` when measuring continuous observation:

```bash
uv run computer-use benchmark sdk \
  --base-url http://127.0.0.1:8080 \
  --token dev \
  --surfaces daemon-observation-stream \
  --iterations 10
```

This surface reports `observation_first_frame`, `observation_steady_no_change`,
`observation_small_patch`, `observation_sparse_patches`, `observation_large_change`,
`observation_capture_now_no_change`, `observation_capture_now_small_patch`, and
`observation_capture_now_sparse_patches`. It also reports action-causal cases:
`observation_action_click_capture_now`, `observation_action_click_observe_change`,
`observation_action_click_observe_change_poll`,
`observation_action_click_observe_change_xdamage`,
`observation_action_click_observe_change_auto_signal`,
`observation_action_click_sparse_observe_change_auto_signal`,
`observation_action_click_observe_change_http_raw`, and
`observation_action_click_fused_raw`.
The capture-now action case opens a synthetic page once, mutates it with a real daemon click action,
then requests an immediate observation frame on the existing stream. The default observe-change case
uses the SDK default `change_signal="auto"`; the poll, XDamage, and auto-signal variants make the
signal policy explicit for A/B comparison. Use the stream observe-change cases as the canonical
low-latency action-observe benchmark because they keep frame/tile/XDamage state alive across turns.
The HTTP raw observe-change case uses the same page and click through
`POST /v1/actions/run/observe-change/raw-screenshot` so benchmarks can separate WebSocket stream
framing from a one-shot binary response. The fused-raw case uses the same page and click through
`POST /v1/actions/run/raw-screenshot`.
`observation_action_click_act_and_observe_paired_envelope_ab_production` is the paired
JSON-binary-vs-binary-envelope diagnostic. It runs seeded randomized `AB`/`BA` pairs in the same
sandbox, client path, and synthetic page, then reports `variant_ms - baseline_ms` deltas so negative
values mean binary-envelope was faster. The stream default remains start-time negotiated, but this
diagnostic uses a command-scoped action-observe `frame_encoding` override so both arms share one
observation stream.
`observation_action_click_act_and_observe_paired_dirty_producer_ab_production` uses the same
paired shape for dirty-frame policy: baseline disables the dirty producer and variant uses
`dirty_frame_producer="auto"`. Negative deltas mean the dirty producer path was faster. Use this
case before changing dirty producer, XDamage, or regional confirmation policy because it preserves
the same sandbox, page, stream, encoding, and randomized pair order while recording the producer
fallback and confirmation metadata for each arm.

The surface records frame payload bytes, full-frame bytes, daemon capture/diff/encode timing,
dirty-region metadata, patch counts, source/emit versions, metadata-only unchanged frames,
action-to-frame timing for `capture_now` cases, action daemon timing for click-driven cases,
observe-change stage timing, causal action-observe attribution, derived
receive-minus-server-pre-emit timing, optional stream transport send/receive timing, and WebSocket
transport labeling. It is
intentionally separate from `screenshot_full_raw` because it measures stream startup, sustained
observation behavior, and action-causal capture behavior rather than only a single
request/response screenshot. The benchmark uses the SDK-default PNG screenshot format. Passive
stream benchmark wall times include stream setup, frame pacing, and visual mutation settling; use
`observation_action_click_capture_now` and `observation_action_click_fused_raw` when comparing hot
action-to-observation SDK loops.

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
create-to-ready time, runs the warm daemon cases through the selected ingress, and terminates the
sandbox in a `finally` block. The JSON keeps the backward-compatible `daemon-http` surface key, and
records the canonical ingress label in `metadata.ingress.canonical_name`. A Modal-created sandbox
defaults to attested tunnel ingress and reports `modal-daemon-attested-tunnel`;
`--modal-ingress connect` reports `modal-daemon-connect`; `--modal-ingress tunnel` reports
`modal-daemon-tunnel`; mock-local reports `modal-daemon-local`; caller-provided daemon URLs report
`modal-daemon-http` unless the caller labels them separately. If `--gpu` is set without
`--resource-profile`, the created sandbox uses `browser-gpu`; otherwise `--resource-profile`
controls the image/resource profile label. Modal Connect token URLs may also be hosted on
`*.modal.host`, so live Modal-created benchmark metadata uses the explicit `modal_ingress` field
instead of inferring Connect-vs-tunnel solely from URL shape.
Supported GPU strings and counts follow Modal's `gpu` argument, such as `T4`, `L4`, `A10`,
`L40S`, `A100`, `H100`, or `H100:2`. For GPU-specific benchmarking where Modal's H100-to-H200
upgrade would pollute attribution, use Modal's strict `H100!` string.

Modal-created sandboxes support three ingress modes:

- `attested-tunnel` (default): bootstrap through a Modal Connect Token, mint a short-lived daemon
  bearer token with `/v1/session/tunnel-authorize`, then send hot primitive calls through the
  encrypted Modal tunnel on port `8080`.
- `connect`: keep every daemon request on the Modal Connect-token URL; use this when Modal
  verified-user metadata on every request matters more than primitive latency.
- `tunnel`: expose daemon port `8080` and use a static per-sandbox daemon bearer token. This is the
  lowest-level mode and should be reserved for trusted benchmark harnesses.

Modal tunnel ingress can also opt into HTTP/2 transport with
`ComputerConfig(network={"daemon_http_version": "2"})`. This keeps the same security semantics as
the selected ingress but creates the daemon port through Modal `h2_ports`, starts the daemon under
Hypercorn, and uses an `httpx` HTTP/2 client. HTTP/1.1 remains the SDK default because it is the
lowest-dependency compatibility path and matches non-h2 local daemon clients. Use the HTTP/2 mode
when benchmarking or operating a hot primitive loop that benefits from request multiplexing and
lower connection overhead.

Modal region placement is an explicit latency knob for created sandboxes. Use
`ComputerConfig(runtime={"modal_region": "us-west"})` or benchmark `--modal-region us-west` when
the caller/model location is known. Leave it unset when availability/cold-start flexibility matters
more than predictable low latency. Region placement only affects new sandboxes; attach/reuse cannot
relocate an existing sandbox.

Region policy:

| Situation | Policy | Why |
| --- | --- | --- |
| General SDK usage | Leave `runtime.modal_region=None` | Preserves Modal's default placement and availability behavior. |
| Production agent/model loop | Pin the fastest measured region near the caller/model loop | The hot path is caller-to-sandbox receive latency, not end-user geography. |
| Published latency claim | Run `modal-region-ab` from the actual caller environment | Region results are operational measurements and should include caller context. |
| Reused sandbox | Do not expect relocation | Region only applies when creating a new Modal sandbox. |

On May 26, 2026, a 30x `daemon-transport-floor` matrix from the development environment showed
region dominated ingress and HTTP-version differences for the 0B receive floor:

| Ingress | HTTP | Region | Fastest 0B p50 | Notes |
| --- | ---: | --- | ---: | --- |
| `attested-tunnel` | 1.1 | `us-west` | 51.4ms | Best measured canonical SDK path. |
| `connect` | 1.1 | `us-west` | 58.2ms | Close to attested for small hot WebSocket frames. |
| `attested-tunnel` | 1.1 | default | 97.3ms | Modal default placement for this run. |
| `attested-tunnel` | 1.1 | `us-east` | 90.0ms | Cross-region path from this caller was slower. |
| `connect` | 1.1 | `us-east` | 100.1ms | Similar cross-region penalty. |
| `attested-tunnel` | 2 | default | 100.3ms | HTTP/2 did not improve the WebSocket receive floor. |

The matching 10x observation sanity run on `attested-tunnel`, HTTP/1.1, `us-west` measured
`73.4ms` p50 action-to-frame for `act_and_observe_auto_signal_production`, while daemon action
work was only `0.7ms` p50. That supports the current diagnosis: for interactive observation, region
and client receive path dominate daemon action execution.

Use the dedicated region A/B helper to regenerate this comparison without hand-rolling multiple
fresh-sandbox commands:

```bash
uv run computer-use benchmark modal-region-ab --iterations 30 \
  --modal-region default --modal-region us-west --modal-region us-east \
  --modal-ingress attested-tunnel --daemon-http-version 1.1 \
  --caller-region-label dev-laptop-us-west \
  --output modal-region-ab-attested-h1-30x-YYYYMMDD.json
```

The helper creates one fresh Modal sandbox per region, keeps the ingress/resource/image knobs fixed,
runs only `daemon-transport-floor`, and reports the fastest 0B receive floor plus common WebSocket
and HTTP payload cases for each region. Use `default` to include Modal's unpinned placement policy.
`--caller-region-label` is free-form metadata for the caller/model-loop location; it does not change
Modal placement.
Render a copyable markdown table from the JSON artifact with:

```bash
uv run computer-use benchmark modal-region-summary modal-region-ab-attested-h1-30x-YYYYMMDD.json
```

Record the caller location, ingress, daemon HTTP version, image profile, resource profile, and
iteration count next to any published table. Region benchmarks are most useful when they are treated
as operational measurements from a specific caller/model-loop location, not as universal provider
truths.

A later May 26, 2026 `modal-region-ab` run from the same development environment measured:

| Region | Fastest 0B p50 | Delta vs fastest | Fastest encoding | HTTP 0B | WS envelope 0B | WS JSON 0B | WS envelope 250KB |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| default | 71.3ms | 41.9ms | `websocket_binary_envelope` | 73.5ms | 71.3ms | 71.7ms | 101.1ms |
| `us-east` | 70.7ms | 41.2ms | `websocket_binary_envelope` | 71.7ms | 70.7ms | 80.8ms | 93.8ms |
| `us-west` | 29.5ms | 0.0ms | `websocket_binary_envelope` | 36.5ms | 29.5ms | 33.2ms | 54.4ms |

Use the co-located client benchmark to test whether moving the client/model-loop side into Modal
reduces the floor further:

```bash
uv run computer-use benchmark modal-colocated-client --iterations 30 \
  --modal-region us-west --modal-ingress attested-tunnel --daemon-http-version 1.1 \
  --browser chromium --surface daemon-transport-floor --surface daemon-observation-stream \
  --runner-path inherited --runner-path connect --runner-path target-loopback \
  --observation-profile causal-action-observe-diagnostic \
  --caller-region-label dev-laptop-us-west \
  --output modal-colocated-client-us-west-30x-YYYYMMDD.json
```

This creates one target desktop sandbox in the selected region, runs the selected benchmark surfaces
from the external caller, then creates an ephemeral Modal runner sandbox in the same region and runs
the same surfaces against the target daemon URL. Treat it as an architecture experiment: it measures
whether co-locating the caller/model loop is likely to help before adding any hosted control-plane
shape. Keep `daemon-transport-floor` in the matrix for raw receive-floor attribution, and add
`daemon-observation-stream` when the question is the causal action-to-frame workload an agent loop
actually experiences.
Observation-stream runs need a browser-capable target image, so pass `--browser chromium` or
`--browser firefox`; the CLI rejects that surface on the standard image because its browser setup
would fail before measuring the workload.

Use runner paths to separate caller placement from target ingress:

| Runner path | Caller location | Target daemon path | Use |
| --- | --- | --- | --- |
| `inherited` | Separate same-region Modal runner sandbox | The same URL/token as the external caller | Backward-compatible baseline. |
| `connect` | Separate same-region Modal runner sandbox | Fresh Modal Connect Token URL/token for the same target sandbox | Tests Modal's documented HTTP/WebSocket Sandbox path from inside Modal. |
| `target-loopback` | The target desktop sandbox itself via `Sandbox.exec` | `http://127.0.0.1:8080` plus the daemon bearer token | Measures the daemon/client loopback floor without tunnel or Connect ingress. |

`target-loopback` is not a separate runner sandbox: `127.0.0.1` only reaches the target daemon from
inside the target sandbox. Treat it as a lower-bound diagnostic for a future same-container hosted
control loop, not as proof that two independent sandboxes can talk over loopback.
The benchmark uses the SDK-owned `run_modal_daemon_command()` helper for these paths so endpoint
selection, Connect Token creation, loopback execution, and reserved daemon environment variables stay
in the Modal SDK boundary instead of benchmark-local code.

A May 29, 2026 `modal-colocated-client` run with a `us-west` target and a development-laptop
external caller measured:

| Caller path | Fastest 0B p50 | Ratio vs external |
| --- | ---: | ---: |
| External caller -> `us-west` target | 29.4ms | 1.00x |
| Same-region Modal runner -> `us-west` target | 1.7ms | 0.06x |

This points to caller/ingress placement as the dominant remaining floor for remote SDK control
loops. It does not make the ephemeral runner itself a product surface; it is a proof point for a
future hosted model-loop/control-plane shape.

For application code, the same pattern is available as a co-located runner Sandbox. The target
desktop sandbox and runner sandbox are created in the same Modal region, and the runner talks
directly to the target daemon. Use `run_modal_daemon_command()` or
`examples/modal_colocated_runner.py` as the minimal shape before building a hosted control plane.

If a runner can reach `/healthz`, `/v1/version`, and `/v1/capabilities` but times out opening
`/v1/observations/stream`, the failure is likely specific to WebSocket ingress rather than daemon
readiness. Modal documents Connect Tokens as the authenticated HTTP/WebSocket path and encrypted
tunnels as raw forwarded ports where the application owns auth, so compare Connect Token and tunnel
ingress before treating same-region placement as proven.

A broker should stay off this hot path. Use a broker for session lifecycle, placement, auth, and
cleanup; have it return direct daemon/runner connection metadata. Use `examples/modal_session_broker.py`
for the ASGI control-plane shape.

When `daemon-observation-stream` is selected, the comparison also reports the preferred causal
observation case as `causal_action_to_frame_p50_ms`. New artifacts prefer
`observation_action_click_act_and_observe_sdk_default_production`, which measures the SDK
`ObservationClient.act_and_observe()` default policy. The SDK resolves `change_detection="auto"` to
`auto_region` only when the last non-wait action has pointer coordinates or the caller provides an
explicit change-detection region; keyboard-only or global actions stay on full-frame detection. That
metric is the better next-step proof than transport floor alone because it includes action
submission, daemon execution, change detection, and frame receipt.
When the `causal-action-observe-diagnostic` profile is selected, the comparison also includes a
`diagnosis` object that relates transport floor, causal action-observe, and JSON-vs-binary-envelope
framing. Treat it as a triage aid: a material binary-envelope win points at WebSocket message
framing, a large transport win with a smaller causal win points at daemon action/capture/change
detection, and matching transport/causal wins point at caller placement or Modal receive floor.
The same profile also includes the paired envelope A/B case. Use its per-pair deltas before making
small encoding-policy claims from separate before/after artifacts; noisy tail movement in unpaired
Modal runs is common enough that p95 alone should not drive small optimization decisions.
The co-located runner also records `metadata.runner_preflight` with safe route-level HTTP probes
from the runner sandbox to the target daemon. Use it to separate target reachability/auth failures
from observation WebSocket upgrade failures. It records route names, elapsed time, HTTP version, and
bounded error metadata only; it does not include the target URL or bearer token.
Transport-floor WebSocket probe cases also record safe `setup` metadata with the number of
WebSocket open attempts, retry count, elapsed setup time, and bounded retry error types/messages.
Retries are only setup resilience: they make transient Modal WebSocket upgrade delays visible and
keep one failed open from hiding the remaining HTTP and WebSocket cases. Do not count retry-covered
setup as a latency improvement; use it to decide whether public ingress is flaky enough to prefer
target-loopback or a hosted control-loop path for hot loops.

A May 29, 2026 5x browser-target run with both `daemon-transport-floor` and
`daemon-observation-stream` selected measured:

| Surface metric | External caller | Same-region Modal runner | Ratio vs external |
| --- | ---: | ---: | ---: |
| Fastest 0B transport floor p50 | 31.5ms | 31.1ms | 0.99x |
| Causal action-to-frame p50 | 82.8ms | 52.5ms | 0.63x |

That run shows the co-located runner improvement on the actual agent-like action-observe path even
when the synthetic 0B transport floor is flat. Treat this as directional until repeated 30x runs
confirm tail behavior.

A June 2, 2026 10x browser-target runner-path matrix in `us-west` measured:

| Caller or runner path | Fastest 0B transport p50 | Causal action-to-frame p50 | Notes |
| --- | ---: | ---: | --- |
| External caller | 22.4ms | 69.6ms | Development caller to `us-west` target. |
| `inherited` runner | n/a | 32.4ms | Transport-floor WebSocket setup timed out, observation stream completed. |
| `connect` runner | n/a | 25.4ms | Transport-floor WebSocket setup timed out, observation stream completed. |
| `target-loopback` | 0.6ms | 32.1ms | Runs inside the target sandbox over `127.0.0.1:8080`. |

The matrix artifact was intentionally partial because the inherited and Connect runner paths timed
out while setting up the synthetic transport-floor WebSocket probe. A separate `target-loopback`-only
10x run completed cleanly and measured external caller `23.3ms` transport / `63.2ms` action-to-frame
versus target loopback `0.6ms` transport / `31.7ms` action-to-frame. That result is a lower-bound
diagnostic: removing public ingress nearly eliminates raw receive floor, while the remaining
action-to-frame time is dominated by daemon/browser change detection and capture work.

Created Modal benchmark sandboxes set `actions.input_rate_limit_per_sec=0` by default. The SDK
product default remains `20`, but primitive latency benchmarks should not measure intentional
throttling. Pass `--input-rate-limit-per-sec` when the benchmark target is rate-limit behavior
instead of transport and daemon hot-path latency.

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
  `modal-daemon-tunnel`, `modal-daemon-attested-h2-tunnel`, `modal-daemon-h2-tunnel`,
  `modal-daemon-attested-hot-session`, `modal-daemon-hot-session`,
  `modal-daemon-connect-hot-session`, `daytona-toolbox-http`, or `e2b-desktop-sdk`.
- Separate cold create, readiness, action, screenshot, stream, command, and cleanup costs.
- Compare deterministic SDK primitives before comparing model-driven task completion.
- Use the binary screenshot path for raw primitive latency comparisons; keep JSON/base64 screenshot
  numbers as backwards-compatible SDK payload overhead.
- Report `click_screenshot_raw` for the model-loop hot path. It uses one daemon request to run the
  action batch and return the observation as image bytes, so it avoids both a second tunnel round trip
  and JSON/base64 screenshot payload overhead.
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
Use batched actions for model turns, and use `modal-daemon-local` or a tunnel ingress run when
isolating daemon implementation cost from hosted ingress cost.

### Modal ingress comparison, 2026-05-18

These runs used the same SDK defaults (`1024x768 @ 96 DPI`, `post_action_delay_ms=0`), 10 measured
iterations, and one warmup iteration per case. All three runs exited with `ok=true`.

```bash
uv run computer-use benchmark sdk --create-modal-sandbox --modal-ingress attested-tunnel \
  --surfaces daemon-http --iterations 10 \
  --output benchmark-sdk-modal-attested-tunnel-1024x768-2026-05-18.json

uv run computer-use benchmark sdk --create-modal-sandbox --modal-ingress tunnel \
  --surfaces daemon-http --iterations 10 \
  --output benchmark-sdk-modal-tunnel-1024x768-2026-05-18.json

uv run computer-use benchmark sdk --create-modal-sandbox --modal-ingress connect \
  --surfaces daemon-http --iterations 10 \
  --output benchmark-sdk-modal-connect-1024x768-2026-05-18.json
```

| Canonical label | Auth path | Cold create ms | Batch 5 actions ms | Separate 5 actions ms | Move+click ms | 4x move/click ms | Screenshot ms | Type 100 chars ms | Type 1000 chars ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `modal-daemon-attested-tunnel` | Connect-token attestation, then short-lived daemon token over encrypted tunnel | 7369.71 | 693.26 | 3142.52 | 750.85 | 1263.14 | 917.75 | 1305.66 | 7201.52 |
| `modal-daemon-tunnel` | Static daemon bearer token over encrypted tunnel | 6752.85 | 542.15 | 2488.93 | 592.76 | 1111.99 | 708.28 | 1143.80 | 6900.53 |
| `modal-daemon-connect` | Modal Connect Token on every request | 11113.76 | 2015.86 | 8278.20 | 1707.00 | 3220.88 | 2027.81 | 2417.86 | 10574.46 |

Screenshot payloads were `258,319` bytes for the two tunnel runs and `258,295` bytes for the
Connect run. The raw tunnel is fastest, but `attested-tunnel` is the recommended default because it
keeps Modal Connect as the authorization bootstrap and only moves hot primitive calls onto the
encrypted tunnel after the daemon issues a short-lived bearer token. Use raw `tunnel` for trusted
benchmark harnesses; use `connect` when every daemon request must carry Modal verified-user
metadata.

To isolate token/auth overhead from fresh-sandbox variance, run the same-sandbox A/B benchmark:

```bash
uv run computer-use benchmark modal-ingress-ab --iterations 10 \
  --output benchmark-sdk-modal-ingress-ab-1024x768-2026-05-18.json
```

That command creates one raw-tunnel sandbox, runs the daemon surface with the static tunnel token,
mints a short-lived token through Modal Connect, and reruns the same daemon surface against the
same encrypted tunnel URL. The 2026-05-18 run exited with `ok=true`:

| Case | Raw static token ms | Attested minted token ms | Delta ms | Delta % |
| --- | ---: | ---: | ---: | ---: |
| `batch_5_actions` | 1244.44 | 1098.91 | -145.53 | -11.69 |
| `separate_5_actions` | 4684.40 | 4871.46 | 187.06 | 3.99 |
| `move_click` | 1108.29 | 1100.10 | -8.19 | -0.74 |
| `move_click_sequence` | 1901.55 | 1900.99 | -0.56 | -0.03 |
| `screenshot_full` | 1301.22 | 1325.13 | 23.91 | 1.84 |
| `command_echo` | 687.30 | 724.72 | 37.42 | 5.45 |
| `type_100_chars` | 1661.69 | 1624.38 | -37.31 | -2.25 |
| `type_1000_chars` | 7554.30 | 7637.62 | 83.33 | 1.10 |

The same-sandbox A/B result shows no meaningful attested-token penalty on hot tunnel requests.
The larger gaps in the three-fresh-sandbox table above should be treated as Modal placement/run
variance plus per-request tunnel and payload costs, not as Connect-token exchange overhead.

## Screenshot storage modes

`screenshots.full(...)` and `screenshots.region(...)` accept a `storage` mode:

- `inline` returns base64 in the HTTP response. Fast for small screenshots; expensive once the image is over a few hundred kilobytes.
- `artifact` writes to disk and returns a `Screenshot` with `artifact_uri`. Cheaper to ship around, slower first hit.
- `auto` (default) picks based on size.

For agent loops that call screenshot every few actions, `auto` is usually the right answer.

For low-latency model turns, prefer `actions.run_and_screenshot_bytes(...)` or
`POST /v1/actions/run/raw-screenshot`. This keeps action execution and observation capture in a
single daemon request while returning the screenshot as binary image bytes. The legacy
`actions.run(..., screenshot_after=True)` path remains useful when callers need a structured JSON
`Screenshot` object, but it pays base64 response overhead.

When the caller needs to wait for the next paint instead of capturing immediately, use
`actions.run_and_observe_change_screenshot_bytes(...)`. It returns binary image bytes plus parsed
`change_result` and `change_timing_ms` metadata. The default `change_signal="auto"` is fastest on
X11 images with DAMAGE support; set `change_signal="poll"` when the caller needs source-hash
verification instead of event-driven paint detection.

For raw PNG screenshots at native scale without the cursor, the daemon first tries an in-process
MSS/XShm capture and falls back to `scrot`, then `maim`, if the fast capture is unavailable.
MSS avoids a screenshot subprocess and uses the X11 shared-memory path, which is fastest for the
raw observation hot path. `scrot` remains a portable native PNG fallback, while `maim` remains the
compatibility path for cursor-visible, scaled, re-encoded, and JSON screenshots. For JSON PNG
screenshots at native scale, the daemon still compares the native `maim` PNG with its Pillow RGB
re-encode and returns the smaller valid payload.
Raw screenshot responses include `x-computer-use-capture-backend` (`mss`, `scrot`, `maim`, or
`unknown`) so benchmark artifacts can attribute the capture path directly instead of inferring it
from timing.

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
