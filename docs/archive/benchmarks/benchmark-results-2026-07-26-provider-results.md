# Provider benchmark results, 2026-07-26

> **Archive category:** Historical
> **Date or revision:** 2026-07-26; evidence harness `6b6a814f460c0d509ef2ebe797edb3b582573b63`
> **Question:** How did the provider-default, Modal-optimized, and Modal visual-change paths compare?
> **Disposition:** The [2026-07-30 warm-operation report](../../benchmark-results-2026-07-30-warm-paths.md)
> is the current provider comparison. This report retains the earlier lifecycle and visual-change evidence.

**Evidence status:** eligible

## Read this before comparing results

This is a point-in-time independent benchmark, not a service-level promise. This project is not affiliated with or endorsed by Modal, Daytona, E2B, or Tzafon. Product names and trademarks belong to their owners.

The provider-default results use three samples from an external public-SDK caller. The Modal optimized results use 30 samples from a Modal Function with the same requested region as its targets. They differ in sample count, caller topology, ingress, and configuration. Read them as two separate experiments, not as an apples-to-apples provider ranking.

## Provider-default comparison

Values are median [observed min–max] milliseconds over three samples. These columns share the external-caller methodology described below.

| Case | Modal default | Daytona default | E2B default | Tzafon default |
| --- | ---: | ---: | ---: | ---: |
| Product create to validated screenshot | 10101.33 [7334.62–10916.58] | 10549.67 [10472.58–10561.09] | 1388.91 [1335.77–2627.98] | 283.03 [277.04–316.21] |
| Full screenshot native/default | 126.86 [126.10–127.49] | 588.74 [574.12–609.17] | 191.70 [190.52–201.18] | 132.38 [106.47–139.29] |
| One coordinate click | 209.01 [205.09–209.87] | 381.63 [380.76–384.16] | 221.15 [217.39–224.00] | 154.49 [153.29–157.77] |
| Four coordinate clicks | 227.56 [225.30–228.82] | 1548.00 [1414.12–1552.59] | 887.19 [886.77–912.69] | 474.00 [465.58–483.22] |
| Type 100 | 249.57 [248.41–254.24] | 806.05 [666.84–809.89] | 4104.69 [4080.06–4117.33] | 111.80 [105.05–116.70] |
| Type 1000 | 248.09 [247.99–250.17] | 5519.92 [5395.29–5540.48] | 41085.75 [40867.36–41426.72] | 145.78 [135.79–155.12] |
| Non-login shell command | 83.89 [80.91–84.31] | 287.97 [286.15–290.64] | 59.18 [56.56–59.59] | 58.03 [57.47–58.13] |

## Modal optimized result

Values are p50 / p95 milliseconds over 30 samples. This table describes the optimized Modal deployment only. Do not combine it with the provider-default table to claim controlled speedups.

| Case | Modal optimized p50 / p95 |
| --- | ---: |
| Product create to validated screenshot | 10246.36 / 17073.80 |
| Full screenshot native/default | 32.42 / 34.71 |
| One coordinate click | 4.43 / 5.06 |
| Four coordinate clicks | 7.02 / 8.41 |
| Type 100 | 9.95 / 10.62 |
| Type 1000 | 49.58 / 52.35 |
| Non-login shell command | 8.98 / 10.14 |

## Tzafon claim boundary

[Tzafon's status post](https://x.com/tzafon_company/status/2080351293533753736) reports 63 ms for its browser, 71 ms for its desktop, and 188 ms for an E2B base sandbox. It reports the median of five runs from San Francisco and measures server-side TTFB minus the TLS handshake. Those figures are vendor-claim context. They are not compared numerically with this report's public create call through decoded and validated screenshot boundary.

## Modal-only experimental result

Action click to first hash-confirmed visual change: 75.25 / 88.78 ms p50 / p95 (30/30, no replacement samples).

Tzafon settle semantics are opaque at this API boundary, so its action acknowledgement is not treated as equivalent to Modal’s hash-confirmed first visual change.

The 200 ms change timeout is the maximum wait for a hash-confirmed first visual change, not a fixed wait, settle period, or application-readiness signal.

## Measurement and fairness boundaries

The lifecycle timer starts immediately before the public create call and ends after the first full-screen image is decoded, parsed, and validated. Cleanup is outside the timer.

Warm-operation timers measure the selected public SDK or daemon request from the caller. They exclude target creation and cleanup.

The command case requests argv ["sh", "-c", "printf '42\n'"] with non-login shell semantics and requires exit code 0 with exact stdout "42\n".

Shell latency covers transport, authentication, request handling and admission, process spawn, output collection, process wait, cleanup, and exact-output validation.

isolated-asyncio affects only subprocess-backed command and compatibility paths; it does not select the native input or screenshot implementation.

Modal default typing requests the public TypeAction defaults (auto with a 10 ms character delay); for these 100- and 1000-character inputs auto resolves to clipboard, so that delay is not applied per character. Modal optimized explicitly resolves to keystrokes with zero delay. Modal default uses 1.05 seconds of untimed pacing before every warmup and measured action invocation to respect the default 20-actions-per-second input limit.

Full screenshots use each provider's native/default format and are not pixel- or codec-normalized.

Observed native/default screenshots were Tzafon 1280x720 JPEG and Modal, Daytona, and E2B 1024x768 PNG.

Lightcone is the computer infrastructure and public API; tzafon 2.44.1 is the pinned Python SDK package used for the Tzafon default column.

Sample counts: provider defaults 3/3; Modal optimized 30/30; Modal experiment 30/30. The default and optimized Modal columns use explicitly different caller topologies.

p50 uses `statistics.median`. p95 uses linear interpolation on sorted values at rank 0.95*(n-1). The report shows p95 only when the sample count is at least 20.

Four coordinate clicks use these request paths: Modal optimized 1 SDK / 1 transport; Modal default 1 SDK / 1 transport; Daytona default 4 SDK / 4 transport; E2B default 4 SDK / 8 transport; Tzafon default 1 SDK / 1 transport.

Modal optimized excludes Modal Function startup from the product-create samples. Provider-default measurements use an external public-SDK caller; Modal optimized uses one Modal Function with the same requested Modal region as its targets.

Eligibility requires successful command and top-level outcomes. Cleanup errors are terminal in the producer, but this combined artifact does not independently prove cleanup beyond those recorded outcomes.

Modal optimized and experimental evidence uses a Modal runner with the same requested Modal region as its target; the unrelated external caller diagnostic is not executed or included. Publishable optimized evidence separately requires every observed target cloud and region to match the runner.

## Evidence and reproducibility

Evidence harness SHA: `6b6a814f460c0d509ef2ebe797edb3b582573b63`. Report source SHA: `f5ba70404b4762e126e6b993f43e04ebc97b8a1e`.

Tracked inputs:

- [Provider defaults](../../../benchmark-data/provider-compare-coordinate-command-2026-07-26.json)
- [Modal optimized](../../../benchmark-data/modal-optimized-provider-2026-07-26.json)
- [Modal observation](../../../benchmark-data/modal-observation-2026-07-26.json)
- [Combined result](../../../benchmark-data/provider-results-2026-07-26.json)

The combined result binds the exact bytes of all three tracked inputs by SHA-256. Its p50 and p95 values are recomputed from the numeric samples in those inputs.
