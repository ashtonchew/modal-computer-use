# Changelog

## Unreleased

- Moved the canonical product specification to the stable `docs/spec/product-spec.md` path and
  removed superseded specification revisions and branch-owned article working files from `main`.

## 1.1.0 - 2026-07-31

- Hardened daemon authentication to fail closed, made unauthenticated local mode explicit, blocked
  minted tunnel sessions from reminting, and added optional non-evicting session capacity.
- Added non-cacheable HTTP responses, 16 MiB HTTP/WebSocket defaults, global WebSocket admission
  caps, bounded nested actions, command arguments, drag paths, and key collections.
- Made artifact quota commits atomic, stopped active recordings during shutdown, removed hashes
  from sensitive redaction markers, and excluded VNC passwords from config repr and serialization.
- Scoped Modal create, attach, reuse, list, and cleanup behavior to the owning app. Added an
  explicit legacy unscoped attach option without permitting bulk legacy cleanup.
- Updated the frozen security-relevant dependency set, including Starlette, Pillow, AnyIO, h2,
  WebSockets, OpenAI, and Anthropic.

The v1.1 daemon requires a v1.1 SDK for the default attested-tunnel flow. Upgrade SDK and daemon
together. The v1.0.0 tag was a private source milestone, not a GitHub Release or PyPI distribution.
Version 1.1.0 is the first public GitHub Release.

## 1.0.0 - 2026-07-31

- Added the canonical v8 product specification, archived v7, and classified stable, experimental,
  benchmark-only, and application-owned surfaces against the 1.0.0 source state.
- Updated the locked Modal SDK to 1.5.3 and explicitly scoped daemon Connect Tokens to port 8080.
- Reworked the project introduction with source installation and local and Modal quickstarts.
- Added a documentation map, a configuration reference, and link and configuration checks.
- Added a security policy that requires private vulnerability reporting before a public release,
  and clarified runtime security guidance.
- Added contribution guidance, a code of conduct, issue forms, a pull request template, and monthly
  dependency updates.
- Updated package metadata to use PEP 639 license fields and well-known project URLs.
- Marked the distribution as typed, added downstream type-consumer checks, and tightened the
  bounded mypy configuration.
- Added the Modal optimized lifecycle benchmark, eligibility-gated tracked provider evidence, and a
  current five-provider report.
- Removed legacy root benchmark output, added a repository hygiene check, and separated provider
  defaults from optimized Modal results in the current report.
- Classified the July 19 Modal optimization harness as commit-pinned historical evidence; current
  measurements use the maintained benchmark workflows.
- Removed the legacy July 19 Modal optimization runner, sanitizer, execution modules, and tests while
  preserving its tracked JSON artifacts and archived reports as commit-pinned provenance.

This release removes compatibility-only names without a deprecation window. Update imports before
you adopt version 1.0.0:

| Removed compatibility name | Canonical replacement | Required migration |
| --- | --- | --- |
| `SandboxManager` | `ComputerSandboxManager` | Replace `from modal_computer_use import SandboxManager` and rename constructor and type references; behavior and arguments are unchanged. |
| `modal_workspace_billing_report` | `modal_billing_report` | Import from `modal_computer_use.sandbox`; omit `environment_name` for the previous workspace-scoped behavior or pass an environment name for environment-scoped billing. |
| `XTestPointerController` | `X11InputSession` | Replace the import from `modal_computer_use.daemon.desktop.xtest`; the `display=` constructor and pointer methods are unchanged, and the canonical session also owns keyboard input. |
| `browser_image` | `default_image` | Call `default_image(profile="browser" or "browser-gpu", browser=..., browser_prewarm=True)` from `modal_computer_use.image`. |
| `modal_computer_use.transports.local.HTTPTransport` | `modal_computer_use.transports.HTTPTransport` | Import the canonical transport from its package export or from `modal_computer_use.transports.http`. |
| `transform_point` | `CoordinateSpace.to_desktop` | Keep the point unchanged when no coordinate space applies; otherwise call `coordinate_space.to_desktop(point)` directly. |
| `sandbox_ref_from_values` | `SandboxRef.model_validate` | Pass the previous value mapping directly to `SandboxRef.model_validate(...)`. |
| `ProcessExecutionError` | `ActionResult` and `DaemonHTTPError` | Inspect `ActionResult.ok` for a completed command failure; catch `DaemonHTTPError` for a failed daemon request. |
| `ErrorInfo` | `DaemonHTTPError` | Read `code`, the exception message, and `details` from `DaemonHTTPError`, or retain an application-owned error model. |
| `modal_computer_use.adapters.anthropic.schemas.AnthropicComputerAction` | `AnthropicAdapter.normalize` | Pass a provider action mapping to `AnthropicAdapter.normalize`; consume its canonical `ComputerAction`-shaped mapping instead of importing the unused provider `TypedDict`. |
| `BrowserKind` | `BrowserConfig` | Use `BrowserConfig(kind="firefox" or "chromium")` for validated public configuration; deep desktop modules no longer expose a separate alias. |

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
