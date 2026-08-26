# `modal-computer-use` canonical product specification

- **Status:** active specification for the published version 2 release line
- **Prepared:** 2026-07-30
- **Updated:** 2026-08-26
- **Revision:** v10, maintenance and launch gates for the `2.0.1` source state
- **Previous released baseline:** `v2.0.0`
- **Release identity:** `v2.0.1`
- **Repository:** `ashtonchew/modal-computer-use`
- **Python package:** `modal_computer_use`

Revision v10 retains the article-backed placed trajectory as the primary documented SDK
composition. It
keeps provider model loops application-owned and keeps the primitive SDK available for explicit
local, direct-daemon, REST/idempotency, debugging, and compatibility work.

Revision v10 supersedes all earlier product-specification revisions. It separates the primary
placed trajectory, supported low-level primitives, experimental observation semantics,
benchmark-only paths, and application-owned provider orchestration.

---

## 0. Authority, scope, and interpretation

This document is the canonical architectural and product-contract map. It explains which surfaces
are supported, where their detailed contracts live, and which repository evidence pins them.

The source-of-truth order is:

1. The implementation and tests define executable behavior.
2. [`docs/openapi.json`](../openapi.json) defines public HTTP request and response schemas.
3. Maintained guides under [`docs/`](../README.md) define operational procedures and detailed
   semantics.
4. This specification defines architecture, ownership, maturity, and cross-surface invariants.
5. Files under [`research/`](../../research/README.md) and benchmark reports are evidence or
   history, not product contracts. Earlier specification revisions remain available in Git history.

If this document conflicts with a checked-in schema or pinning test, the executable source wins and
the specification must be corrected. `MUST`, `MUST NOT`, `SHOULD`, and `MAY` describe requirements
for future changes. Present-tense statements describe the baseline above.

v10 does not make benchmark helpers, experimental Modal APIs, example control planes, or provider
model loops part of the stable core API.

## 1. Revision v10 baseline changes

The immediately preceding revision documented the hardened daemon and SDK shortly after their
initial implementation. The repository advanced by 371 commits from that revision's landing
(`3a30e69`) to the v8 baseline. Earlier specification revisions are available only in Git history.

| Area | v10 canonical state |
| --- | --- |
| Source version | The package, daemon, and OpenAPI report `2.0.1`; Python 3.12+ and `uv` are the maintained development baseline. The release tag is `v2.0.1`. |
| Modal SDK | The compatible line remains `modal~=1.5.3`. Every Connect Token is explicitly scoped to daemon port 8080. |
| Architecture | Modal-native orchestration and daemon-native primitive execution remain the defining boundary. Behavior has been localized by route, desktop controller, transport, or SDK namespace. |
| Input | A persistent native Xlib/XTest/XKB path is preferred. `xdotool` is a compatibility adapter. Fallback is allowed only before native emission starts. |
| Screenshots | Inline `screenshots.full()` uses the raw binary route and reconstructs a semantic, byte-backed `Screenshot`. Persistent MSS remains the production default. X11 shared-memory capture is available through explicit `x11-shm` or opt-in `auto`, but was not promoted after readiness-latency and display-restart gates failed. Captures report complete validated metadata. |
| Computer Step | Borrowed sync and async computers expose `computer.step()`. It sends one ordered action array and returns `ComputerStepResult.actions`, `.screenshot`, and `.timing` through the versioned `computer-step-envelope-v1` capability. The screenshot is an immediate post-action frame, not application readiness. |
| Transport | `attested-tunnel` is the default Modal ingress. An SDK-managed bootstrap bearer authorizes a short-lived daemon-issued bearer for the encrypted tunnel. HTTP/2 is opt-in. |
| Sessions | Sync and native-async daemon clients, persistent hot sessions, and observation WebSocket transports are implemented. |
| Observations | The transport and action primitives are supported. First-visual-change composition remains Alpha and explicitly experimental. |
| Handoff | An async owner passes a versioned `ComputerSessionHandle` to an application-owned Modal Function in the same exact requested region. The Function resolves fresh access and borrows one exclusive trajectory lease around the whole model loop. |
| Delivery ambiguity | Borrowed mutations use gap-free operation sequences and target-local durable receipts. The SDK resolves response loss without replaying a possibly applied mutation. |
| Recovery | A completed result may be followed by explicit read-only reobservation. Indeterminate target state is quarantined until owner acknowledgment. |
| Capacity | `ComputerSandboxManager` implements attach/reuse, cleanup, and owned warm-pool fill/claim/reconcile behavior. |
| Provider support | OpenAI and Anthropic adapters track current computer-use protocols while remaining translation-only layers. Provider SDKs are optional dependencies. |
| Application orchestration | A bounded application-owned run-gateway example documents admission, idempotency, deadlines, reconciliation, cancellation, and operator recovery. It is not a permissive production service. |
| Evidence | Provider comparisons and Modal optimization experiments use sanitized, provenance-bound artifacts and explicit eligibility states. Benchmark claims do not redefine the SDK contract. |

Earlier specification revisions remain available in Git history and are not current product
contracts.

## 2. Product definition

`modal-computer-use` turns a Modal Sandbox into a remotely controllable Linux desktop through a
typed, provider-neutral SDK and an in-sandbox daemon. It owns:

- desktop process supervision;
- mouse, keyboard, clipboard, screenshot, recording, display, window, browser, app, command, and
  artifact primitives;
- action validation, serialization, budgeting, tracing, and structured errors;
- Modal Sandbox create, attach, reuse, persistence, network, and cleanup integration;
- safe session handoff to deployed Modal Functions;
- transport and benchmark tooling needed to measure the primitive layer.

The package is not an autonomous-agent framework. Model loops, task authorization, end-user
identity, tenant policy, semantic completion, and application business state belong to callers or
examples.

### 2.1 Goals

