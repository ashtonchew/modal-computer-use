# How I got computer-use clicks to 10 ms on Modal

*The same warm path returns a full 1024x768 PNG in 29 ms.*

I built the fastest cloud computer-use API on Modal. A lot faster than E2B or Daytona.

Sandboxes and Functions, wiring them together, I first built the simplest computer-use implementation I could: a Linux desktop inside a Sandbox, a HTTP daemon beside it, and an SDK that asks for screenshots and sends clicks.

With a simple Modal computer-use API, returning a screenshot of the screen and clicking once took about or half a second (550ms).

E2B and Daytona ship computer-use APIs as a product primitive. To compare, I ran the same task of a screenshot and clicking a spot. E2B took ~420 ms and Daytona took about ~480 ms. Not bad.

But, in a fifty-turn loop with one screenshot and one click pays that half-a-second lag 50 times. In that scenario, it comes to about 27 seconds spent on nothing but a simple screenshot and click. After all the work below, the Optimized Modal computer-use API runs all fifty turns in less than 2 seconds.

[OpenAI says GPT-5.6 Sol on Cerebras can generate up to 750 tokens per second](https://openai.com/index/previewing-gpt-5-6-sol/). At that speed, hundreds of milliseconds in the computer interface stop hiding behind slow generation. Longer trajectories make it worse, because the delay is paid on every turn before the user sees anything. Infra will be the next issue in frontier computer-use agents.

Building on primitives instead of a product meant I could change things a computer-use API keeps on its own side of the boundary: where the caller runs, how it processes clicks, what a single request is allowed to contain, and how a Function hands work to a Sandbox. A custom framework built on Modal primitives.

I took the idea from RustDesk, an [open-source remote desktop system](https://rustdesk.com/docs/en/self-host/). It connects to the remote machine once, when the session starts, and reuses that connection for everything after. Moving your mouse sends an event over a connection that is already open. Contrast this with existing computer-use APIs that reconnect for each event, suitable only for short agentic tasks.

Branching off the idea of decoupling connection and usage. I mapped out the latency

![Creation is separate from the repeated computer-use loop](../assets/modal-optimized-agent-loop.svg)

## Every screenshot made a round trip to my laptop

Each screenshot request left my laptop, crossed into Modal, reached the warm daemon inside the desktop Sandbox, and carried the PNG back over the same authenticated route. A full 1024x768 screenshot took 230 ms. Then the model picked a click, and the action request made the same trip again.

The desktop already ran on Modal. What if the client did too?

I moved the same Python client off my laptop and into a Modal Function, an autoscaled container for application code, and requested `us-west-2` for both the Function and the target Sandbox. The target and the authenticated request path stayed fixed. The only thing that changed was where the caller sat.

One move-and-click fell from 32.4 ms to 4.6 ms.

A shared requested region is a scheduling policy, not a physical guarantee. The Function and the Sandbox can still land on different hosts or availability zones, and traffic still goes through authenticated ingress. What changed is that the recurring route no longer left Modal. In the final warm run, a full screenshot came back in 29 ms.

![Default and optimized Modal screenshot request paths](../assets/modal-optimized-screenshot-paths.svg)

## Why did every screenshot start a process?

With the route shortened, I went looking inside the screenshot handler. Every frame launched a command-line capture program, wrote a temporary PNG to disk, reopened the file, and returned its bytes of an image. A computer-use agent needs a screenshot (almost) every single request, so the fifty-turn loop from the opening spawns fifty of those processes and writes fifty temporary files. The screenshot handler of the past was certainly built without that fact in mind.

Every window on the desktop draws through X11, the display server that owns the pixels, and X11 was running the whole time. What restarted on every frame was everything on my side of it: a new process, a new connection to the display, a new buffer, a file on disk.

So I kept the capture client open instead. The daemon opens MSS, a small Python screen-capture library, on the first screenshot that can use it and holds that session open for as long as the daemon runs. On Linux MSS uses XShm, which lets the X server hand back a frame through shared memory rather than pushing it down the display socket. A screenshot became a read out of a buffer that already existed, encoded to PNG in memory.

Two cases still take the old path. MSS cannot compose the X11 cursor, so a cursor-visible screenshot uses file capture. If the display connection breaks, the daemon reopens it once and falls back to file capture if that fails too.

The comparison table further down cannot isolate this change, because by the time I ran it both Modal paths already used MSS. I never ran the capture path as its own A/B either, so I do not know what it saved. The process launch is a cost I did measure, one section down: deleting a per-action `xdotool` process took a move plus one click from about 146 ms to 1.2 ms inside the daemon. Screenshots were paying that same kind of setup, and a temporary file on top of it.

## A process for every click

On my laptop a click feels like one event. The window system underneath sees three: pointer motion, button press, button release, each delivered to whichever application owns that spot on the screen. [macOS routes them through Quartz](https://developer.apple.com/documentation/coregraphics/cgevent); my Sandbox runs a Linux desktop under X11. Either way, an agent's `click(x, y)` has to become those separate events before an application can respond to it.

My first implementation handed that translation to [`xdotool`](https://github.com/jordansissel/xdotool), the standard command-line tool for X11 automation. It already knew how to talk to the display and synthesize input, and one line of shell per action was hard to argue with. Every API action launched a new `xdotool` process.

A pointer move plus one click took about 146 ms inside the daemon. Before replacing it I went to look at what `xdotool` does beneath the CLI, expecting to find a slow protocol. It uses [XTest](https://www.x.org/releases/X11R7.6/doc/xextproto/xtest.html), the X11 extension for synthetic input, through Xlib. The events were not the expensive part.

A click at `(x, y)` needs three XTest calls: move the pointer, press the button, release it. `xdotool` wraps those three calls in an entire program lifetime. Before X11 saw the pointer move, Linux had to create a child process, load the `xdotool` binary and its shared libraries, parse the coordinates and the button, and open a connection to the display. After the release, the process closed the connection and exited.

That trade is the right one in a shell script. The script never has to manage a display connection, and a broken `xdotool` cannot take its caller down with it. It is also invisible to a person, because 146 ms disappears into the time spent deciding what to click next. An agent sends the click the moment the model produces it, so the setup lands on the critical path and is paid again on the next click.

I kept XTest and deleted the process. The daemon loads the X11 client libraries once, holds a single display connection open for its lifetime, and builds the motion, press, and release events in memory. Each request takes the input lock, pushes the whole sequence, and synchronizes with the X server once at the end.

![Per-action xdotool setup compared with one persistent X11 input connection](../assets/modal-optimized-input-session.svg)

Inside the daemon, the mean for a move plus one click went from about 146 ms to 1.2 ms, a 128x speedup.

Four move-and-click pairs went from 444 ms to 4.8 ms, a near 100x speedup. Typing gained less, for a more interesting reason. Every character costs at least a key-down and a key-up, plus modifiers when the layout needs them (on a US keyboard, `!` is Shift held over `1`), so the events start to matter on their own. A hundred characters fell from about 120 ms to 21 ms, and a thousand from 607 ms to 201 ms. Deleting one process per action does nothing about the two thousand events a thousand characters still have to emit.

One connection for the whole daemon means every request shares X11's keyboard and pointer state. Suppose two requests arrive together. One sends `Ctrl+L` to focus the browser's address bar; the other starts typing a URL. If a `w` lands before the first request releases Ctrl, the browser reads `Ctrl+W` and closes the tab. The daemon resolves each sequence against the active XKB layout and holds the input lock from the first press through the final release. A drag gets the same protection, so nothing can move the pointer between mouse-down and mouse-up.

Failure moved inside the daemon along with the connection. If XTest is missing when the daemon probes for it, input falls back to `xdotool` before any event is emitted. Once a press may already have reached the X server, replaying the request could double-click or type a character twice, so the daemon releases whatever it pressed, returns the error, and does not retry.

The persistent connection brought one more hazard with it. The daemon also uses Xlib to list and control windows, and an application window can close between the call that lists it and the call that reads its attributes. Xlib's default asynchronous error handler treats that ordinary race as fatal and exits the process, which would take the whole desktop API down with it. I install a nonfatal handler before opening the display and check each call's result, so a window that vanishes fails one request.

## Four clicks, one request

Making a local click cost about a millisecond exposed the next repeated cost: asking for it over HTTP.

Models already produce more than one action per turn. [OpenAI's computer tool](https://developers.openai.com/api/docs/guides/tools-computer-use) returns an ordered `actions[]` array. [Claude can return several `tool_use` blocks](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use) in one response and leaves the client to sequence operations that share state. [Browser Use](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py), an open-source agent framework, hands the model's whole action list to `multi_act`. Splitting those sequences into one HTTP request per click throws the batching away in the last hop before the desktop.

The SDK keeps the model's sequence intact:

```python
computer.actions.run([
    {"type": "click", "x": 100, "y": 100},
    {"type": "click", "x": 300, "y": 100},
    {"type": "click", "x": 300, "y": 300},
    {"type": "click", "x": 100, "y": 300},
])
```

The daemon validates the entire batch before it touches the desktop, then holds the input lock while it runs the clicks in order. If click three fails, the response reports clicks one and two, and click four never happens. The lock that protects a single drag protects the batch too, so no other request can slip a pointer move between a press and its release.

![One action request validates and serializes four ordered clicks](../assets/modal-optimized-action-batch.svg)

I ran the same four clicks both ways, 30 times each. One ordered request took 11.5 ms at p50. Four sequential requests took 26.8 ms. The clicks were identical. The difference was three more trips through the request stack.

## Why did command p95 jump to 220 ms?

The daemon runs shell commands in the desktop too. An agent that has just downloaded a file can confirm it with `ls ~/Downloads` instead of opening a file manager and reading the answer off the screen. With screenshots and clicks down in the tens of milliseconds, that endpoint started to look wrong.

Its median was 55 ms and its p95 was 220 ms, so one request in twenty took four times as long as a typical one. Work that is simply expensive raises the median along with the tail. A median that stays low while the tail stretches out means most requests are fine and a few are stuck waiting behind something else.

The command already ran in its own OS process, so the child was not the problem. Ownership was. Uvicorn, the ASGI server running the daemon, held that child's stdin, its output pipes, its wait state, and its cleanup, all on the same event loop that scheduled every unrelated HTTP request.

I moved the lifecycle onto a private `SelectorEventLoop` on a daemon thread. A capacity limit bounds how many commands can be outstanding, a thread-safe handoff starts the child on the private loop, and Uvicorn is left awaiting a single future. The private loop owns the pipes, the wait, and the cleanup. Cancelling a request kills the process group and waits for a cleanup acknowledgement before the slot is released, so a cancelled command cannot leak a child into the next one.

Over 30 samples per arm, the private loop measured 7.6 ms p50 and 8.7 ms p95. A thread pool fixes the tail too, at 10.6 ms and 13.2 ms, and stays about 3 ms behind at the median.

![Subprocess I/O ownership moves from Uvicorn to a private event loop while the child process remains separate](../assets/modal-optimized-command-loop-isolation.svg)

## The clipboard only exists while a process holds it

The same ownership question turned up somewhere stranger. Agents use the clipboard constantly, because it is how you paste a shell command into a GUI terminal or drop a block of text into an editor without synthesizing hundreds of key events. Mine had a copy request that sat open long after the clipboard was ready.

Under X11 there is no central buffer holding clipboard text. A client process owns the selection, and when another application pastes, X11 goes back to that process and asks it for the bytes. That is why `xclip` leaves a small process alive after a write: if the process exits, the clipboard is empty. In my daemon, that necessary background process was also holding the HTTP request open.

My generic subprocess helper assumed a child's output mattered. It created pipes for stdout and stderr, then called `communicate()`, which waits until every process holding the write ends has closed them. The long-lived `xclip` owner had inherited those handles and had no reason to close them. From the outside the failure looked absurd. The agent copies a shell command, the selection is ready, a paste from any application would work, and the copy request is still open.

`xclip` has nothing to say on stdout or stderr. I pointed both at `DEVNULL` instead of creating pipes. The request now returns as soon as `xclip` owns the selection, and the process stays alive for the paste that comes later.

## The warm results, and where 650x comes from

A full screenshot returned in 29 ms. One click took 10 ms.

The biggest ratio in the table belongs to typing. The 1,000-character case took 63 ms through my optimized Modal path, which sends every character over the persistent XTest connection, against about 41 seconds through E2B's computer-use SDK. That is where 650x comes from, and it describes the two tested default paths, not every configuration either product can be run in.

| Warm operation | Modal optimized p50 / p95 | Modal default in this library p50 / p95 / ratio | Daytona default p50 / p95 / ratio | E2B default p50 / p95 / ratio | Tzafon default p50 / p95 / ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, provider-native format | 28.69 / 43.96 ms | 229.94 / 235.78 ms / 8.02x | 264.85 / 297.67 ms / 9.23x | 212.75 / 225.28 ms / 7.42x | 146.14 / 168.34 ms / 5.09x |
| One click on the screen | 9.69 / 15.55 ms | 323.81 / 331.35 ms / 33.43x | 216.04 / 221.02 ms / 22.31x | 210.56 / 214.88 ms / 21.74x | 125.75 / 127.92 ms / 12.98x |
| Four ordered clicks | 13.64 / 21.98 ms | 342.68 / 349.16 ms / 25.12x | 865.55 / 1,061.66 ms / 63.45x | 855.81 / 876.02 ms / 62.74x | 455.96 / 531.37 ms / 33.42x |
| Type 100 characters | 15.27 / 20.69 ms | 379.28 / 389.18 ms / 24.84x | 638.50 / 706.86 ms / 41.81x | 4,090.74 / 4,194.92 ms / 267.86x | 82.27 / 85.45 ms / 5.39x |
| Type 1,000 characters | 63.34 / 83.91 ms | 378.40 / 413.17 ms / 5.97x | 5,356.25 / 5,425.81 ms / 84.57x | 41,048.75 / 41,619.86 ms / 648.12x | 181.03 / 212.66 ms / 2.86x |
| Non-login shell command | 9.54 / 17.03 ms | 182.43 / 346.47 ms / 19.12x | 116.92 / 120.86 ms / 12.26x | 52.18 / 54.35 ms / 5.47x | 29.60 / 30.84 ms / 3.10x |

Every provider ran through its public default computer-use path, and only Modal optimized was tuned. The Modal default column is the public `ComputerSandbox` configuration in this library at the same revision, and it already included MSS screenshot capture, so its screenshot row reflects caller placement and configuration rather than the capture change. Daytona, E2B, and Tzafon stayed on their defaults. Each screenshot row uses that path's native format: Tzafon returned a 1280x720 JPEG, and Modal, Daytona, and E2B returned a 1024x768 PNG.

The four-click row is where batching shows up. Modal optimized, Modal default, and Tzafon each accepted one four-click request. Daytona needed four requests, and four E2B SDK calls turned into eight transport requests. Adding three clicks cost the two Modal rows about 4 ms and 19 ms. It cost Daytona and E2B about 650 ms each. The request path had become more expensive than the input it carried.

## What counts as the next frame?

A 10 ms click does not mean the application has drawn anything. When an agent clicks **Save** and immediately asks for a screenshot, the input request can succeed before the application repaints, and the frame that comes back still shows the unsaved form. The model reads that as failure and clicks **Save** again.

My first detector polled: wait an interval, capture a frame, compare it with the baseline, repeat. That works, and every miss costs a full capture.

XDamage is an X11 extension that reports when a region of the display has been repainted. I arm a watcher before the action and treat its notification as a cue to capture instead of a fixed schedule. Where XDamage is unavailable, the detector polls.

A repaint can redraw identical pixels, so the notification alone cannot prove the screen changed. After each wake-up the daemon captures the full-resolution RGB frame and hashes every pixel, then compares that digest with the baseline before any resizing or PNG encoding. A matching hash sends the detector back to sleep. A different hash gets encoded and returned as the next screenshot. XDamage decides when to look. The hash decides whether anything happened.

Click to first changed frame: 65 ms p50 and 76 ms p95, across 30 samples.

![XDamage hints or polling trigger a pixel check, while application readiness remains caller-owned](../assets/modal-optimized-first-change.svg)

A first changed frame answers one narrow question: have new pixels appeared yet? It can replace a fixed sleep when the first visual response is all the agent needs. It cannot tell me that **Save** finished. A blinking cursor or an intermediate paint can satisfy the pixel check first, and a successful save can leave the watched region unchanged. Before a dependent action, the caller still needs an application-specific condition, such as the saved confirmation appearing.

## Three cents a minute

Modal bills a Function and a Sandbox for the seconds each one is alive. A run costs about 3 cents a minute for the two together, and I pay nothing between runs.

I run the benchmark from a Function that serves one invocation and then exits. Each target desktop lives only as long as the samples that use it. The 2026-07-28 run made 31 of them, and the cleanup sweep at the end found nothing still running.

## Startup still takes 7.8 seconds

Creating a fresh Modal desktop and receiving its first validated screenshot still takes 7.8 seconds. That timer covers Sandbox allocation, desktop startup, daemon readiness, and the first frame, and I have not split it by stage. Another provider's template startup time does not tell me which of those four to attack.

The next run needs a timestamp at each boundary. If allocation dominates, a pool of ready desktops is worth testing. If desktop or daemon startup dominates, the work belongs in the image instead. Choosing before that trace would be guessing.

Startup is a measurement problem: I know it costs 7.8 seconds and I do not know where the time goes. Readiness is the harder one. Once a click costs a millisecond, the slow and uncertain part of a turn is deciding when the screen is worth looking at again, and the first changed frame is the closest thing I have to an answer.

## Source notes

- Current warm measurements: [Modal optimized samples, 2026-07-28](../../benchmark-data/modal-optimized-provider-2026-07-28.json), [provider-default samples, 2026-07-28](../../benchmark-data/provider-compare-coordinate-command-2026-07-28.json), [changed-frame samples, 2026-07-28](../../benchmark-data/modal-observation-2026-07-28.json), [four-click batching A/B, 2026-07-29](../../benchmark-data/modal-action-batching-ab-2026-07-29.json), [Connect versus attested-tunnel A/B, 2026-07-29](../../benchmark-data/modal-optimized-ingress-ab-2026-07-29.json), and [subprocess-runner A/B, 2026-07-30](../../benchmark-data/modal-subprocess-runner-ab-2026-07-30.json). The warm table predates ingress standardization and uses Connect; the subsequent controlled A/B found no clear ingress winner, and the current optimized path uses the repository's standardized attested ingress. The command section cites the 2026-07-30 subprocess-runner A/B, which ran 30 measured samples and one warmup per arm over the attested-tunnel path; its status is candidate because the run was not preregistered. Its shell-command figures are a different measurement from the table's shell-command row, which comes from the 2026-07-28 provider run. The shared-loop arm's p95 rests on two of its thirty samples; without them that arm's mean falls from 74.7 ms to 54.8 ms. The 50-turn opener is arithmetic over separate warm p50s, not a full agent trajectory.
- Historical diagnostics: [provider benchmark results, 2026-07-26](../benchmark-results-2026-07-26-provider-results.md), [combined sanitized result](../../benchmark-data/provider-results-2026-07-26.json), [Connect caller-placement evidence](../../benchmark-data/modal-optimized-competitive-us-west-2-2026-07-24.json), [native X11 input benchmark](../archive/benchmarks/benchmark-results-2026-07-23-native-x11-input.md), and [command runner A/B context](../../benchmark-data/tzafon-coordinate-command-context-2026-07-24.json). The Modal-default screenshot diagnostic separately summarized 22.83 ms p50 for daemon capture and encoding, 126.86 ms end to end, and a 103.91 ms remainder. Those summaries used different samples, so I do not add the component medians or label the remainder as network time. The ten-sample subprocess-runner A/B in that context artifact predates the process-group safety fix and is superseded by the 2026-07-30 run. The two are not interchangeable: the older arms used the Connect runner path, did not pin CPU or memory, and measured a slightly different shell payload. At ten samples per arm the thread pool and the private loop were within noise of each other, and the thirty-sample run is what separates them. The historical command benchmark asked for `sh -lc`; the current comparison gives every provider the same logical `sh -c` command and requires exit zero with exact stdout `"42\n"`.
- Implementation and contracts: [performance documentation](../performance.md), [benchmarking methodology](../benchmarking.md), [visual-change observation contract](../experimental-visual-change-observation.md), and [create-to-validated-screenshot method](../../research/modal-optimized-create-benchmark-method.md).
- Product surfaces: [E2B Computer use](https://e2b.dev/docs/use-cases/computer-use) and [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/).
- Modal mechanics and cost: [Functions and Apps](https://modal.com/docs/guide/apps), [region selection](https://modal.com/docs/guide/region-selection), [Sandbox Connect](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets), [encrypted tunnels](https://modal.com/docs/guide/tunnels), [input concurrency](https://modal.com/docs/guide/concurrent-inputs), [Function scaling](https://modal.com/docs/guide/scale), [Sandbox resources](https://modal.com/docs/guide/sandbox-resources), [current list pricing](https://modal.com/pricing), and the tracked [provider and cost research memo](../../research/modal-computer-use-provider-cost-comparison-2026-07-29.md). Cost figures come from Modal's rate card accessed 2026-07-29, for the 4-core, 8 GiB Function and Sandbox the run requests, and include the 1.75x narrow-region multiplier that `us-west-2` carries. They cover requested CPU and memory.
- External mechanisms: [OpenAI's GPT-5.6 Sol preview](https://openai.com/index/previewing-gpt-5-6-sol/), [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use), [Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use), [Browser Use's multi-action implementation](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py), [RustDesk self-hosting](https://rustdesk.com/docs/en/self-host/), [Quartz event services](https://developer.apple.com/documentation/coregraphics/cgevent), [`xdotool`'s XTest/Xlib implementation](https://github.com/jordansissel/xdotool), [the XTest protocol specification](https://www.x.org/releases/X11R7.6/doc/xextproto/xtest.html), [MSS shared-memory capture](https://python-mss.readthedocs.io/latest/release-history/v10.2.0.html), [X11 selection ownership](https://www.x.org/releases/X11R7.6/doc/libX11/specs/libX11/libX11.html), and Modal's [sandbox architecture account](https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds).
