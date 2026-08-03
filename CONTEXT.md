# Modal Computer Use

This context defines the language for a daemon-first primitive layer that controls Modal Sandboxes and can be measured without becoming a provider-comparison product.

## Language

**Adapter Benchmark**:
A deterministic benchmark of action normalization or in-process action execution that does not call provider APIs or create provider sandboxes.
_Avoid_: Provider benchmark, live provider benchmark

**Benchmark Surface**:
An SDK-owned code path that a benchmark can measure, such as daemon HTTP routes, Modal exec, or an adapter normalization path.
_Avoid_: Provider, vendor, competitor

**External Provider Benchmark**:
A live benchmark that calls a third-party provider SDK, creates third-party provider resources, or reports cross-provider performance and cost.
_Avoid_: Adapter benchmark, SDK benchmark

**Sandbox Exec Surface**:
An opt-in raw Modal `Sandbox.exec` benchmark baseline used to compare daemon overhead against direct command execution inside the same Modal Sandbox.
_Avoid_: modal-exec, provider, primitive API

## Relationships

- An **Adapter Benchmark** may run in the SDK repository because adapters are provider-shape translators, not provider API clients.
- A **Benchmark Surface** belongs to this SDK and can be measured without treating another service as the benchmark subject.
- An **External Provider Benchmark** may live in this repository as an optional, credential-gated,
  benchmark-only workflow. Its provider dependencies stay outside core, and it is not a stable SDK
  product API.
- A **Sandbox Exec Surface** is a Modal-native benchmark baseline, not the SDK's preferred primitive API.
- The public SDK benchmark command is named `benchmark sdk`; documentation may describe the measured entries as **Benchmark Surfaces**.
- `benchmark compare` is a maintained benchmark-only workflow. `benchmark provider-results`
  remains available to verify archived combined evidence. Neither is a public SDK compatibility
  contract.
- Sanitized, revision-bound provider evidence may be tracked in `benchmark-data/`. Credentials,
  private raw results, endpoints, and provider resource identifiers remain untracked.

## Example dialogue

> **Dev:** "Should Daytona and E2B live runs be part of the SDK benchmark command?"
> **Domain expert:** "Keep them in the optional `benchmark compare` workflow, not `benchmark sdk`.
> They are **External Provider Benchmarks**, so they remain benchmark-only and outside the stable SDK
> compatibility contract."

## Flagged ambiguities

- "provider benchmark" was used for both no-API adapter measurements and live third-party sandbox comparisons — resolved: use **Adapter Benchmark** and **External Provider Benchmark**.
- "compare" was used for both SDK-owned paths and live vendor comparisons — resolved: use
  **Benchmark Surface** for SDK-owned paths and `benchmark compare` only for the optional external
  provider workflow.
