# Provider Benchmark Current Reference, 2026-07-18

This is the current reference for the neutral `provider-default` SDK comparison. It measures each
provider's on-demand product lifecycle and default warm action APIs from the same external caller.
It is a correctness and provenance foundation, not the separately planned
`modal-platform-optimized` profile.

## Provenance

- Tracked sanitized report: `benchmark-data/provider-compare-2026-07-18-current.json`
- Raw untracked report:
  `benchmark-results/candidates/provider-compare-live-20260718-refactor.json`
- Raw report SHA-256: `b4051172a76b70bf8e3a5aa20e64e0bd48cfa7a4ce835baa2a4e971bab46c983`
- Harness commit: `f75ccc867800e6c7565e0a08db6d1d8385c96d19`
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
  --output benchmark-results/candidates/provider-compare-live-20260718-refactor.json \
  --json
```

The tracked artifact was generated and drift-checked with:

```bash
uv run python scripts/sanitize_provider_benchmark.py \
  benchmark-results/candidates/provider-compare-live-20260718-refactor.json \
  benchmark-data/provider-compare-2026-07-18-current.json \
  --raw-artifact-path benchmark-results/candidates/provider-compare-live-20260718-refactor.json \
  --harness-commit f75ccc867800e6c7565e0a08db6d1d8385c96d19 \
  --status current_reference \
  --scope "provider-default SDK paths at 1024x768, one warmup and three measured iterations" \
  --check
```

## Results

Headline values are p50 over three measured iterations. Ratios greater than `1.00x` mean Modal is
faster; values below `1.00x` mean the other provider is faster.

| Case | Modal p50 | Daytona p50 | E2B p50 | Modal vs Daytona | Modal vs E2B |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to first screenshot | 13040.4 ms | 10555.5 ms | 1325.3 ms | 0.81x | 0.10x |
| Provider-default full screenshot | 85.6 ms | 190.1 ms | 181.0 ms | **2.22x** | **2.11x** |
| Move and click | 41.5 ms | 344.0 ms | 224.6 ms | **8.29x** | **5.41x** |
| Four move/click pairs | 43.2 ms | 1512.9 ms | 1046.5 ms | **34.99x** | **24.20x** |
| Type 100 characters | 835.7 ms | 715.4 ms | 4135.4 ms | 0.86x | **4.95x** |
| Type 1000 characters | 8130.4 ms | 5428.4 ms | 41848.9 ms | 0.67x | **5.15x** |
| Command echo | 186.0 ms | 118.0 ms | 56.9 ms | 0.63x | 0.31x |

Modal's strongest provider-default results are action transport. Four move/click pairs are 34.99x
faster than Daytona and 24.20x faster than E2B because the daemon accepts the sequence as one batch.
Modal also leads both providers on one move/click and full screenshot. E2B leads startup and command
echo; Daytona leads startup, typing, and command echo. These losses remain part of the reference
rather than being hidden.

## Startup Scope

| Provider | Sample 1 | Sample 2 | Sample 3 | p50 |
| --- | ---: | ---: | ---: | ---: |
| Modal | 19231.1 ms | 10960.9 ms | 13040.4 ms | 13040.4 ms |
| Daytona | 10640.8 ms | 10555.5 ms | 10516.9 ms | 10555.5 ms |
| E2B | 1506.1 ms | 1309.8 ms | 1325.3 ms | 1325.3 ms |

This is a product-level create-to-first-successful-screenshot comparison, not a normalized
infrastructure boot comparison. Modal uses a sandbox image plus daemon and attested-ingress startup;
Daytona uses its managed default snapshot; E2B uses its desktop template snapshot. E2B is 9.84x
faster than Modal at p50 under these provider-default startup models.

## Screenshot Scope

`screenshot_full` measures each provider's default binary screenshot path. It supports API-latency
claims, not identical-pixel visual or encoder claims, because provider desktop contents differ.
The final observed payloads were 391574 raw PNG bytes for Modal, 115910 decoded PNG bytes for
Daytona, and 15308 raw PNG bytes for E2B. Daytona's separate base64 transport was 154548 bytes.
Use synthetic-canvas cases for normalized visual comparisons.

## Cost Scope

Public-rate estimates covered measured resource lifetime through cleanup. Daytona estimated
`$0.001690` and E2B estimated `$0.006521` for this run. Modal's estimate remained `partial` because
the resolved CPU and memory allocations were unavailable in the artifact, so no Modal total is
claimed. These estimates are not delayed provider billing reconciliation.

## Status History

| Result | Status | Reason |
| --- | --- | --- |
| 2026-07-18 clean refactor rerun | Current reference | Modular committed harness, exact 3x lifecycle sampling, all gates passed |
| 2026-07-18 dirty corrected run | Superseded candidate | Corrected behavior, but run from an uncommitted review tree |
| 2026-07-18 v3 | Rejected | External lifecycle included teardown; Daytona bytes counted base64 transport |
| 2026-07-18 v1 | Rejected | Modal lifecycle had one sample while Daytona and E2B had three |
| 2026-05-17 provider compare | Historical diagnostic | Older ingress and screenshot semantics |

Do not combine rows from different statuses or workloads into one provider ranking.