1. Provide a high-quality computer-use primitive layer on Modal without hiding the underlying
   lifecycle and security decisions.
2. Keep primitive execution close to the desktop daemon and orchestration close to Modal.
3. Remain model- and provider-neutral.
4. Fail closed when readiness, identity, action validity, mutation outcome, or ownership is
   ambiguous.
5. Make latency and backend selection observable without making benchmark code part of the public
   contract.
6. Preserve reproducible artifacts, traces, and benchmark evidence without leaking secrets.
7. Support local deterministic testing, direct Modal SDK use, and application-owned hosted
   orchestration.

### 2.2 Non-goals

- A provider-owned model loop in core.
- Imports of `openai` or `anthropic` from core modules.
- A generic multi-tenant authorization service.
- DOM automation as a core primitive.
- Windows or macOS desktop support.
- Public raw-control endpoints without authentication.
- Automatic replay after a mutation might have reached the desktop.
- Automatic promotion of experimental Modal Sandbox APIs.
- `modal.NetworkFileSystem`; use Sandbox filesystem APIs and optional Volumes.
- A promise that visual change means semantic readiness or task completion.

### 2.3 Terms with intentionally separate scopes

- **Sandbox `run_id`** identifies one Sandbox lifetime in config, tags, lookup, and metadata.
- **Trajectory `run_id`** identifies one exclusive borrow and its receipt sequence. It is an
  application-generated value with a shorter scope and cannot be reused after sealing.
- **Action idempotency** is the ordinary batch API's bounded fingerprint cache.
- **Operation receipts** resolve one borrowed mutation after transport loss; they do not claim
  exactly-once execution.
- **Gateway idempotency** is application-store admission and replay fencing.
- **Readiness** means the daemon and desktop can accept primitives. **Visual change** means pixels
  differed. **Application readiness** and task success remain caller-owned semantic decisions.

## 3. Architectural invariants

### 3.1 Ownership boundary

The permanent rule is:

> **Modal-native orchestration; daemon-native primitive execution.**

Modal-specific calls remain isolated to `sandbox.py`, `image.py`, `manager.py`, and `registry.py`.
The daemon owns desktop-affecting behavior. Application and provider loops remain outside core.

### 3.2 Runtime layers

```text
application / provider loop / run gateway
                    |
          typed SDK and adapters
                    |
       HTTP or authenticated WebSocket
                    |
       in-sandbox FastAPI daemon :8080
                    |
 feature-local desktop controllers
                    |
 Xvfb + window manager + native X11 / compatibility tools
```

`ComputerSandbox` and `AsyncDaemonClient` expose typed namespaces. `DaemonClient` remains the
low-level synchronous HTTP facade used by `ComputerSandbox`. The daemon validates requests,
enforces budgets, serializes input, supervises processes, and records artifacts and traces.

The default desktop stack is Xvfb plus XFCE, with Openbox available as a lighter option. noVNC and
x11vnc are opt-in. The default display is `1024x768`, 24-bit, 96 DPI.

### 3.3 Locality and fallback

Behavior belongs to the module that can prove its safety:

| Behavior | Preferred path | Compatibility path | Retry boundary |
| --- | --- | --- | --- |
| Mouse and keyboard | Persistent XTest/XKB | `xdotool` | Only before native emission starts. Possible partial emission is terminal. |
| Window control | Native EWMH/Xlib | `wmctrl` | Only when the controller can prove the native request was unavailable or not completed. |
| Cursor-hidden lossless PNG | Persistent MSS | Optional X11 shared memory; then `scrot`/`maim` where allowed | `mss` is the production default. Opt-in `auto` evaluates X11 shared memory during readiness. Extension unavailability or ordinary native failure selects MSS for that X-server generation. An X-server timeout fails closed; display restart clears the source state and re-probes. Explicit `x11-shm` never changes source. |
| Other cursor-hidden formats or scaling | Persistent MSS | `scrot`, then `maim` | The X11 shared-memory source is not eligible. After a bounded MSS reset/retry cannot return a valid frame, file capture is allowed. |
| Cursor-visible capture | `maim` | None | Failure is terminal. |
| Change notification | XDamage hint | Source-hash polling | Pixel capture and hashes, not the hint, confirm change. |
| Same-region command runner | Prepared Modal Connect runner | Explicit external runner | Only before command dispatch; auth, validation, version, quota, and permission errors are terminal. |
| Warm capacity | Locked, live-verified owned slot | Cold create | Only after rejecting an owned candidate. Unverifiable ownership cannot authorize termination. |

There is no global fallback chain. Orchestration MUST NOT replay an operation after dispatch may
have begun.

## 4. Surface maturity

| Classification | Surfaces |
| --- | --- |
| Stable product contract | Public SDK namespaces; synchronous and native-async Modal Sandbox lifecycle; borrowed sync and async `computer.step()`; daemon HTTP primitives; action batches; binary screenshots; native/compatibility input; artifacts; traces; budgets; provider adapters; manager create/attach/reuse/cleanup; sync and async clients; hot-session protocol v1; observation-stream transport; versioned Modal Function handoff; borrowed trajectory fencing and receipt resolution. |
| Alpha / experimental | `_experimental_act_until_visual_change()` and first-visual-change semantics. It composes stable action and observation primitives but does not promise semantic readiness. |
| Benchmark-only | Modal V2 candidate creation, optimized-frontier paths, transport-floor probes, provider harness internals, and unpublished/raw result handling. |
| Application-owned example | Provider model loops, session broker, co-located runner/broker, Modal Function trajectory body, and run gateway. Callers must supply identity, authorization, durable storage, policy, and operational ownership. |
| Historical | Earlier specification revisions in Git history and archived/rejected benchmark reports. |

A future change MUST state its maturity. Experimental or benchmark-only code cannot become stable
through naming, documentation links, or successful benchmark results alone.

