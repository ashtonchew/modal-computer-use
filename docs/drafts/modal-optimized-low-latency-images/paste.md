# How I Got Computer-Use Clicks to under 10 ms on Modal

A model that uses a computer needs a computer to use. Not yours, so you rent one from your favorite local sandbox company. A Linux desktop runs in a sandbox and your computer-use agent (CUA) takes the wheel. It asks for a screenshot, looks at the screen, sends back some action like a click or to type.

E2B & Daytona provide their own computer-use SDKs, so I wanted to see how efficient they were. To type 1,000 characters, roughly 10 sentences, with no agent latency included, it took 41 seconds on E2B. And on Daytona, it took 5.5 seconds. A huge waste that adds up as computer-use is increasingly used in realistic longer-horizon tasks.

Across the four providers and five paths in this benchmark, the computer-use framework I built with Modal primitives was fastest on every measured task. For the same 1,000-character typing task, my optimized Modal setup took 0.05 seconds.

[1_typing-comparison.png  ::  Typing 1,000 characters takes 41 seconds on E2B and 5.5 seconds on Daytona, and 0.05 seconds on the optimized Modal setup]

Then I tried to map out the most common task: The screenshot and action loop primitive is what a CUA repeats, once a turn, until the task is complete. So, how long does a screenshot and a single click take? Daytona took 950 ms, then E2B took about 410 ms. Before I optimized anything, my simple Modal setup took about 330 ms, already faster than both. After optimizing, the same task took only 47 ms, ~7x faster than the simple Modal setup.

Let's see this in action: Over fifty total agent turns, counting no agent reasoning and generation time at all, Daytona's 950ms is 48 whole seconds spent on nothing but processing screenshots and clicks. E2B's 410ms is 21 seconds. And with the optimized Modal setup, 47ms turns to only ~2 seconds.

