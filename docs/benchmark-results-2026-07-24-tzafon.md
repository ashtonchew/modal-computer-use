# Tzafon provider comparison, 2026-07-24

## Canonical results

These are the final canonical `coordinate-click-v1` and `shell-command-echo-v2` results. Provider
defaults are one warmup plus three measured iterations. The separately selected Modal optimized arm
has 30 measured iterations. Values are p50 / p95 milliseconds.

| `coordinate-click-v1` case | Modal default | Daytona | E2B | Tzafon | Modal optimized |
| --- | ---: | ---: | ---: | ---: | ---: |
| One coordinate click | 75.48 / 75.86 | 215.14 / 215.21 | 216.28 / 217.02 | 163.68 / 166.57 | **4.12 / 4.81** |
| Four-click sequence | 79.56 / 79.77 | 846.14 / 854.18 | 873.19 / 882.25 | 483.72 / 484.36 | **7.49 / 8.54** |

| `shell-command-echo-v2` | Modal default | Daytona | E2B | Tzafon | Modal optimized |
| --- | ---: | ---: | ---: | ---: | ---: |
| `sh -c 'printf 42'` | 76.78 / 77.09 | 115.47 / 119.83 | **57.90 / 58.28** | 68.20 / 70.29 | **9.62 / 11.04** |

The bold values identify the lowest provider-default result and, separately, the selected optimized
Modal result; the optimized arm is not a provider-default product path. Every displayed case
completed all measured iterations without a recorded failure. Exact cursor and controlled typing
readbacks also passed for all four providers and the optimized arm.

The rest of the fresh warm-operation p50 context is:

| Case | Modal default | Daytona | E2B | Tzafon | Modal optimized |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, native/default format | **116.40** | 191.68 | 195.75 | 190.00 | **39.64** |
| Type 100 characters | **78.24** | 631.53 | 4,085.28 | 119.74 | **10.91** |
| Type 1,000 characters | **98.74** | 5,263.92 | 40,988.98 | 216.85 | **55.29** |

The screenshot row is intentionally native/default rather than resolution- and codec-normalized;
the boundary is detailed below. The typing rows use the same controlled readback contract across
providers.

## What the cases actually compare

One coordinate click means one destination click, not a synthetic move-plus-click pair. Modal,
Daytona, and Tzafon issue one request. E2B issues one SDK call whose pinned implementation makes two
transport requests. The four-click sequence is one native batch request for Modal and Tzafon, four
sequential requests for Daytona, and four SDK calls/eight pinned-SDK transport requests for E2B.

Tzafon therefore participates honestly in the coordinate benchmark: it exposes a native coordinate
click. It does not expose a public move-only primitive, so this report makes no standalone
pointer-move claim for Tzafon.

The command case always requests non-login shell behavior with the exact argv
`["sh", "-c", "printf 42"]`, a 30-second timeout, and validated stdout `42`. Modal carries argv to
the daemon. Daytona, E2B, and Tzafon receive the equivalent provider command string because that is
their public transport shape.

## Shell decomposition and subprocess-runner A/B

For Modal, total command latency is decomposed into daemon execution and caller/transport overhead
using the daemon's measured route timing. The selected 30-sample result was:

| Stage | p50 ms | p95 ms |
| --- | ---: | ---: |
| Total request | 9.62 | 11.04 |
| Daemon route, including process execution | 6.62 | 7.46 |
| Caller/transport overhead | 3.02 | 3.59 |

The daemon route still includes admission to the configured process runner, child creation,
stdin/stdout/stderr handling, waiting, and cleanup. The timing does not claim that the shell program
itself took the full daemon duration.

A clean 10-sample-per-arm ablation held region, Connect runner path, HTTP version, browser, surface,
and input throttling constant:

