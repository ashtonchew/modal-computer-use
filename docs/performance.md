# Performance

Placement, network round trips, desktop input, and screenshot capture account for most
computer-use latency. The primary trajectory keeps repeated operations on one borrowed connection.
Change one setting at a time and measure the complete workload.

For deployment steps, see [Modal deployment](modal-deployment.md). For commands, sample rules, and
publication gates, see [Benchmarking](benchmarking.md).

## Use the placed trajectory

1. An async owner creates one desktop.
2. The owner passes a versioned session handle to an application-owned Modal Function.
3. The Function enters one `borrow_async()` context for the complete trajectory.
4. The borrowed computer reuses one pooled async HTTP client.
5. Each model-produced action array uses one `computer.step()` call.
6. The Function releases the lease before the owner cleans up the Sandbox.

The primary placed trajectory requires one exact requested region for both the Function and the
Sandbox, such as `us-west-2`. A missing or broad region fails before lease acquisition or desktop
mutation. An unset or broad selector remains available only to explicit low-level SDK workflows.

Requesting the same region does not prove that the Function and Sandbox share a host or
availability zone. Every daemon request still crosses authenticated Modal ingress.

Inspect `resolved_trajectory_configuration()` before deployment. It reports the requested region,
resources, images, timeouts, retry policy, container limits, and warm-capacity state without
including secret-bearing values.

## Measure the whole lifecycle

Report these stages separately:

| Stage | Measurement boundary |
| --- | --- |
| Cold allocation | Sandbox create request to allocated Sandbox |
| Desktop startup | Allocation to daemon and desktop readiness |
| Function dispatch | Caller invocation to placed Function start |
| Borrow entry | Borrow request to authenticated, ready leased client |
| Warm operation | Request start to validated result on an existing borrow |
| Cleanup | Borrow release and owned Sandbox termination |

Native async provisioning supports cancellation and cleanup without reducing cold allocation or
desktop startup. A warm-operation result begins after provisioning, readiness, and borrow entry.

For a valid comparison, hold the caller topology, target, region, resources, image, ingress, HTTP
version, input backend, screenshot format, action payload, warmup, and connection reuse constant.
Record failures and cleanup with the successful samples.

## Use `computer.step()` for model turns

`computer.step(actions)` sends one ordered action batch to `POST /v1/steps` and receives one
versioned binary envelope. `ComputerStepResult` exposes the action result, screenshot, and timing
through its `actions`, `screenshot`, and `timing` fields.

The returned screenshot is an immediate post-action frame. It is not application readiness. The
caller decides whether to wait for a workload-specific condition before the next model turn.

The [Computer Step promotion report](benchmark-results-2026-08-08-computer-step.md) measured 100
interleaved pairs in one fixed topology. `computer.step()` measured 44.29 ms p50 and 52.57 ms p95.
The prior `actions.run()` followed by `screenshots.full()` measured 47.14 ms p50 and 58.22 ms p95.
The dated report contains the complete configuration and promotion decision.

The article's opening 47.10 ms value is arithmetic over separate 37.25 ms raw-screenshot and
9.85 ms click medians. It is not a measured fused turn and is not a latency promise for
`computer.step()`.

Action-only calls, immediate action-to-frame calls, first-visual-change observations, and semantic
readiness have different timer boundaries. Compare like with like:

| Measurement | Timer ends when |
| --- | --- |
| Action-only | The daemon acknowledges the action |
| Immediate action-to-frame | The requested screenshot is validated |
| First visual change | A correlated pixel change is verified |
| Semantic readiness | An application-owned predicate passes |

First visual change remains experimental. See
[Observe the first visual change](experimental-visual-change-observation.md) for its XDamage,
polling, timeout, and frame-validation contract.

## Reuse screenshot state

Use `screenshots.full()` for the initial model observation and for screenshot-only work. Inline full
screenshots use `/v1/screenshots/full/raw` over the pooled client and return a byte-backed
`Screenshot`. The SDK validates the response body and metadata before returning it.

Cursor-hidden capture uses one persistent MSS session and in-memory encoding when available. The
daemon resets that session once after an open or grab failure, then uses the bounded `scrot` and
`maim` fallback path. Cursor-visible capture uses `maim` because MSS does not compose the desktop
cursor.

### Compare screenshot sources

