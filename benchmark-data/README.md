# Benchmark Data

This directory contains tracked benchmark references that have been sanitized and normalized.

- Commit only artifacts that pass their repository validator and provenance gates.
- Keep raw provider responses, credentials, preregistrations, rejected runs, and replay evidence in `benchmark-results/`.
- Do not write benchmark output to the repository root.
- Modal V2 candidate evidence is promotable only through
  `scripts/sanitize_modal_v2_candidate_benchmark.py`; pilot failures, rejected runs, and partial
  full runs remain ignored under `benchmark-results/modal-v2-candidate-2026-07-19/`.
- A placement matrix with `measurement_performed: false` may be tracked as descriptive capability
  evidence. It is not a candidate performance result and cannot contain latency or ratio claims.
- Modal optimized-frontier evidence is promotable only through
  `scripts/sanitize_modal_optimized_frontier_benchmark.py`. Its V1/V2 ratio must remain labeled
  `optimized-frontier-path-ratio` and `descriptive-best-system`; it cannot be relabeled as a backend
  causal speedup.
- Subprocess-runner A/B evidence has no generator. It is assembled by hand from the ignored raw arms,
  binds each arm by SHA-256, and is guarded by pinning assertions in
  `tests/benchmarks/test_provider_artifacts.py`. Its arms compare to each other only; do not restate
  them against a run with a different runner path, requested resources, or command payload.
- Caller-placement evidence has no generator and no sanitizer. It is assembled by hand from the
  ignored raw draws, binds each draw by SHA-256, and is guarded by pinning assertions in
  `tests/benchmarks/test_provider_artifacts.py`. Both of its arms share one ingress, so no
  measurement key may name a transport; the ingress is recorded once under
  `configuration.observed`. Two draws are recorded, one pinned and one replication, and they are
  never averaged.

## Combined provider report inputs

The current provider evidence is:

- [`provider-compare-coordinate-command-2026-07-26.json`](provider-compare-coordinate-command-2026-07-26.json),
  the sanitized provider-default input;
- [`modal-optimized-provider-2026-07-26.json`](modal-optimized-provider-2026-07-26.json), the
  allowlisted Modal optimized input with exact numeric samples;
- [`modal-observation-2026-07-26.json`](modal-observation-2026-07-26.json), the allowlisted Modal
  observation input with exact numeric samples;
- [`provider-results-2026-07-26.json`](provider-results-2026-07-26.json), the combined report
  artifact.

For a new run, use this file flow:

| Role | Path | Tracking |
| --- | --- | --- |
| Raw provider-default run | `benchmark-results/candidates/provider-default.json` | Ignored |
| Sanitized provider-default run | `benchmark-data/provider-default.json` | Tracked |
| Raw Modal optimized provider run | `benchmark-results/modal-optimized.json` | Ignored |
| Sanitized Modal optimized run | `benchmark-data/modal-optimized.json` | Tracked |
| Raw Modal observation runner-only run | `benchmark-results/modal-observation.json` | Ignored |
| Sanitized Modal observation run | `benchmark-data/modal-observation.json` | Tracked |
| Combined provider results | `benchmark-data/provider-results.json` | Tracked |

Generate `provider-default.json` with `scripts/sanitize_provider_benchmark.py`. Generate both
allowlisted Modal inputs with `scripts/sanitize_modal_provider_inputs.py`, then generate
`provider-results.json` with `scripts/sanitize_provider_results.py`. The combined artifact binds all
three tracked inputs by SHA-256 and records the evidence-harness and report-source revisions. See
[`docs/benchmarking.md`](../docs/benchmarking.md) for the commands and reporting policy.

Older tracked artifacts keep their original paths because reports, validators, and provenance
records refer to them. Location does not indicate status. See the corresponding report's archive
notice for its disposition.