## 5. Stable Python SDK contract

### 5.1 Construction and lifecycle

The primary optimized composition is async owner → versioned handle → explicitly placed Modal
Function → one `borrow_async()` context around the whole trajectory. The application owns the
Function and provider model loop. The Function and Sandbox use one exact requested region. The
Function reuses one pooled, authenticated async HTTP client. Each model-produced ordered action
array uses one `computer.step()` request and receives one immediate post-action frame.

Missing, broad, mismatching, or unverifiable placement fails before lease acquisition or desktop
mutation. Protocol preflight also fails before lease acquisition when the daemon lacks the binary
screenshot metadata, trajectory lease, operation receipt, or `computer-step-envelope-v1` contract.
The runtime does not fall back to an external caller or to separate action and screenshot requests.

`AsyncComputerSandbox.create()` is the owner interface. `session_handle()` produces the handoff
value. The canonical executable composition is in
[`examples/modal_function_session_handoff.py`](../../examples/modal_function_session_handoff.py).

`ComputerSandbox` remains the synchronous low-level entry point for explicit primitive and
compatibility use.

```python
from modal_computer_use import ComputerConfig, ComputerSandbox

computer = ComputerSandbox.create(config=ComputerConfig())
try:
    computer.wait_until_ready()
    computer.mouse.click(320, 240)
    screenshot = computer.screenshots.full()
finally:
    try:
        computer.terminate(wait=True)
    finally:
        computer.detach()
```

Supported construction paths are:

- `ComputerSandbox.local(...)` for an existing local daemon;
- `ComputerSandbox.create(...)` for a new SDK-owned Modal Sandbox;
- `ComputerSandbox.attach(...)` for an existing Sandbox;
- `ComputerSandbox.attach_or_create(name=...)` for one live named Sandbox.

`run_id` is the canonical Sandbox-lifetime correlation tag. `request_id` is a deprecated input alias.
The creator owns termination. Exiting a created context terminates and detaches its Modal Sandbox.
Exiting an attached or existing-target context only detaches and closes its daemon connection. Local and
direct-URL contexts close connections only. `detach()` transfers a created Sandbox to
caller-managed ownership without claiming the remote Sandbox was terminated. A failed creation
cleans up any allocated Sandbox; a failed attachment never terminates the existing target.

`attach()` accepts exactly one of sandbox ID, name, run ID, or direct daemon URL. Direct tokens are
valid only with a direct URL. `attach_or_create(name=...)` requires a Modal Sandbox name and uses
that app-scoped live name as its only allocation key. Run IDs remain correlation tags. Only a
typed Modal not-found result permits creation. A named creation conflict performs a bounded lookup
of the winner; operational failures never trigger another allocation. Construction copies caller
configuration before adopting, generating, or validating a run ID.

`AsyncComputerSandbox` is the stable native-async Modal lifecycle surface:

```python
from modal_computer_use import AsyncComputerSandbox, ComputerConfig

config = ComputerConfig(
    runtime={"modal_environment": "main", "modal_region": "us-west-2"},
)
async with AsyncComputerSandbox.create(config=config) as computer:
    await computer.mouse.click(320, 240)
```

`create()` is the primary placed-owner interface. Entry validates a non-empty Modal environment
and one exact Modal region before any Modal lookup or Sandbox allocation. It also rejects tunnel
ingress, control VNC, and warm-pool tagging because those modes cannot produce its handoff. The explicitly named
`create_unplaced()` compatibility method retains low-level async ownership without promising an
eligible handoff. `create()`, `create_unplaced()`, `attach()`, and `attach_or_create(name=...)`
return lazy, one-shot async context managers. They perform no Modal work until entry and yield only
after the Sandbox and daemon are ready. Async attach accepts exactly one of sandbox ID, name, or run ID. Direct URLs use
`AsyncDaemonClient`; async orchestration does not include `wait=False`.

Async creation, attachment, and named acquisition use Modal-native `.aio` operations without
worker-thread bridges. They share the synchronous path's copied configuration, image choice, tags,
authorization, networking, compatibility checks, and security-owned creation plan. Exiting a
created context terminates and detaches its owned target. Exiting an attached or existing-target context
detaches without termination. Explicit async detachment transfers ownership. Failed or cancelled
creation completes cleanup for any allocated resource before the primary error escapes. Importing
the core package does not require Modal.

### 5.2 Computer Step

`BorrowedComputer.step()` and `AsyncBorrowedComputer.step()` are the stable model-loop Interfaces.
They accept one non-empty ordered action array plus explicit continuation, call identity, screenshot
options, and timeout values. Both return `ComputerStepResult` with these fields:

- `actions`: the semantic `ActionBatchResult`;
- `screenshot`: one byte-backed immediate post-action `Screenshot`;
- `timing`: typed step timing metadata.

The complete action tree validates before mutation. The daemon holds the input lock through ordered
execution and the trailing immediate capture. It stops on the first failure unless continuation is
explicit. It never replays a step after dispatch may have started. Response decoding is part of the
leased mutation; malformed or truncated envelope data enters receipt recovery.

Screenshot and zoom actions are valid inside a step. Their action-item outputs remain available in
`actions`. The final `screenshot` remains the one immediate post-action frame. A provider may
suppress a duplicate final frame in its own output when the last action already supplies the same
semantic image. Cursor-position queries may stay on the action-only interface when no frame is
needed.

### 5.3 Namespaces

The stable namespaces are:

