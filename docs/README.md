# Documentation

Use this page to find the maintained guide for a task. Dated benchmark reports record evidence, and
specifications preserve design history.

## Start and operate

- [Local development](local-development.md): install the development environment and run a local
  daemon.
- [Modal deployment](modal-deployment.md): create, attach, recover, and clean up Modal Sandboxes.
- [Configuration](configuration.md): daemon settings, defaults, and environment variables.
- [Troubleshooting](troubleshooting.md): diagnose readiness, X11, adapter, artifact, and deployment
  failures.

## Use the SDK

- [API](api.md): Python SDK and daemon behavior.
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
- [Active specification](spec/modal_computer_use_spec_v8.md): canonical architecture, maturity,
  safety, and product-contract
  history.

## Benchmark

- [Benchmarking](benchmarking.md): run benchmarks and interpret, retain, or publish their output.
- [Current provider results](benchmark-results-2026-07-26-provider-results.md): eligible
  provider-default, Modal-optimized, and Modal-only experimental evidence.
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
- [Drafts](drafts/README.md): write, preview, and export long-form articles built on tracked
  benchmark evidence.
- The repository [README](../README.md) is the short project introduction and first-run path.
