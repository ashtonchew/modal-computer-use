# Changelog

## Unreleased

- Replaced the daemon's abrupt 20-action rolling window with a daemon-local token bucket. The
  default refills 200 normalized input-work tokens per second and permits a 400-token burst.
  Action arrays reserve their complete recursive cost before mutation, so rate limiting cannot
  interrupt a validated batch halfway through. Transient limits return `429` with precise
  `retry_after_ms` and an integer `Retry-After` header.

## 2.0.0 - 2026-08-08

- Made the primary SDK path an async, explicitly placed Modal trajectory: one owner creates the
  desktop, passes a versioned session handle to an application-owned Modal Function, and the
  Function enters one `borrow_async()` context for the whole model trajectory.
- Made placement fail closed before lease acquisition or desktop mutation. The Function and
  Sandbox must declare the same exact region, and the runtime verifies the observed Function and
  target placement instead of silently falling back to an external caller.
- Made inline `screenshots.full()` requests use the raw binary HTTP representation through the
  trajectory's pooled async client. The method still returns a semantic `Screenshot`; artifact and
  automatic storage continue to use the structured JSON route, and `full_bytes()` remains
  available as a low-level byte convenience.
- Added `computer.step()` to the borrowed sync and async Interfaces. One step sends an ordered
  action batch and returns a `ComputerStepResult` with the action result, immediate post-action
  screenshot, and timing metadata through the `computer-step-envelope-v1` protocol capability.
  The retained low-level action-only Interface still sends one `actions.run(...)` HTTP batch.
- Added complete, authenticated metadata to raw screenshot responses and strict SDK validation for
  image type, dimensions, size, digest, timestamp, coordinate space, cursor state, timing, and
  capture backend. A malformed response fails once without a JSON retry.
- Added lazy, Base64URL JSON serialization for byte-backed `Screenshot` models. `as_bytes()` and
  `to_base64()` keep their existing byte and standard-Base64 behavior.
- Reused one attested-tunnel client and authentication state for readiness, protocol preflight,
  lease acquisition, repeated screenshots and action batches, and lease release. Requests still
  cross authenticated Modal ingress.
- Required daemon protocol capabilities for binary screenshot metadata, trajectory leases, and
  operation receipts. Package versions do not decide compatibility when the tested protocol
  behavior is present.
- Kept persistent MSS/XShm screenshot capture and XTest/Xlib/XKB input sessions across warm
  requests. Native input synchronizes before success, all input state shares one lock, and fallback
  is allowed only before an event is emitted.
- Kept ordered model action arrays as one validated HTTP batch. A batch validates in full before
  mutation, runs in order, and stops on its first failure unless continuation is explicit.
- Kept command subprocess work on its private selector loop and clipboard ownership free of unused
  output pipes. Failed command errors no longer expose command output or backend messages.
- Corrected first-visual-change outcomes so verified unchanged observations differ from deadline
  timeouts. First visual change remains experimental and is not application readiness.
- Made owner, attached, borrowed, failed, timed-out, and cancelled cleanup ordering explicit.
  Cleanup aggregates stage failures without masking the primary application or cancellation error.
- Added inspectable, secret-free cost and placement resolution. Region, Function and Sandbox
  resources, images, timeouts, scaling limits, and warm capacity remain explicit; warm capacity is
  off by default.
- Updated the OpenAI and Anthropic examples so their application-owned model loops run inside the
  placed Function, borrow once, preserve each ordered action array as one batch, and use the normal
  semantic screenshot method.
- Preserved REST and JSON daemon routes for local execution, direct-daemon access, idempotency,
  debugging, and compatibility. The release does not add an `optimized` flag or a hidden legacy
  default.

The historical 37.25 ms screenshot result used the raw binary endpoint, persistent capture, a
reused pooled HTTP connection, and a same-region Function/Sandbox topology. The article's 47.10 ms
screenshot-plus-click number is arithmetic over separate warm medians, not a measured fused turn.
Version 2.0.0 does not promise that latency for `computer.step()`.

The separate 2026-08-08 Computer Step promotion run retained 100 interleaved pairs and measured
the fused path at 44.29 ms p50 and 52.57 ms p95. The prior two-request path measured 47.14 ms and
58.22 ms. The paired bootstrap interval showed a 2.65–4.66 ms improvement, with no failures,
retries, replacement samples, or cleanup survivors.

Migrate the primary SDK path as follows:

