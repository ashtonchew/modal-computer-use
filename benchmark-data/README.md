# Benchmark Data

This directory contains tracked, sanitized, normalized benchmark references.

- Commit only artifacts that pass their repository validator and provenance gates.
- Keep raw provider responses, credentials, preregistrations, rejected runs, and replay evidence in `benchmark-results/`.
- Do not write benchmark output to the repository root.
- Modal V2 candidate evidence is promotable only through
  `scripts/sanitize_modal_v2_candidate_benchmark.py`; pilot failures, rejected runs, and partial
  full runs remain ignored under `benchmark-results/modal-v2-candidate-2026-07-19/`.
