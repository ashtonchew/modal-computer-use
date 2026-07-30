# How I built 10 ms computer-use clicks on Modal

*The tuned warm path returns a full 1024x768 PNG in 29 ms and sends one click in 10 ms.*

The first computer-use interface I built on Modal exposed the delay immediately. Looking at the screen and sending one click took about 550 ms on untuned Modal. The E2B and Daytona defaults I tested took about 420 ms and 480 ms.

A screenshot followed by a click completes one useful exchange with the desktop. A few hundred milliseconds is easy to ignore once. Repeat it fifty times and the interface alone consumes roughly 20 to 28 seconds. The primitives I built on Modal complete the same arithmetic in under 2 seconds.

[OpenAI says GPT-5.6 Sol on Cerebras can generate up to 750 tokens per second](https://openai.com/index/previewing-gpt-5-6-sol/). As inference gets faster, the computer starts holding up the model. Longer trajectories make computer-use agents more valuable, but they also make every repeated infrastructure wait visible to the person waiting for the final result.

The shape of the problem reminded me of RustDesk, an [open-source remote desktop system](https://rustdesk.com/docs/en/self-host/). RustDesk separates the work of finding and connecting peers from the path that carries every frame and input event. That trained me to look at control latency as an architectural property. The important question was no longer how quickly I could create a desktop. It was what happened every time the agent looked at it or touched it.

![Creation is separate from the repeated computer-use loop](../assets/modal-optimized-agent-loop.svg)

I needed to change both sides of that path. Modal let me run the desktop in a Sandbox and my caller in a Function, user code running in an autoscaled container, while keeping both in one Python-defined system. I could move the caller and replace code inside the desktop. I left the documented E2B and Daytona paths at their provider defaults.

## Move the caller before choosing the ingress

My first trace began outside Modal. The client sent each request through an attested HTTP tunnel to a warm daemon inside the desktop Sandbox. A full 1024x768 PNG took 230 ms p50.

I moved the same client into a Modal Function and requested `us-west-2` for both Function and Sandbox. The request still crossed an authenticated ingress, but it no longer started from my laptop. The screenshot fell to 29 ms p50. In a smaller test that held Connect ingress fixed, one move-and-click fell from 32.4 ms outside Modal to 4.6 ms from the Function.

![Default and optimized Modal screenshot request paths](../assets/modal-optimized-screenshot-paths.svg)

That first experiment changed two variables: caller placement and ingress. I ran a second A/B from one Function to one warm target, alternating Connect and the attested tunnel across 30 requests per arm. The winner flipped between runs, and the largest gap was 1.24%. Every request succeeded. I standardized the optimized SDK path on the attested tunnel used by the general client.

The useful result came from moving the caller. Requesting the same Modal region shortened the recurring route without implying the same availability zone, host, or private network. The application now passes a credential-free `ComputerSessionHandle` into a deployed Function and borrows one authenticated client for the trajectory. My application still owns the model loop and desktop lifetime.

The Function bills while the trajectory is active, then can scale to zero. The Sandbox remains billable until its owner terminates it, and the application must prevent two trajectories from driving the same desktop at once.

## Keep screenshot capture in memory

Once the request route was short, work inside the daemon was large enough to see. The original default path launched `scrot` or `maim` for every screenshot, wrote a temporary image, then opened that file again to return it. The agent paid for process startup and filesystem work on every observation.

I applied the RustDesk lesson again. X11 is the display system that owns the Linux desktop's pixels. MSS became a persistent bridge between the daemon and that display. On Linux, its preferred XShm backend lets the X server write pixels into shared memory instead of sending them through the slower `XGetImage` path.

The daemon now keeps one MSS session open and encodes the PNG in memory. A normal screenshot no longer needs a child process or a filename before it becomes an HTTP response. If the session fails, the daemon reopens it once. A second failure, or a capture mode that needs the file adapter, falls back to `scrot` and then `maim`.

By the final provider run, both the Modal default and optimized paths used MSS-first capture. The 230 ms to 29 ms gap in that table therefore reflects the Function-runner configuration, not a claim that the default row still wrote screenshots to disk.

## A process for every click

On a local computer, a click feels atomic. X11 sees a short sequence: move the pointer, press a button, release it. My first implementation asked `xdotool`, a command-line X11 automation program, to produce that sequence. Every API action started a process, opened a display connection, sent the events, and exited.

The event mechanism was already fast. `xdotool` itself uses XTest, the X11 extension for synthetic keyboard and mouse events, together with Xlib. The repeated cost lived around it. I removed the command-line boundary and opened one Xlib connection when the daemon started. Each request now builds its XTest events in memory, takes one input lock, queues the sequence, and synchronizes with the X server once at the end.

![Per-action xdotool setup compared with one persistent X11 input connection](../assets/modal-optimized-input-session.svg)

Daemon-side mean move-and-click time, a pointer move followed by one left click, fell from 146.33 ms to 1.15 ms, a 127.7x speedup. Four move-and-click pairs fell from 443.99 ms to 4.80 ms. Typing 100 characters dropped from 119.77 ms to 20.61 ms; 1,000 characters dropped from 607.35 ms to 201.34 ms. Typing improves less because every character still expands into key-down and key-up events.

The persistent session also owns keyboard state. On a US layout, `A` requires Shift plus `a`, while `@` requires Shift plus `2`; another layout may choose different keys. The daemon reads the active XKB map and builds the right key sequence. It holds the input lock throughout, so a second request cannot type while the first has Shift held or a mouse button down.

Retries stop at the same boundary. Before the first XTest event, the daemon can safely use the default `xdotool` path if XTest is unavailable. Once an event may have reached X11, replaying the request could type a character twice or click twice, so that failure is returned without fallback. Xlib errors are contained and reported instead of terminating the daemon.

## Batch clicks at the API boundary

Making one local click take about a millisecond exposed the next repeated cost: asking for it over HTTP. Models can already return several ordered actions in one turn. OpenAI's computer tool, for example, returns them in an `actions[]` array and asks the client to execute them in order. Turning that array into separate requests gives the transport more work than the desktop.

The SDK keeps the model's sequence intact:

```python
computer.actions.run([
    {"type": "click", "x": 100, "y": 100},
    {"type": "click", "x": 300, "y": 100},
    {"type": "click", "x": 300, "y": 300},
    {"type": "click", "x": 100, "y": 300},
])
```

The daemon validates the batch before touching the desktop and holds the input lock while it executes each click in order. If one fails, it reports the completed prefix and stops. Another request cannot move the pointer between a button press and release.

![One action request validates and serializes four ordered clicks](../assets/modal-optimized-action-batch.svg)

In a matched 30-sample A/B, one four-click request took 11.54 ms p50. Sending the same four clicks as sequential requests took 26.84 ms. Batching cut the recurring HTTP and request-admission work while preserving the sequence the model had already produced.

## Move subprocess I/O off the request loop

Commands had a different failure shape. The median looked acceptable, but p95 jumped to 249 ms. The child command already ran in its own process. The problem was that Uvicorn's event loop also managed the child's pipes, wait state, and cleanup, so a slow subprocess lifecycle could hold up unrelated HTTP work.

I moved that bookkeeping onto a private `SelectorEventLoop` running on a daemon thread. A bounded queue caps outstanding work, and a small bridge returns each result to the HTTP loop. Cancellation terminates the child's process group before releasing capacity. The shared-loop path measured 55.92 ms p50 and 248.67 ms p95. Isolation brought them to 9.86 ms and 10.63 ms.

![Subprocess I/O ownership moves from Uvicorn to a private event loop while the child process remains separate](../assets/modal-optimized-command-loop-isolation.svg)

The clipboard produced a stranger version of the same bug. Agents use the clipboard to paste a command into a GUI terminal, put a long block into an editor, or fill a field without synthesizing hundreds of key events. Under X11, the clipboard is a selection owned by a client process. When another application pastes, X11 asks that owner to provide the data. This is why `xclip` leaves a small process alive after a write.

My generic subprocess helper captured stdout and stderr. The long-lived selection owner inherited both pipes, so `communicate()` waited for EOF after the clipboard had changed. Clipboard writes never consumed those streams. Sending them to `DEVNULL` let the request finish while `xclip` stayed alive for the eventual paste. The optimized typing results below use direct XTest keystrokes and do not depend on this fix.

## Modal optimized was up to 650x faster than provider defaults

After these changes, every tuned warm primitive in the final run finished below 64 ms p50. The largest observed gap was 648x on a 1,000-character typing request. A full screenshot returned in 29 ms, and one click took 10 ms. At this point, the computer interface stopped dominating short agent steps.

| Warm operation | Modal optimized p50 / p95 | Modal default p50 / p95 / ratio | Daytona default p50 / p95 / ratio | E2B default p50 / p95 / ratio | Tzafon default p50 / p95 / ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, provider-native format | 28.69 / 43.96 ms | 229.94 / 235.78 ms / 8.02x | 264.85 / 297.67 ms / 9.23x | 212.75 / 225.28 ms / 7.42x | 146.14 / 168.34 ms / 5.09x |
| One click on the screen | 9.69 / 15.55 ms | 323.81 / 331.35 ms / 33.43x | 216.04 / 221.02 ms / 22.31x | 210.56 / 214.88 ms / 21.74x | 125.75 / 127.92 ms / 12.98x |
| Four ordered clicks | 13.64 / 21.98 ms | 342.68 / 349.16 ms / 25.12x | 865.55 / 1,061.66 ms / 63.45x | 855.81 / 876.02 ms / 62.74x | 455.96 / 531.37 ms / 33.42x |
| Type 100 characters | 15.27 / 20.69 ms | 379.28 / 389.18 ms / 24.84x | 638.50 / 706.86 ms / 41.81x | 4,090.74 / 4,194.92 ms / 267.86x | 82.27 / 85.45 ms / 5.39x |
| Type 1,000 characters | 63.34 / 83.91 ms | 378.40 / 413.17 ms / 5.97x | 5,356.25 / 5,425.81 ms / 84.57x | 41,048.75 / 41,619.86 ms / 648.12x | 181.03 / 212.66 ms / 2.86x |
| Non-login shell command | 9.54 / 17.03 ms | 182.43 / 346.47 ms / 19.12x | 116.92 / 120.86 ms / 12.26x | 52.18 / 54.35 ms / 5.47x | 29.60 / 30.84 ms / 3.10x |

Only the Modal optimized path was tuned. The other columns show provider defaults from the pinned harness. Tzafon returned 1280x720 JPEG screenshots; Modal, Daytona, and E2B returned 1024x768 PNG. Modal optimized typed with zero-delay XTest events, Modal default selected clipboard, and the other SDKs did not expose their internal pacing.

Request shape matters most in the four-click row. Modal optimized, Modal default, and Tzafon used one request. The measured Daytona path used four; E2B used four SDK calls over eight transport requests. The row describes those public defaults and shows why batching mattered in my implementation.

## Stop on the first changed frame

Suppose an agent clicks **Save** and immediately asks for another screenshot. The input endpoint can return successfully before the application paints its confirmation. The next frame may still show the old form, inviting the model to click again.

I built a Modal-only experimental endpoint around that gap. It captures a baseline before the action, waits for an XDamage notification or a polling interval, then hashes every pixel in a new full-resolution frame. XDamage only says that X11 repainted an area, so the hash verifies that the pixels actually differ. The endpoint returns the same frame that passed the check. Click to first changed frame took 65.39 ms p50 and 76.31 ms p95 across 30 samples.

![XDamage hints or polling trigger a pixel check, while application readiness remains caller-owned](../assets/modal-optimized-first-change.svg)

This experiment stops at a visual change. A blinking cursor, spinner, animation, unrelated repaint, or intermediate frame can win the race. Some meaningful effects leave the watched pixels unchanged. A caller that needs “save completed” still has to check for that state. The result was useful because it separated fast observation from the harder question of what evidence lets the agent proceed.

## Startup is now the slow path

With warm actions below 64 ms, the 7.8-second product create path is impossible to miss. In the same create-to-validated-screenshot benchmark, E2B's default desktop template reached a frame in 1.2 seconds p50 and a Tzafon nonpersistent desktop did it in 0.22 seconds p50.

Those calls package different work. The Modal timer begins before Sandbox creation and ends after the daemon is ready and the first PNG is transferred and validated. The E2B harness uses its default desktop template, and E2B documents that templates can snapshot a running process so it is already alive at creation. Tzafon's public call does not expose enough stage timing to explain its 220 ms from this evidence. The products restore different images and wait for different readiness conditions.

The next experiment is a startup trace. I want to separate scheduling, desktop and daemon readiness, credential issuance, first capture, and validation, then attack the stage that owns the 7.8 seconds. A pool of ready desktops could trade idle cost for immediate assignment. A better prepared image could reduce work without keeping capacity warm. The trace should decide which approach is worth paying for.

The bottleneck moved. A 10 ms click makes a 7.8-second create path look enormous, and a 29 ms screenshot makes application readiness visible. I removed the repeated setup I could find. The next gains will come from preparing the desktop earlier and defining exactly what the agent must observe before it acts again.

## Source notes

- Current warm measurements: [Modal optimized samples, 2026-07-28](../../benchmark-data/modal-optimized-provider-2026-07-28.json), [provider-default samples, 2026-07-28](../../benchmark-data/provider-compare-coordinate-command-2026-07-28.json), [changed-frame samples, 2026-07-28](../../benchmark-data/modal-observation-2026-07-28.json), [four-click batching A/B, 2026-07-29](../../benchmark-data/modal-action-batching-ab-2026-07-29.json), and [Connect versus attested-tunnel A/B, 2026-07-29](../../benchmark-data/modal-optimized-ingress-ab-2026-07-29.json). The 50-turn opener is arithmetic over separate warm p50s, not a full agent trajectory.
- Historical diagnostics: [provider benchmark results, 2026-07-26](../benchmark-results-2026-07-26-provider-results.md), [combined sanitized result](../../benchmark-data/provider-results-2026-07-26.json), [Connect caller-placement evidence](../../benchmark-data/modal-optimized-competitive-us-west-2-2026-07-24.json), [native X11 input benchmark](../archive/benchmarks/benchmark-results-2026-07-23-native-x11-input.md), and [command runner A/B context](../../benchmark-data/tzafon-coordinate-command-context-2026-07-24.json). The Modal-default screenshot diagnostic separately summarized 22.83 ms p50 for daemon capture and encoding, 126.86 ms end to end, and a 103.91 ms remainder. Those summaries used different samples, so I do not add the component medians or label the remainder as network time. The historical command benchmark asked for `sh -lc`; the current comparison gives every provider the same logical `sh -c` command and requires exit zero with exact stdout `"42\n"`.
- Implementation and contracts: [performance documentation](../performance.md), [benchmarking methodology](../benchmarking.md), [visual-change observation contract](../experimental-visual-change-observation.md), and [create-to-validated-screenshot method](../../research/modal-optimized-create-benchmark-method.md).
- Product surfaces: [E2B Computer use](https://e2b.dev/docs/use-cases/computer-use), [E2B running-process snapshots](https://e2b.dev/docs/template/start-ready-command), [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/), and [Daytona snapshots](https://www.daytona.io/docs/snapshots/).
- Modal mechanics and cost: [Functions and Apps](https://modal.com/docs/guide/apps), [region selection](https://modal.com/docs/guide/region-selection), [Sandbox Connect](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets), [encrypted tunnels](https://modal.com/docs/guide/tunnels), [input concurrency](https://modal.com/docs/guide/concurrent-inputs), [Function scaling](https://modal.com/docs/guide/scale), [Sandbox resources](https://modal.com/docs/guide/sandbox-resources), [current list pricing](https://modal.com/pricing), and the tracked [provider and cost research memo](../../research/modal-computer-use-provider-cost-comparison-2026-07-29.md).
- External mechanisms: [OpenAI's GPT-5.6 Sol preview](https://openai.com/index/previewing-gpt-5-6-sol/), [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use), [RustDesk self-hosting](https://rustdesk.com/docs/en/self-host/), [`xdotool`'s XTest/Xlib implementation](https://github.com/jordansissel/xdotool), [MSS shared-memory capture](https://python-mss.readthedocs.io/latest/release-history/v10.2.0.html), [X11 selection ownership](https://www.x.org/releases/X11R7.6/doc/libX11/specs/libX11/libX11.html), and Modal's [sandbox architecture account](https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds).
