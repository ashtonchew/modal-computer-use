# Provider Screenshot Payload Debug, 2026-05-19

> **Archive category:** Diagnostic
> **Date or revision:** 2026-05-19
> **Question:** Why did provider screenshot payload sizes differ in the early comparison?
> **Disposition:** This investigation explains base64 accounting and visual-workload differences.
> It does not define a current provider result; the
> [2026-07-26 provider report](benchmark-results-2026-07-26-provider-results.md) retains the later
> eligible evidence set.

> **Historical visual diagnostic.** This report preserves the browser-page and synthetic-canvas
> investigations under their original definitions. Its use of "canonical" is local to those visual
> workloads, not a claim that it is the current provider-default reference. The later
> [2026-07-18 run](benchmark-results-2026-07-18.md) was rejected during review and is not a current
> provider reference.

This run was focused on screenshot payload accounting for Daytona and E2B after adding provider
payload metadata instrumentation. It used the provider benchmark worktree on
`research/external-provider-benchmarks` and loaded provider credentials from the existing untracked
worktree env file:

```sh
uv run computer-use benchmark compare \
  --providers daytona,e2b \
  --env-file /path/to/untracked/.env \
  --iterations 10 \
  --output benchmark-results/provider-screenshot-debug-daytona-e2b-10x-20260519.json
```

The raw benchmark output is in `benchmark-results/`, which is ignored. The table below records the
safe, non-secret screenshot metadata needed to compare payload sizes.

## Screenshot Payloads

Both provider screenshot cases completed 10/10 iterations. The overall provider run was marked
failed because Daytona had one transient cold-create `502 Bad Gateway`, and E2B used its default
300-second timeout and expired during the long text-entry case. Those failures do not affect the
screenshot payload rows below.

| Provider | Source | Format | Dimensions | Mean latency | Mean transport bytes | Mean decoded bytes | Last decoded bytes |
|---|---|---|---:|---:|---:|---:|---:|
| Daytona | `ScreenshotResponse.screenshot.base64_string` | PNG | 1024x768 | 609.2 ms | 154612 | 115958 | 115958 |
| E2B | `raw_bytes` | PNG | 1024x768 | 177.6 ms | 14350.6 | 14350.6 | 10521 |

Modal attested tunnel, measured separately on `main`, returned decoded PNG bytes for the same
`1024x768 @ 96 DPI` desktop size:

| Provider | Source | Format | Dimensions | Decoded bytes |
|---|---|---|---:|---:|
| Modal attested tunnel | daemon screenshot response | PNG | 1024x768 | 258319 |

## Interpretation

The original Daytona screenshot byte value was mostly an accounting artifact: the SDK returned a
base64 string, and the old benchmark measured the base64 transport string length. The decoded PNG is
`115958` bytes, not `154612` bytes.

E2B already returns raw PNG bytes, so its transport and decoded byte counts match. Its screenshots
are much smaller than Modal's because the image content and compression output are lower entropy,
not because of base64 accounting.

Modal and E2B are now both being compared as decoded PNG bytes. Daytona must be compared using
`payload.decoded_size_bytes`, not the base64 `transport_size_bytes`.

## Fair Visual Workload Plan

Provider-default idle screenshots are useful diagnostics, but they are not a fair cross-provider
screenshot comparison:

- E2B's observed idle screenshot is an `Xvfb -retro` two-color root screen.
- Daytona's observed idle screenshot is XFCE with a lower-entropy desktop-base wallpaper.
- Modal's observed XFCE idle screenshot uses a higher-entropy Debian desktop-base wallpaper.

The canonical visual workload is now `synthetic_canvas_screenshot`. It writes the same embedded
HTML fixture into `/tmp`, launches Chromium/Chrome/Firefox with a clean temporary profile, requests a
`1024x768` app window, and renders a deterministic full-viewport `<canvas>`. The fixture avoids web
fonts, text rendering, network resources, and provider-default wallpaper, so the PNG entropy and
screenshot latency are driven by the same synthetic raster content.

`browser_page_screenshot` remains a secondary realistic workload. It is useful because browser text,
CSS layout, and the window manager are closer to many real computer-use tasks, but those same
features make it a less controlled cross-provider comparison.

The original `screenshot_full` case remains provider-default idle. Use it to understand provider
startup state, not to claim screenshot-path superiority.

Setup is excluded from warm visual timings. The setup phase writes the fixture, launches the browser,
and waits for the window to render once per provider sandbox. Measured iterations then time only the
provider's public screenshot or action-plus-screenshot path.

Fairness tiers:

