# How I built 10 ms computer-use clicks on Modal

*The tuned warm path returns a full 1024x768 PNG in 29 ms and sends one click in 10 ms.*

The first computer-use interface I built on Modal exposed the delay immediately. Looking at the screen and sending one click took about 550 ms on untuned Modal. The E2B and Daytona defaults I tested took about 420 ms and 480 ms.

A screenshot-and-click pair is one observation and one action: return the current frame, then send one click. A few hundred milliseconds is easy to ignore once. At those warm rates, fifty pairs add roughly 20 to 28 seconds of primitive time. The same arithmetic comes in under 2 seconds for the primitives I built on Modal.

[OpenAI says GPT-5.6 Sol on Cerebras can generate up to 750 tokens per second](https://openai.com/index/previewing-gpt-5-6-sol/). At that speed, the computer can become slower than the model using it. Long computer-use trajectories are useful because they let an agent complete whole workflows. They already consume model and tool time at each step, and every infrastructure wait delays the person waiting for the result.

That bottleneck reminded me of RustDesk, an [open-source remote desktop system](https://rustdesk.com/docs/en/self-host/). Its rendezvous server handles discovery, then traffic takes a direct path when possible or falls back to a relay. What stuck with me was the separation between connection setup and the path used for every frame and input event. A computer-use agent has the same recurring exchange: pixels go out, an action comes back, and the cycle continues for the life of the session. I started looking for work that Modal repeated inside that loop.

E2B and Daytona expose ready-made APIs for screenshots, mouse input, typing, and commands. Both also let developers customize the environment through templates or snapshots. In this benchmark, I left their documented screenshot and input paths at provider defaults.

Modal let me define the desktop, its computer-use server, and the benchmark caller in one Python project. An Image describes the desktop packages and files, and Modal starts it inside an isolated Sandbox. A small FastAPI daemon in that Sandbox owns screenshots, input, and commands.

For the benchmark client, I used a Modal Function, Python code in an autoscaled container. The SDK gives me a credential-free `ComputerSessionHandle` to pass into my deployed Function. Inside, it borrows an authenticated connection for the loop. My application owns the model loop and desktop lifetime.

![Creation is separate from the repeated computer-use loop](../assets/modal-optimized-agent-loop.svg)

Creation happens once. I create the Sandbox, start the desktop, and validate its first frame. The agent then repeats screenshots, clicks, typing, and commands. Those warm operations determine how responsive a running agent feels.

## Move the caller before choosing the ingress

The default Modal setup ran the benchmark client outside Modal and reached the warm daemon through an attested HTTP tunnel. A full 1024x768 PNG took 230 ms p50. I moved the client into a Modal Function, requested `us-west-2` for the Function and target Sandbox, and used Connect for repeated requests. That configuration returned the PNG in 29 ms p50.

An earlier diagnostic reported about 23 ms for capture and PNG encoding. Separate summaries put the full request at 127 ms and the remainder at 104 ms. Their medians came from different samples, so they only show that capture was part of the delay.

Those runs changed caller placement and ingress together. A separate Connect-only test held ingress fixed: one move-and-click fell from 32.4 ms with the external caller to 4.6 ms from a Function requesting the target's region. I then held that Function caller fixed and tested ingress.

![Default and optimized Modal screenshot request paths](../assets/modal-optimized-screenshot-paths.svg)

I ran both ingress paths from one Function to one warm target. They used the same image, resources, daemon, payloads, and persistent HTTP/1.1 clients. Both requested `us-west-2` and reported the same cloud and region. I alternated their order across 30 samples per arm. Connect authorization finished before tunnel warmup and took 237 ms, then 228 ms on confirmation, outside the recurring samples.

The initial run favored the tunnel by 1.24%; confirmation favored Connect by 0.02%. Every request succeeded, and neither cleared the 10% gate. I standardized on attested tunnel, matching the general SDK: Connect mints a short-lived daemon token once, then recurring calls use the encrypted tunnel.

The tunnel led the zero-byte floor by 0.9 ms, then 0.3 ms. That probe did not select the winner. The screenshot A/B continued through in-memory frame validation, so its 84 to 87 ms results have a different boundary from the 29 ms provider row.

This separates caller placement from ingress selection. Moving the caller shortened the repeated route. Once placed there, Connect and the attested tunnel performed alike. Modal reported the requested cloud and region for both containers. I did not test their availability zone, host, or network class.

I had also removed target-side setup. The compatibility path launches `scrot` or `maim`, writes a temporary file, and reads it back. The optimized path keeps an MSS capture session open, reads through shared memory when available, and encodes in RAM.

X11 is the Linux display server that owns the desktop's pixels and input state. MSS is the screenshot library that reads them. Its session prefers XShm, which gives a local X11 client shared-memory access to the frame. After a failed reset and retry, the daemon falls back to the compatibility path.

Both Modal paths used MSS-first capture. The 230 ms to 29 ms result therefore covers the Function-runner configuration against the default Modal setup.

The A/B requested 4 CPU cores and 8 GiB to control resources. Functions bill only while active. There is no shared Function pool, and my application must keep overlapping trajectories off a desktop. Each desktop stays billable, so idle sessions cost money.

## Keep one X11 input session open

The original input path shelled out to `xdotool`, a command-line X11 automation tool. Each click started a process, opened a new X11 connection, moved the pointer, pressed the left mouse button, and released it. The agent paid that setup cost again on every action.

I replaced that path with one persistent Xlib connection. Xlib is the client library for X11, and its XTest extension sends synthetic mouse and keyboard events. For each request, I precompute the event sequence, acquire one lock so the sequence owns the input state, queue the events in order, and issue one final X sync before returning.

![Per-action xdotool setup compared with one persistent X11 input connection](../assets/modal-optimized-input-session.svg)

Clicks showed the largest gain. Daemon-side mean move-and-click time, a pointer move followed by one left click, fell from 146.33 ms to 1.15 ms, a 127.7x speedup. Four move-and-click pairs fell from 443.99 ms to 4.80 ms, a 92.5x speedup.

Typing still paid for many key-down and key-up events. Daemon-side mean time for a fixed 100-character ASCII workload fell from 119.77 ms to 20.61 ms, while the 1,000-character case fell from 607.35 ms to 201.34 ms. Both arms used the same workload and zero inter-character delay, so the controlled difference was the input backend.

Direct input also needs to preserve keyboard behavior. I resolve characters through the active X11 layout and key symbols. The lock stops simultaneous requests from corrupting held-key or button state. A request is never retried after XTest may have emitted part of its sequence, because replaying it could duplicate input. One bad X11 request cannot terminate the daemon, while synchronous failures still return an error.

Typing benefits from the same persistent connection, although each character still needs key-down and key-up events. The controlled backend A/B above reports daemon-side means. In the end-to-end provider run, 30 samples of direct typing with zero delay between characters measured 15.27 ms p50 for 100 characters and 63.34 ms p50 for 1,000 characters.

## Batch the actions the model already grouped

OpenAI's current computer tool can return several ordered actions in one turn through its `actions[]` array. The documentation tells clients to execute those actions in order. Sending each click as a new API call throws away that structure.

The project's computer-use SDK sends the model's ordered actions to the daemon in one request:

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

I gave subprocess work a private `SelectorEventLoop`, Python's event loop for file-descriptor readiness, on a dedicated daemon thread. A bounded queue prevents command requests from creating unlimited subprocess work while Uvicorn remains responsive. A small result bridge carries each completed result back. Cancellation kills the child's process group before cleanup, so capacity does not leak when a request disappears.

![Subprocess I/O ownership moves from Uvicorn to a private event loop while the child process remains separate](../assets/modal-optimized-command-loop-isolation.svg)

Giving subprocess I/O its own loop collapsed the tail. The shared asyncio path measured 55.92 ms p50, but p95 reached 248.67 ms. With the isolated loop, p50 was 9.86 ms and p95 was 10.63 ms. In the final 30-sample run, the command finished in 9.54 ms p50 and 17.03 ms p95. The test runs `sh -c`, prints `42`, and requires exit code zero with exact stdout `"42\n"`.

Clipboard writes use `xclip`, a command-line program that must stay alive because X11 asks the current selection owner for clipboard data.

The generic subprocess helper gave `xclip` captured stdout and stderr pipes. Its long-lived selection owner inherited those pipes, leaving `communicate()` waiting for EOF after the clipboard had already changed. The helper never used that output, so I redirected both streams to `DEVNULL`. A long-lived helper cannot keep ownership of pipes that belong to one request. Direct XTest typing bypasses this clipboard path.

## Modal optimized was up to 650x faster than provider defaults

The July 28 Connect-backed optimized run measured each warm operation 30 times. Only that path was tuned. The remaining columns show the provider defaults exercised by the harness. Each ratio divides the provider p50 by the Modal optimized p50.

The screenshot calls kept each provider's native format: Tzafon returned 1280x720 JPEG; Modal, Daytona, and E2B returned 1024x768 PNG. Modal optimized used direct XTest keystrokes with zero delay. Modal default's public `auto` mode selected clipboard for both strings; the harness could not observe the internal typing method or pacing behind the other provider SDKs.

For four clicks in this benchmark, Modal optimized, Modal default, and Tzafon each used one SDK call and one transport request. Daytona used four SDK calls and four transport requests; E2B used four SDK calls and eight transport requests. These counts describe the pinned SDK paths measured here.

| Warm operation | Modal optimized p50 / p95 | Modal default p50 / p95 / ratio | Daytona default p50 / p95 / ratio | E2B default p50 / p95 / ratio | Tzafon default p50 / p95 / ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, provider-native format | 28.69 / 43.96 ms | 229.94 / 235.78 ms / 8.02x | 264.85 / 297.67 ms / 9.23x | 212.75 / 225.28 ms / 7.42x | 146.14 / 168.34 ms / 5.09x |
| One click on the screen | 9.69 / 15.55 ms | 323.81 / 331.35 ms / 33.43x | 216.04 / 221.02 ms / 22.31x | 210.56 / 214.88 ms / 21.74x | 125.75 / 127.92 ms / 12.98x |
| Four ordered clicks | 13.64 / 21.98 ms | 342.68 / 349.16 ms / 25.12x | 865.55 / 1,061.66 ms / 63.45x | 855.81 / 876.02 ms / 62.74x | 455.96 / 531.37 ms / 33.42x |
| Type 100 characters | 15.27 / 20.69 ms | 379.28 / 389.18 ms / 24.84x | 638.50 / 706.86 ms / 41.81x | 4,090.74 / 4,194.92 ms / 267.86x | 82.27 / 85.45 ms / 5.39x |
| Type 1,000 characters | 63.34 / 83.91 ms | 378.40 / 413.17 ms / 5.97x | 5,356.25 / 5,425.81 ms / 84.57x | 41,048.75 / 41,619.86 ms / 648.12x | 181.03 / 212.66 ms / 2.86x |
| Non-login shell command | 9.54 / 17.03 ms | 182.43 / 346.47 ms / 19.12x | 116.92 / 120.86 ms / 12.26x | 52.18 / 54.35 ms / 5.47x | 29.60 / 30.84 ms / 3.10x |

## Observe the first changed frame

An action acknowledgement tells me that input reached the desktop. The application may not have painted its response yet. I built a Modal-only alpha endpoint that captures a baseline before the action, uses XDamage as a wake hint, and verifies the change with a full-resolution pixel hash. XDamage is an X11 notification that some area may have been repainted. The endpoint returns the same frame that passed the hash check. Click to first changed frame measured 65.39 ms p50 and 76.31 ms p95 across 30 samples.

![XDamage hints or polling trigger a pixel check, while application readiness remains caller-owned](../assets/modal-optimized-first-change.svg)

The first changed pixel is a narrow terminal condition. A cursor blink, animation, or unrelated repaint can satisfy it, and the first change may be an intermediate frame. A semantic effect may leave identical pixels or happen outside the observed region. This endpoint does not wait for settle or prove application readiness. A dependent next action still needs a workload-specific condition.

## Startup is the next bottleneck

In a fresh product create to validated screenshot test with 30 samples per reported product, Modal optimized measured 7.8 seconds p50. The E2B default desktop template measured 1.2 seconds p50, and a Tzafon nonpersistent desktop measured 0.22 seconds p50. Each product starts a different image on a different provisioning substrate, so these numbers describe the product boundary users encounter, not equivalent physical boots. The Modal measurement also excludes startup of the Function runner. Warm operations are now fast enough that startup is my next target.

After chasing these numbers, I now treat repeated setup inside an agent turn as a bug. Recurring work should move closer to the desktop, stay alive across calls, or be shared across an ordered batch. Observation needs a different kind of care: returning a new frame quickly helps only when that frame proves what the next action requires.

## Source notes

- Current warm measurements: [Modal optimized samples, 2026-07-28](../../benchmark-data/modal-optimized-provider-2026-07-28.json), [provider-default samples, 2026-07-28](../../benchmark-data/provider-compare-coordinate-command-2026-07-28.json), [changed-frame samples, 2026-07-28](../../benchmark-data/modal-observation-2026-07-28.json), [four-click batching A/B, 2026-07-29](../../benchmark-data/modal-action-batching-ab-2026-07-29.json), and [Connect versus attested-tunnel A/B, 2026-07-29](../../benchmark-data/modal-optimized-ingress-ab-2026-07-29.json). The 50-turn opener is arithmetic over separate warm p50s, not a full agent trajectory.
- Historical diagnostics: [provider benchmark results, 2026-07-26](../benchmark-results-2026-07-26-provider-results.md), [combined sanitized result](../../benchmark-data/provider-results-2026-07-26.json), [Connect caller-placement evidence](../../benchmark-data/modal-optimized-competitive-us-west-2-2026-07-24.json), [native X11 input benchmark](../archive/benchmarks/benchmark-results-2026-07-23-native-x11-input.md), and [command runner A/B context](../../benchmark-data/tzafon-coordinate-command-context-2026-07-24.json).
- Implementation and contracts: [performance documentation](../performance.md), [benchmarking methodology](../benchmarking.md), [visual-change observation contract](../experimental-visual-change-observation.md), and [create-to-validated-screenshot method](../../research/modal-optimized-create-benchmark-method.md).
- Product surfaces: [E2B Computer use](https://e2b.dev/docs/use-cases/computer-use), [E2B templates](https://e2b.dev/docs/template/quickstart), [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/), and [Daytona snapshots](https://www.daytona.io/docs/snapshots/).
- Modal mechanics and cost: [Functions and Apps](https://modal.com/docs/guide/apps), [region selection](https://modal.com/docs/guide/region-selection), [Sandbox Connect](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets), [encrypted tunnels](https://modal.com/docs/guide/tunnels), [input concurrency](https://modal.com/docs/guide/concurrent-inputs), [Function scaling](https://modal.com/docs/guide/scale), [Sandbox resources](https://modal.com/docs/guide/sandbox-resources), [current list pricing](https://modal.com/pricing), and the tracked [provider and cost research memo](../../research/modal-computer-use-provider-cost-comparison-2026-07-29.md).
- External mechanisms: [OpenAI's GPT-5.6 Sol preview](https://openai.com/index/previewing-gpt-5-6-sol/), [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use), [RustDesk self-hosting](https://rustdesk.com/docs/en/self-host/), and Modal's [sandbox architecture account](https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds).
