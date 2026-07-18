# Provider Benchmark Current Reference, 2026-07-18

This is the current reference for the neutral `provider-default` SDK comparison. It measures each
provider's on-demand product lifecycle and default warm action APIs from the same external caller.
It is a correctness and provenance foundation, not the separately planned
`modal-platform-optimized` profile.

## Provenance

- Tracked sanitized report: `benchmark-data/provider-compare-2026-07-18-current.json`
- Raw untracked report:
  `benchmark-results/candidates/provider-compare-live-20260718-final.json`
- Raw report SHA-256: `4059f3f95d11c614e762d89f94c4fb36cee9922284f3717b85911fab933991d9`
- Harness commit: `7bd8cc1b0ffa73b02364916fffcf7231077f8bf5`
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
  --output benchmark-results/candidates/provider-compare-live-20260718-final.json \
  --json
```

The tracked artifact was generated and drift-checked with:

```bash
uv run python scripts/sanitize_provider_benchmark.py \
  benchmark-results/candidates/provider-compare-live-20260718-final.json \
  benchmark-data/provider-compare-2026-07-18-current.json \
  --raw-artifact-path benchmark-results/candidates/provider-compare-live-20260718-final.json \
  --harness-commit 7bd8cc1b0ffa73b02364916fffcf7231077f8bf5 \
  --status current_reference \
  --scope "provider-default SDK paths at 1024x768, one warmup and three measured iterations" \
  --check
```

## Results

Headline values are p50 over three measured iterations. Ratios greater than `1.00x` mean Modal is
faster; values below `1.00x` mean the other provider is faster.

| Case | Modal p50 | Daytona p50 | E2B p50 | Modal vs Daytona | Modal vs E2B |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to first screenshot | 10191.6 ms | 10790.0 ms | 1736.1 ms | **1.06x** | 0.17x |
| Provider-default full screenshot | 137.3 ms | 208.1 ms | 201.7 ms | **1.52x** | **1.47x** |
| Move and click | 78.1 ms | 326.3 ms | 234.6 ms | **4.18x** | **3.00x** |
| Four move/click pairs | 83.3 ms | 1402.7 ms | 1052.3 ms | **16.84x** | **12.63x** |
| Type 100 characters | 792.2 ms | 617.3 ms | 4152.1 ms | 0.78x | **5.24x** |
| Type 1000 characters | 6709.3 ms | 5369.7 ms | 41959.2 ms | 0.80x | **6.25x** |
| Command echo | 145.9 ms | 123.5 ms | 74.1 ms | 0.85x | 0.51x |

Modal's strongest provider-default results are action transport. Four move/click pairs are 16.84x
faster than Daytona and 12.63x faster than E2B because the daemon accepts the sequence as one batch.
Modal also leads both providers on one move/click and full screenshot. E2B leads startup and command
echo; Daytona leads typing and command echo. These losses remain part of the reference rather than
being hidden.

## Startup Scope

| Provider | Sample 1 | Sample 2 | Sample 3 | p50 |
| --- | ---: | ---: | ---: | ---: |
| Modal | 10014.8 ms | 10191.6 ms | 11080.9 ms | 10191.6 ms |
| Daytona | 11127.4 ms | 10670.8 ms | 10790.0 ms | 10790.0 ms |
| E2B | 1736.1 ms | 1265.0 ms | 2425.9 ms | 1736.1 ms |

This is a product-level create-to-first-successful-screenshot comparison, not a normalized
infrastructure boot comparison. Modal uses a sandbox image plus daemon and attested-ingress startup;
Daytona uses its managed default snapshot; E2B uses its desktop template snapshot. E2B is 5.87x
faster than Modal at p50 under these provider-default startup models.

## Screenshot Scope

`screenshot_full` measures each provider's default binary screenshot path. It supports API-latency
claims, not identical-pixel visual or encoder claims, because provider desktop contents differ.
The final observed payloads were 391609 raw PNG bytes for Modal, 115845 decoded PNG bytes for
Daytona, and 15308 raw PNG bytes for E2B. Daytona's separate base64 transport was 154460 bytes.
Use synthetic-canvas cases for normalized visual comparisons.

## Cost Scope

Public-rate estimates covered measured resource lifetime through cleanup. Daytona estimated
`$0.001662` and E2B estimated `$0.006578` for this run. Modal's estimate remained `partial` because
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
