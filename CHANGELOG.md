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
- Classified the July 19 Modal optimization harness as commit-pinned historical evidence; current
  measurements use the maintained benchmark workflows.

This pre-release cutover removes compatibility-only names without a deprecation window. Update
imports before adopting the cutover:

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
