# Provider benchmark results, 2026-07-26

Provider-default values are median [observed min–max] milliseconds. Modal optimized values are p50 / p95 milliseconds.

| Case | Modal optimized | Modal default | Daytona default | E2B default | Tzafon default |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to validated screenshot | not measured | 8978.65 [8845.96–27761.64] | 10639.23 [10531.12–10673.59] | 1239.03 [1166.15–1359.81] | 273.36 [266.47–312.36] |
| Full screenshot native/default | 30.95 / 33.42 | 123.97 [122.85–128.64] | 537.05 [419.01–576.56] | 215.90 [186.05–230.68] | 129.93 [128.16–133.89] |
| One coordinate click | 6.08 / 8.58 | 74.36 [74.21–76.05] | 382.59 [382.15–388.77] | 218.50 [214.34–248.95] | 179.72 [156.98–183.69] |
| Four coordinate clicks | 11.89 / 16.81 | 77.58 [76.39–79.76] | 1412.99 [1393.83–1533.18] | 901.76 [861.79–922.87] | 477.83 [476.07–533.73] |
| Type 100 | 17.38 / 22.76 | 87.85 [85.27–99.86] | 803.50 [801.89–803.86] | 4131.47 [4111.87–4132.65] | 113.58 [112.41–115.23] |
| Type 1000 | 98.82 / 113.15 | 121.81 [120.84–124.61] | 5460.40 [5355.03–5482.29] | 41046.72 [41042.29–41063.85] | 145.77 [133.50–147.25] |
| Non-login shell command | 16.46 / 19.16 | 77.89 [75.27–78.05] | 279.95 [279.73–282.49] | 52.75 [52.62–52.96] | 63.54 [59.85–73.56] |

## Modal-only experiment

The experiment measured a click to the first hash-confirmed visual change. The result was 80.35 / 95.62 ms p50 / p95. All 30 of 30 samples passed. The run used no replacement samples.

The maximum wait was 200 ms. This value was a timeout. It was not a fixed sleep. The operation returned when it verified a changed full-resolution source hash. A detected first change does not prove that the application is ready or settled.

Tzafon settle semantics are opaque at this API boundary, so its action acknowledgement is not treated as equivalent to Modal’s hash-confirmed first visual change.

This is a Modal-only experimental result. The report does not publish a Tzafon observation result or a cross-provider observation comparison.

## Related public API

Lightcone computers are comparable cloud desktop and browser-control infrastructure. Its [computer guide](https://docs.lightcone.ai/guides/computers/) documents that public surface.

Lightcone also documents [fused act and observe](https://docs.lightcone.ai/guides/observability#fused-act-and-observe). The option `screenshot_after=true` fuses an action with its post-action screenshot. The option `settle_ms=N` waits for up to N ms. The public boundary does not expose a hash-confirmed change criterion. It is therefore not equivalent to this Modal experiment.

## Fairness and measurement boundaries

The default columns use each provider's public SDK and default product path. These columns have three samples each. The Modal optimized column uses a separate same-region Modal runner. It has 30 samples. The Modal experiment also has 30 samples. The unrelated external caller diagnostic was not run or included in the optimized and experimental evidence.

The table does not show ratios or ranks. It does not publish p95 for the three-sample default cells. The machine artifact retains p95 for those cells. It records the display threshold as `n < 20`. It defines p50 as `statistics.median`. It defines p95 as linear interpolation on sorted values at rank `0.95*(n-1)`.

The columns have different caller topologies and sample counts. Use the values as measurements of the stated paths.

Full screenshots use each provider's native default. Tzafon returned 1280x720 JPEG images. Modal, Daytona, and E2B returned 1024x768 PNG images. The screenshot row is not normalized for pixels or codecs.

One coordinate click is one destination click. Modal, Daytona, and Tzafon use one request. E2B uses one SDK call. Its pinned SDK implementation uses two transport requests. Four coordinate clicks use one native batch request for Modal and Tzafon. Daytona uses four sequential requests. E2B uses four SDK calls and eight transport requests.

The command case requests exact argv `["sh", "-c", "printf 42"]` with non-login shell behavior. It validates exact stdout `42`. Modal sends argv to the daemon. The other providers receive the equivalent command string that their public SDK accepts. The Modal daemon route latency includes admission, child-process creation, output handling, waiting, and cleanup. It is not only the execution time of `printf`.

Provider creation starts at the public create call. It ends after the returned full-screen image is decoded, parsed, and validated. Provider startup models are different. Requested placement is recorded. It is not independent proof of a physical availability zone.

The [Tzafon tweet](https://x.com/tzafon_company/status/2080351293533753736) reports 63 ms for browser, 71 ms for desktop, and 188 ms for E2B. It reports server-side time to first byte minus the TLS handshake over five runs. That boundary is different. Those values are not numerically comparable to this create-to-validated-screenshot lifecycle or to the warm operation rows.

## Eligibility and evidence

Two preliminary attempts at revision `99ada99` were rejected. An unrelated external sidecar failed in each attempt. The sidecar was not part of the selected same-region runner path. The final reporting path therefore uses runner-only evidence and records that choice explicitly.

All published benchmark arms ran at evidence revision `628f776995927680ba386a79ae1b5537dfe2dfed`. The combined artifact binds the three input digests to that evidence revision. Reporter revision `77738d506ed8bf7a8bc54b87dff91ec324d1f3ac` generated the combined artifact. Reporting-only changes did not rerun the evidence.

The tracked provider-default source is [`benchmark-data/provider-compare-coordinate-command-2026-07-26.json`](../benchmark-data/provider-compare-coordinate-command-2026-07-26.json). The tracked combined result is [`benchmark-data/provider-results-2026-07-26.json`](../benchmark-data/provider-results-2026-07-26.json). Raw artifacts stay ignored under `benchmark-results/candidates/`.

Eligibility requires successful command and top-level outcomes. Cleanup errors are terminal in the producer. The combined artifact does not make an independent cleanup claim beyond those recorded outcomes.