| Namespace | Main operations |
| --- | --- |
| `mouse` | move, click, drag, scroll, down, up, position |
| `keyboard` | type, press, hotkey, hold, supported keys |
| `clipboard` | get, set, clear |
| `screenshots` | full, region, zoom, zoom-around |
| `recordings` | start, stop, list, get, download, delete |
| `display` | display information |
| `windows` | list, active, activate, close, wait |
| `actions` | apply, validate, ordered batch execution |
| `artifacts` | list, read, write, upload, download, delete, manifest, sync |
| `browser` | open URL, status, render metrics |
| `apps` | launch, open artifact |
| `commands` | bounded command execution |
| `input` | release all held input |
| `lifecycle` | start, stop, restart, status |
| `processes` | status, restart, logs, stderr, errors |
| `session` | metadata and refresh |
| `debug` | sanitized debug URLs and optional noVNC access |

`DaemonClient` and `AsyncDaemonClient` provide synchronous and native-async interfaces to an
existing daemon. `AsyncDaemonClient` owns its pooled HTTP connection and the WebSocket connections
it opens. Closing it closes those connections only; it does not stop the daemon or terminate a
Modal Sandbox. `AsyncComputerSandbox` composes the same async namespaces with Modal lifecycle
ownership. `AsyncBorrowedComputer` remains a separate lease-restricted trajectory interface.
`ComputerSandbox.hot_session()` provides a persistent action/screenshot channel.
`ComputerSandbox.observation_stream()` provides a correlated observation stream.

### 5.4 Configuration

`ComputerConfig` is strict: unknown fields fail validation. Its public groups are `desktop`,
`runtime`, `resources`, `image`, `network`, `storage`, `browser`, `actions`, and `budgets`.

Current defaults with architectural significance are:

- `ingress="attested-tunnel"`;
- noVNC off;
- inline screenshots use the raw binary route and return byte-backed `Screenshot` models;
- standard resources;
- `actions.input_backend="auto"`;
- `actions.subprocess_backend="isolated-asyncio"`;
- input admission refills 100 normalized input-work tokens per second with a 400-token burst;
- daemon HTTP/1.1;
- browser prewarm when a browser is configured;
- ordered batches stop on the first error unless `continue_on_error` is explicit.

Region, Function and Sandbox CPU/memory, images, timeouts, retries, scaling limits, and warm
capacity are explicit cost-bearing choices. Warm capacity remains off unless the operator enables
it. The SDK does not expose an `optimized` toggle or hidden legacy-default environment variable.

The exhaustive field and environment-variable contract lives in
[`docs/configuration.md`](../configuration.md).

## 6. Daemon protocol

### 6.1 Health and compatibility

The daemon exposes:

- `GET /healthz` for HTTP-process liveness;
- `GET /readyz` for desktop/backend readiness;
- `GET /v1/version` for daemon/SDK compatibility;
- `GET /v1/capabilities` for primitive, adapter, and observed backend support.

Liveness never implies readiness. Mutating desktop routes fail closed when readiness, recovery, or
borrow state does not permit execution.

### 6.2 Public route families

The public OpenAPI contract covers:

| Family | Prefix or route |
| --- | --- |
| Lifecycle | `/v1/computer/*` |
| Processes | `/v1/processes/*` |
| Mouse | `/v1/mouse/*` |
| Keyboard | `/v1/keyboard/*` |
| Clipboard | `/v1/clipboard/*` |
| Screenshots | `/v1/screenshots/*` |
| Recordings | `/v1/recordings/*` |
| Display and windows | `/v1/display/*`, `/v1/windows/*` |
| Actions | `/v1/actions/validate`, `/v1/actions/run`, binary action/screenshot variants |
| Computer Step | `/v1/steps` |
| Artifacts | `/v1/artifacts/*` |
| Browser and apps | `/v1/browser/*`, `/v1/apps/*` |
| Commands and input recovery | `/v1/commands/run`, `/v1/input/release-all` |
| Session and debug | `/v1/session/*`, `/v1/debug/*` |

The checked-in OpenAPI schema, not a handwritten route list, is authoritative for request and
response fields.

Trajectory-lease, operation-receipt, and owner-recovery routes are deliberately excluded from the
public OpenAPI schema. They are an internal versioned protocol used by the supported borrowed
session surface.

### 6.3 Action semantics

`ComputerAction` is a discriminated union covering move, click, double/triple click, drag, scroll,
mouse down/up, type, keypress, hotkey, held key, wait, screenshot, zoom, cursor position, and
release-all.

Action batches:

- validate the complete request before input execution;
- serialize input-emitting work under the daemon input lock;
- stop at the first failure by default;
- support explicit `continue_on_error`;
- reserve the complete recursive weighted input cost before mutation;
- enforce batch size, per-action deadline, batch deadline, screenshot-pixel, rate, and configured
  budget limits;
- return per-item results and timing;
- support idempotency fingerprinting for the ordinary batch API;
- attempt best-effort release of held input on failure paths and report
  `release_all_incomplete` with remaining controls when cleanup cannot be completed;
- treat an optional `screenshot_after` as a trailing operation, not a rollback boundary.

Nested `hold_key` action trees are canonical only through the action-batch route. Direct keyboard
hold requests accept a key and optional duration only.

### 6.4 Errors

Application route errors normally use a structured body with `code`, sanitized `message`, and
`details`, plus `X-Computer-Use-Error-Code`. Auth middleware, `/readyz`, and WebSocket frames use
purpose-specific schemas. Validation is `422`; unsafe paths are `400`; conflicts are `409`;
oversized requests are `413`; transient budget/rate failures are `429`; a batch that can never fit
the configured input burst is `422 input_cost_exceeds_burst`; readiness and recovery failures are
service errors. Unhandled route exceptions return a generic `internal_error` and sanitized details.

Clients must branch on stable codes and typed exceptions, not raw exception text.

## 7. Input, screenshots, and observations

### 7.1 Input safety

The `auto` input policy prefers persistent native XTest/XKB. Capability output separates:

- the last observed backend;
- the configured policy;
- implementations supported by the backend;
- implementations verified available by the latest readiness probe.

