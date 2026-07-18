# Provider Benchmark Results, 2026-07-18

This is the current reference run for the provider-default SDK comparison defined by commit
`86c15252cc5be188f2b79fddad98a438a0331e85`. "Current reference" means the newest accepted run for
this exact benchmark definition. It is not a permanent or cross-workload claim of canonicality.

## Provenance

- Tracked sanitized report: `benchmark-data/provider-compare-2026-07-18.json`
- Raw local report: `benchmark-results/candidates/provider-compare-live-20260718-v3.json`
- Raw report SHA-256: `1cb34cfe01c5eea1b341e583fcbb9de8c8854019a577475b37ba01c7ac437fc0`
- Harness commit: `86c15252cc5be188f2b79fddad98a438a0331e85`
- Result: `ok=true`; zero top-level or provider failures
- Sampling: one warmup and three measured iterations; three fresh product lifecycles per provider
- Desktop: `1024x768`; Modal used Chromium, the browser resource profile, HTTP/1.1, and the
  attested encrypted tunnel

The tracked report removes ephemeral ingress URLs, Modal run IDs, and Modal sandbox IDs. It keeps
all timing samples, summaries, provider metadata, case definitions, and verification results.

The command was:

```sh
uv run computer-use benchmark compare \
  --create-modal-sandbox \
  --provider modal-daemon \
  --provider daytona \
  --provider e2b \
  --modal-ingress attested-tunnel \
  --resource-profile browser \
  --browser chromium \
  --iterations 3 \
  --env-file /Users/ashtonchew/projects/modal-computer-use/.env \
  --output benchmark-results/candidates/provider-compare-live-20260718-v3.json \
  --json
```

## Results

Headline values use p50 because the sample count is three. Ratios greater than `1.00x` in the last
two columns mean Modal is faster; values below `1.00x` mean the other provider is faster.

| Case | Modal p50 | Daytona p50 | E2B p50 | Modal vs Daytona | Modal vs E2B |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to first screenshot | 10980.9 ms | 10925.3 ms | 1445.9 ms | 0.99x | 0.13x |
| Provider-default full screenshot | 120.7 ms | 218.3 ms | 188.8 ms | **1.81x** | **1.56x** |
| Move and click | 71.9 ms | 352.6 ms | 245.5 ms | **4.90x** | **3.41x** |
| Four move/click pairs | 77.6 ms | 1445.6 ms | 1050.1 ms | **18.64x** | **13.54x** |
| Type 100 characters | 786.3 ms | 637.4 ms | 4208.9 ms | 0.81x | **5.35x** |
| Type 1000 characters | 6735.2 ms | 5355.3 ms | 42087.7 ms | 0.80x | **6.25x** |
| Command echo | 144.8 ms | 118.7 ms | 61.0 ms | 0.82x | 0.42x |

Modal's strongest result is action transport. Four move/click pairs cost only 5.6 ms more than one
move/click at p50 because the daemon accepts the sequence as one batch. The equivalent public SDK
sequence is 18.64x slower on Daytona and 13.54x slower on E2B.

Modal's fused click-and-screenshot path is also stable at 118.1 ms p50. It has no directly equivalent
fused case in this provider-default report, so it is recorded as a Modal capability rather than a
cross-provider win.

Typing is mixed. Modal is 5.35x to 6.25x faster than E2B's default GUI typing path, but Daytona is
about 1.23x faster for 100 characters and 1.26x faster for 1000 characters. E2B wins command echo.

## Screenshot Scope

`screenshot_full` is the canonical binary screenshot API path for the Modal SDK. It is not a
normalized-pixel visual benchmark. The final payloads were approximately 392 KB for Modal, 154 KB
for Daytona, and 15 KB for E2B, reflecting different provider-default desktop content and PNG
entropy. The latency row therefore answers "how long does each product's default screenshot API
take now?", not "which encoder is fastest for identical pixels?"

Within Modal, the binary path was 120.7 ms p50 while the structured JSON/base64 compatibility path
was 252.8 ms p50, making the binary path 2.09x faster. The daemon itself spent 22.7 ms p50 capturing
and encoding; the remaining typical latency was transport and client overhead.

Use a deterministic synthetic-canvas run for an identical-content visual comparison. The May 22
synthetic-canvas run remains a separate historical visual diagnostic and is not replaced by this
provider-default report.

## Startup Scope

The lifecycle case now has three fresh samples for every provider. Modal and Daytona are effectively
tied at p50: Daytona is only 0.5% faster. E2B is 7.59x faster than Modal.

That result is fair as a product-level "create to first successful screenshot" metric, but it is
not a normalized infrastructure boot comparison:

| Provider | Startup model | Snapshot/template | Readiness endpoint |
| --- | --- | --- | --- |
| Modal | Sandbox image plus daemon startup | No | Raw screenshot after daemon and attested ingress readiness |
| Daytona | Managed default snapshot | Yes | Computer Use start plus first full screenshot |
| E2B | Desktop template snapshot | Yes | First sandbox screenshot |

E2B's lead is real for the out-of-box product experience and is also explained by a materially
different startup model. A separate normalized startup experiment would require equivalent prepared
desktop images or snapshots on all providers.

## Result Status

| Result | Status | Reason |
| --- | --- | --- |
| 2026-07-18 v3 | Current reference | Committed harness, symmetric 3x lifecycle sampling, all gates passed |
| 2026-07-18 v2 | Superseded candidate | Correct behavior, but run from an uncommitted harness tree |
| 2026-07-18 v1 | Rejected | Modal lifecycle had one sample while Daytona and E2B had three |
| 2026-05-13 provider compare | Superseded | One iteration, old cold definition, and ambiguous screenshot path |
| 2026-05-17 provider compare | Historical diagnostic | Older ingress and screenshot semantics; useful only with its original scope |
| 2026-05-22 synthetic canvas | Historical visual diagnostic | Separate normalized visual workload; not comparable to provider-default screenshots |

Do not combine rows from different statuses or workloads into one provider ranking.
