# Provider benchmark results

Provider-default values are median [observed min–max] milliseconds. Modal optimized values are p50 / p95 milliseconds.

| Case | Modal optimized | Modal default | Daytona default | E2B default | Tzafon default |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to validated screenshot | 8889.47 / 9637.15 | 9343.54 [9046.85–11101.86] | 10619.98 [10531.73–10622.59] | 1319.16 [1312.85–1343.83] | 374.39 [369.80–605.66] |
| Full screenshot native/default | 33.44 / 35.08 | 143.10 [139.86–144.05] | 212.18 [183.44–216.91] | 189.70 [188.55–191.86] | 220.61 [216.91–224.63] |
| One coordinate click | 4.21 / 4.77 | 73.98 [73.21–74.62] | 255.13 [217.08–309.09] | 220.64 [219.21–220.86] | 198.47 [190.53–211.46] |
| Four coordinate clicks | 7.54 / 8.60 | 77.99 [76.84–79.67] | 839.39 [828.18–844.55] | 913.45 [880.03–914.52] | 537.46 [528.02–569.39] |
| Type 100 | 10.25 / 11.88 | 80.03 [79.44–82.39] | 633.18 [632.55–638.03] | 4083.03 [4082.96–4092.77] | 175.65 [172.06–183.03] |
| Type 1000 | 50.76 / 53.32 | 119.44 [118.24–119.68] | 5380.17 [5365.04–5426.91] | 40890.06 [40802.10–40934.84] | 230.87 [221.25–256.96] |
| Non-login shell command | 9.30 / 10.26 | 79.30 [78.05–79.94] | 111.33 [110.65–114.30] | 60.53 [58.54–60.75] | 149.29 [141.88–168.00] |

## Tzafon claim boundary

[Tzafon's status post](https://x.com/tzafon_company/status/2080351293533753736) reports
63 ms for its browser, 71 ms for its desktop, and 188 ms for an E2B base sandbox. It reports the
median of five runs from San Francisco and measures server-side TTFB minus the TLS handshake. Those
figures are vendor-claim context. They are not compared numerically with this report's public create
call through decoded and validated screenshot boundary.

## Modal-only experimental result

Action click to first hash-confirmed visual change: 70.88 / 83.49 ms p50 / p95 (30/30, no replacement samples).

Tzafon settle semantics are opaque at this API boundary, so its action acknowledgement is not treated as equivalent to Modal’s hash-confirmed first visual change.

Full screenshots use each provider's native/default format and are not pixel- or codec-normalized.

Sample counts: provider defaults 3/3; Modal optimized 30/30; Modal experiment 30/30. The default and optimized Modal columns use explicitly different caller topologies.

Eligibility requires successful command and top-level outcomes. Cleanup errors are terminal in the producer, but this combined artifact does not independently prove cleanup beyond those recorded outcomes.

Modal optimized and experimental evidence uses a Modal runner with the same requested Modal region as its target; the unrelated external caller diagnostic is not executed or included. Publishable optimized evidence separately requires every observed target cloud and region to match the runner.
