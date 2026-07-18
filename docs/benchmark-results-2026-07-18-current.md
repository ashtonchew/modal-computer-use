# Provider Benchmark Current Reference, 2026-07-18

This is the current reference for the neutral `provider-default` SDK comparison. It measures each
provider's on-demand product lifecycle and default warm action APIs from the same external caller.
It is a correctness and provenance foundation, not the separately planned
`modal-platform-optimized` profile.

## Provenance

- Tracked sanitized report: `benchmark-data/provider-compare-2026-07-18-current.json`
- Raw untracked report:
  `benchmark-results/candidates/provider-compare-live-20260718-current.json`
- Raw report SHA-256: `81df7141ff432efca4532fb5b5f1de93c3fb1579b69bcf4c77dfe5b65b1e220e`
- Harness commit: `9138749361c011ceda5debdd41b6ec67dee56c85`
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
  --output benchmark-results/candidates/provider-compare-live-20260718-current.json \
  --json
```

The tracked artifact was generated and drift-checked with:

```bash
uv run python scripts/sanitize_provider_benchmark.py \
  benchmark-results/candidates/provider-compare-live-20260718-current.json \
  benchmark-data/provider-compare-2026-07-18-current.json \
  --raw-artifact-path benchmark-results/candidates/provider-compare-live-20260718-current.json \
  --harness-commit 9138749361c011ceda5debdd41b6ec67dee56c85 \
  --status current_reference \
  --scope "provider-default SDK paths at 1024x768, one warmup and three measured iterations" \
  --check
```

## Results

Headline values are p50 over three measured iterations. Ratios greater than `1.00x` mean Modal is
faster; values below `1.00x` mean the other provider is faster.

| Case | Modal p50 | Daytona p50 | E2B p50 | Modal vs Daytona | Modal vs E2B |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to first screenshot | 10293.8 ms | 10604.6 ms | 1281.8 ms | **1.03x** | 0.12x |
| Provider-default full screenshot | 253.6 ms | 460.6 ms | 193.2 ms | **1.82x** | 0.76x |
| Move and click | 87.2 ms | 718.5 ms | 265.6 ms | **8.24x** | **3.05x** |
| Four move/click pairs | 99.4 ms | 2097.5 ms | 1046.5 ms | **21.09x** | **10.52x** |
| Type 100 characters | 838.1 ms | 695.1 ms | 4194.4 ms | 0.83x | **5.00x** |
| Type 1000 characters | 6472.7 ms | 5504.5 ms | 41977.0 ms | 0.85x | **6.49x** |
| Command echo | 111.8 ms | 290.9 ms | 60.9 ms | **2.60x** | 0.54x |

Modal's strongest provider-default results are action transport. Four move/click pairs are 21.09x
faster than Daytona and 10.52x faster than E2B because the daemon accepts the sequence as one batch.
Modal also leads both providers on one move/click. E2B leads startup, screenshot, and command echo;
Daytona leads both typing cases. These losses remain part of the reference rather than being hidden.

## Startup Scope

| Provider | Sample 1 | Sample 2 | Sample 3 | p50 |
| --- | ---: | ---: | ---: | ---: |
| Modal | 7753.6 ms | 11074.1 ms | 10293.8 ms | 10293.8 ms |
| Daytona | 10593.0 ms | 10604.6 ms | 10618.2 ms | 10604.6 ms |
| E2B | 1929.7 ms | 1224.7 ms | 1281.8 ms | 1281.8 ms |

This is a product-level create-to-first-successful-screenshot comparison, not a normalized
infrastructure boot comparison. Modal uses a sandbox image plus daemon and attested-ingress startup;
Daytona uses its managed default snapshot; E2B uses its desktop template snapshot. E2B is 8.03x
faster than Modal at p50 under these provider-default startup models.

## Screenshot Scope

`screenshot_full` measures each provider's default binary screenshot path. It supports API-latency
claims, not identical-pixel visual or encoder claims, because provider desktop contents differ.
The final observed payloads were 391573 raw PNG bytes for Modal, 115580 decoded PNG bytes for
Daytona, and 15308 raw PNG bytes for E2B. Daytona's separate base64 transport was 154108 bytes.
Use synthetic-canvas cases for normalized visual comparisons.

## Cost Scope

Public-rate estimates covered measured resource lifetime through cleanup. Daytona estimated
`$0.001833` and E2B estimated `$0.006529` for this run. Modal's estimate remained `partial` because
the resolved CPU and memory allocations were unavailable in the artifact, so no Modal total is
claimed. These estimates are not delayed provider billing reconciliation.

## Status History

| Result | Status | Reason |
| --- | --- | --- |
| 2026-07-18 clean corrected run | Current reference | Committed harness, exact 3x lifecycle sampling, all gates passed |
| 2026-07-18 dirty corrected run | Superseded candidate | Corrected behavior, but run from an uncommitted review tree |
| 2026-07-18 v3 | Rejected | External lifecycle included teardown; Daytona bytes counted base64 transport |
| 2026-07-18 v1 | Rejected | Modal lifecycle had one sample while Daytona and E2B had three |
| 2026-05-17 provider compare | Historical diagnostic | Older ingress and screenshot semantics |

Do not combine rows from different statuses or workloads into one provider ranking.