| Tier | Cases | Purpose | Setup Included? |
|---|---|---|---|
| Canonical visual | `synthetic_canvas_screenshot`, `synthetic_canvas_sequence` | Same 1024x768 deterministic raster; compare warm screenshot and click-observation paths | No |
| Secondary visual | `browser_page_screenshot`, `browser_page_sequence` | More realistic browser-rendered UI; compare sensitivity to browser/window-manager differences | No |
| Provider diagnostic | `screenshot_full` | Show provider out-of-box desktop state and PNG entropy | No warm setup |
| Lifecycle | `cold_create_to_ready` | Measure sandbox create/start/readiness cost | Yes, by definition |
| Primitive actuation | `move_click`, `move_click_sequence`, `type_*`, `command_echo` | Provider public API behavior without the visual fixture | No cold create |

For visual sequence cases, each provider uses its documented coordinate-click surface where one is
available:

- Modal daemon: `/v1/actions/run` with one `click` action at the target coordinate.
- Daytona: `computer_use.mouse.click(x, y)`.
- E2B: `left_click(x, y)`.

This intentionally measures the provider's public coordinate-action path. It does not substitute a
non-coordinate click just because it is faster, because that would stop testing the same semantic
operation.

## Focused Browser-Page 10x Result

After fixing the setup command so `xdotool` is optional, the focused live run measured only the new
`browser_page_screenshot` case for Daytona and E2B:

`benchmark-results/provider-browser-page-focused-daytona-e2b-10x-20260519.json`

| Provider | Status | Mean latency | Decoded PNG bytes | Transport bytes | Unique colors | Entropy |
|---|---|---:|---:|---:|---:|---:|
| Daytona | ok | 248.3 ms | 81144 | 108192 | 2401 | 4.676 |
| E2B | ok | 227.7 ms | 150247.2 | 150247.2 | 2382 | 4.725 |

This is the apples-to-apples screenshot result. It renders the same local browser page before the
capture, so it is not dominated by idle wallpaper differences. The providers are close on latency;
payload size still differs because the browser/window-manager raster output and PNG encoder path are
not byte-identical even for the same HTML fixture.

## Visual-Only Browser-Page 10x Result

After changing the SDK and daemon defaults to `1024x768 @ 96 DPI` with
`post_action_delay_ms=0`, the visual-only benchmark measured the canonical browser-page screenshot
and a two-action `move + click + screenshot` sequence.

Artifacts:

- `benchmark-results/provider-browser-page-visual-only-modal-10x-1024x768-20260520.json`
- `benchmark-results/provider-browser-page-visual-only-modal-daytona-e2b-10x-20260520-rerun.json`

| Provider | Status | Screenshot mean | Sequence mean | Decoded screenshot bytes | Decoded sequence bytes | Observed screenshot size |
|---|---|---:|---:|---:|---:|---|
| Modal daemon | ok | 911.6 ms | 1811.7 ms | 90606 | 90619 | 1024x768 |
| Daytona | ok | 498.1 ms | 1056.1 ms | 80863 | 80863 | 1024x768 |
| E2B | ok | 218.3 ms | 15629.3 ms | 149128 | 149221.2 | 1024x768 |

Modal now uses the daemon app-launch route for this setup instead of launching a long-running
browser through `/v1/commands/run`; the command route is only used to write the deterministic HTML
fixture. That avoids treating a GUI process lifetime as a command lifetime.

These browser-page results are superseded for canonical comparison by the synthetic-canvas workload.
They remain useful as the secondary realistic browser workload.

## E2B Coordinate-Click Probe, 2026-05-21

Artifact:

- `benchmark-results/e2b-click-path-probe-10x-20260521.json`

The probe created an E2B desktop sandbox with `resolution=(1024, 768)`, `dpi=96`, `display=":0"`,
and a 300-second sandbox timeout. Before installing the fixture, the process probe showed `Xvfb`,
`xfce4-session`, and `dbus-launch`; it did not show a browser process. That is evidence that this
benchmark setup is not inheriting a browser-prewarmed E2B sandbox.

| E2B path | Status | Mean | p50 | p95 | Notes |
|---|---|---:|---:|---:|---|
| `screenshot()` | ok | 218.5 ms | 211.7 ms | 237.6 ms | Screenshot path itself is not the 15s bottleneck |
| `left_click()` | ok | 159.9 ms | 159.4 ms | 163.3 ms | Fast but only clicks the current cursor position |
| `move_mouse(x, y)` | ok | 13743.1 ms | 15231.4 ms | 15385.5 ms | First sample was 59.4 ms; later samples were ~15.2s |
| `left_click(x, y)` | failed after 9 samples | 15393.7 ms | 15390.5 ms | 15429.8 ms | Documented coordinate-click path; final sample hit a stream reset |

