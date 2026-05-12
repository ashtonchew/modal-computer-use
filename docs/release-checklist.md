# Release Checklist

Run this checklist before publishing a release or opening a production-readiness PR.

## Verification

- `uv run ruff check .`
- `uv run mypy src`
- `uv run pytest`
- `uv run computer-use benchmark report --mock-local --iterations 5 --output benchmark-report.json`

## Architecture Boundaries

- Core imports without Modal, OpenAI, Anthropic, or provider credentials.
- `src/` has no OpenAI or Anthropic SDK imports.
- `src/` has no `modal.NetworkFileSystem` usage.
- Modal-specific SDK calls remain isolated to `sandbox.py`, `image.py`, `manager.py`, and `registry.py`.
- Provider adapters translate provider-returned actions only; they do not call provider APIs or own prompts, policies, credentials, or model loops.

## Security

- noVNC is off by default and any enabled noVNC URL is treated as a secret.
- Examples and docs do not print bearer tokens, noVNC URLs, artifact URIs, raw artifact paths, recording bytes, screenshot bytes, typed text, clipboard text, raw command strings, stdout, or stderr.
- Trace validation and replay dry-runs handle redacted typed text and provider provenance.
- Artifact traversal, encoded traversal, absolute paths, and symlink escapes are covered by tests.
- Recording examples report bounded metadata only.

## Compatibility

- OpenAI and Anthropic fixture tests pass and unknown provider actions fail closed by default.
- Modal boundary tests cover `Sandbox.create`, connect tokens, readiness probes, encrypted noVNC ports, tags/listing, attach/reuse, cleanup, and filesystem snapshot delegation.
- Modal smoke tests remain marked and skipped unless credentials are explicitly available.

## Performance

- Benchmark output distinguishes `measured`, `not_measured`, and failed cases.
- Browser prewarm and GPU guidance is documented as optional and measured-workload dependent.
- Warm-pool and snapshot examples remain example-level; core lifecycle does not auto-create pools, snapshots, public tunnels, or Volumes.
