# How I built 10 ms computer-use clicks on Modal

*The tuned warm path returns a full 1024x768 PNG in 29 ms and sends one click in 10 ms.*

The first computer-use interface I built on Modal felt slow in the exact place that mattered: the agent loop. Looking at the screen and carrying out one click took about 550 ms on untuned Modal. The E2B and Daytona defaults I tested took about 420 ms and 480 ms.

A few hundred milliseconds is easy to ignore once. At those warm rates, fifty screenshot-and-click pairs add roughly 20 to 28 seconds of primitive time. The same arithmetic comes in under 2 seconds for the primitives I built on Modal.

[OpenAI says GPT-5.6 Sol on Cerebras can generate up to 750 tokens per second](https://openai.com/index/previewing-gpt-5-6-sol/). At that speed, the computer can become slower than the model using it. Long computer-use trajectories are useful because they let an agent complete whole workflows. They already consume model and tool time at each step, and every infrastructure wait delays the person waiting for the result.

That bottleneck reminded me of RustDesk, an [open-source remote desktop system](https://rustdesk.com/docs/en/self-host/). Its rendezvous server handles discovery, then traffic takes a direct path when possible or falls back to a relay. What stuck with me was the separation between connection setup and the path used for every frame and input event. A computer-use agent has the same recurring exchange: pixels go out, an action comes back, and the cycle continues for the life of the session. I started looking for work that Modal repeated inside that loop.

E2B and Daytona expose ready-made APIs for screenshots, mouse input, typing, and commands. Both also let developers customize the environment through templates or snapshots. In this benchmark, I left their documented screenshot and input paths at provider defaults.

Modal let me define the desktop, its computer-use server, and the benchmark caller in one Python project. A Modal Image is the build recipe for the desktop packages and files. I started that image in a Sandbox and ran a daemon inside it, a small FastAPI server served by Uvicorn that owns screenshots, input, and commands. For the benchmark client, I used a Modal Function, Python code running in an autoscaled container. Modal Connect supplied authenticated HTTP access from the Function to the daemon. I could change the request route and the code touching the desktop together.

![Creation is separate from the repeated computer-use loop](../assets/modal-optimized-agent-loop.svg)

Creation is the one-time boundary: create the Sandbox, start the desktop, and validate its first frame. Once that succeeds, the agent repeats screenshots, clicks, typing, and commands. I measured those warm operations separately because that loop determines how responsive a running agent feels.

## A 29 ms screenshot starts with a shorter route

The default Modal setup ran the benchmark client outside Modal and reached the warm daemon through an attested HTTP tunnel. Returning a full 1024x768 PNG took 230 ms p50.

An earlier three-sample diagnostic put median daemon capture and PNG encoding at about 23 ms, median end-to-end time at 127 ms, and the separately summarized remainder at 104 ms. Those medians are not additive, and the remainder is not a pure network measurement. The useful clue was simpler: capture accounted for only part of the delay.

I moved the benchmark client into a Modal Function and requested `us-west-2` for both the Function and target Sandbox. Connect still carried the request between them.

![Default and optimized Modal screenshot request paths](../assets/modal-optimized-screenshot-paths.svg)

The other half of the change lives inside the daemon. X11 is the Linux display server that owns the desktop's pixels and input state. MSS is the screenshot library that reads those pixels. The daemon keeps one MSS session open and prefers XShm, an X11 shared-memory extension that lets a local client read the frame without copying it through the normal X11 request path. It encodes the PNG in memory. If MSS cannot capture after a reset and retry, the compatibility path uses `scrot` or `maim` and a temporary file.

Both final Modal paths were configured for the MSS-first capture path. With the Function runner and optimized daemon configuration, the same 1024x768 PNG fell from 230 ms to 29 ms p50.

Keeping resources warm buys latency with billable resource-seconds. At current list rates, the requested 4 physical CPU cores and 8 GiB work out to about $32 for the Sandbox and $11 for the Function runner over a continuous 24 hours in a narrow selected region. Sandbox usage above the request can raise that estimate. The calculation applies Modal's documented 1.75x regional multiplier and assumes no GPU or credits.

An actual service would not need to keep both allocations alive all day. Functions scale to zero without inputs, and one concurrent, I/O-bound runner could broker requests for several target Sandboxes. I have not built that shared runner yet. Each desktop would still remain its own isolated, billable Sandbox, so long idle lifetimes would continue to cost money.

## Keep one X11 input session open

The original input path shelled out to `xdotool`, a command-line X11 automation tool. Each click started a process, opened a new X11 connection, moved the pointer, pressed the left mouse button, and released it. The agent paid that setup cost again on every action.

I replaced that path with one persistent Xlib connection. Xlib is the client library for X11, and its XTest extension sends synthetic mouse and keyboard events. For each request, I precompute the event sequence, acquire one lock so the sequence owns the input state, queue the events in order, and issue one final X sync before returning.

![Per-action xdotool setup compared with one persistent X11 input connection](../assets/modal-optimized-input-session.svg)

Clicks showed the largest gain. Daemon-side mean move-and-click time, a pointer move followed by one left click, fell from 146.33 ms to 1.15 ms, a 127.7x speedup. Four move-and-click pairs fell from 443.99 ms to 4.80 ms, a 92.5x speedup.

Typing still paid for many key-down and key-up events. Daemon-side mean time for a fixed 100-character ASCII workload fell from 119.77 ms to 20.61 ms, while the 1,000-character case fell from 607.35 ms to 201.34 ms. Both arms used the same workload and zero inter-character delay, so the controlled difference was the input backend.

Direct input also needs to preserve keyboard behavior. I resolve characters through the active X11 layout and key symbols, serialize held keys and buttons, and allow fallback only before XTest could have emitted an event. A request that may have partially emitted input returns an error instead of replaying the action. Xlib's process-wide error handler is nonfatal, while synchronous operations still check their results.

## Send typing through the same persistent session

Typing uses the same XTest connection as clicks. The optimized benchmark sends direct keystrokes with no delay between characters. It measured 15.27 ms p50 for 100 characters and 63.34 ms for 1,000.

## Batch the actions the model already grouped

OpenAI's current computer tool can return several ordered actions in one turn through its `actions[]` array. The documentation tells clients to execute those actions in order. Sending each click as a new API call throws away that structure.

The SDK accepts the array directly:

```python
computer.actions.run([
    {"type": "click", "x": 100, "y": 100},
    {"type": "click", "x": 300, "y": 100},
    {"type": "click", "x": 300, "y": 300},
    {"type": "click", "x": 100, "y": 300},
])
```

The daemon validates the request once, acquires the input lock once, executes in order, and stops at the first error by default. The lock prevents two concurrent requests from interleaving pointer moves and button transitions into a sequence that neither caller requested.

![One action request validates and serializes four ordered clicks](../assets/modal-optimized-action-batch.svg)

Four ordered clicks took 13.64 ms p50 in the provider run. I followed that with a matched 30-sample A/B: one ordered request took 11.54 ms p50, while four sequential requests took 26.84 ms. Batching the calls was 2.33x faster and preserved the order the model supplied.

## Move subprocess I/O off the request loop

The command endpoint looked simple: start a child process, collect its output, and return the result. The shell already ran in its own process, but asyncio still managed its pipes, wait state, and cleanup on Uvicorn's HTTP event loop. Slow subprocess cleanup could therefore delay unrelated requests handled by the same loop.

I gave subprocess work a private `SelectorEventLoop`, Python's event loop for file-descriptor readiness, on a dedicated daemon thread. A bounded bridge submits work from Uvicorn and carries the result back. Cancellation kills the child's process group before cleanup, so capacity does not leak when a request disappears.

![Subprocess I/O ownership moves from Uvicorn to a private event loop while the child process remains separate](../assets/modal-optimized-command-loop-isolation.svg)

Giving subprocess I/O its own loop collapsed the tail. The shared asyncio path measured 55.92 ms p50 and 248.67 ms p95; the isolated loop measured 9.86 ms and 10.63 ms. In the final 30-sample run, the command finished in 9.54 ms p50 and 17.03 ms p95. The test runs `sh -c`, prints `42`, and requires exit code zero with exact stdout `"42\n"`.

The generic subprocess helper caused a second problem with the clipboard. X11 clipboard data belongs to a live selection owner, so `xclip` keeps running after it receives the text. It inherited stdout and stderr pipes, which left `communicate()` waiting for EOF even though the clipboard had already changed. The helper never used that output, so I redirected both streams to `DEVNULL`. A long-lived helper cannot keep ownership of pipes that belong to one request. Direct XTest typing bypasses this clipboard path.

## The optimized warm path stayed below 64 ms

I ran each warm operation 30 times. Only the Modal optimized path was tuned. The remaining columns show the provider defaults exercised by the harness. Each ratio divides the provider p50 by the Modal optimized p50.

| Warm operation | Modal optimized p50 / p95 | Modal default p50 / p95 / ratio | Daytona default p50 / p95 / ratio | E2B default p50 / p95 / ratio | Tzafon default p50 / p95 / ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, provider-native format | 28.69 / 43.96 ms | 229.94 / 235.78 ms / 8.02x | 264.85 / 297.67 ms / 9.23x | 212.75 / 225.28 ms / 7.42x | 146.14 / 168.34 ms / 5.09x |
| One click on the screen | 9.69 / 15.55 ms | 323.81 / 331.35 ms / 33.43x | 216.04 / 221.02 ms / 22.31x | 210.56 / 214.88 ms / 21.74x | 125.75 / 127.92 ms / 12.98x |
| Four ordered clicks | 13.64 / 21.98 ms | 342.68 / 349.16 ms / 25.12x | 865.55 / 1,061.66 ms / 63.45x | 855.81 / 876.02 ms / 62.74x | 455.96 / 531.37 ms / 33.42x |
| Type 100 | 15.27 / 20.69 ms | 379.28 / 389.18 ms / 24.84x | 638.50 / 706.86 ms / 41.81x | 4,090.74 / 4,194.92 ms / 267.86x | 82.27 / 85.45 ms / 5.39x |
| Type 1000 | 63.34 / 83.91 ms | 378.40 / 413.17 ms / 5.97x | 5,356.25 / 5,425.81 ms / 84.57x | 41,048.75 / 41,619.86 ms / 648.12x | 181.03 / 212.66 ms / 2.86x |
| Non-login shell command | 9.54 / 17.03 ms | 182.43 / 346.47 ms / 19.12x | 116.92 / 120.86 ms / 12.26x | 52.18 / 54.35 ms / 5.47x | 29.60 / 30.84 ms / 3.10x |

The screenshot calls kept each provider's native format. Tzafon returned 1280x720 JPEG; Modal, Daytona, and E2B returned 1024x768 PNG. The typing rows preserve each provider's public input behavior. For four clicks, Modal optimized, Modal default, and Tzafon used one request; Daytona and E2B used multiple requests as described above.

## Observe the first changed frame

An action acknowledgement tells me that input reached the desktop. The application may not have painted its response yet. I built a Modal-only alpha endpoint that captures a baseline before the action, uses XDamage as a wake hint, and verifies the change with a full-resolution pixel hash. XDamage is an X11 notification that some area may have been repainted. The endpoint returns the same frame that passed the hash check. Click to first changed frame measured 65.39 ms p50 and 76.31 ms p95 across 30 samples.

![XDamage hints or polling trigger a pixel check, while application readiness remains caller-owned](../assets/modal-optimized-first-change.svg)

The first changed pixel is a narrow terminal condition. A cursor blink, animation, or unrelated repaint can satisfy it, and the first change may be an intermediate frame. A semantic effect may leave identical pixels or happen outside the observed region. This endpoint does not wait for settle or prove application readiness. A dependent next action still needs a workload-specific condition.

## Startup is the next bottleneck

In a fresh product create to validated screenshot test, Modal optimized took 7.8 seconds. The E2B default desktop template took 1.2 seconds, and a Tzafon nonpersistent desktop took 0.22 seconds. Each product starts a different image on a different provisioning substrate, so these numbers describe the product boundary users encounter, not equivalent physical boots. The Modal measurement also excludes startup of the Function runner. Warm operations are now fast enough that startup is my next target.

After chasing these numbers, I now treat repeated setup inside an agent turn as a bug. Recurring work should move closer to the desktop, stay alive across calls, or be shared across an ordered batch. Observation needs a different kind of care: returning a new frame quickly helps only when that frame proves what the next action requires.

## Source notes

- Current warm measurements: [Modal optimized samples, 2026-07-28](../../benchmark-data/modal-optimized-provider-2026-07-28.json), [provider-default samples, 2026-07-28](../../benchmark-data/provider-compare-coordinate-command-2026-07-28.json), [changed-frame samples, 2026-07-28](../../benchmark-data/modal-observation-2026-07-28.json), and [four-click batching A/B, 2026-07-29](../../benchmark-data/modal-action-batching-ab-2026-07-29.json). The 50-turn opener is arithmetic over separate warm p50s, not a full agent trajectory.
- Historical diagnostics: [provider benchmark results, 2026-07-26](../benchmark-results-2026-07-26-provider-results.md), [combined sanitized result](../../benchmark-data/provider-results-2026-07-26.json), [native X11 input benchmark](../archive/benchmarks/benchmark-results-2026-07-23-native-x11-input.md), and [command runner A/B context](../../benchmark-data/tzafon-coordinate-command-context-2026-07-24.json).
- Implementation and contracts: [performance documentation](../performance.md), [benchmarking methodology](../benchmarking.md), [visual-change observation contract](../experimental-visual-change-observation.md), and [create-to-validated-screenshot method](../../research/modal-optimized-create-benchmark-method.md).
- Product surfaces: [E2B Computer use](https://e2b.dev/docs/use-cases/computer-use), [E2B templates](https://e2b.dev/docs/template/quickstart), [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/), and [Daytona snapshots](https://www.daytona.io/docs/snapshots/).
- Modal mechanics and cost: [Functions and Apps](https://modal.com/docs/guide/apps), [region selection](https://modal.com/docs/guide/region-selection), [Sandbox Connect](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets), [input concurrency](https://modal.com/docs/guide/concurrent-inputs), [Function scaling](https://modal.com/docs/guide/scale), [Sandbox resources](https://modal.com/docs/guide/sandbox-resources), [current list pricing](https://modal.com/pricing), and the tracked [provider and cost research memo](../../research/modal-computer-use-provider-cost-comparison-2026-07-29.md).
- External mechanisms: [OpenAI's GPT-5.6 Sol preview](https://openai.com/index/previewing-gpt-5-6-sol/), [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use), [RustDesk self-hosting](https://rustdesk.com/docs/en/self-host/), and Modal's [sandbox architecture account](https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds).