Interpretation: the E2B anomaly is on coordinate-targeted mouse actions, not on screenshots and not
on the visual fixture. A no-argument `left_click()` is much faster, but it is not semantically
equivalent unless the cursor is already at the target. The benchmark therefore keeps
`left_click(x, y)` for visual sequence cases and labels the result as E2B's public coordinate-click
behavior.

## Focused Visual 10x Result, 2026-05-21

Artifact:

- `benchmark-results/provider-focused-visual-modal-daytona-e2b-10x-20260521.json`

This focused run excludes typing, command echo, and legacy move/click cases. It measures only:

- provider idle screenshot diagnostic;
- canonical synthetic canvas screenshot and click-observation sequence;
- secondary browser-page screenshot and click-observation sequence.

The visual fixture setup is excluded from warm timings. Modal uses the current connect-token path
with browser prewarm disabled. Daytona and E2B use their documented provider SDK computer-use paths.

| Provider | Case | Mean | p50 | p95 | Mean decoded PNG bytes | Action path |
|---|---|---:|---:|---:|---:|---|
| Daytona | `synthetic_canvas_screenshot` | 206.8 ms | 171.5 ms | 329.6 ms | 27372 |  |
| E2B | `synthetic_canvas_screenshot` | 210.3 ms | 208.0 ms | 216.4 ms | 38880 |  |
| Modal daemon | `synthetic_canvas_screenshot` | 666.9 ms | 616.5 ms | 906.5 ms | 27573 |  |
| Daytona | `synthetic_canvas_sequence` | 439.0 ms | 437.1 ms | 548.4 ms | 27372 | `computer_use.mouse.click(x, y)` |
| E2B | `synthetic_canvas_sequence` | 15613.2 ms | 15592.1 ms | 15672.7 ms | 38887 | `left_click(x, y)` |
| Modal daemon | `synthetic_canvas_sequence` | 1261.1 ms | 1239.6 ms | 1347.5 ms | 27607 | daemon click action |
| Daytona | `browser_page_screenshot` | 286.2 ms | 266.6 ms | 437.4 ms | 80240 |  |
| E2B | `browser_page_screenshot` | 227.2 ms | 224.3 ms | 243.0 ms | 148294 |  |
| Modal daemon | `browser_page_screenshot` | 713.5 ms | 707.6 ms | 752.9 ms | 90817 |  |
| Daytona | `browser_page_sequence` | 512.9 ms | 536.2 ms | 628.2 ms | 80308 | `computer_use.mouse.click(x, y)` |
| E2B | `browser_page_sequence` | 15665.2 ms | 15651.7 ms | 15745.4 ms | 148338 | `left_click(x, y)` |
| Modal daemon | `browser_page_sequence` | 1441.5 ms | 1437.7 ms | 1529.6 ms | 90758 | daemon click action |
| Daytona | `screenshot_full` | 254.6 ms | 223.1 ms | 351.5 ms | 115730 | diagnostic only |
| E2B | `screenshot_full` | 203.0 ms | 197.4 ms | 226.3 ms | 13872 | diagnostic only |
| Modal daemon | `screenshot_full` | 1039.6 ms | 1018.4 ms | 1115.0 ms | 257328 | diagnostic only |

Interpretation:

- The canonical screenshot case is now fairer than idle screenshots and less browser-dependent than
  the browser-page case.
- Daytona and E2B are close on canonical screenshot latency. Modal's connect-token path remains
  materially slower for screenshots.
- Modal's sequence is much faster than E2B's documented coordinate-click sequence but slower than
  Daytona's.
- E2B's sequence result is not a screenshot issue; it reproduces the coordinate-click probe.

## Modal Connect Token vs Encrypted Tunnel

Modal docs recommend Sandbox Connect Tokens for HTTP/WebSocket requests to sandboxes. Modal also
supports `encrypted_ports` tunnels for raw public TCP/TLS access; the tunnel docs describe them as
cryptographically random public URLs over Modal's relay network and say the application must handle
its own authentication.

That means the benchmark modes are not interchangeable:

| Modal path | Authentication model | Benchmark role |
|---|---|---|
| Connect token | Modal-issued token and connect proxy headers | Current SDK-safe default; comparable to provider SDK proxy calls |
| Encrypted tunnel | Public TLS tunnel; app must authenticate requests | Candidate low-latency SDK transport, but needs an explicit daemon auth mode before it is safe as a default |
| Local daemon | Loopback token or local-only access | Developer diagnostic; not comparable to remote providers |

The next fair Modal tunnel benchmark should add a purpose-built tunnel auth mode before exposing the
daemon over `encrypted_ports`. It should not bypass auth just to get lower latency numbers.
