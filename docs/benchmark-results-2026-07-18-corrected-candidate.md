# Corrected Provider Benchmark Candidate, 2026-07-18

This is a successful candidate run of the corrected provider-default SDK harness. It is not a
current reference because it ran from the uncommitted isolated review branch. The tracked JSON is
the canonical result; this page records interpretation and reproduction details only. This neutral
external-caller profile establishes correctness and provenance and is not the future
`modal-platform-optimized` profile described in `docs/performance.md`.

## Provenance

- Tracked sanitized report:
  `benchmark-data/provider-compare-2026-07-18-corrected-candidate.json`
- Raw untracked report:
  `benchmark-results/candidates/provider-compare-live-20260718-corrected.json`
- Raw report SHA-256: `a93b9b8abaa3ba56fd096fb434c600074106326947714a43f0e30fc0ad0b4912`
- Base commit: `e0818f14ddd1cda38fcbaf6053801d1800116cde`
- Harness diff SHA-256: `0e0bcf1ff7d4adb79b271889a7fcbbfa43aded4ed788cc1e17a807dcd0cd3b29`
- Artifact status: `candidate`; harness state: `dirty`; sanitizer version: `1`
- Result: `ok=true`; all provider, cleanup, and verification gates passed

Each provider has one warmup plus three independent measured
`product_create_to_first_screenshot` samples. Cleanup is outside lifecycle timing. Only the final
measured Modal sandbox is reused for warm cases; every other lifecycle resource is cleaned first.
Cursor and typing readbacks passed for Modal, Daytona, and E2B. The deprecated
`cold_create_to_ready` alias remains emitted but is excluded from status and failure aggregation.

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
  --output benchmark-results/candidates/provider-compare-live-20260718-corrected.json \
  --json
```

The tracked artifact was generated and drift-checked with:

```bash
uv run python scripts/sanitize_provider_benchmark.py \
  benchmark-results/candidates/provider-compare-live-20260718-corrected.json \
  benchmark-data/provider-compare-2026-07-18-corrected-candidate.json \
  --raw-artifact-path benchmark-results/candidates/provider-compare-live-20260718-corrected.json \
  --harness-commit e0818f14ddd1cda38fcbaf6053801d1800116cde \
  --harness-diff-sha256 0e0bcf1ff7d4adb79b271889a7fcbbfa43aded4ed788cc1e17a807dcd0cd3b29 \
  --status candidate \
  --status-reason "Uncommitted isolated review branch; awaiting user approval." \
  --scope "provider-default SDK paths at 1024x768, one warmup and three measured iterations" \
  --check
```

`screenshot_full` measures each provider's default binary screenshot path. The artifact separately
records transport and decoded payload sizes where applicable. These are provider-default API claims,
not identical-pixel visual claims; use the synthetic-canvas cases for normalized visual comparison.
