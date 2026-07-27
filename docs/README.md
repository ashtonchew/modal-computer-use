# Documentation

Use this page to find the maintained answer for a task. Dated benchmark reports and specifications
record evidence or design history; they do not replace the current guides.

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
- [Active specification](spec/modal_computer_use_spec_v7.md): detailed shipped design and contract
  history.

## Benchmark

- [Benchmarking](benchmarking.md): run benchmarks and interpret, retain, or publish their output.
- [Provider comparison report](benchmark-results-2026-07-24-tzafon.md): provider-default and Modal
  optimization evidence with its eligibility stated in the report.
- [Native X11 input evidence](benchmark-results-2026-07-23-native-x11-input.md): implementation
  validation for the native input path.
- [Modal V2 candidate methodology](modal-v2-candidate-benchmark.md) and
  [optimized-frontier methodology](modal-optimized-frontier-benchmark.md): experiment-specific
  protocols and eligibility gates.

Dated reports own their evidence status, measurement boundaries, and provenance. Do not infer status
from a filename or date. The [benchmark data policy](../benchmark-data/README.md) defines tracked
artifact eligibility, and the [archive policy](archive/README.md) defines archive categories.

## Contribute and release

- [Release checklist](release-checklist.md): verification, packaging, protected smoke tests, and
  release review.
- The repository [README](../README.md) is the short project introduction and first-run path.