Typing supports `auto`, canonical direct `keystrokes`, clipboard-assisted input, and explicit
legacy `xdotool`. Unsupported or active-layout-unmapped keys fail before emission. A possibly
partial native operation is non-replayable and terminal.

The `normalized-input-work-v1` policy gives extra weight to repeated clicks, long typing, large
scrolls, drag paths, hotkeys, and nested actions. One daemon-local token bucket covers borrowed and
direct mutation routes. Lease handoff does not reset it. Rate admission is resource protection; it
does not replace approvals or semantic policy.

Direct mouse and window responses report request-specific input or window backend headers. Raw
screenshot routes report the request-specific capture backend. Global capability fields are
observational and cannot identify the backend used by a concurrent request.

### 7.2 Screenshot contract

PNG, JPEG, and WebP are supported. The JSON `Screenshot` model includes dimensions, format,
SHA-256, and coordinate-space metadata. Raw screenshot routes expose capture-backend and timing
headers; observation and action metadata expose their own backend/timing attribution. Binary
routes avoid base64 and unnecessary image re-encoding. Output pixel budgets are validated before
capture.

The default `mss` source uses persistent MSS for cursor-hidden, full-resolution lossless PNG.
Opt-in `auto` evaluates persistent X11 shared-memory capture; it remains unpromoted after failing
readiness-latency and display-restart gates. Persistent MSS also owns JPEG, WebP, scaled, and
raw-pixel compatibility paths. An X-server reply timeout fails closed because every capture source
depends on that display. A display restart clears source state and re-probes. Cursor-visible capture
uses the file compatibility path because neither in-process source composes the cursor.

### 7.3 Observation contract

The observation WebSocket supports authenticated streaming, bounded/coalesced frame delivery,
binary envelopes, full frames and lossless patches, action correlation, and transport/capture
timing metadata.

XDamage is only a notification hint. A changed source or frame hash confirms pixel change.
Consumers must validate the frame envelope and apply patches to the declared base frame.

The Alpha first-visual-change composition reports the first correlated visual difference or a
bounded timeout. It does not prove:

- semantic readiness;
- task success;
- DOM stability;
- that no invisible side effect occurred;
- that intermediate animation frames were retained.

Its authoritative contract and promotion gates live in
[`docs/experimental-visual-change-observation.md`](../experimental-visual-change-observation.md).

## 8. Modal orchestration and transport

### 8.1 Supported Modal baseline

The `modal` extra pins the Modal 1.5 line and requires 1.5.3 or later. The v8 baseline was tested
with Modal 1.5.3, the latest stable patch in the audited line on 2026-07-30. The maintained production
path uses standard `modal.Sandbox.create`, Connect Tokens, encrypted tunnels, optional Volumes,
tags, readiness probes, and supported snapshot methods. Modal 1.5.3 offers the Beta
`sandbox.filesystem` orchestration API, but the product artifact boundary remains daemon-native.
The deprecated `Sandbox.open`, `ls`, `mkdir`, `rm`, and `watch` methods are not canonical.

Modal V2 helpers remain benchmark candidates because creation, listing, and name lookup still use
experimental APIs and V2 lacks GPU and `modal shell` support. They are not selected by
`ComputerSandbox.create`.

### 8.2 Ingress

The supported ingress policies are:

| Policy | Contract |
| --- | --- |
| `attested-tunnel` | Default. Use an SDK-managed bootstrap bearer to obtain a short-lived daemon-issued bearer, then use the encrypted daemon tunnel. Attach and handoff recover the bootstrap bearer through the Modal control plane. |
| `connect` | Use Modal Sandbox Connect access directly. Required when outbound networking is fully blocked. |
| `tunnel` | Use the encrypted daemon tunnel. Intended for explicitly managed compatibility and benchmark paths. |

Every SDK-created daemon Connect Token is explicitly scoped with `port=8080` and is used only for
pure Connect ingress. Raw and attested tunnel modes do not trust verified-user headers. The daemon rejects
`_modal_connect_token` query parameters to keep credentials out of URLs and logs. The SDK extracts
a query token returned by older Modal shapes and sends it as a bearer header.

noVNC is a separate optional encrypted tunnel. It is off by default and supports `view_only` or
`control`. URLs, passwords, and bearer tokens are secrets.

### 8.3 Placement and latency

Requested Modal region and observed placement are recorded separately. Co-location is an
application scheduling property, not proof of loopback, host identity, availability-zone identity,
or private reachability.

Performance tuning must preserve the supported contract:

- batch related actions;
- prefer binary screenshot routes;
- reuse sessions and WebSocket transports when appropriate;
- use native input and isolated-asyncio subprocess execution defaults;
- use a same-region Modal runner only when application topology requires it;
- preserve fallback attribution and terminal error classes.

Measured values belong to dated benchmark evidence, not this specification.

### 8.4 Persistence and snapshots

Artifacts use a daemon-owned root on the Sandbox-local filesystem and may be backed by a verified
Modal Volume. A successful sync claim requires configured and verified persistence. Volume v2
`sync` success means Modal accepted the mountpoint commit; Modal still classifies Volumes v2 as
Beta and does not recommend them as the sole store for mission-critical data. Irreplaceable
artifacts need a separate durable copy. `modal.NetworkFileSystem` is forbidden.

Filesystem and directory snapshots are explicit lifecycle operations and default to a 30-day TTL
in Modal 1.5. Filesystem snapshots restore a root filesystem; directory snapshots are a Beta,
directory-scoped Image that may be mounted or used as a root. Neither preserves GUI process state.
Memory snapshots remain Alpha, expire after seven days, terminate the source, close TCP
connections, and do not support GPU-backed Sandboxes. Snapshot APIs do not replace artifact
manifests, Volume synchronization, or application recovery.

## 9. Deployed Modal Function handoff

### 9.1 Handle contract

