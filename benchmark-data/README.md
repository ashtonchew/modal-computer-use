# Benchmark Data

This directory contains tracked, sanitized, normalized benchmark references.

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