| Subprocess runner | Total p50 / p95 ms | Daemon p50 / p95 ms | Caller/transport p50 / p95 ms |
| --- | ---: | ---: | ---: |
| `asyncio` | 55.92 / 248.67 | 49.80 / 242.14 | 6.17 / 6.95 |
| `threaded` | **9.67 / 10.84** | **5.34 / 6.17** | 4.37 / 5.23 |
| `isolated-asyncio` | 9.86 / **10.63** | 6.68 / 7.44 | **3.07 / 3.50** |

`isolated-asyncio` is the selected default. It retains asyncio subprocess behavior on a dedicated
event-loop thread, avoiding the high tail observed in the shared-loop `asyncio` arm while keeping
timeout, cancellation, and cleanup ownership isolated. `threaded` was marginally lower at p50 in
this small A/B, but the difference between it and `isolated-asyncio` is much smaller than the
shared-loop tail.

This is a portable-shell ablation, not a claim that every daemon operation became faster. The
runner benefits shell commands and subprocess-backed compatibility paths. It does not accelerate
native XTest input, native Xlib window operations, MSS screenshot capture, or the runtime of a long
command after it has started.

## Screenshot normalization boundary

Tzafon desktop was requested at 1024x768 but returned 1280x720 JPEG screenshots. Modal, Daytona,
and E2B returned 1024x768 PNG screenshots in the provider-default run. The older screenshot numbers
are useful observations of each provider's native/default path, but they are not pixel- or
codec-normalized.

A strict all-provider, all-native 1280x720 JPEG ablation is not possible with the current public
surfaces: not every provider exposes a native JPEG screenshot primitive at that resolution.
Transcoding or resizing would instead measure a portable normalization layer. This report therefore
keeps native/default screenshot evidence labeled as such and does not present a portable conversion
as native provider performance.

## API fit and lifecycle context

The [official Computers guide](https://docs.lightcone.ai/guides/computers/) describes an isolated
Lightcone OS computer with lifecycle, screenshot, mouse, keyboard, shell, batching, and browser CDP
capabilities. That is the same infrastructure layer measured here. Lightcone's Northstar Tasks and
Responses API can additionally own an agent loop, but this comparison uses only directly controlled
Computers primitives.

The provider-default run pinned `tzafon==2.44.1`, used a nonpersistent desktop, requested inline
screenshots, and retained the SDK's default two retries. Its product-create-to-first-validated-
screenshot p50 was 266.13 ms, versus 2,055.56 ms for E2B, 19,129.82 ms for Modal's neutral external
path, and 10,774.33 ms for Daytona.

Those lifecycle values do not reproduce or contradict Tzafon's separately published 71 ms desktop
number. That number used server-side TTFB minus TLS handshake over five runs; this harness measures
from product create through returned, decoded, parsed, and validated full-screen pixels.

## Evidence and limits

The tracked sanitized source is
[`benchmark-data/provider-compare-coordinate-command-2026-07-24.json`](../benchmark-data/provider-compare-coordinate-command-2026-07-24.json).
The compact allowlisted context is
[`benchmark-data/tzafon-coordinate-command-context-2026-07-24.json`](../benchmark-data/tzafon-coordinate-command-context-2026-07-24.json).
The earlier
[`benchmark-data/tzafon-competitive-context-us-west-2-2026-07-24.json`](../benchmark-data/tzafon-competitive-context-us-west-2-2026-07-24.json)
is preserved as historical context.

The compact artifact records content and safe-configuration hashes, exact p50/p95 values, action
shapes, verification, and limitations. Endpoint URLs, resource identifiers, credentials, screenshot
payloads, and typed or clipboard content are excluded. Raw selected and A/B artifacts remain ignored
under `benchmark-results/candidates/`.

The provider-default arms have only three measured samples, the optimized arm has 30, and each
runner A/B arm has 10. The provider-default and optimized measurements use different caller and
ingress configurations, so their ratios describe these runs rather than a universal provider
ranking. Requested region placement is recorded but not independently attested to a physical
availability zone. Tzafon cost remains unknown because the run did not expose the resource usage
needed to apply its public usage-based pricing.
