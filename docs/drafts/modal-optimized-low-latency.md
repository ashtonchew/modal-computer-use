# 32 ms screenshots and 4 ms clicks: tuning computer use on Modal

*How I built a warm computer-use path that ran faster than the E2B and Daytona defaults in my dated comparison.*

E2B Desktop and Daytona Computer Use made it easy to put a Linux desktop behind an API. Their default paths also made me wait. In my dated warm-operation comparison, a 1024x768 PNG took 191.70 ms from E2B and 588.74 ms from Daytona. One coordinate click took 221.15 and 381.63 ms. Those pauses look small in isolation. A computer-use agent pays them each time it observes and acts.

Consider a simple 50-turn model with one click and one screenshot per turn. Adding the separate central measurements gives 20.64 seconds of interface time on E2B default and 48.52 seconds on Daytona default. My optimized Modal path comes to 1.84 seconds, leaving modeled gaps of 18.80 and 46.68 seconds. This is arithmetic, not a measured trajectory or a median of complete turns. It excludes inference, startup, application settle, failures, and retries. It still shows how a few hundred milliseconds inside a loop becomes product latency.

I wanted to make the computer side feel immediate. On Modal I reached 32.42 ms p50 for a full screenshot, 4.43 ms for a click, 9.95 ms for 100 typed characters, and 8.98 ms for a command. In the final dated comparison, the tuned path was faster in all six reported warm-operation rows. Only Modal optimized was tuned, with 30 samples versus three per provider default, so this compares dated public paths rather than ranking providers universally.

These waits also occupy metered infrastructure. E2B, Daytona, and Modal charge active sandbox resources by duration, so shorter sessions can use fewer resource-seconds at a fixed configuration. I did not normalize resources, reconcile bills, or run a model. The optimized topology also used a Modal Function runner. I measured latency, not token or dollar savings.

