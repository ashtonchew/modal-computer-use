# Provider Benchmark Current Reference, 2026-07-18

This is the current reference for the neutral `provider-default` SDK comparison. It measures each
provider's on-demand product lifecycle and default warm action APIs from the same external caller.
It is a correctness and provenance foundation, not the separately planned
`modal-platform-optimized` profile.

## Provenance

- Tracked sanitized report: `benchmark-data/provider-compare-2026-07-18-current.json`
- Raw untracked report:
  `benchmark-results/candidates/provider-compare-live-20260718-final-refactor-rerun.json`
- Raw report SHA-256: `ce7d743d0ebc859b08c573f05cb5b36a7c5b7a0bb2a74dec913a95d326396d86`
- Harness commit: `74edc6317ea4bac61ef512d7a68060445cf42720`
- Artifact status: `current_reference`; harness state: clean; sanitizer version: `1`
- Result: `ok=true`; zero top-level, provider, verification, or cleanup failures
- Sampling: one warmup and three measured iterations; three independent product lifecycles per
  provider
- Desktop: `1024x768`; Modal used Chromium, the browser resource profile, HTTP/1.1, and attested
  encrypted tunnel ingress

Cleanup occurs after lifecycle timing. Only the final measured Modal sandbox is reused for Modal
warm cases; every other lifecycle resource is cleaned before the next sample. Cursor and typing
readbacks passed for Modal, Daytona, and E2B. The deprecated `cold_create_to_ready` alias remains
emitted through 1.1.x but is excluded from status and failure aggregation.

The provider run was:

```bash
uv run computer-use benchmark compare \
  --create-modal-sandbox \
  --provider modal-daemon \
  --provider daytona \
  --provider e2b \
  --modal-ingress attested-tunnel \
  --resource-profile browser \
  --browser chromium \
  --iterations 3 \
  --env-file /path/to/untracked/.env \
  --output benchmark-results/candidates/provider-compare-live-20260718-final-refactor-rerun.json \
  --json
```

The tracked artifact was generated and drift-checked with:

```bash
uv run python scripts/sanitize_provider_benchmark.py \
  benchmark-results/candidates/provider-compare-live-20260718-final-refactor-rerun.json \
  benchmark-data/provider-compare-2026-07-18-current.json \
  --raw-artifact-path benchmark-results/candidates/provider-compare-live-20260718-final-refactor-rerun.json \
  --harness-commit 74edc6317ea4bac61ef512d7a68060445cf42720 \
  --status current_reference \
  --scope "provider-default SDK paths at 1024x768, one warmup and three measured iterations" \
  --check
```

## Results

Headline values are p50 over three measured iterations. Ratios greater than `1.00x` mean Modal is
faster; values below `1.00x` mean the other provider is faster.

| Case | Modal p50 | Daytona p50 | E2B p50 | Modal vs Daytona | Modal vs E2B |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to first screenshot | 23387.8 ms | 10678.3 ms | 1241.0 ms | 0.46x | 0.05x |
| Provider-default full screenshot | 240.0 ms | 163.0 ms | 192.8 ms | 0.68x | 0.80x |
| Move and click | 169.1 ms | 340.2 ms | 218.0 ms | **2.01x** | **1.29x** |
| Four move/click pairs | 173.5 ms | 1343.2 ms | 878.6 ms | **7.74x** | **5.06x** |
| Type 100 characters | 975.0 ms | 617.0 ms | 4074.4 ms | 0.63x | **4.18x** |
| Type 1000 characters | 8589.5 ms | 5253.9 ms | 41172.9 ms | 0.61x | **4.79x** |
| Command echo | 250.3 ms | 90.9 ms | 61.4 ms | 0.36x | 0.25x |

Modal's strongest provider-default results remain action transport. Four move/click pairs are 7.74x
faster than Daytona and 5.06x faster than E2B because the daemon accepts the sequence as one batch.
Modal also leads both providers on one move/click. E2B leads startup and command echo; Daytona leads
startup, full screenshot, typing, and command echo. These losses remain part of the reference rather
than being hidden. Daytona's move/click samples were outlier-sensitive, so use its p50 rather than
its mean for the headline ratio.

## Modal-Optimized Configuration

The platform-optimized Modal result uses the same operation boundaries as the table above, but moves
the caller into a separate Modal runner with the same narrow `us-west-2` selector as the target and
uses the daemon Connect path directly. It is an explicit deployment configuration, not a silent
replacement for the provider-default external caller.

