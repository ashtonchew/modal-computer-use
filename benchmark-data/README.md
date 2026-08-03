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
  `tests/benchmarks/test_subprocess_runner_evidence.py`. Its arms compare to each other only. Do not
  restate them against a run with a different runner path, requested resources, or command payload.
- The 2026-07-31 one-core rerun is the single exception to the rule above, and it is a narrow one.
  It carries the 2026-07-30 figures under `comparison_baseline`, bound to that artifact by SHA-256
  so they cannot drift, and it carries them only so both runs can be read side by side. The pair
  varies date and requested shape at once, so it is not a shape ablation, no causal claim about
  cores or memory follows from it, and neither run supersedes the other.
- Caller-placement evidence has no generator and no sanitizer. It is assembled by hand from the
  ignored raw draws, binds each draw by SHA-256, and is guarded by pinning assertions in
  `tests/benchmarks/test_provider_artifacts.py`. Both of its arms share one ingress, so no
  measurement key may name a transport; the ingress is recorded once under
  `configuration.observed`. Two draws are recorded, one pinned and one replication, and they are
  never averaged.

## Provider evidence

The current warm-operation presentation uses:

- [`provider-compare-coordinate-command-2026-07-30.json`](provider-compare-coordinate-command-2026-07-30.json),
  the sanitized provider-default input;
- [`modal-optimized-provider-2026-07-30.json`](modal-optimized-provider-2026-07-30.json), the
  allowlisted Modal optimized input with exact numeric samples.

The separate [`modal-observation-2026-07-30.json`](modal-observation-2026-07-30.json) retains the
current action-to-frame observation evidence. See the
[warm-operation report](../docs/benchmark-results-2026-07-30-warm-paths.md) and
[benchmarking guide](../docs/benchmarking.md) for current interpretation and workflow guidance.

### Archived July 26 combined report

The archived combined report remains bound to these immutable inputs:

- [`provider-compare-coordinate-command-2026-07-26.json`](provider-compare-coordinate-command-2026-07-26.json);
- [`modal-optimized-provider-2026-07-26.json`](modal-optimized-provider-2026-07-26.json);
- [`modal-observation-2026-07-26.json`](modal-observation-2026-07-26.json);
- [`provider-results-2026-07-26.json`](provider-results-2026-07-26.json).

The combined renderer, validator, and artifact-pinning test remain to verify this evidence set.
They are not the current provider publication workflow.

Older tracked artifacts keep their original paths because reports, validators, and provenance
records refer to them. Location does not indicate status. See the corresponding report's archive
notice for its disposition.