Faster inference makes repeated computer-side waits more visible. [OpenAI says its limited Cerebras preview of GPT-5.6 Sol can generate up to 750 tokens per second](https://openai.com/index/previewing-gpt-5-6-sol/). At that rate, the computer can become slower than the model using it. Longer tasks repeat that infrastructure delay more often, pushing out the final result.

I chose Modal for control of the path. E2B and Daytona each expose computer-use APIs and support customized environments. Modal gave me a Function caller, a Sandbox target, the Image, a shared region selector, readiness, and authenticated Connect access in one Python system. I could move the caller, own the daemon, and replace its hot-path machinery without building a scheduler or cloud control plane. That was the ergonomic advantage I needed.

I came to this problem through RustDesk. [RustDesk is an open-source remote desktop system](https://rustdesk.com/docs/en/self-host/), and working with it trained me to see control latency as an architectural property. Encoding matters, but so do connection lifetime, process boundaries, placement, and the number of times a request changes hands. I did not copy RustDesk's implementation, and Modal Connect is not a peer-to-peer path. RustDesk taught me to trace each request route and remove work repeated on every call.

![Creation is separate from the repeated computer-use loop](../assets/modal-optimized-agent-loop.svg)

## The latency that repeats inside every agent turn

A computer-use session has two time scales. Creation provisions a sandbox, starts the desktop and daemon, establishes authenticated access, and validates the first frame. The agent then enters an observe, reason, act loop. Creation happens once. Screenshots, clicks, typing, and commands can happen hundreds of times.

That distinction set my priority. Startup matters, especially for short tasks, but repeated latency compounds with every turn. I optimized the warm path first and kept creation as a separate metric.

I kept the measurement boundary outside the primitive. A warm timer begins before the selected public SDK or daemon request and ends after the caller validates the result. It includes routing, authentication, request handling, execution, and response transfer. Internal timings locate work, but the user waits for the whole call. The optimized arm has 30 samples with p50 and p95. Provider defaults have three, so I report their medians and observed ranges without characterizing their tails.

## The same-region warm screenshot path measured 32 ms

I shortened the request route by running the benchmark client in a Modal Function and requesting the same Modal region for the runner and target Sandbox. Requests still went through Modal Connect. I cannot infer availability zone, host, private networking, or loopback from that placement request. The runner and target reported matching cloud and region in the accepted evidence, so I describe the topology as "same requested Modal region."

Inside the target, one FastAPI/Uvicorn daemon owns the session's desktop state. It keeps display connections, capture state, input state, and the command runner ready. A hot click should not rediscover the display or launch a helper program.

The labels in the two rows name different public routes. The default attested tunnel used a Connect token to bootstrap short-lived daemon authorization over an encrypted tunnel. Modal optimized sent the recurring request through Connect itself. The experiment changed caller placement and configuration as well as that route, so the diagram does not attribute the end-to-end gap to transport alone.

![Default and optimized Modal screenshot request paths](../assets/modal-optimized-screenshot-paths.svg)

X11 is the desktop server that owns the pixels and input. MSS is a capture client, and the daemon keeps one MSS session open. On Linux, MSS prefers XShm, which transfers local pixels through shared memory, and can fall back to XGetImage. The daemon encodes the MSS capture in memory, avoiding a helper subprocess and the temporary-file write, read, and delete cycle; conversion paths also avoid file decode and re-encode. If MSS cannot open or grab after one reset and retry, the compatibility path uses `scrot`, then `maim`, and temporary-file work.

The final Modal default run also reported MSS. Its daemon capture-and-encode median was about 22.83 ms. A separate summary put the end-to-end remainder at 103.91 ms and the complete request at 126.86 ms. Adding medians from different distributions would be wrong. The remainder covers everything outside daemon capture and encoding; it is not pure network time. The optimized artifact does not contain a comparable component breakdown. It records the same end-to-end 1024x768 PNG operation at 32.42 ms p50 over 30 samples.

## Persistent input cut daemon execution from 146 ms to 1 ms

The compatibility path launched `xdotool` for each action and reopened desktop state. It is a sensible adapter for a broad set of X11 operations, but that lifecycle is expensive inside an agent loop. The native path uses Xlib, the X11 client library, to hold one connection to the server. The XTest extension sends synthetic mouse and keyboard events over it. Each operation precomputes the events, takes one lock, queues them in order, and finishes with one X sync before the call returns.

A controlled A/B against the retained `xdotool` path isolated daemon execution cost. This diagnostic used three samples per arm and arithmetic means from one dirty implementation worktree; both arms used the same source state. Move-and-click fell from 146.33 to 1.15 ms, a 127.7x reduction. Four move/click pairs went from 443.99 to 4.80 ms, or 92.5x. Typing 100 characters moved from 119.77 to 20.61 ms, 5.8x, while 1,000 characters moved from 607.35 to 201.34 ms, 3.0x.

Reducing execution time exposed the correctness work. I resolve characters against the active keyboard layout and X11 key symbols, then serialize held-key and button state. I permit the compatibility fallback only before native emission could have happened, so an action is never replayed after possible partial input. I also enabled Xlib thread support and installed a nonfatal error handler so an asynchronous window error cannot terminate the daemon; synchronous operations still check their own results.

### One request and one lock put four clicks at 7 ms

Once input was cheap, batching removed repeated request admission. The four-click case sends one ordered request. The daemon validates the whole batch before execution, acquires the input lock once, preserves order, and stops at the first error by default. In the final run, one click was 4.43 ms p50 and four clicks were 7.02 ms p50. I did not measure a four-separate-request optimized counterfactual. Modal default and Tzafon also used one batch for the four-click row, while Daytona used four SDK and transport requests and E2B used four SDK calls over eight transport requests.

That lock is also a correctness boundary. Without it, two concurrent requests could interleave a move, a button press, and a release into a sequence neither caller asked for. Validation keeps malformed work out of the critical section. Holding the lock across the batch makes the desktop transition match the requested order.

For typing, Modal optimized forces direct zero-delay keystrokes through the persistent XTest session. It measured 9.95 ms for 100 characters and 49.58 ms for 1,000. Modal default kept its public `auto` behavior, which selected clipboard for both strings. Other providers kept their defaults, and their internal pacing or input method was not observable. The final cross-provider gaps are descriptive. They exceed the controlled XTest-only gains because the final comparison also changes caller placement, transport count, configuration, and typing semantics.

## Isolating subprocess I/O cut command p95 from 249 ms to 11 ms

Our original benchmark asked for a login shell. That mixed shell initialization with the primitive I wanted to compare. The current harness gives every provider the same logical `sh -c` command, requires exit zero, and checks exact stdout `"42\n"`.

Command pipe setup and cleanup shared Uvicorn's request loop and produced a long tail. The child program never ran on that thread, but creating its asyncio transport, reading its pipes, waiting, and cleaning up happened there. I moved the entire lifecycle to a persistent private `SelectorEventLoop` on a dedicated daemon thread, leaving Uvicorn to await a bridged result. Capacity is bounded, and cancellation kills the process group before cleanup. An earlier controlled 10-sample-per-arm A/B, retained as directional evidence and captured before the process-group safety fix, measured shared `asyncio` at 55.92 ms p50 and 248.67 ms p95. Isolated `asyncio` measured 9.86 and 10.63 ms. The final command result was 8.98 and 10.14 ms.

The subprocess runner also exposed a clipboard bug. X11 clipboard data is served by a live selection owner. The runner launched `xclip`, which stayed alive after taking ownership and inherited captured stdout and stderr pipes. `communicate()` waited for EOF even though the clipboard had changed. Output capture made sense as a generic default, but this helper did not use it. Sending those streams to `DEVNULL` removed the wait. A long-lived helper cannot retain pipes owned by one request. This fix does not explain optimized typing, which uses direct keystrokes.

## Warm-path results: 32 ms screenshots, 4 ms clicks, 9 ms commands

Only Modal optimized was tuned. Its cells show p50 / p95 with `n=30`. Provider-default cells show the median with `n=3`, followed by the ratio versus Modal optimized p50. A ratio above 1 means the provider-default request took longer.

| Warm operation | Modal optimized p50 / p95 | Modal default median / ratio | Daytona default median / ratio | E2B default median / ratio | Tzafon default median / ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, native/default | 32.42 / 34.71 ms | 126.86 ms / 3.91x | 588.74 ms / 18.16x | 191.70 ms / 5.91x | 132.38 ms / 4.08x |
| One coordinate click | 4.43 / 5.06 ms | 209.01 ms / 47.18x | 381.63 ms / 86.15x | 221.15 ms / 49.92x | 154.49 ms / 34.87x |
| Four coordinate clicks | 7.02 / 8.41 ms | 227.56 ms / 32.44x | 1,548.00 ms / 220.66x | 887.19 ms / 126.47x | 474.00 ms / 67.57x |
| Type 100 | 9.95 / 10.62 ms | 249.57 ms / 25.09x | 806.05 ms / 81.05x | 4,104.69 ms / 412.71x | 111.80 ms / 11.24x |
| Type 1000 | 49.58 / 52.35 ms | 248.09 ms / 5.00x | 5,519.92 ms / 111.33x | 41,085.75 ms / 828.61x | 145.78 ms / 2.94x |
| Non-login shell command | 8.98 / 10.14 ms | 83.89 ms / 9.34x | 287.97 ms / 32.07x | 59.18 ms / 6.59x | 58.03 ms / 6.46x |

The full observed ranges are in the [dated provider report](../benchmark-results-2026-07-26-provider-results.md). Screenshots retained each provider's native format: Tzafon returned 1280x720 JPEG, while Modal, Daytona, and E2B returned 1024x768 PNG. Typing semantics differed as described above. The four-click row also mixed one-request batching with multi-request defaults. This table describes the public systems I tested; it is not a universal provider ranking or a controlled attribution of every difference.

## Changed pixels mark a transition, not application readiness

An immediate frame is not always the frame an agent needs. The Modal-only experimental endpoint captures a baseline. It can use XDamage, an X11 notice that a drawable region may have changed, as a wake hint before checking a full-resolution pixel hash. The response returns the hash-confirmed frame that triggered the result. Click to first changed frame measured 75.25 ms p50 and 88.78 ms p95 over 30 samples. The sanitized artifact does not retain whether XDamage or polling supplied the wake signal.

The terminal condition is modest. A blink, cursor, animation, or unrelated repaint can satisfy it, and the first change can be an intermediate paint. A semantic effect can leave identical pixels or occur outside the observed region. XDamage only prompts a pixel check. It does not prove settle or application readiness. A fixed sleep has different failure modes and proves neither. When the next action depends on application state, the caller still needs a workload-specific condition.

## Warm latency improved; startup did not

Startup remains the exception to the warm-path results. Measured as fresh product create to validated screenshot, Modal optimized was 10.25 seconds p50, the E2B default desktop template was 1.39 seconds median, and a Tzafon nonpersistent desktop was 283 ms median. The Modal timer excluded startup of its Modal Function runner. Different templates, images, provisioning substrates, and caller topologies make this a product-level result rather than an equivalent physical boot. I report it without speed ratios. I want to reduce startup next.

After this work, I am suspicious of any cost paid on every agent turn. The remedy depends on where it sits. I can shorten a repeated route, leave useful session state alive, or admit ordered work once. Observation is different: an acknowledgement measures actuation, while changed pixels measure only the first visual response. Before a dependent action, I still wait for an application-specific condition when one exists.

## Source notes

- Current measurements and fairness boundaries: [provider benchmark results, 2026-07-26](../benchmark-results-2026-07-26-provider-results.md), [combined sanitized result](../../benchmark-data/provider-results-2026-07-26.json), [Modal optimized samples](../../benchmark-data/modal-optimized-provider-2026-07-26.json), and [provider-default samples](../../benchmark-data/provider-compare-coordinate-command-2026-07-26.json).
- Controlled input evidence: [native X11 input benchmark](../archive/benchmarks/benchmark-results-2026-07-23-native-x11-input.md). Earlier directional command-runner evidence is recorded in the tracked [Tzafon coordinate and command context](../../benchmark-data/tzafon-coordinate-command-context-2026-07-24.json).
- Implementation and contracts: [performance documentation](../performance.md), [benchmarking methodology](../benchmarking.md), [visual-change observation contract](../experimental-visual-change-observation.md), and [create-to-validated-screenshot method](../../research/modal-optimized-create-benchmark-method.md).
- Product surfaces and billing: [E2B Computer use](https://e2b.dev/docs/use-cases/computer-use), [E2B billing](https://e2b.dev/docs/billing), [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/), [Daytona billing](https://www.daytona.io/docs/billing), and [Modal Sandbox resources](https://modal.com/docs/guide/sandbox-resources).
- External architecture and mechanism references: [OpenAI's GPT-5.6 Sol preview](https://openai.com/index/previewing-gpt-5-6-sol/), [RustDesk self-hosting documentation](https://rustdesk.com/docs/en/self-host/), Modal's [sandbox architecture account](https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds), [region selection](https://modal.com/docs/guide/region-selection), [Sandbox networking](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets), and [Images](https://modal.com/docs/guide/images).