`ComputerSandbox.session_handle()` and `AsyncComputerSandbox.session_handle()` return a frozen,
versioned `ComputerSessionHandle` for an SDK-owned desktop created with:

- an explicit requested Modal region;
- `attested-tunnel` or `connect` ingress;
- noVNC off or view-only.

The handle contains routing and policy identity, not bearer credentials. It omits endpoints,
tokens, noVNC URLs, task text, screenshots, and artifacts. Sandbox identity is necessarily present
in serialized form and remains sensitive.

Provisioning happens once in the owner. Inside a deployed Modal Function, `borrow_async()` is
canonical for async code and `borrow()` is available for synchronous code. One borrow surrounds
the complete repeated trajectory; it never provisions the target and is not entered once per
action. Borrow entry:

1. verifies the deployed-Function environment and exact requested region declaration;
2. verifies the observed Function region and the target's requested exact region;
3. resolves the live Sandbox through the Function's Modal identity;
4. validates the live config/session policy tags;
5. mints fresh access;
6. requires daemon readiness, version, and capabilities;
7. acquires one exclusive trajectory lease.

One pooled async daemon client and attested-tunnel authentication state are reused from preflight
through lease release. Requests still cross authenticated Modal ingress. The SDK does not claim
that ingress routing is eliminated.

The borrower detaches on exit and never terminates the creator-owned desktop.

### 9.2 Lease and operation sequence

One borrow uses an application-generated, unique `run_id`. A sealed run ID cannot be reused.
Mutations across HTTP, hot-session, and observation transports receive one gap-free sequence.
Read-only calls consume no sequence.

The lease heartbeat is independent of ordinary async model/application work. Blocking the async
event loop is still unsupported application behavior.

### 9.3 Durable receipt and ambiguity rules

The daemon writes target-local SQLite operation receipts. It records `IN_PROGRESS` before
dispatching a mutation and records a terminal outcome after completion. There is no automatic
mutation retry or replay after possible dispatch.

On response loss, the SDK resolves the exact sequence:

| Proven state | SDK outcome |
| --- | --- |
| Not applied | `OperationNotAppliedError` |
| Completed but original result unavailable | `OperationResultUnavailableError` |
| Outcome indeterminate | `ActionOutcomeUnknownError` or `SessionRecoveryRequiredError` |

After `OperationResultUnavailableError`, `observe_after_result_loss()` may take a full inline PNG
without consuming a sequence or clearing the mutation block. It cannot reconstruct the lost result
or invisible effects. The caller must leave the borrow before any application-elected
continuation, and any continuation uses a fresh run ID.

An indeterminate target is quarantined. Only the original owner proof can inspect and acknowledge
recovery. Attaching to the Sandbox does not grant owner proof.

## 10. Security and secret handling

The secure default is authenticated daemon access, noVNC off, strict input validation, serialized
input, bounded work, and sanitized diagnostics.

The following are secrets in logs, traces, exceptions, benchmark artifacts, and PR evidence:

- Connect and bearer tokens;
- noVNC URLs and passwords;
- clipboard and typed text;
- screenshots and recording bytes;
- artifact contents and private paths;
- provider credentials;
- serialized session handles and private orchestration identities.

Security invariants:

1. Query-string Connect tokens are rejected.
2. Local-token mode is loopback-only.
3. Missing authentication fails closed; unauthenticated local mode is explicit and loopback-only.
4. Verified-user headers are trusted only in pure Connect mode. Raw and attested tunnels require
   daemon bearer authentication even when a client supplies a verified-user-shaped header.
5. Minted tunnel tokens cannot mint another token and expire according to daemon policy.
6. HTTP responses are non-cacheable, JSON and WebSocket inputs are bounded, and artifact uploads
   remain streamed.
7. Unknown action keys, invalid coordinates, invalid regions, and unsupported keys fail before
   execution.
8. Nested action depth, command arguments, drag points, and key collections are bounded before
   execution.
9. Artifact paths reject absolute paths, traversal after repeated percent decoding, control
   characters, protected control paths, and symlink escapes.
10. Logs, traces, process diagnostics, command output, and error details pass through redaction.
11. Budgets are reserved at the owning route before expensive or mutating work.
12. noVNC is opt-in and its takeover semantics are explicit.
13. Modal lifecycle operations verify the requested app and app-ownership tag.
14. Prompt-injection and sensitive-action policy remain above core.
15. Ambiguous mutation state fails closed and is never hidden by transport fallback.

The maintained threat model is [`docs/security.md`](../security.md).

## 11. Artifacts, traces, and observability

### 11.1 Artifacts

The artifact API is a bounded object-store façade over a configured root, not a general filesystem
escape hatch. Manifests include content hashes and metadata. Control files such as private trace,
receipt, and runtime state are not public artifacts.

Persistence reports are explicit:

- non-persistent stores return a successful no-op with `persistent=false`;
- persistent success requires a verified mount and a completed sync operation;
- configuration that asks for persistence without a verified mount fails the persistence claim.

### 11.2 Traces

Trace NDJSON records provider provenance, normalized actions, results, screenshot references,
coordinate spaces, redactions, and errors. Sensitive values are replaced with exactly a redaction
marker and length; content hashes are not retained. Replay validates the entire trace and skips
redacted input. Replay never turns redacted content back into executable text.

### 11.3 Observability

Structured logs and optional OpenTelemetry spans use route templates and stable error codes.
Telemetry must not attach raw URLs, tokens, text, screenshot bytes, artifact bytes, or unbounded
exception strings.

## 12. Provider adapters

OpenAI, Anthropic, and generic adapters translate provider actions to the provider-neutral action
model. They preserve versioned provider provenance and fail closed on unsupported actions.

The current advertised compatibility is:

- OpenAI `computer-use`;
- Anthropic `computer_20241022`, `computer_20250124`, and `computer_20251124`.