| Version 1 pattern | Version 2 default | Required migration |
| --- | --- | --- |
| A laptop or other external process owns `ComputerSandbox.create()` and calls the daemon for every model turn. | An async owner creates the desktop once and sends its versioned `ComputerSessionHandle` to an explicitly placed, application-owned Modal Function. | Move the model trajectory into that Function. Keep provider SDK imports and model calls in application code, not in core. |
| Each remote operation creates or attaches to its own desktop/client context. | The Function calls `borrow_async(handle)` exactly once around the whole trajectory. | Hoist borrowing outside the model-turn loop and release it only after the trajectory ends. |
| Region may be absent or broad, and a mismatched caller can continue over ingress. | Primary `AsyncComputerSandbox.create()` requires an explicit environment and exact region such as `us-west-2` before allocation; the Function, observed Function runtime, and Sandbox must then match that region. | Select an exact region for both resources and make environment, CPU, memory, image, timeout, retries, scaling limits, and capacity inspectable. Use `create_unplaced()` only for an intentional low-level path without handoff. |
| Async creation may use tunnel ingress, control VNC, or warm-pool tagging even though those modes cannot produce the default handoff. | Primary `AsyncComputerSandbox.create()` rejects these modes before Modal work. | Use `create_unplaced()` for an intentional low-level owner, or select attested-tunnel/connect ingress, off/view-only VNC, and default ownership tags. |
| `screenshots.full(storage="inline")` returns a JSON/base64-backed `Screenshot`. | The same semantic method uses the raw binary response and returns `Screenshot(bytes=...)`. | Prefer `as_bytes()` or `to_base64()` instead of reading `data_base64` directly. JSON serialization of `bytes` uses Base64URL. |
| A provider loop calls `actions.run(...)` and then `screenshots.full()` after each model action array. | The borrowed `computer.step(...)` Interface sends the ordered array and returns one `ComputerStepResult` with `actions`, `screenshot`, and `timing`. | Replace the two calls with one step. Use its immediate post-action `screenshot` for the next model turn. Do not treat the frame as application readiness or replay a step after a possible dispatch. |
| Provider examples may send model actions one at a time. | One ordered model `actions[]` becomes one `computer.step(...)` request. | Preserve model order, choose continuation explicitly, use the returned immediate screenshot, and never replay automatically after possible dispatch. |
| Cleanup commonly relies on the outer owner context only. | The borrowed client and lease close first; the owner then detaches or terminates according to ownership. | Keep the owner alive until the placed Function reaches a terminal result, including cancellation cleanup. |
| The main quickstart presents direct namespace calls as the performance path. | The placed owner-to-handle-to-Function trajectory is the primary documented path. | Use the low-level primitive SDK only when local, direct REST, idempotency, debugging, or compatibility behavior is intentional. |

Version 1.1 clients remain supported against the version 2 daemon for the retained v1 REST and JSON
contracts. The version 2 optimized trajectory requires the capabilities it verifies during
preflight. Do not infer support from package major versions alone.

## 1.1.0 - 2026-08-03

- Added a typed native-async interface for existing daemons, including cached namespaces,
  readiness, persistent action and observation connections, and connection-only cleanup.
- Added native-async Modal creation and attachment through lazy `AsyncComputerSandbox` contexts.
  Created contexts own termination, attached contexts detach only, and cancelled partial creation
  reclaims allocated resources with Modal-native `.aio` operations.
- Made `attach_or_create(name=...)` the single named-Sandbox acquisition contract for synchronous
  and native-async callers. Modal names arbitrate one live allocation, run IDs remain correlation
  metadata, and named creation conflicts attach to the winning compatible Sandbox.
- Defined ownership-aware Sandbox cleanup for created, attached, reused, local, and explicitly
  detached clients; failed creation now reclaims allocated resources without letting cleanup mask
  the original error.
- Made attachment selectors exact and limited reuse fallback to genuine Modal not-found results.
  Creation and reuse now copy caller configuration before generating or overriding a run ID.
- Moved the canonical product specification to the stable `docs/spec/product-spec.md` path and
  removed superseded specification revisions and branch-owned article working files from `main`.
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
- Removed the obsolete `types-Pillow` development stub package now that Pillow ships inline typing;
  retained direct `h2` because the daemon's HTTP/2 runtime imports it.
- Removed superseded numbered quickstarts and repaired the retained local input, existing-Sandbox
  attach, local-daemon launcher, and Modal Volume v2 persistence examples.
- Removed the benchmark `--json` compatibility flag; benchmark commands already emit JSON, so omit
  the flag without changing output format.
- Retired two pre-release internal import paths: use `modal_computer_use.tracing` instead of
  `modal_computer_use.daemon.trace`, and `modal_computer_use.daemon.supervisor` instead of
  `modal_computer_use.daemon.desktop.processes`.

The v1.1 daemon requires a v1.1 SDK for the default attested-tunnel flow. Upgrade SDK and daemon
together. The v1.0.0 tag was a private source milestone, not a GitHub Release or PyPI distribution.
Version 1.1.0 is the first public GitHub Release and PyPI distribution.

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
