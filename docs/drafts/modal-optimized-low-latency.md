# How I got computer-use clicks down to 4 ms on Modal

*A computer-use agent can wait on screenshots and input hundreds of times. I shortened that path and kept the desktop machinery alive between calls.*

E2B Desktop and Daytona Computer Use put a Linux desktop behind an API in a few lines. Their APIs were quick to build with, but a screenshot followed by a click still kept the agent waiting for hundreds of milliseconds on the default paths.

A basic computer-use turn wraps model reasoning between two interface calls: fetch the current screen, then click the point the model chose. Those two calls add up to about 413 ms on E2B default and 970 ms on Daytona default. On my tuned Modal path, they add up to 37 ms.

Repeat that work across 50 turns and the computer-use primitives I built on Modal finish in under 2 seconds. E2B's default adds about 21 seconds; Daytona's adds about 49 seconds. As agents take on longer tasks, they pay that infrastructure cost again on every turn.

[OpenAI says its limited Cerebras preview of GPT-5.6 Sol can generate up to 750 tokens per second](https://openai.com/index/previewing-gpt-5-6-sol/). At that speed, the computer can become slower than the model using it. Longer computer-use tasks also consume more model and tool time. Every repeated interface wait pushes the final result farther out.

I started thinking about this while using RustDesk, an [open-source remote desktop system](https://rustdesk.com/docs/en/self-host/). A remote desktop feels fast or slow because of the whole route between an input and the next frame: where encoding runs, how long connections stay open, which processes sit in the path, and how many network handoffs each request crosses. As models got faster, I kept wondering why cloud computers for agents still felt so far away.

I chose Modal because I needed control over both ends of that route. E2B and Daytona expose useful computer-use APIs and customizable environments. Modal let me define the Function caller and Sandbox target in the same Python system, build the desktop Image, request a region for both, wait for readiness, and reach the daemon through authenticated Connect. I could change caller placement and the daemon's hot path together without first building a scheduler or cloud control plane.

The architecture starts with one split: create and connect once, then repeat the screen, model, and action loop for every turn.

![Creation is separate from the repeated computer-use loop](../assets/modal-optimized-agent-loop.svg)

## Create once, then optimize every turn

A computer-use session has two time scales. Creation provisions a sandbox, starts the desktop and daemon, establishes authenticated access, and validates the first frame. The agent then enters an observe, reason, act loop. Creation happens once. Screenshots, clicks, typing, and commands can happen hundreds of times.

That distinction set my priority. Startup matters, especially for short tasks, but repeated latency compounds with every turn. I optimized the warm path first and kept creation as a separate metric.

I kept the measurement boundary outside the primitive. A warm timer begins before the public SDK or daemon request and ends after the caller validates the result. It includes routing, authentication, request handling, execution, and response transfer. Internal timings help locate work, but the user waits for the complete call.

## Screenshots: shorten the route and keep capture alive

I shortened the request route by running the benchmark client in a Modal Function and requesting the target Sandbox's region for that Function. Requests still went through Modal Connect. The runner and target reported the same cloud and region. The evidence records placement at that granularity; it does not identify availability zone, host, network path, or loopback.

Inside the target, one FastAPI/Uvicorn daemon owns the session's desktop state. It keeps display connections, capture state, input state, and the command runner ready. A hot click can reuse the existing display and input state.

The default row starts with my benchmark client outside Modal and reaches the daemon through an attested tunnel. The optimized row starts with a Modal Function in the target's requested region and reaches the same warm daemon through Connect. Each latency number covers the complete request at the caller.

![Default and optimized Modal screenshot request paths](../assets/modal-optimized-screenshot-paths.svg)

Keeping the Function and Sandbox warm changes the cost shape. Modal bills both for the time their resources are allocated. The active tunnel has no separate charge. The optimized path adds a Function runner, and an idle warm session continues to accrue duration. Total cost depends on how quickly the task finishes and how long those resources sit idle. Resource-normalized bills and token usage remain unmeasured here.

X11 is the desktop server that owns the pixels and input. MSS is a capture client, and the daemon keeps one MSS session open. On Linux, MSS prefers XShm, which transfers local pixels through shared memory, and can fall back to XGetImage. The daemon encodes the MSS capture in memory, avoiding a helper subprocess and the temporary-file write, read, and delete cycle; conversion paths also avoid file decode and re-encode. If MSS cannot open or grab after one reset and retry, the compatibility path uses `scrot`, then `maim`, and temporary-file work.

The final Modal default run also reported MSS. It spent about 23 ms inside daemon capture and encoding. The report separately summarized about 104 ms outside that component and about 127 ms end to end. Those medians come from different distributions, so adding them would produce a false decomposition. The remainder includes every other part of the request; the evidence cannot isolate network time.

The optimized artifact reports only the complete 1024x768 PNG request. It measured about 32 ms p50 over 30 samples.

## Input: keep one X11 session open

The compatibility path launched `xdotool` for each action and reopened desktop state. It is a sensible adapter for a broad set of X11 operations, but that lifecycle is expensive inside an agent loop. The native path uses Xlib, the X11 client library, to hold one connection to the server. The XTest extension sends synthetic mouse and keyboard events over it. Each operation precomputes the events, takes one lock, queues them in order, and finishes with one X sync before the call returns.

A controlled A/B against the retained `xdotool` path isolated daemon execution cost. This diagnostic used three samples per arm and arithmetic means from one dirty implementation worktree; both arms used the same source state. Move-and-click fell from about 146 ms to 1.2 ms, a 128x reduction. Four move/click pairs fell from about 444 ms to 4.8 ms, or 93x.

Typing improved less dramatically. One hundred characters fell from about 120 ms to 21 ms, a 5.8x change. One thousand characters fell from about 607 ms to 201 ms, or 3x.

Reducing execution time exposed the correctness work. I resolve characters against the active keyboard layout and X11 key symbols, then serialize held-key and button state. I permit the compatibility fallback only before native emission could have happened, so an action is never replayed after possible partial input. I also enabled Xlib thread support and installed a nonfatal error handler so an asynchronous window error cannot terminate the daemon; synchronous operations still check their own results.

### One request put four ordered clicks at 7 ms

Once input was cheap, batching removed repeated request admission. The four-click case sends one ordered request. The daemon validates the whole batch before execution, acquires the input lock once, preserves order, and stops at the first error by default. In the final run, one click took about 4 ms p50. Four ordered clicks in one request took about 7 ms.

I did not measure a four-separate-request optimized counterfactual. Modal default and Tzafon also used one batch for the four-click row, while Daytona used four SDK and transport requests and E2B used four SDK calls over eight transport requests.

That lock is also a correctness boundary. Without it, two concurrent requests could interleave a move, a button press, and a release into a sequence neither caller asked for. Validation keeps malformed work out of the critical section. Holding the lock across the batch makes the desktop transition match the requested order.

For typing, Modal optimized forces direct zero-delay keystrokes through the persistent XTest session. It took about 10 ms for 100 characters and 50 ms for 1,000. Modal default kept its public `auto` behavior, which selected clipboard for both strings. Other providers kept their defaults, and their internal pacing or input method was not observable. The final provider gaps also include caller placement, transport count, configuration, and typing semantics. I treat those ratios as descriptions of the tested public paths.

## Commands: isolate subprocess I/O from the request loop

Our original benchmark asked for a login shell. That mixed shell initialization with the primitive I wanted to compare. The current harness gives every provider the same logical `sh -c` command, requires exit zero, and checks exact stdout `"42\n"`.

Command pipe setup and cleanup shared Uvicorn's request loop and produced a long tail. The child ran in its own process; its asyncio transport, pipe reads, wait, and cleanup lived on the request loop. A persistent private `SelectorEventLoop` on a daemon thread owns that lifecycle and bridges the result to Uvicorn. Capacity is bounded, and cancellation kills the process group before cleanup.

An earlier controlled 10-sample-per-arm A/B, retained as directional evidence and captured before the process-group safety fix, put the shared loop at about 56 ms p50 and 249 ms p95. The isolated loop tightened those results to 10 and 11 ms. The final command path measured 9 and 10 ms.

The subprocess runner exposed a clipboard bug. A live X11 selection owner serves clipboard data. `xclip` stayed alive after taking ownership and inherited captured stdout and stderr pipes. `communicate()` waited for EOF even though the clipboard had changed. Output capture made sense as a generic default, but this helper did not use it. Sending those streams to `DEVNULL` removed the wait. A long-lived helper cannot retain pipes owned by one request. Optimized typing bypasses the clipboard and uses direct keystrokes, so this fix sits outside its path.

## Warm results across the interface

I tuned Modal and left each provider at its public default. The Modal optimized cells show p50 / p95 from 30 samples. The default cells show the median from three samples, followed by the ratio against Modal optimized p50.

| Warm operation | Modal optimized p50 / p95 | Modal default median / ratio | Daytona default median / ratio | E2B default median / ratio | Tzafon default median / ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, native/default | 32.42 / 34.71 ms | 126.86 ms / 3.91x | 588.74 ms / 18.16x | 191.70 ms / 5.91x | 132.38 ms / 4.08x |
| One click on the screen | 4.43 / 5.06 ms | 209.01 ms / 47.18x | 381.63 ms / 86.15x | 221.15 ms / 49.92x | 154.49 ms / 34.87x |
| Four clicks in one batch | 7.02 / 8.41 ms | 227.56 ms / 32.44x | 1,548.00 ms / 220.66x | 887.19 ms / 126.47x | 474.00 ms / 67.57x |
| Type 100 | 9.95 / 10.62 ms | 249.57 ms / 25.09x | 806.05 ms / 81.05x | 4,104.69 ms / 412.71x | 111.80 ms / 11.24x |
| Type 1000 | 49.58 / 52.35 ms | 248.09 ms / 5.00x | 5,519.92 ms / 111.33x | 41,085.75 ms / 828.61x | 145.78 ms / 2.94x |
| Non-login shell command | 8.98 / 10.14 ms | 83.89 ms / 9.34x | 287.97 ms / 32.07x | 59.18 ms / 6.59x | 58.03 ms / 6.46x |

The full observed ranges are in the [provider report](../benchmark-results-2026-07-26-provider-results.md). Screenshots retained each provider's native format: Tzafon returned 1280x720 JPEG, while Modal, Daytona, and E2B returned 1024x768 PNG. Typing semantics differed as described above. The four-click row also mixed one-request batching with multi-request defaults.

## Changed pixels are an early transition signal

An agent often needs a frame that reflects the previous action. The Modal-only experimental endpoint captures a baseline. It can use XDamage, an X11 notice that a drawable region may have changed, as a wake hint before checking a full-resolution pixel hash. The response returns the hash-confirmed frame that triggered the result. Click to first changed frame measured about 75 ms p50 and 89 ms p95 over 30 samples. The sanitized artifact does not retain whether XDamage or polling supplied the wake signal.

The terminal condition is modest. A blink, cursor, animation, or unrelated repaint can satisfy it, and the first change may capture an intermediate paint. Semantic effects can preserve identical pixels or occur outside the observed region. XDamage supplies a wake hint for the pixel check. A fixed sleep supplies only elapsed time. When the next action depends on application state, the caller still needs a workload-specific readiness condition.

## Fresh creation is still slow

Startup remains the exception to the warm-path results. Fresh product create to validated screenshot measured about 10.3 seconds on Modal optimized, 1.4 seconds for the E2B default desktop template, and 0.28 seconds for a Tzafon nonpersistent desktop. The Modal timer excluded startup of its Modal Function runner. Different templates, images, provisioning substrates, and caller topologies make this a product-level result. An equivalent physical-boot comparison would require matched systems. I omit speed ratios. Startup is the next thing I want to reduce.

After this work, I trace every cost that repeats inside an agent turn and ask where it belongs. Network work can move closer to the target. Setup can survive between calls or spread across a batch. Observation adds another decision: when to stop waiting. Changed pixels show the first visual response, but a dependent action may need stronger evidence that the application is ready.

## Source notes

- Current measurements and fairness boundaries: [provider benchmark results, 2026-07-26](../benchmark-results-2026-07-26-provider-results.md), [combined sanitized result](../../benchmark-data/provider-results-2026-07-26.json), [Modal optimized samples](../../benchmark-data/modal-optimized-provider-2026-07-26.json), and [provider-default samples](../../benchmark-data/provider-compare-coordinate-command-2026-07-26.json).
- Controlled input evidence: [native X11 input benchmark](../archive/benchmarks/benchmark-results-2026-07-23-native-x11-input.md). Earlier directional command-runner evidence is recorded in the tracked [Tzafon coordinate and command context](../../benchmark-data/tzafon-coordinate-command-context-2026-07-24.json).
- Implementation and contracts: [performance documentation](../performance.md), [benchmarking methodology](../benchmarking.md), [visual-change observation contract](../experimental-visual-change-observation.md), and [create-to-validated-screenshot method](../../research/modal-optimized-create-benchmark-method.md).
- Product surfaces and billing: [E2B Computer use](https://e2b.dev/docs/use-cases/computer-use), [E2B billing](https://e2b.dev/docs/billing), [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/), [Daytona billing](https://www.daytona.io/docs/billing), [Modal Sandbox resources](https://modal.com/docs/guide/sandbox-resources), and [Modal tunnel pricing](https://modal.com/docs/guide/tunnels#pricing).
- External architecture and mechanism references: [OpenAI's GPT-5.6 Sol preview](https://openai.com/index/previewing-gpt-5-6-sol/), [RustDesk self-hosting documentation](https://rustdesk.com/docs/en/self-host/), Modal's [sandbox architecture account](https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds), [region selection](https://modal.com/docs/guide/region-selection), [Sandbox networking](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets), and [Images](https://modal.com/docs/guide/images).