Adapters do not call provider APIs, own model loops, bypass validation, or weaken budgets.
Provider SDK dependencies are optional. The maintained provider guides and examples own current
request/response loop details.

The OpenAI example preflights every computer call in one provider response before the first step.
It preserves provider call order and call IDs and sends each ordered action array through one step.

The Anthropic example keeps cursor-position queries action-only. It sends other hosted actions and
custom ordered batches through step. It preserves native screenshot, zoom, and nested image order.
When the last action already supplies the semantic image, it suppresses only the duplicate final
step frame from the provider tool result.

## 13. Manager, warm capacity, and cleanup

`ComputerSandboxManager` owns orchestration behavior:

- create and attach;
- attach-or-create with config identity;
- list and find by run ID;
- terminate and TTL cleanup;
- fill, claim, and reconcile warm capacity.

Warm capacity is ownership-sensitive. A claim verifies the live Sandbox, configuration, region,
expiry, tags, and lock before use. A failed or unverifiable ownership read is terminal. Cleanup may
terminate only exact, verified, application-owned targets. Dry-run is the default for broad
expiry cleanup.

New Sandboxes carry `computer-use.app_id`. List, attach, reuse, warm-capacity, and cleanup queries
are scoped to Modal's app ID and verify the ownership tag. A migration-only
`allow_legacy_unscoped` option may attach an untagged Sandbox already resolved inside the requested
app. It never permits a conflicting tag or broad legacy cleanup.

Modal Queues and object tags coordinate capacity; they are not an authorization database.

## 14. Application-owned hosted runs

[`examples/modal_run_gateway.py`](../../examples/modal_run_gateway.py) and
[`examples/run_gateway/`](../../examples/run_gateway/) define a bounded reference boundary for
non-Python or hosted callers. The example requires the application to inject:

- principal authentication;
- tenant and desktop/task authorization;
- versioned HMAC identity bindings;
- an atomic durable run store;
- quota and exclusive desktop claims;
- one deployed trajectory dispatcher;
- a scheduled reconciler;
- cancellation and operator-recovery policy.

The gateway uses a closed lifecycle, absolute deadlines, idempotent admission, opaque run IDs,
leased reconciliation, and fail-closed indeterminate state. It deliberately has no permissive
resolver, in-memory production store, migration worker, or public admin-recovery endpoint.

Function dispatch and durable state are not one transaction. A missing Function-call identity
after dispatch is indeterminate; the system does not guess or spawn again. This example is a
design reference, not a claim that core provides a production multi-tenant control plane.

## 15. Benchmark and evidence contract

Benchmark tooling measures the product; it does not define it. Published evidence must:

- state the workload, system boundary, sample count, percentile method, and status;
- pin code and provider SDK provenance;
- retain or hash-bind raw samples according to repository policy;
- separate provider-default, project-optimized, and experimental comparisons;
- record failures and cleanup;
- sanitize identifiers, credentials, endpoints, and private paths;
- avoid treating a small-sample result as stable evidence.

Candidate, diagnostic, rejected, eligible, and current-reference states are not interchangeable.
See [`docs/benchmarking.md`](../benchmarking.md) and
[`benchmark-data/README.md`](../../benchmark-data/README.md).

## 16. Upstream Modal alignment

The production design follows current first-party Modal contracts:

- Sandboxes have explicit lifecycle, readiness, timeout, detach, and termination behavior.
- Connect Tokens support authenticated HTTP and WebSocket access.
- Encrypted tunnels are separate from Connect access and should have their own authentication.
- Inbound CIDR allowlists can restrict tunnel and Connect access.
- Volumes use the `volumes` mapping. Modal's privileged `sandbox.filesystem` API owns direct
  orchestration-level file access; project artifact access remains daemon-native and path-bounded.
- Filesystem and directory snapshots have retention policies and capability limits.
- Warm-pool patterns require readiness checks, expiry margins, ownership, and explicit cleanup.

Current Modal V2 documentation says V2 supports Connect Tokens, encrypted tunnels, filesystem APIs,
Volumes, snapshots, readiness probes, and region placement. It remains under active development
and depends on experimental create/list/name APIs, is absent from stable `Sandbox.list()`, and
lacks GPUs and `modal shell`. Therefore v10 keeps V2 in benchmark-only code and keeps the standard
Sandbox path canonical.

Primary references:

- [Modal Sandboxes](https://modal.com/docs/guide/sandboxes)
- [Sandbox networking and security](https://modal.com/docs/guide/sandbox-networking)
- [Sandbox snapshots](https://modal.com/docs/guide/sandbox-snapshots)
- [V2 Sandboxes](https://modal.com/docs/guide/sandbox-v2)
- [Warm Sandbox pool example](https://modal.com/docs/examples/sandbox_pool)
- [Anthropic computer-use Sandbox example](https://modal.com/docs/examples/anthropic_computer_use)

## 17. Versioning and compatibility

- Package, daemon, and checked-in OpenAPI versions are `2.0.1`. The release tag is `v2.0.1` after
  all release gates pass.
- The optional extras are `modal`, `openai`, `anthropic`, provider-specific benchmark extras, the
  combined provider benchmark extra, and `dev`. Provider and benchmark dependencies remain outside
  core.
- `/v1/version` advertises the tested `1.1.0` through `2.x` client range and internal
  lease/receipt protocol versions through headers. Version 2 trajectory compatibility is decided
  by API version and required capabilities, not by package semver alone.
- Public configuration models forbid unknown keys and retain narrow aliases only where documented.
- `request_id` is deprecated in favor of `run_id`.
- Direct `xdotool` typing is a compatibility request; direct native `keystrokes` is canonical.
- Experimental methods retain explicit naming until their promotion gates pass.
- Internal lease/receipt routes may evolve only with their protocol versions and handoff
  compatibility tests.

Breaking a stable request, result, error code, security invariant, or ownership rule requires an
explicit versioning decision, migration guidance, and tests.

## 18. Verification and release gates

Every behavior change must add focused success and failure-path coverage. Before release or
merge-ready handoff, the repository requires:

```bash
uv sync --extra dev --extra modal --frozen
uv run python scripts/export_openapi.py --check
uv run ruff check .
uv run mypy src
uv run pytest -q
```

Boundary scans must confirm:

- core has no `openai` or `anthropic` imports;
- core has no `NetworkFileSystem` use;
- examples and docs do not print secret-bearing URLs, tokens, artifacts, or content.
- the frozen dependency graph passes audit;
- Bandit and Semgrep report no material finding;
- security regressions and the documented performance gates pass locally.
- the minimum supported runtime sustains at least 200 representative normalized input-work
  tokens per second before the 100/400 default is promoted.

Modal smoke tests are credential-gated. The deployed-Function handoff smoke is a protected manual
workflow and validates one bounded handoff, not benchmark performance or continuous production
health.

The release checklist in [`docs/release-checklist.md`](../release-checklist.md) is authoritative
for packaging and CI parity.

## 19. Canonical implementation truth table

| Contract | Primary implementation | Pinning evidence |
| --- | --- | --- |
| Package and daemon 2.0 version | `pyproject.toml`, `_version.py`, daemon app | project metadata and OpenAPI tests |
| Health, readiness, version, capabilities | `daemon/routes/health.py` | daemon route and readiness tests |
| Auth and secret redaction | `daemon/auth.py`, `routes/websocket_auth.py`, `redaction.py`, `daemon/logging.py` | auth, observability, trace tests |
| Strict config and environment mapping | `config.py`, `daemon/settings.py`, `configuration_reference.py` | config/settings/documentation tests |
| Ordered action batches and budgets | `daemon/actions/`, `daemon/budget_policy.py` | action, timeout, budget, validation tests |
| Native input and safe compatibility fallback | `daemon/desktop/xtest.py`, `keyboard.py`, `mouse.py` | native X11, keyboard, X11 backend tests |
| Native windows and fallback ownership | `daemon/desktop/windows.py` | windows and fallback tests |
| Binary and encoded screenshot paths | `daemon/desktop/screenshots.py`, `routes/screenshots.py` | screenshot, pixel, route, benchmark tests |
| Observation stream and patches | `routes/observations.py`, `observations.py`, `transports/observation.py` | observation stream and XDamage tests |
| Alpha first-visual-change composition | `observations.py`, action observation routes | experimental guide and observation tests |
| Hot-session transport | `routes/hot_session.py`, `hot_session.py`, `transports/hot_session.py` | hot-session tests |
| Artifacts and persistence claims | `artifacts.py`, `routes/artifacts.py` | artifact traversal, symlink, sync tests |
| Traces and replay | `tracing.py`, `daemon/actions/traces.py` | trace, adapter, budget tests |
| Provider adapters | `adapters/` | provider fixtures and cookbook tests |
| Modal create/attach/reuse | `sandbox.py`, `manager.py`, `image.py` | Modal boundary/foundation/integration tests |
| Warm capacity and cleanup | `manager.py` | manager and Modal boundary tests |
| Sync and async clients | `client.py`, `transports/http.py` | async SDK and namespace tests |
| Versioned session handle and borrow | `models.py`, `sandbox.py`, `borrowed.py` | session lease and Function handoff tests |
| Exclusive trajectory lease | `daemon/leases.py`, `session_lease.py`, `routes/leases.py` | daemon and SDK lease tests |
| Durable operation receipts | `daemon/receipts.py`, `routes/recovery.py` | receipt, session lease, recovery tests |
| Lost-result reobservation | `borrowed.py`, `session_lease.py` | async SDK and session lease tests |
| Application-owned run gateway | `examples/run_gateway/` | focused gateway example tests |
| OpenAPI parity | `docs/openapi.json`, `scripts/export_openapi.py` | OpenAPI schema test |
| Release and handoff validation | `.github/workflows/`, release docs | release-documentation and protected smoke tests |

## 20. Outstanding work and promotion gates

The repository source is the `2.0.1` maintenance release. The preregistered 100-pair Computer Step
benchmark and protected Modal handoff smoke passed on 2026-08-08. Every publication remains gated
on a fresh protected live smoke from the exact release commit, then the release-matched package
and hosted-documentation sequence.

1. Promote first-visual-change only after its documented correctness, fallback, compatibility, and
   benchmark gates pass. Until then, retain the experimental method name and Alpha guide.
2. Keep Modal V2 experimental until creation, listing, and name lookup have stable first-party
   APIs and matched capability, recovery, placement, and performance evidence passes the
   repository promotion gates.
3. Add a production run-store adapter or service only in an application-owned package with an
   explicit tenant/security model; do not place a permissive service in core.
4. Preserve protected live validation for session handoff as the Modal SDK evolves.
5. Continue evidence-gated performance work without changing defaults from a single candidate or
   non-preregistered run.
6. Treat public API additions as complete only when schema, redaction, budget, error, ownership,
   documentation, and failure-path tests all land together.

## 21. Final recommendation

Maintain `modal-computer-use` as a daemon-first primitive library, not as a provider loop or hosted
agent product. The strongest current differentiators are:

- owned and inspectable desktop infrastructure on Modal;
- native X11 input with explicit compatibility boundaries;
- binary and correlated observation transports;
- provider-neutral action and trace models;
- versioned deployed-Function handoff;
- exclusive trajectory leases and durable operation receipts that refuse unsafe replay;
- evidence-bound performance work.

Future changes should deepen those contracts rather than broaden core into application policy. A
surface is canonical only when its ownership, maturity, security boundary, executable behavior,
and pinning evidence agree.

(End of v10.)