[OpenAI says GPT-5.6 Sol on Cerebras can generate up to 750 tokens per second](https://openai.com/index/previewing-gpt-5-6-sol/). At that speed, hundreds of milliseconds in the computer interface stop hiding behind slow generation. Longer trajectories make it worse, because the delay is paid on every turn before the user sees anything. Infra will be the next bottleneck in frontier computer-use agents.

I first thought of all the ways we use remote desktops today. For IT support, remote desktop control has to feel almost seamless. So, I searched for the most lightweight remote desktop control framework. Then I found RustDesk, an [open-source remote desktop system](https://rustdesk.com/docs/en/self-host/). I knew this was it, even the name RustDesk sounded fast. Looking inside, I saw that it connected to the remote machine once, when the session starts, and reuses that connection for everything after. For example, moving your mouse sends an event over a connection that is actively ready.

My first Modal setup did the opposite. Every action launched, used that launch once, and would throw it away. It provided a quick way to push actions and not have to worry about complexity. But in the need for speed, I identified the various odd ways this waste happens, over and over again in different parts of the system, and saved that time using Modal primitives and engineering. The result was an open-source SDK whose optimized path led the benchmark.

That split is now literal in the API. An async owner creates the desktop once with `AsyncComputerSandbox.create()`, waits until its daemon is ready, and produces a compact `session_handle()`. A Modal Function receives that handle and enters `borrow_async()` once around the whole trajectory. Screenshots and actions then reuse the same async daemon connection until the model is done.

[2_agent-loop.png  ::  An async owner provisions once, then a Modal Function borrows one connection for the repeated computer-use loop]

The owner and borrower solve different problems. The owner controls the expensive cloud resource. The borrower controls one run against that resource. Leaving `borrow_async()` releases the run's lease but keeps the desktop and its daemon state alive. Leaving the owner's create context terminates the Sandbox. That makes it possible to run several trajectories against one prepared desktop without hiding who pays for it or who is allowed to shut it down.

## Round trips

Each screenshot request left my laptop, crossed into Modal, reached the daemon inside the desktop Sandbox, and carried the PNG back over the same authenticated route. A full 1024x768 screenshot took 116 ms. This was on the simple Modal setup. Then the model picked a click, and the action request made the same full round trip again.



The desktop is a Linux machine in a Sandbox, and the daemon inside it owns the screen and the mouse. The client is the code that asks for a screenshot and sends back a click, and it can run anywhere that can reach the daemon.

The desktop already ran in a Modal Sandbox. What if the client ran on Modal too?

So to test this, I ran the same client code used to drive requests into the desktop in two places. First from my laptop. Then from a Modal Function, a primitive that gives an autoscaled container for application code. I requested `us-west-2` for the Function and for the Sandbox, which keeps both physically close together. From each, I measured a simple "move-and-click" task: The client sends an x and y coordinate and which mouse button to press, and the reply is the coordinate the pointer landed on. Barely anything travels in either direction, so nearly all of what it costs is the trip itself.

From my laptop the move-and-click task took 39.3 ms. From the Modal Function it took only 4.7 ms.

Looking one layer deeper, the daemon itself processing the click did about a millisecond of work in both cases. Precisely, the trip to the desktop and back took 38 ms from my laptop and 3.4 ms from the Function.

Then I ran the same comparison on a different task, a full screenshot. The client asks for a frame and a 1024x768 PNG comes back. It took 86.8 ms from my laptop and 38.1 ms from the Function. Looking into the function, why was it still 38.1 ms?

To look deeper I isolated the capture/encode/transport cycle. Breaking down the remaining 38.1 ms from the Function, the desktop spent 23 ms capturing and encoding the frame, and so it spent that 23 ms whether the client ran on my laptop or in the Function.

Zooming back out to the transport cycle, even colocated, a click carrying almost nothing still spent 3.4 ms on the trip from a Modal Function to the Modal Sandbox. Requesting `us-west-2` does not put the two machines as close as they could be. The Function and the Sandbox can still land in different buildings, and every request still goes through authenticated ingress. I use a Modal attested tunnel for this ingress. The authentication token is exchanged once at startup, but the routing happens on every request.

You may ask why don't I delete this round trip entirely by running the client inside the Sandbox, but then my code would live in the machine I am isolating. Here are the two main issues with placing the client in the Sandbox: (1) Our Sandbox is an untrusted machine, (2) The client is tied to the Sandbox. To contrast, because we use autoscaling Modal Functions to host the client then we can use a single client to control many Sandboxes.



[3_screenshot-paths.png  ::  Default and optimized Modal screenshot request paths]

## Why did every screenshot start a new process?

With the route shortened, I went looking inside the screenshot handler. Every frame launched a command-line capture program, wrote a temporary PNG to disk, reopened the file, and returned its bytes of an image. A computer-use agent needs a screenshot for almost every single request, so the fifty-turn loop from the opening spawns fifty of those processes and writes fifty temporary files. The screenshot handler of the past was certainly built without that fact in mind.

Every window on the desktop draws through X11, the display server that owns the pixels, and X11 was running the whole time. What restarted on every frame was everything on my side of it: a new process, a new connection to the display, a new buffer, a file on disk.

So I kept the capture client open instead. The daemon opens MSS, a small Python screen-capture library, on the first screenshot that can use it and holds that session open for as long as the daemon runs. On Linux MSS uses XShm, which lets the X server hand back a frame through shared memory rather than pushing it down the display socket. A screenshot became a read out of a buffer that already existed, encoded to PNG in memory.

However, two cases still take the old path. MSS cannot compose the X11 cursor visually, so a cursor-visible screenshot uses file capture. If the display connection breaks, the daemon reopens it once and falls back to file capture if that fails too.

Deleting a per-action `xdotool` process took a move plus one click from about 146 ms to 1.2 ms inside the daemon. Screenshots were paying that same kind of setup, and a temporary file on top of it.

## A process for every click

On my laptop a click feels like one event, it's just a single click after all. The actual system underneath sees three: pointer motion, button press, button release, each delivered to whichever application owns that spot on the screen. [macOS routes them through Quartz](https://developer.apple.com/documentation/coregraphics/cgevent); my Sandbox runs a Linux desktop under X11. Either way, an agent's `click(x, y)` has to become those separate events before an application can respond to it.

My first implementation handed that translation to [`xdotool`](https://github.com/jordansissel/xdotool), the standard command-line tool for X11 automation. It already knew how to talk to the display and synthesize input, and one line of shell per action was hard to argue with. Every API action launched a new `xdotool` process.

A pointer move plus one click took about 146 ms inside the daemon. Before replacing it I went to look at what `xdotool` does beneath the CLI, expecting to find a slow protocol. It uses [XTest](https://www.x.org/releases/X11R7.6/doc/xextproto/xtest.html), the X11 extension for synthetic input, through Xlib. So, the events were actually not the expensive part.

A click at `(x, y)` needs three XTest calls: move the pointer, press the button, release it. `xdotool` wraps those three calls in an entire program lifetime. Before X11 saw the pointer move, Linux had to create a child process, load the `xdotool` binary and its shared libraries, parse the coordinates and the button, and open a connection to the display. After the release, the process closed the connection and exited.

That trade is the right one for what `xdotool` was built for. Someone writes one line of shell to dismiss a dialog that keeps stealing focus, binds it to a key, and never thinks about it again. The script never has to manage a display connection, and a broken `xdotool` cannot take its caller down with it. The 146 ms lands between a keypress and a glance at the screen, where nobody has ever noticed it.

An agent has no glance. The model produces a click, the client sends it, and the next one arrives as soon as the model produces that. The setup is on the critical path every time, and a fifty-turn task pays it fifty times.

Interestingly, the fix was the same one as the screenshot handler but applied at a lower level. I kept XTest and stopped launching a program to reach it every time. The optimized daemon loads the X11 client libraries once and holds a single display connection open for its lifetime, so everything `xdotool` did per action now happens at startup instead. A click became three XTest calls from code that is already running, with nothing forked and no connection opened or closed. Each request takes the input lock, pushes the motion, press, and release, and synchronizes with the X server once at the end.

That last sync replaces something the old path did for free. A process cannot exit without closing its display connection, and closing it sends whatever Xlib still has buffered, so waiting for `xdotool` to exit was also waiting for the events to land. Nothing closes a connection held open for the daemon's lifetime, so without an explicit flush the daemon could report a click that never reached the screen. One call at the end of the sequence buys back the guarantee, and the connection stays open.

[4_input-session.png  ::  Per-action xdotool setup compared with one persistent X11 input connection]

Inside the daemon, the mean for a move plus one click went from about 146 ms to 1.2 ms, a 127x speedup.

Four move-and-click pairs went from 444 ms to 4.8 ms, a near 100x speedup. Typing gained less, for a more interesting reason. Every character costs at least a key-down and a key-up, plus modifiers when the layout needs them (on a US keyboard, `!` is Shift held over `1`), so the events start to matter on their own. A hundred characters fell from about 120 ms to 21 ms, and a thousand from 607 ms to 201 ms. Deleting one process per action does nothing about the two thousand events a thousand characters still have to emit.

One connection for the whole daemon means every request shares X11's keyboard and pointer state. Suppose two requests arrive together. One sends `Ctrl+L` to focus the browser's address bar; the other starts typing a URL. If a `w` lands before the first request releases Ctrl, the browser reads `Ctrl+W` and closes the tab. The daemon resolves each sequence against the active XKB layout and holds the input lock from the first press through the final release. A drag gets the same protection, so nothing can move the pointer between mouse-down and mouse-up.

Failure moved inside the daemon along with the connection. If XTest is missing when the daemon probes for it, input falls back to `xdotool` before any event is emitted. Once a press may already have reached the X server, replaying the request could double-click or type a character twice, so the daemon releases whatever it pressed, returns the error, and does not retry.

The persistent connection brought one more hazard with it. The daemon also uses Xlib to list and control windows, and an application window can close between the call that lists it and the call that reads its attributes. Xlib's default asynchronous error handler treats that ordinary race as fatal and exits the process, which would take the whole desktop API down with it. To hack around this, I install a nonfatal handler before opening the display and check each call's result, so a window that vanishes fails one request.

## Four clicks, four requests?

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

The daemon validates the entire batch of actions before it touches the desktop, then holds the input lock while it runs the clicks in order. If click three fails, the response reports clicks one and two, and click four never happens. The lock that protects a single drag protects the batch too, so no other request can slip a pointer move between a press and its release.

[5_action-batch.png  ::  One action request validates and serializes four ordered clicks]

To test this, I sent a batch of four clicks. Four sequential requests took 26.8 ms. But, one ordered request took only 11.5 ms. The difference was in the three more trips through the request stack.

This matters because [OpenAI's current computer-use API](https://developers.openai.com/api/docs/guides/tools-computer-use) already returns ordered action lists. Keeping that list intact avoids paying one transport round trip per click.

## Why did command p95 jump to 220 ms?

The daemon runs shell commands in the desktop too. An agent that has just downloaded a file can confirm it with `ls ~/Downloads` instead of opening a file manager and reading the answer off the screen. With screenshots and clicks down in the tens of milliseconds, that endpoint started to look wrong.

Its median was 55 ms and its p95 was 220 ms, so one request in twenty took four times as long as a typical one. Work that is simply expensive raises the median along with the tail. A median that stays low while the tail stretches out means most requests are fine and a few are stuck waiting behind something else.

The command already ran in its own OS process, so the child was not the problem. Ownership was. Uvicorn, the ASGI server running the daemon, held that child's stdin, its output pipes, its wait state, and its cleanup, all on the same event loop that scheduled every unrelated HTTP request.

I moved the lifecycle onto a private `SelectorEventLoop` on a daemon thread. A capacity limit bounds how many commands can be outstanding, a thread-safe handoff starts the child on the private loop, and Uvicorn is left awaiting a single future. The private loop owns the pipes, the wait, and the cleanup. Cancelling a request kills the process group and waits for a cleanup acknowledgement before the slot is released, so a cancelled command cannot leak a child into the next one.

Over 30 samples per arm, the private loop measured 7.6 ms p50 and 8.7 ms p95. A thread pool fixes the tail too, at 10.6 ms and 13.2 ms.

[6_command-loop-isolation.png  ::  Subprocess I/O ownership moves from Uvicorn to a private event loop while the child process remains separate]

## The disappearing clipboard

The same ownership question turned up somewhere stranger. Agents use the clipboard constantly, because it is how you paste a shell command into a GUI terminal or drop a block of text into an editor without synthesizing hundreds of key events. Mine had a copy request that sat open long after the clipboard was ready.

Under X11 there is no central buffer holding clipboard text. A client process owns the selection, and when another application pastes, X11 goes back to that process and asks it for the bytes. That is why `xclip` leaves a small process alive after a write: if the process exits, the clipboard is empty. In my daemon, that necessary background process was also holding the HTTP request open.

My generic subprocess helper assumed a child's output mattered. It created pipes for stdout and stderr, then called `communicate()`, which waits until every process holding the write ends has closed them. The long-lived `xclip` owner had inherited those handles and had no reason to close them. From the outside the failure looked absurd. The agent copies a shell command, the selection is ready, a paste from any application would work, and the copy request is still open.

`xclip` has nothing to say on stdout or stderr. I pointed both at `DEVNULL` instead of creating pipes. The request now returns as soon as `xclip` owns the selection, and the process stays alive for the paste that comes later.

## Up to 770x faster

In our optimized Modal setup: A full screenshot returned in 37 ms. One click took 10 ms.

The biggest ratio in the table belongs to typing. The 1,000-character case took 53 ms through my optimized Modal path, which sends every character over the persistent XTest connection, against about 41 seconds through E2B's computer-use SDK. 770x faster with our setup.

[7_warm-results.png  ::  Warm p50 latency across four providers and five paths, with Modal optimized fastest on every task]

Every provider ran through its public default computer-use path, and only Modal optimized was tuned. The Modal simple column is the public `ComputerSandbox` configuration in this library, with identical source between the two runs' revisions, and it already included MSS screenshot capture. The computer-use SDKs of Daytona, E2B, and Tzafon stayed on their defaults.

Each screenshot row uses that path's native format: Tzafon returned a 1280x720 JPEG, and Modal, Daytona, and E2B returned a 1024x768 PNG.

The four-click row is where batching shows up. Modal optimized, Modal simple, and Tzafon each accepted one four-click request. Daytona needed four requests, and four E2B SDK calls, which turned into eight transport requests. Adding three clicks cost the two Modal rows about 3 ms and 16 ms, respectively. It cost E2B about 650 ms and Daytona about 1,160 ms! The request path had become more expensive than the input it carried.

## What counts as the next frame?

A 10 ms click does not mean the application has drawn anything. When an agent clicks **Save** and immediately asks for a screenshot, the input request can succeed before the application repaints, and the frame that comes back still shows the unsaved form. The model reads that as failure and clicks **Save** again.

My first detector polled: wait an interval, capture a frame, compare it with the baseline, repeat. That works, and every miss costs a full capture.

XDamage is an X11 extension that reports when a region of the display has been repainted. I arm a watcher before the action and treat its notification as a cue to capture instead of a fixed schedule. Where XDamage is unavailable, the detector polls.

A repaint can redraw identical pixels, so the notification alone cannot prove the screen changed. After each wake-up the daemon captures the full-resolution RGB frame and hashes every pixel, then compares that digest with the baseline before any resizing or PNG encoding. A matching hash sends the detector back to sleep. A different hash gets encoded and returned as the next screenshot. XDamage decides when to look. The hash decides whether anything happened.

Click to first changed frame: 76 ms p50 and 88 ms p95, across 30 samples.

[8_first-change.png  ::  XDamage hints or polling trigger a pixel check, while application readiness remains caller-owned]

A first changed frame answers one narrow question: have new pixels appeared yet? It can replace a fixed sleep when the first visual response is all the agent needs. It cannot tell me that **Save** finished. A blinking cursor or an intermediate paint can satisfy the pixel check first, and a successful save can leave the watched region unchanged. Before a dependent action, the caller still needs an application-specific condition, such as the saved confirmation appearing.

This is an experimental feature that will matter even more as CUAs get faster over time. In tailored use cases, I can foresee the need for application-layer changed frame contracts as a naive optimization.

## Under a cent a minute

Modal bills a Function and a Sandbox for the seconds each one is alive. The two together cost under a cent a minute. Between runs, it costs nothing. The entire benchmark run cost only about 6 cents.

## Startup still takes 10 seconds

Creating a fresh Modal desktop and receiving its first validated screenshot right now takes 10.2 seconds.

Native async provisioning does not make that cold start shorter. It keeps the owner's event loop free while Modal allocates the Sandbox, starts the desktop, opens ingress, and waits for the daemon. If the owner is cancelled halfway through, the SDK finishes cleaning up anything Modal already allocated instead of leaving a paid resource behind.

The latency win begins after startup. Provision the desktop once, pass its session handle to a nearby Modal Function, and hold one `borrow_async()` context across the full screenshot, model, and action loop. The cold start is paid once. The repeated path keeps its connection, lease, and daemon state.

Further work needs a timestamp at lifecycle boundaries. If allocation dominates, a pool of ready desktops is worth testing. If desktop or daemon startup dominates, the work belongs on the image instead. A pool or a heavier image each buy startup time at a price, so I kept this SDK light enough that the choice belongs to whoever runs it.

## Computer-use SDKs for fun and profit

On Modal, a fifty-turn task that spent sixteen seconds waiting now spends under two and a half. Every one of those changes was the same change. Something that was being built once per action became something built once per session. The desktop is created once, the trajectory borrows one connection, and the daemon keeps its useful state between turns.

The compared provider-default APIs did not expose these seams to me: where the client runs, what a single request carries, how the daemon holds the display, and who owns a child process. Modal did. A Sandbox and a Function are separate things I could place in the same region, and cost efficiency came from intuitive knobs to control resources.

That is how I tuned Modal's general-purpose AI infra platform to lead this four-provider, five-path benchmark. The future is customization.

## Source notes

- Current warm measurements: [Modal optimized samples, 2026-07-30](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/modal-optimized-provider-2026-07-30.json), [provider-default samples, 2026-07-30](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/provider-compare-coordinate-command-2026-07-30.json), [changed-frame samples, 2026-07-30](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/modal-observation-2026-07-30.json), [caller-placement comparison, 2026-07-31](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/modal-caller-placement-us-west-2-2026-07-31.json), [four-click batching A/B, 2026-07-29](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/modal-action-batching-ab-2026-07-29.json), [Connect versus attested-tunnel A/B, 2026-07-29](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/modal-optimized-ingress-ab-2026-07-29.json), and [subprocess-runner A/B, 2026-07-30](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/modal-subprocess-runner-ab-2026-07-30.json). Every column in the warm table runs over the repository's standardized attested ingress. A controlled A/B between that path and Connect found no clear winner. The command section cites the 2026-07-30 subprocess-runner A/B, which ran 30 measured samples and one warmup per arm over the attested-tunnel path; its status is candidate because the run was not preregistered. Its shell-command figures are a different measurement from the table's shell-command row. That A/B requested 4 cores and 8 GiB for the desktop and for the runner, and the warm run requested 1 core and 2 GiB, so reading one against the other changes the machine shape as well as the subprocess backend. That row's Modal optimized cell comes from the 2026-07-30 optimized run and its other cells come from the 2026-07-30 provider-default run. The shared-loop arm's p95 rests on two of its thirty samples; without them that arm's mean falls from 74.7 ms to 54.8 ms. The 50-turn opener is arithmetic over separate warm p50s, not a full agent trajectory.
- Historical diagnostics: [provider benchmark results, 2026-07-26](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/docs/archive/benchmarks/benchmark-results-2026-07-26-provider-results.md), [combined sanitized result](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/provider-results-2026-07-26.json), [Connect caller-placement evidence](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/modal-optimized-competitive-us-west-2-2026-07-24.json), [native X11 input benchmark](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/docs/archive/benchmarks/benchmark-results-2026-07-23-native-x11-input.md), and [command runner A/B context](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/benchmark-data/tzafon-coordinate-command-context-2026-07-24.json). The Modal-default screenshot diagnostic separately summarized 22.83 ms p50 for daemon capture and encoding, 126.86 ms end to end, and a 103.91 ms remainder. Those summaries used different samples, so I do not add the component medians or label the remainder as network time. The 2026-07-24 caller-placement figures are superseded by the 2026-07-31 rerun. That run measured over Connect with the desktop at 4 cores and 8 GiB and the runner at 1 core and 1 GiB, so its ratios do not carry over to the current section. The ten-sample subprocess-runner A/B in that context artifact predates the process-group safety fix and is superseded by the 2026-07-30 run. The two are not interchangeable: the older arms used the Connect runner path, did not pin CPU or memory, and measured a slightly different shell payload. At ten samples per arm the thread pool and the private loop were within noise of each other, and the thirty-sample run is what separates them. The historical command benchmark asked for `sh -lc`; the current comparison gives every provider the same logical `sh -c` command and requires exit zero with exact stdout `"42\n"`.
- Implementation and contracts: [async lifecycle API](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/docs/api.md), [native async owner example](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/examples/async_modal_owner.py), [Function session-handoff example](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/examples/modal_function_session_handoff.py), [performance documentation](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/docs/performance.md), [benchmarking methodology](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/docs/benchmarking.md), [visual-change observation contract](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/docs/experimental-visual-change-observation.md), and [create-to-validated-screenshot method](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/research/modal-optimized-create-benchmark-method.md).
- Product surfaces: [E2B Computer use](https://e2b.dev/docs/use-cases/computer-use) and [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/).
- Modal mechanics and cost: [Functions and Apps](https://modal.com/docs/guide/apps), [region selection](https://modal.com/docs/guide/region-selection), [Sandbox Connect](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets), [encrypted tunnels](https://modal.com/docs/guide/tunnels), [input concurrency](https://modal.com/docs/guide/concurrent-inputs), [Function scaling](https://modal.com/docs/guide/scale), [Sandbox resources](https://modal.com/docs/guide/sandbox-resources), [current list pricing](https://modal.com/pricing), and the tracked [provider and cost research memo](https://github.com/ashtonchew/modal-computer-use/blob/draft/modal-computer-use-latency-article/research/modal-computer-use-provider-cost-comparison-2026-07-29.md). Costs use Modal's rates as of 2026-07-29, for the 1-core, 2 GiB Function and Sandbox the run requests. Region selection adds 1.5 to 1.75x, and I used the higher end (1.75x) so the figure is the most this run could have cost.
- External mechanisms: [OpenAI's GPT-5.6 Sol preview](https://openai.com/index/previewing-gpt-5-6-sol/), [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use), [Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use), [Browser Use's multi-action implementation](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py), [RustDesk self-hosting](https://rustdesk.com/docs/en/self-host/), [Quartz event services](https://developer.apple.com/documentation/coregraphics/cgevent), [`xdotool`'s XTest/Xlib implementation](https://github.com/jordansissel/xdotool), [the XTest protocol specification](https://www.x.org/releases/X11R7.6/doc/xextproto/xtest.html), [MSS shared-memory capture](https://python-mss.readthedocs.io/latest/release-history/v10.2.0.html), [X11 selection ownership](https://www.x.org/releases/X11R7.6/doc/libX11/specs/libX11/libX11.html), and Modal's [sandbox architecture account](https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds).
- Source revisions: the warm table's optimized column ran at `a497671` and its four default columns ran at `7cb39f7`. One commit separates those revisions, and it touched only documentation, examples, and tests, so the `src/` diff between them is empty.

The results image rounds this table, which keeps the p95s.

| Task | Modal optimized p50 / p95 | Daytona default p50 / p95 / ratio | E2B default p50 / p95 / ratio | Modal simple in this library p50 / p95 / ratio | Tzafon default p50 / p95 / ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, provider-native format | 37.25 / 48.76 ms | 563.57 / 603.79 ms / 15.13x | 198.78 / 223.20 ms / 5.34x | 115.80 / 132.91 ms / 3.11x | 154.25 / 192.53 ms / 4.14x |
| One click on the screen | 9.85 / 16.85 ms | 386.40 / 394.19 ms / 39.22x | 209.86 / 213.42 ms / 21.30x | 214.09 / 218.19 ms / 21.73x | 130.27 / 170.55 ms / 13.22x |
| Four ordered clicks | 12.52 / 22.07 ms | 1,546.74 / 1,577.44 ms / 123.50x | 860.68 / 897.95 ms / 68.72x | 230.10 / 235.09 ms / 18.37x | 458.03 / 499.49 ms / 36.57x |
| Type 100 characters | 15.76 / 28.15 ms | 805.55 / 812.84 ms / 51.11x | 4,083.30 / 4,156.65 ms / 259.08x | 259.67 / 270.18 ms / 16.48x | 85.16 / 101.65 ms / 5.40x |
| Type 1,000 characters | 53.35 / 79.69 ms | 5,528.38 / 5,554.88 ms / 103.63x | 40,914.66 / 41,374.28 ms / 766.95x | 263.95 / 269.71 ms / 4.95x | 185.03 / 188.37 ms / 3.47x |
| Non-login shell command | 11.69 / 14.12 ms | 285.33 / 294.57 ms / 24.40x | 55.90 / 69.27 ms / 4.78x | 72.64 / 158.22 ms / 6.21x | 31.73 / 33.35 ms / 2.71x |
