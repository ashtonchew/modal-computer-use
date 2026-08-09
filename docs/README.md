# Documentation

Use the [public documentation](https://modal-computer-use.mintlify.app) for installation, tasks,
integrations, operations, benchmark summaries, and API reference.

This directory owns engineering contracts, benchmark evidence, development procedures, release
procedures, and concise pointers for public guides.

## Start and operate

- [Local development](local-development.md): install the development environment and run a local
  daemon.
- [Modal deployment](modal-deployment.md): create, attach, recover, and clean up Modal Sandboxes.
- [Modal optimization](modal-optimization.md): place the caller, reuse a trajectory connection,
  reduce round trips, and measure workload-specific tradeoffs.
- [Configuration](configuration.md): daemon settings, defaults, and environment variables.
- [Troubleshooting](troubleshooting.md): diagnose readiness, X11, adapter, artifact, and deployment
  failures.

## Use the SDK

- [API](api.md): synchronous and native-async Python surfaces, ownership, and daemon behavior.
- [Version 2 migration](migration-v2.md): hard-cutover changes, `computer.step()` replacements,
  screenshot payload migration, and rollback.
- [OpenAPI schema](openapi.json): generated HTTP request and response schemas.
- [Artifacts](artifacts.md): artifact paths, persistence, and synchronization.
- [Trace and replay](trace-replay.md): capture, validate, and replay action traces.
- [Experimental visual-change observation](experimental-visual-change-observation.md): Alpha
  action-to-first-change semantics and limits.
- [OpenAI adapter](openai-adapter.md) and [Anthropic adapter](anthropic-adapter.md): translate
  provider actions without moving model loops into the core package.

## Understand the system

- [Architecture](architecture.md): component boundaries and ownership.
- [Performance](performance.md): stable latency mechanisms and tuning guidance.
- [Security](security.md): runtime threat model and operational controls.
- [Glossary](glossary.md): project terms.
- [Product specification](spec/product-spec.md): canonical architecture, maturity, safety, and
  product-contract history.

## Benchmark

- [Benchmarking](benchmarking.md): run benchmarks and interpret, retain, or publish their output.
- [Warm-operation results, 2026-07-30](benchmark-results-2026-07-30-warm-paths.md): eligible p50,
  p95, configuration, and provenance for the README comparison.
- [Optimized-default promotion results, 2026-08-08](benchmark-results-2026-08-08-optimized-default.md):
  eligible same-topology evidence for the SDK cutover and the precise meaning of the historical
  47 ms arithmetic figure.
- [Standard Image lifecycle results, 2026-08-08](benchmark-results-2026-08-08-image-lifecycle.md):
  paired inline-recipe and exact managed-Image evidence from Sandbox creation through the first
  valid frame.
- [Computer Step promotion results, 2026-08-08](benchmark-results-2026-08-08-computer-step.md):
  100-pair same-topology evidence for the fused `computer.step()` default.
- [Weighted input-capacity results, 2026-08-08](benchmark-results-2026-08-08-input-capacity.md):
  three passing same-runtime gates for the 100-token refill and 400-token burst.
- [Archived benchmark evidence](archive/README.md#archived-benchmarks): superseded, rejected,
  diagnostic, and historical reports and protocols.

Each dated report states its evidence status, measurement boundaries, and provenance. A filename or
date alone does not indicate status. The [benchmark data policy](../benchmark-data/README.md) defines
tracked artifact eligibility, and the [archive policy](archive/README.md) defines archive categories.

## Contribute and release

- [Contributing guide](../CONTRIBUTING.md): report issues, propose changes, run local checks, and
  submit pull requests.
- [Code of conduct](../CODE_OF_CONDUCT.md): follow the community behavior and reporting policy.
- [Release checklist](release-checklist.md): verification, packaging, protected smoke tests, and
  release review.
- [Version 2 release candidate](v2-release-candidate.md): release identity, package and runtime
  order, gates, and selected rollback.
- [Hosted documentation release system](hosted-documentation-release.md): source ownership,
  previews, production publication, version navigation, and rollback.
- [Drafts](drafts/README.md): write, preview, and export long-form articles built on tracked
  benchmark evidence.
- The repository [README](../README.md) is the short project introduction and first-run path.
- The [examples index](../examples/README.md) identifies the complete primary trajectory and the
  low-level compatibility examples.
