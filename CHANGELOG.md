# Changelog

## Unreleased

- Added the canonical v8 product specification, archived v7, and classified stable, experimental,
  benchmark-only, and application-owned surfaces against the 1.0.0 source state.
- Updated the locked Modal SDK to 1.5.3 and explicitly scoped daemon Connect Tokens to port 8080.
- Reworked the project introduction with source installation and local and Modal quickstarts.
- Added a documentation map, a configuration reference, and link and configuration checks.
- Added a security policy that requires private vulnerability reporting before a public release,
  and clarified runtime security guidance.
- Updated package metadata to use PEP 639 license fields and well-known project URLs.
- Added the Modal optimized lifecycle benchmark, eligibility-gated tracked provider evidence, and a
  current five-provider report.

## 0.1.0

- Daemon-first Modal Sandbox computer-use primitives with local and Modal SDK paths.
- Typed daemon routes for health/readiness, mouse, keyboard, clipboard, screenshots, recordings,
  display/windows, artifacts, actions, tracing, browser/apps, processes, and lifecycle.
- Provider-neutral OpenAI, Anthropic, and generic action adapters without provider SDK imports in
  core modules.
- Modal attach/reuse, manager cleanup, noVNC opt-in/view-only smoke coverage, filesystem snapshot
  delegation, optional browser/GPU profiles, and example-level warm-pool patterns.
- Release readiness artifacts: checked-in OpenAPI schema, mock-local benchmark report, security
  scans, Modal boundary tests, and live Modal smoke coverage.