| Case | Modal default | Same-run external Connect | Modal optimized | Daytona default | E2B default | Optimized comparison |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Full screenshot | 240.0 ms | 80.0 ms | **39.0 ms** | 163.0 ms | 192.8 ms | **4.18x / 4.95x faster** |
| Move and click | 169.1 ms | 32.4 ms | **4.6 ms** | 340.2 ms | 218.0 ms | **74.39x / 47.68x faster** |
| Four move/click pairs | 173.5 ms | 37.1 ms | **9.2 ms** | 1343.2 ms | 878.6 ms | **145.43x / 95.12x faster** |
| Type 100 characters | 975.0 ms | 742.1 ms | 720.5 ms | **617.0 ms** | 4074.4 ms | Daytona 1.17x faster; Modal 5.65x faster than E2B |
| Type 1000 characters | 8589.5 ms | 6557.1 ms | 6531.4 ms | **5253.9 ms** | 41172.9 ms | Daytona 1.24x faster; Modal 6.30x faster than E2B |
| Command echo | 250.3 ms | 110.7 ms | 93.8 ms | 90.9 ms | **61.4 ms** | Daytona 1.03x and E2B 1.53x faster |

The same-run external Connect control and Modal optimized arm use matching operation boundaries from
one raw artifact. Co-location reduced those p50s by 2.05x for screenshot, 7.08x for move-and-click,
4.01x for four move/click pairs, 1.03x for 100-character typing, 1.004x for 1000-character typing,
and 1.18x for command echo. The Modal optimized arm has 30 measured iterations after one warmup,
30/30 valid samples for every row, and zero failures. `Type 100 characters` is the standard
100-character operation; it does not mean 100 benchmark iterations.

The Modal default column is the historical provider-default reference. The Daytona and E2B cells are
the dated three-sample provider-default reference from this page, not a contemporaneous rerun, so
treat the cross-provider ratios as the current reference rather than a randomized paired experiment.

The largest wins come from removing the external ingress floor while retaining daemon-native
batching. Move-and-click fell from 169.1ms provider-default to 4.6ms optimized: daemon work was
0.99ms p50 and remaining client/transport overhead was 3.54ms. Four pairs fell from 173.5ms to
9.2ms, with 5.16ms in the daemon and 4.08ms of remaining overhead. Typing remains dominated by
daemon-side key generation, so co-location removes only a small fraction and Daytona still leads.
Command echo is effectively flat against Daytona at p50 and has a noisy 278.9ms p95, so it is not
an optimized Modal win.

The compact evidence record is
[`benchmark-data/modal-optimized-competitive-us-west-2-2026-07-24.json`](../benchmark-data/modal-optimized-competitive-us-west-2-2026-07-24.json).

## Startup Scope

| Provider | Sample 1 | Sample 2 | Sample 3 | p50 |
| --- | ---: | ---: | ---: | ---: |
| Modal | 15223.7 ms | 27477.7 ms | 23387.8 ms | 23387.8 ms |
| Daytona | 10596.8 ms | 10678.3 ms | 11231.3 ms | 10678.3 ms |
| E2B | 1249.4 ms | 1241.0 ms | 1140.9 ms | 1241.0 ms |

This is a product-level create-to-first-successful-screenshot comparison, not a normalized
infrastructure boot comparison. Modal uses a sandbox image plus daemon and attested-ingress startup;
Daytona uses its managed default snapshot; E2B uses its desktop template snapshot. E2B is 18.85x
faster than Modal at p50 under these provider-default startup models.

## Screenshot Scope

`screenshot_full` measures each provider's default binary screenshot path. It supports API-latency
claims, not identical-pixel visual or encoder claims, because provider desktop contents differ.
The final observed payloads were 391581 raw PNG bytes for Modal, 115768 decoded PNG bytes for
Daytona, and 15308 raw PNG bytes for E2B. Daytona's separate base64 transport was 154360 bytes.
Use synthetic-canvas cases for normalized visual comparisons.

## Cost Scope

Public-rate estimates covered measured resource lifetime through cleanup. Daytona estimated
`$0.001650` and E2B estimated `$0.006370` for this run. Modal's estimate remained `partial` because
the resolved CPU and memory allocations were unavailable in the artifact, so no Modal total is
claimed. These estimates are not delayed provider billing reconciliation.

## Status History

| Result | Status | Reason |
| --- | --- | --- |
| 2026-07-18 clean refactor rerun | Current reference | Modular committed harness, exact 3x lifecycle sampling, all gates passed |
| 2026-07-18 exact-head first attempt | Rejected candidate | All three Modal 1000-character samples hit the daemon batch timeout |
| 2026-07-18 dirty corrected run | Superseded candidate | Corrected behavior, but run from an uncommitted review tree |
| 2026-07-18 v3 | Rejected | External lifecycle included teardown; Daytona bytes counted base64 transport |
| 2026-07-18 v1 | Rejected | Modal lifecycle had one sample while Daytona and E2B had three |
| 2026-05-17 provider compare | Historical diagnostic | Older ingress and screenshot semantics |

Do not combine rows from different statuses or workloads into one provider ranking.
