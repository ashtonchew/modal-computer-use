# Provider Screenshot Payload Debug, 2026-05-19

This run was focused on screenshot payload accounting for Daytona and E2B after adding provider
payload metadata instrumentation. It used the provider benchmark worktree on
`research/external-provider-benchmarks` and loaded provider credentials from the existing untracked
worktree env file:

```sh
uv run computer-use benchmark compare \
  --providers daytona,e2b \
  --env-file /Users/ashtonchew/projects/modal-computer-use/.worktrees/provider-benchamark/.env \
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

The benchmark now includes `browser_page_screenshot` as the canonical fair visual workload. It
writes the same embedded HTML fixture into `/tmp`, launches Chromium/Chrome/Firefox with a clean
temporary profile, requests a `1024x768` app window, optionally positions it at `(0, 0)` when
`xdotool` is available, waits for the browser process to stay alive long enough to render, then
captures a full-screen PNG with `show_cursor=false`.

The original `screenshot_full` case remains provider-default idle. Use it to understand provider
startup state, not to claim screenshot-path superiority.

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