MSS is the default. See [configuration](configuration.md#actions) for source selection, Managed
Image requirements, eligible requests, and codec policy. Measure the complete SDK call and inspect
the raw response metadata when comparing sources.

One retained X11-SHM fixture produced a 57,721-byte full PNG. A separate retained MSS campaign
produced a 52,315-byte comparator. The X11-SHM payload was 10.33% larger. The runs were separate,
so this is not a paired payload result. Rare long calls occurred in the X11-SHM evidence. These
measurements do not provide a latency guarantee.

Screenshot storage modes have different purposes:

| Mode | Use |
| --- | --- |
| `inline` | Return the image to the caller. This is the default. |
| `artifact` | Write the image to daemon-owned storage and return a reference. |
| `auto` | Let the daemon select inline or artifact storage. |

The raw binary optimization applies to full inline screenshots. Region captures and artifact
storage retain their structured response contracts. JSON and base64 routes remain available for
direct REST clients and compatibility work.

`COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION` selects where resizing and re-encoding happen:

- `daemon` keeps the work inside the Sandbox.
- `client` moves processing to the SDK and sends more bytes over the connection.
- `auto` selects a location from the request and image size.

Measure CPU use and transferred bytes before changing the default.

The optional `COMPUTER_USE_SCREENSHOT_CAPTURE_SOURCE=auto` policy probes X11 shared memory only
after its extension and live display pass readiness. MSS handles ordinary native failures in that
X-server generation. An X-server reply timeout fails closed, and a display restart clears the
quarantine before the source is probed again. Raw screenshot responses report `x11-shm`, `mss`,
`mss-fallback`, `scrot`, `maim`, or `unknown` in their capture-backend metadata.

## Batch input

Send one model-produced ordered action array as one batch. The daemon validates the complete batch
before mutation, runs actions in order under one input lock, and stops after the first failure
unless `continue_on_error=True`.

`COMPUTER_USE_MAX_BATCH_ACTIONS` limits batch length and defaults to `50`. A larger request still
has to satisfy payload, timeout, screenshot, and trajectory budgets.

The default input backend is `auto`. It uses a persistent XTest/Xlib/XKB session when available and
falls back to `xdotool` only before native input emits an event. A possibly partial mutation is
terminal and is never replayed. Use `xtest` for a benchmark that must prove the native backend.

The daemon applies one weighted token bucket across input routes. The portable baseline for the
minimum tested Sandbox is:

```text
refill: 100 normalized input-work tokens per second
burst:  400 tokens
```

Reserving the whole batch cost before mutation prevents a rate boundary from partially executing a
validated batch. The limit works with batch size, action and batch timeouts, trajectory budgets,
payload bounds, and one-in-flight Step serialization.

Run the same-runtime capacity gate before setting higher values for a faster setup. CPU and memory
provide too little information to select a safe rate automatically. The
[weighted input-capacity report](benchmark-results-2026-08-08-input-capacity.md) records the tested
minimum setup and its promotion gates.

## Account for receipt durability

Each leased mutation writes an `IN_PROGRESS` receipt to the target-local SQLite WAL with
`synchronous=FULL` before dispatch. Terminal receipt bookkeeping uses `synchronous=NORMAL`. This
boundary prevents dispatch without a surviving receipt on the target filesystem.

The guarantee ends at the target filesystem. Remote persistence and reconstruction after Sandbox
loss remain outside it. Measure the local cost on the intended runtime filesystem, and keep the WAL
there instead of moving it to a network filesystem or Modal Volume.

## Choose capacity explicitly

Warm capacity is optional spend. It is not article parity. The primary example uses
`min_containers=0` and does not create or fill a Sandbox warm pool.

Choose capacity from the workload and budget:

- A positive Function `min_containers` reduces idle-to-invocation delay and incurs idle Function
  cost.
- A Sandbox warm pool reduces cold Sandbox allocation and has a separate claim, health-check, and
  cleanup contract.
- Browser `prewarm` starts the selected browser during Sandbox startup. It does not keep a Sandbox
  alive.
- `max_containers` and application admission control prevent overlapping trajectories for one
  desktop. The daemon also enforces one active lease per target.
- `retries=0` disables configured Function retries. A platform reschedule can still restart a
  crashed Function container.

Cancellation stops the Function invocation without reversing prior GUI effects or terminating a
desktop owned by another scope. The owner waits for remote work to reach a terminal state before
terminating its Sandbox.

## Tune startup behavior

Set `browser.prewarm=true` when every trajectory opens the selected browser and the startup cost is
acceptable. `open_url_on_start` can also pay the first navigation cost during startup. Both
settings move work into the cold path, so keep them explicit.

Use a managed Image only when its measured build and lifecycle behavior fits the deployment. The
SDK does not select a managed Image automatically. Pin the exact release identity and keep the
inline recipe available for rollback. See [Configuration](configuration.md) and the
[Standard Image lifecycle report](benchmark-results-2026-08-08-image-lifecycle.md).

Request a GPU only for a measured browser-rendering workload. It can increase cost without
improving ordinary desktop input or screenshot capture.

Filesystem snapshots preserve filesystem and application state. They do not promise to preserve
GUI memory state or a live browser session. Use Volumes or external storage for durable artifacts.

## Diagnose before tuning

Use the smallest measurement that answers the question:

| Question | Start with |
| --- | --- |
| Is daemon input slow? | Compare `timing.daemon_ms` under fixed XTest and `xdotool` arms. |
| Is the connection slow? | Compare client elapsed time with daemon timing. |
| Is capture slow? | Inspect capture backend, encoding format, dimensions, and daemon timing. |
| Is startup slow? | Split allocation, desktop readiness, Function dispatch, and borrow entry. |
| Is a frame late? | Separate immediate capture, first visual change, and application readiness. |
| Is warm capacity useful? | Compare cold-arrival latency and idle cost over the real arrival pattern. |

The low-level raw action-screenshot route, hot-session WebSocket, and observation stream remain
available for explicit diagnostics and compatibility. Each has its own protocol and correctness
contract. Their results do not establish the performance of `computer.step()`.

Run benchmark commands and read their retention rules in [Benchmarking](benchmarking.md). Current
dated evidence is indexed in [Documentation](README.md#benchmark). Historical, rejected, and
diagnostic results are indexed under [Archived benchmark evidence](archive/README.md#archived-benchmarks).
