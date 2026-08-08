# Optimized-default promotion results, 2026-08-08

**Evidence status:** eligible for SDK-default promotion

## Result

The optimized SDK default passed its preregistered same-topology promotion gate.

| Warm operation | p50 | p95 |
| --- | ---: | ---: |
| Prior public JSON/base64 screenshot, then one pointer move | 147.27 ms | 763.47 ms |
| Candidate raw-binary semantic screenshot, then one pointer move | 81.11 ms | 739.17 ms |

The paired median difference was -77.39 ms, or -44.92%. The paired bootstrap 95% confidence
interval was -81.20 to -72.03 ms. Its relative interval was -62.34% to -43.01%. Negative values
favor the candidate.

Both arms completed 30 measured samples with no replacement samples, retries, or failures. Cleanup
succeeded with no recorded survivors.

## What the timer contains

Each warm-operation timer contains two sequential public operations:

1. Capture one full 1024x768 cursor-hidden PNG screenshot.
2. Send one ordered HTTP action batch containing one harmless pointer move.

The candidate calls `await computer.screenshots.full()` and receives a byte-backed `Screenshot`
from the raw binary HTTP route. The prior arm reproduces the retained JSON/base64 inline route
through an internal compatibility Adapter. Both arms then call `await computer.actions.run(...)`.

This is not a fused action-and-screenshot request. The benchmark does not use WebSocket control,
HTTP/2, a managed release Image, input-rate-limit removal, or warm capacity.

The p95 values include shared long-tail stalls. The promotion decision uses paired, interleaved
samples and the preregistered confidence interval instead of comparing the two p95 values alone.

## The historical 47 ms figure

The article's opening figure is **47.10 ms by arithmetic**:

- 37.25 ms historical warm median for one raw-binary screenshot.
- 9.85 ms historical warm median for one click.

Those medians came from separate benchmark cases. No request measured a fused screenshot-plus-click
turn at 47.10 ms. This report therefore does not call 47 ms a measured turn and does not promise it
as the SDK default's latency.

The 2026-08-08 result is also not directly comparable with that arithmetic figure. It measures a
sequential screenshot and pointer move through the new public default, uses a new allocation, and
includes the current semantic `Screenshot` reconstruction. It proves the candidate improves the
retained path under controlled conditions; it does not reproduce the historical absolute number.

## Controlled configuration

The two arms held these values constant:

- Caller topology: one application-owned Modal Function.
- Requested and observed Function placement: `aws`, `us-west-2`.
- Requested and observed Sandbox placement: `aws`, `us-west-2`.
- Function resources: 1 CPU and 2048 MiB.
- Sandbox resources: 1 CPU and 2048 MiB.
- Ingress: attested tunnel. Requests still crossed authenticated Modal ingress.
- HTTP: HTTP/1.1 through one reused pooled async client.
- Input: native XTest.
- Screenshot: PNG, full resolution, cursor hidden, daemon processing, inline storage.
- Function minimum containers: 0.
- Sandbox warm-pool capacity: 0.
- Warmup: one operation per arm.
- Measured samples: 30 per arm in one deterministic interleaved schedule.

Only the screenshot response representation changed: JSON/base64 for the prior arm and raw binary
with semantic `Screenshot` reconstruction for the candidate arm.

## Lifecycle timings

Cold allocation and setup were paid once and reported separately from the repeated warm path.

| Phase | Recorded time |
| --- | ---: |
| Sandbox cold allocation | 1,141.10 ms |
| Daemon startup and attestation | 9,815.60 ms |
| Placed Function dispatch/probe | 5,084.19 ms |
| One trajectory borrow | 1,165.04 ms |

These one-time values appear on every observation only to keep each retained row self-describing.
They are not repeated costs within the trajectory.

## Evidence and reproducibility

The live run used runtime source commit
`31bcafefbba2ba75653075a04b12ce2eb816c838`. The subsequent evidence commit changes documentation
and tracked benchmark files, not the measured runtime implementation.

The sanitized artifacts are immutable inputs to this report:

- [Prior public arm](../benchmark-data/optimized-default-prior-public-2026-08-08.json)
- [Candidate default arm](../benchmark-data/optimized-default-candidate-2026-08-08.json)
- [Promotion decision](../benchmark-data/optimized-default-promotion-decision-2026-08-08.json)

Run the offline verifier with:

```bash
computer-use benchmark promotion-gate \
  --prior-public benchmark-data/optimized-default-prior-public-2026-08-08.json \
  --candidate benchmark-data/optimized-default-candidate-2026-08-08.json
```

No screenshot bytes, daemon URL, token, clipboard text, typed text, or artifact bytes are present in
the retained evidence.
