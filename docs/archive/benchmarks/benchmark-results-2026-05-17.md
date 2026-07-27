# Provider Benchmark Results, 2026-05-17

> **Archive category:** Diagnostic  
> **Date or revision:** 2026-05-17  
> **Question:** How did the early Modal, Daytona, and E2B paths behave under the original
> 10-iteration comparison?  
> **Disposition:** The harness and measurement contracts predate the current provider report.
> Preserve this report for ingress and payload-debugging context; use the
> [2026-07-26 provider report](../../benchmark-results-2026-07-26-provider-results.md) for current
> evidence.

> **Historical diagnostic.** The later
> [2026-07-18 run](benchmark-results-2026-07-18.md) was rejected during review. Keep this report only for its original
> ingress, payload-debugging, and 10-iteration context; do not combine its rows with current results.

These results captured the then-current provider comparison after normalizing the Modal daemon benchmark to
`1024x768 @ 96 DPI` with `post_action_delay_ms=0`.

## Artifacts

- Modal daemon, 10x:
  `/Users/ashtonchew/projects/modal-computer-use/benchmark-sdk-modal-connect-1024x768-2026-05-17.json`
- Daytona and first E2B attempt, 10x:
  `benchmark-results/provider-compare-daytona-e2b-10x-20260517.json`
- E2B rerun, 10x, with longer sandbox lifetime:
  `benchmark-results/provider-compare-e2b-10x-timeout900-20260517.json`
- Daytona display probe:
  `benchmark-results/daytona-display-probe-20260517.json`
- Daytona display DPI probe:
  `benchmark-results/daytona-display-dpi-probe-20260517.json`

The first combined Daytona/E2B run is authoritative for Daytona. It is not authoritative for E2B
because the E2B sandbox used the documented 300 second lifetime and expired during the 10x
`type_1000_chars` case. The E2B-only rerun used the same desktop defaults with
`E2B_TIMEOUT_SECONDS=900`, and records that timeout in provider metadata.

## Display and DPI

Daytona docs expose `sandbox.computer_use.display.get_info()` for display introspection and document
width, height, origin, primary display, and total display count. The VNC docs also document that
Computer Use/VNC starts an X11 desktop stack with Xvfb, xfce4, x11vnc, and noVNC, and that the
default image includes the packages needed for VNC and Computer Use:

- https://www.daytona.io/docs/en/computer-use/#get-info
- https://www.daytona.io/docs/en/python-sdk/sync/computer-use/#displayget_info
- https://www.daytona.io/docs/en/vnc-access/

Because Daytona does not expose DPI directly through `display.get_info()`, DPI was measured from the
running X server in a temporary Daytona sandbox using:

```sh
DISPLAY=${DISPLAY:-:0} xdpyinfo | sed -n -e "/dimensions:/p" -e "/resolution:/p"
DISPLAY=${DISPLAY:-:0} xrdb -query | grep -i "Xft.dpi"
```

The live probe returned:

```text
dimensions:    1024x768 pixels (271x204 millimeters)
resolution:    96x96 dots per inch
Xft.dpi:       96
```

So the Daytona default observed for this benchmark is `1024x768 @ 96 DPI`.

| Provider | Resolution | DPI | Source |
|---|---:|---:|---|
| Daytona | 1024x768 | 96 | Live `display.get_info()` plus X11 `xdpyinfo`/`xrdb` probe |
| E2B | 1024x768 | 96 | E2B desktop Python docs and benchmark metadata |
| Modal daemon | 1024x768 | 96 | Benchmark config after default normalization |

E2B’s current desktop Python docs explicitly document the defaults used here:
`resolution=(1024, 768)`, `dpi=96`, `display=":0"`, and `timeout=300`.

- https://e2b.dev/docs/sdk-reference/desktop-python-sdk/v2.0.0/sandbox

## 10x Results

All values are arithmetic means in milliseconds across 10 successful iterations unless noted.

| Provider | Ingress/API path | Cold ready | Screenshot | Move/click | 8-action sequence | Type 100 | Type 1000 | Command | Verification |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| Modal daemon | SDK over Modal Connect to daemon HTTP | 12193 | 1176 | 968 | 1661 | 1559 | 7513 | 553 | daemon run succeeded |
| Daytona | Daytona Computer Use SDK | 11010 | 445 | 540 | 2074 | 688 | 5367 | 210 | cursor ok; type ok |
| E2B | E2B Desktop SDK | 1346 | 215 | 230 | 907 | 4180 | 42116 | 69 | cursor ok; type ok |

Screenshot payload sizes:

| Provider | Screenshot bytes |
|---|---:|
| Modal daemon | 257992 |
| Daytona | 154404 |
| E2B | 10521 |

These 2026-05-17 screenshot byte values should be treated as provider-observed payload sizes, not
as a normalized PNG-to-PNG comparison. The provider benchmark at that point recorded only a generic
`size_bytes` value for Daytona and E2B. It did not record whether the provider returned decoded
image bytes, a base64 string, a provider-reported `size_bytes` field, JPEG/PNG/WebP format, or image
dimensions. Modal daemon `size_bytes` is decoded PNG bytes from the daemon response.

As of the screenshot-debug instrumentation, future provider runs record:

- `payload.source`: raw bytes, base64 string, provider object attribute path, or provider-reported
  field.
- `payload.provider_reported_size_bytes`: the provider SDK object's own size field, when present.
- `payload.transport_size_bytes`: bytes in the SDK-returned string/bytes payload.
- `payload.decoded_size_bytes`: decoded bytes when the payload is raw bytes or valid base64.
- `payload.format`, `payload.width`, and `payload.height` when inferable from image bytes or SDK
  attributes.

Use those fields to decide whether the size gap is real image compression/content difference or an
encoding/accounting difference.

Estimated run costs:

| Provider | Cost |
|---|---:|
| Daytona 10x | $0.004416 |
| E2B 10x with 900s timeout | $0.017672 |
| Modal daemon | not reconciled in this artifact |

## Batching Result

The Modal batching claim comes directly from the 10x Modal daemon artifact:

- `surfaces["daemon-http"].cases.action_batch.cases.batch_5_actions.summary_ms.mean`
  = `961.064958103816`
- `surfaces["daemon-http"].cases.action_batch.cases.separate_5_actions.summary_ms.mean`
  = `3900.723854196258`
- `surfaces["daemon-http"].cases.action_batch.comparison.batch_vs_separate_speedup`
  = `4.058751514457875`

So the same five primitive actions were about `4.06x` faster as one daemon batch than as five
separate SDK calls through Modal Connect. The interpretation is transport-sensitive: batching
removes repeated client-to-Connect-to-daemon request overhead and repeated route handling, while the
daemon still executes the primitive actions serially.

## Interpretation

This is now a closer provider-default comparison for desktop size and post-action delay:

- Modal daemon, E2B, and observed Daytona resolution are all `1024x768`.
- Modal daemon, E2B, and observed Daytona are all `96 DPI`.
- Modal daemon timings include Modal Connect ingress overhead for each SDK call.
- Daytona and E2B timings are provider-native SDK calls.

Modal’s slow single-call numbers are therefore primarily an ingress-path comparison, not evidence
that daemon primitive execution is that slow locally. The local mock benchmark shows the daemon’s
batch/action machinery is sub-millisecond to low-single-digit milliseconds without network ingress,
while Modal Connect adds roughly one round trip per SDK call.

E2B’s text-entry path is the outlier: `type_1000_chars` averages about 42 seconds through the
provider default GUI typing path. The benchmark preserves provider-default behavior rather than
replacing text input with a faster custom clipboard or shell write path.
