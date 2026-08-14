# API

The primary interface is an async owner, a versioned session handle, and one borrowed trajectory in
an application-owned Modal Function. This establishes the placed, connection-reusing path by
default. There is no `optimized=True` switch and no performance-profile toggle.

Start with:

```python
from modal_computer_use import (
    AsyncComputerSandbox,
    AsyncDaemonClient,
    ComputerConfig,
    ComputerSandbox,
    ComputerSessionHandle,
    ComputerStepResult,
    DaemonClient,
)
```

Choose the surface by who owns the desktop:

| Application need | Surface | Lifecycle ownership |
| --- | --- | --- |
| Connect to a daemon that already exists | `AsyncDaemonClient` | Connections only |
| Create or attach to a Modal desktop from async Python | `AsyncComputerSandbox` | Created desktops are owned; attached desktops are not |
| Run one repeated trajectory in a deployed Modal Function | `ComputerSessionHandle.borrow_async()` | One lease and connection; the original owner keeps the Sandbox |
| Send one ordered action array and receive its immediate frame | `computer.step()` on a borrowed computer | Uses the active trajectory lease |

For the primary path, create the desktop with `AsyncComputerSandbox.create()`, call
`owner.session_handle()`, and pass that handle to the placed Function. Enter one `borrow_async()`
context around the complete screenshot, model, and action loop. Do not borrow once per turn.
Send each ordered model action array through `computer.step([...])`. It uses one HTTP request for
the action batch and its immediate post-action frame.

`AsyncDaemonClient` connects to a daemon that is already running. `AsyncComputerSandbox` performs
Modal provisioning and attachment without blocking the event loop. `borrow_async()` reconnects to
an already-provisioned desktop for one complete deployed-Function trajectory.

`screenshots.full()` returns a typed `Screenshot`. Inline full screenshots use the binary HTTP
response and populate `Screenshot.bytes`; call `as_bytes()` or `to_base64()` when an integration
needs a different representation. The borrowed `AsyncDaemonClient` and its pooled async HTTP client
remain open for the complete trajectory.

`computer.step()` is available on `BorrowedComputer` and `AsyncBorrowedComputer`. It returns a
`ComputerStepResult` with `actions`, `screenshot`, and `timing` fields. The `actions` field is the
normal `ActionBatchResult`. The `screenshot` field is a byte-backed `Screenshot` captured
immediately after the action phase. This immediate post-action frame is not application readiness.

### Screenshot source reporting

The SDK field or environment variable selects a capture policy. The raw response header
`X-Computer-Use-Capture-Backend` reports the source that ran. `auto` may report `mss-fallback`.
The `ComputerStepResult.screenshot` model omits the backend. See
[configuration](configuration.md#actions) for source selection and scope.

## Low-level compatibility

`ComputerSandbox`, `DaemonClient`, `AsyncDaemonClient.local()`, direct REST routes, attach flows,
idempotency controls, and `screenshots.full_bytes()` remain supported for local control, direct
daemon access, debugging, and compatibility. These primitives do not create or verify the placed
owner-to-Function topology. Missing placement or handoff prerequisites never cause an automatic
fallback to an external caller.

`AsyncDaemonClient` provides the same typed namespaces for a daemon that is already running:

```python
import asyncio

from modal_computer_use import AsyncDaemonClient


async def main() -> None:
    async with AsyncDaemonClient.local(token="dev") as computer:
        await computer.wait_until_ready()
        await computer.mouse.move(100, 120)
        await computer.screenshots.full(show_cursor=True)


asyncio.run(main())
```

The client owns its pooled HTTP connection and any WebSocket connections opened through
`hot_session()` or `observation_stream()`. Exiting it closes those connections only. It does not
stop the daemon or terminate a Modal Sandbox. Lifecycle operations remain explicit.

Namespaces on `ComputerSandbox`, `AsyncComputerSandbox`, and `AsyncDaemonClient`:

- `computer.mouse`: `click`, `move`, `drag`, `scroll`, `down`, `up`, `position`
- `computer.keyboard`: `type`, `press`, `hotkey`, `hold`, `supported_keys`
- `computer.clipboard`: `get_text`, `set_text`, `clear`
- `computer.screenshots`: `full`, `region`, `zoom`, `zoom_around`
- `computer.recordings`: `start`, `stop`, `list`, `get`, `download`, `delete`
- `computer.display`: `info`
- `computer.windows`: `list`, `active`, `activate`, `close`, `wait_for`
- `computer.actions`: `apply`, `run`, `validate`
- `computer.artifacts`: `list`, `read_bytes`, `write_bytes`, `download`, `upload`, `delete`, `manifest`, `sync`
- `computer.browser`: `open_url`, `status`, `render_metrics`
- `computer.apps`: `launch`, `open_artifact`
- `computer.commands`: `run`
- `computer.input`: `release_all`
- `computer.lifecycle`: `start`, `stop`, `restart`, `status`
- `computer.processes`: `status`, `restart`, `logs`, `stderr`, `errors`
- `computer.session`: `metadata`, `refresh`
- `computer.debug`: `urls`, `vnc_url`

The daemon exposes `/healthz`, `/readyz`, `/v1/version`, `/v1/capabilities`, and `/v1/*` primitive routes.
The checked-in OpenAPI schema lives at [openapi.json](openapi.json) and is verified with
`uv run python scripts/export_openapi.py --check`.

## Modal Function session handoff

`ComputerSandbox.session_handle()` and `AsyncComputerSandbox.session_handle()` return a frozen,
versioned `ComputerSessionHandle` for an SDK-owned Modal desktop created with an explicit requested
region and either `attested-tunnel` or `connect` ingress and `vnc_mode="off" | "view_only"`.
Control-mode noVNC targets cannot produce or enter a handoff. The v2 handle serializes only its
protocol version, sandbox/session identity, app and Modal environment, `requested_modal_region`,
ingress, daemon HTTP version, vnc policy, and config hash. Sandbox/session identity is hidden
from `repr()` but necessarily remains in JSON or cloudpickle/pickle data so Modal can reconnect.
Endpoint URLs, bearer or Connect credentials, noVNC URLs, tags, prompts, typed text, screenshots,
and artifacts are never fields on the handle.

The handle is routing identity, not a bearer credential or an authorization boundary. Do not log
or publish it. A public HTTP wrapper must authenticate callers and authorize the target before it
invokes the deployed Function; the Function's Modal identity is what resolves fresh access.

Non-Python applications can place a bounded spawn-and-poll gateway in front of the deployed
trajectory Function. The gateway has a separate state, idempotency, reconciliation, recovery, and
retention contract. Read [Run gateway](reference/run-gateway.md) before adapting the
[`modal_run_gateway.py`](../examples/modal_run_gateway.py) example. The core package does not own
that application policy.

Use the native-async borrow context inside an async user-owned Modal Function:

```python
# Replace this with one exact region measured for your workload.
FUNCTION_REGION = "us-west-2"


async def trajectory(handle: ComputerSessionHandle, task: str, run_id: str) -> None:
    async with handle.borrow_async(
        run_id=run_id, function_region=FUNCTION_REGION
    ) as computer:
        screenshot = await computer.screenshots.full()
        for _ in range(3):
            action = await application_model_call(task, screenshot)
            result = await computer.step([action], continue_on_error=False)
            screenshot = result.screenshot
```

Constructing the context does not contact Modal or create credentials. Entering it requires an
application-generated `run_id` unique to that one trajectory/borrow; a durably sealed run ID cannot
be reused. It also requires a positive finite readiness timeout, Modal's official deployed-Function
runtime marker `MODAL_IS_REMOTE=1`, `MODAL_ENVIRONMENT`, exact equality between
`function_region` and `requested_modal_region`, and handle/vnc policy before credential issuance.
It then validates the live object, SDK marker, config tag, and policy-bound session tag before
requesting fresh access, and requires daemon readiness. It
does not retry, fall back to another transport, replay a callback, or replay an action. A failed
endpoint, authorization, readiness, or config check propagates and closes any created client.
Leaving the context detaches the borrower and never terminates the desktop; the creator retains
lifecycle ownership. Borrow contexts are one-shot.

The compact session tag combines randomness with a digest over the handle's app name, Modal
environment, requested region, ingress, daemon HTTP version, VNC policy, and config hash. Borrow
recomputes that digest from the handle and requires an exact live session-tag match, so changing any
of those routing or policy fields is rejected before credentials are issued without consuming more
of Modal's 10-tag Sandbox budget.

`borrow_async()` is canonical for async Modal Functions and uses Modal's native `.aio` calls.
One borrow uses one `AsyncDaemonClient` and its pooled HTTP client for screenshots, actions, lease
requests, and readiness checks. With `attested-tunnel`, the SDK exchanges the attested token once
when the borrow starts. It then reuses that authentication state until the borrow ends. Every
request still crosses authenticated Modal ingress. Authentication reuse does not remove ingress
routing.

Borrowed async application code, including model calls, must not block the event loop so the
independent heartbeat and other desktop trajectories can progress. `borrow()` remains supported
for synchronous callers. Entry acquires one exclusive daemon trajectory
lease and starts an independent heartbeat. Every borrower-reachable mutation receives one gap-free
sequence shared across HTTP, hot-session, and observation transports; an ordered action batch uses
one sequence, while read-only calls consume none. Artifact/auto screenshot storage is a mutation;
inline/raw capture is read-only.

There is no automatic retry, fallback, callback replay, or action replay after possible dispatch.
On a lost response the borrower resolves that exact sequence from the daemon's durable receipt:
proven missing raises `OperationNotAppliedError`, completed-without-result raises
`OperationResultUnavailableError`, and indeterminate work raises `ActionOutcomeUnknownError` or
`SessionRecoveryRequiredError`. `OperationResultUnavailableError` exposes only the completed
operation's nonnegative `sequence` and an allowlisted `operation_kind` when the daemon supplied a
known stable route label; its message and representation remain generic.

After that specific completed-without-result error, the borrowed facade provides one explicit
read-only recovery convenience:

```python
try:
    await computer.actions.run(actions)
except OperationResultUnavailableError as exc:
    frame = await computer.observe_after_result_loss()
    # Decide from visible state, then leave this borrow. Use exc.sequence and
    # exc.operation_kind only as safe diagnostic metadata.
```

`observe_after_result_loss()` accepts no options. It always captures the full screen as an inline
PNG with daemon processing, creates no artifact, consumes no operation sequence, and may be called
repeatedly while the borrow remains open. The original result is neither retained nor replayed.
Observation success never clears the mutation block: every later mutation in that borrow still
fails, so any application-elected continuation must leave the context and use a fresh run ID.
Other read-only namespaces remain available; this helper does not create generic result recovery
for commands or artifacts.

A reobserved frame shows only one later visible state. It cannot reconstruct intermediate frames
or animation, prove readiness, or establish semantic success. It also cannot reveal invisible
command, clipboard, download, filesystem, network, or other effects outside the captured screen.
All receipt outcomes seal the current borrow; only an indeterminate target requires owner recovery.
The original `ComputerSandbox` or `AsyncComputerSandbox` owner can inspect `recovery_status()` and
call `acknowledge_recovery(incident_id=...)`; await both methods on the async owner. Attached or
detached objects without the private owner proof fail closed. Exclusive lease ownership prevents
overlapping trajectories for one desktop but is not a claim that one desktop is safe for multiple
tenants.

Keyboard typing accepts `method="auto" | "keystrokes" | "clipboard" | "xdotool"`.
`keystrokes` is the canonical direct-input behavior and uses the configured native or compatibility
input adapter; `auto` uses clipboard paste for long or active-layout-unmapped Unicode text.
`xdotool` remains a legacy explicit compatibility request. The daemon default input adapter is
`auto`, which prefers the persistent XTest/XKB path and falls back before emission when necessary.
For Modal-created sandboxes, noVNC tunnel URLs are owned by Modal orchestration; use
`ComputerSandbox.debug_urls()` or await `AsyncComputerSandbox.debug_urls()` rather than the
daemon-only `computer.debug.urls()` helper when you need to know whether a Modal noVNC URL exists.

## Input capabilities

`GET /v1/capabilities` separates input configuration from runtime observation:

- `input_backend` is the legacy observational field. It reports the most recently selected input
  adapter and may change while the daemon runs or be `null` before an adapter is observed.
- `input_backend_configured` is the requested policy: `auto`, `xtest`, `xdotool`, or `mock`.
- `input_backends_supported` lists implementations provided by the desktop backend. It does not
  claim that their runtime dependencies are ready.
- `input_backends_available` is the most recent readiness probe's usable set. It is empty before
  the first probe and whenever the probe has not observed a usable adapter. `xdotool` appears only
  after a bounded, display-aware command probe succeeds; finding its executable is not sufficient.
- `input_rate_limit_policy` identifies the normalized weight contract. Version 1 reports
  `normalized-input-work-v1`.
- `input_rate_limit_tokens_per_sec` and `input_rate_limit_burst` report the resolved daemon values.

Capability reads report cached state and do not trigger a new input probe.

Successful direct primitive responses also attribute the implementation used for that operation:

- Direct mouse routes return `X-Computer-Use-Input-Backend`.
- Direct window routes return `X-Computer-Use-Window-Backend`.
- Raw screenshot routes return `X-Computer-Use-Capture-Backend`.

Use these response headers for concurrent diagnostics and benchmarks. The legacy `input_backend`
capability reports the last observed process state. It does not identify a specific request.
Attribution headers do not change response bodies or SDK return models.

## Input failure and cleanup contracts

Direct primitive failures use `{code, message, details}`. Batch item failures use the same `code`
as `error_code` and place `{"code": code, ...details}` in `output`.

Native input preserves whether replay is safe:

| Code | HTTP | Meaning |
|---|---:|---|
| `input_backend_unavailable` | 503 | Input emission did not start. Details identify the selected `input_backend`, with `retry_safe=true` and `emission_state="not_started"`. |
| `input_may_be_partial` | 500 | Input emission may have started. Details identify the selected `input_backend` and use `emission_state="possibly_partial"`. `retry_safe` is false for presses, typing, clicks, and drags, but true for idempotent key/button releases. |
| `input_state_conflict` | 409 | The target key or button was already held, so emission did not start. Release or change the conflicting state before retrying. |

`retry_safe` describes duplicate-execution safety only. It is not a promise that the condition is
transient or a recommendation to retry automatically. In particular, callers must not replay an
`input_may_be_partial` press, typing, click, or drag operation through another adapter. A
key/button release is idempotent and explicitly reports `retry_safe=true`.

`input.release_all` retains successfully released controls in `output.keys` and `output.buttons`.
If cleanup is incomplete it fails with `code="release_all_incomplete"`, lists controls still held
under `output.remaining`, and includes bounded per-control failure metadata in `output.failures`.
Raw adapter exception messages are not returned. When batch error handling performs secondary
cleanup, incomplete cleanup is attached under the primary item's `output.cleanup`; it never
replaces the original action or timeout code.

## Experimental observations

Post-action first-visual-change observation is an Alpha feature exposed through an Experimental
Python SDK interface:

```python
with computer.observation_stream(fps=0.01) as observations:
    result = observations._experimental_act_until_visual_change(
        actions=[{"type": "click", "x": 100, "y": 100}],
    )
```

The method issues the batch once and returns a correlated frame after detected change or timeout.
It does not establish semantic application readiness, visual stability, or safety of the next
action. `ObservationClient.act_and_observe(...)` remains a behavior-preserving compatibility name,
not a promoted stable contract. Parameters, result interpretation, failure modes, and maturity are
owned by the [Alpha visual-change observation guide](experimental-visual-change-observation.md).

Provider adapters remain translation layers over stable action primitives. They do not own
model-loop synchronization or application settle policy.

## Observability

Structured JSON logs are enabled by default and redact secret-bearing fields. Optional
OpenTelemetry is disabled by default and only activates when `COMPUTER_USE_OTEL_ENABLED=true` and
`opentelemetry-api` is importable in the current environment. The implemented span boundaries are
SDK request, daemon route, daemon action execution, artifact write/sync, and trace replay step.
Attributes use bounded route/action metadata, never query strings, Authorization headers, typed
text, clipboard text, screenshot bytes, recording bytes, stdout, or stderr.

## Provider adapters

`OpenAIAdapter`, `AnthropicAdapter`, and `ActionExecutor` are translation layers over
`computer.actions`. They validate provider-returned action JSON, normalize it to the native
action schema, optionally apply an explicit `CoordinateSpace`, run the `before_action` hook, and
then call the SDK action namespace. They do not instantiate provider clients, call provider APIs,
hold prompts, or own confirmation policy.

Unknown provider actions raise `UnsupportedActionError` by default, even when a future provider
payload includes fields this SDK does not yet know about. `allow_unknown=True` is an explicit
provider-adapter compatibility escape hatch that normalizes unknown provider payloads to a
zero-duration wait with redacted provider-action metadata. It does not make the native
`ActionExecutor` accept unknown `ComputerAction` types; the native daemon action schema remains
closed so caller bugs fail before execution.

Adapter-normalized actions carry redacted provider provenance in action metadata. When daemon
action tracing is enabled, the trace writer promotes that metadata to `TraceEntry.provider_action`
and keeps `TraceEntry.normalized_action` as the native action that was executed. Typed text and
other sensitive provider fields are replaced with redaction metadata before they can be written to
trace NDJSON.

Provider-shaped output helpers are pure conversion helpers over native models. Use
`openai_computer_call_output(screenshot, call_id=...)` to build an OpenAI
`computer_call_output` item from a native `Screenshot`. Use `anthropic_tool_result(...)` to build
an Anthropic `tool_result` from a native `Screenshot` or safe `ActionResult` summary. These helpers
do not call provider APIs, do not import provider SDKs, and keep native screenshot metadata
available through `openai_screenshot_metadata(...)` and `anthropic_screenshot_metadata(...)`
without putting raw bytes or base64 payloads in metadata.

Policy hooks run before execution and see the normalized native action after any explicit
coordinate transform. If a hook returns anything other than `ActionDecision(decision="allow")`,
the executor raises before sending the batch to the daemon.

Anthropic tool versions are gated. `computer_20241022` supports the reference action set such as
`mouse_move`, click variants, destination-only drag, `key`, `type`, `screenshot`, and
`cursor_position`. `computer_20250124` adds enhanced input actions such as `scroll`,
`left_mouse_down`, `left_mouse_up`, `hold_key`, `wait`, and `triple_click`. `computer_20251124`
adds `zoom`. Older versions reject newer actions instead of accepting them silently.

## Modal orchestration signatures

Native async creation, attachment, and named acquisition are lazy, one-shot context managers:

```python
placed = ComputerConfig(
    runtime={"modal_environment": "main", "modal_region": "us-west-2"},
)
async with AsyncComputerSandbox.create(config=placed) as computer:
    await computer.mouse.click(320, 240)

async with AsyncComputerSandbox.create_unplaced(
    config=ComputerConfig(),
) as computer:
    await computer.mouse.click(320, 240)

async with AsyncComputerSandbox.attach(sandbox_id="sb-...") as computer:
    await computer.screenshots.full()

async with AsyncComputerSandbox.attach_or_create(
    name="support-desktop",
    config=ComputerConfig(),
) as computer:
    await computer.screenshots.full()
```

`create()` is the primary handoff owner. On entry, it rejects a missing environment, a missing or
broad region, malformed placement, tunnel ingress, control VNC, and warm-pool tagging before any
Modal lookup or Sandbox allocation. Those modes cannot produce the primary handoff contract.
`create_unplaced()` is the explicit low-level compatibility path. It can own a desktop for direct
namespace work, but it does not promise an eligible placed handoff.

Modal work begins on entry, and every accepted context is ready when it yields. Async attachment accepts
exactly one Modal selector: `sandbox_id`, `name`, or `run_id`. Direct daemon URLs belong to
`AsyncDaemonClient`. Async orchestration does not expose `wait=False`.

Exiting an async created context terminates, detaches, and closes its owned Sandbox. Exiting an
async attached context detaches and closes without termination. `await computer.detach()`
transfers a created Sandbox to caller-managed ownership. `await computer.terminate()` explicitly
stops either an owned or attached target. Failed or cancelled creation reclaims any allocated
Sandbox before the error escapes. Core imports remain usable without Modal installed; entering a
provisioning context requires the Modal extra.

See [`async_modal_owner.py`](../examples/async_modal_owner.py) for unconditional ownership and
[`async_named_desktop.py`](../examples/async_named_desktop.py) for named acquisition.

The SDK supports four explicit attach paths:

```python
ComputerSandbox.attach(sandbox_id="sb-...")
ComputerSandbox.attach(name="desktop-1", app_name="modal-computer-use")
ComputerSandbox.attach(run_id="support-ticket-123")
ComputerSandbox.attach(base_url="https://daemon.example", token="...")
```

The executable [`attach_existing_sandbox.py`](../examples/attach_existing_sandbox.py) example
accepts exactly one Modal selector from command-line arguments or environment variables, waits for
readiness, and detaches without terminating the caller-owned Sandbox:

```bash
uv run python examples/attach_existing_sandbox.py --sandbox-id sb-...
```

Pass exactly one selector. `token` is valid only with `base_url`; Modal-backed selectors resolve
their authorization from the target Sandbox.

Modal-backed attachment is app-scoped. New Sandboxes carry `computer-use.app_id`, and ID, name,
and run-ID attachment verify the requested app before returning a client. Set
`allow_legacy_unscoped=True` only while migrating an untagged Sandbox that Modal already resolves
inside that app. A conflicting app tag always fails.

`run_id` is the canonical Sandbox-lifetime correlation identifier. It is a tag, not an allocation
lock. `request_id` is only a deprecated configuration alias for compatibility boundaries.

`ComputerSandbox.create(wait=True)` waits for both Modal's sandbox readiness probe, when the
installed Modal SDK exposes it, and the daemon `/readyz` endpoint. `wait=False` returns after the
connect token is created and does not poll daemon desktop readiness.

`ComputerSandbox.attach_or_create(name=...)` acquires one live named Sandbox:

```python
computer = ComputerSandbox.attach_or_create(
    name="support-desktop",
    config=ComputerConfig(),
)
```

The required name is Modal's app-scoped live allocation key. The SDK first resolves the name. A
genuine `NotFoundError` permits creation with that exact name. If a competing creator wins, Modal
returns `AlreadyExistsError` and the SDK performs a bounded lookup of the winner. Run-ID tags are
available for correlation and `attach(run_id=...)`, but never arbitrate creation.

Existing targets must carry the expected app and config-hash tags. If the caller omitted a run ID,
the target's tagged run ID is adopted before the hash check. Explicit run-ID conflicts, missing
run IDs, missing hashes, and hash mismatches fail closed with `ConfigConflictError`. Use
`attach(name=...)` for an intentional connection to a legacy or incompatible target.

Creation arguments such as `image`, VNC mode, tags, secrets, volumes, owner, and extra Modal
Sandbox options are validated before acquisition but apply only when this call creates the target.
They do not mutate an existing target or participate in its compatibility check. Named acquisition
uses the default tag profile because its run-ID and config-hash tags are part of that check.

`attach(...)` is non-blocking by default because callers may attach only to inspect metadata or
terminate a Sandbox. Pass `wait=True` to poll `/readyz` after attaching. Synchronous
`attach_or_create(...)` defaults to `wait=True` for both branches. Native async named acquisition
is always ready on entry.
If readiness times out, `attach(...)` closes the client it created but does not terminate the
existing target. This cleanup applies equally to Modal handles and direct `base_url` attachments.
The readiness timeout remains the primary error if client cleanup also fails; only the cleanup
exception type is attached as diagnostic context.
Detaching closes the local client and Modal handle. It does not terminate an attached Sandbox; only
the lifecycle owner should do that.

Context cleanup reflects ownership:

- `create()` owns its Modal Sandbox, so context exit terminates, detaches, and closes the client;
- Modal-backed `attach()` and existing-target `attach_or_create()` handles detach and close without
  terminating the remote Sandbox;
- `local()` and direct `base_url` attachments close only their daemon connection;
- an explicit `detach()` transfers a created Sandbox to caller-managed ownership and prevents a
  later context exit from terminating it.

Any failure after a new Sandbox is allocated terminates and detaches that resource. Attachment
failure never terminates an existing Sandbox. Cleanup errors are recorded without replacing the
original provisioning or readiness failure.

Attached `ComputerSandbox.metadata()` returns Modal metadata when available: sandbox ID,
app name, name, run ID, owner, creation time, config hash, tags, and artifact directory. It does
not include connect tokens or noVNC URLs produced outside explicit debug helpers.

`ComputerSandboxManager` is a thin Modal orchestration facade. It can create, attach, list,
find by run ID, terminate by sandbox ID, and inspect stale sandboxes for cleanup. It does not own
prompts, provider policies, messages, or task loops.
`cleanup_expired(ttl_seconds=..., owner=None, dry_run=True)` returns a `SandboxCleanupResult` with
candidate, skipped, and error items. Missing or malformed creation timestamps are skipped;
`dry_run=False` terminates only expired app-tagged sandboxes with valid creation metadata. Untagged
legacy resources are never bulk terminated.

`ComputerSandbox.create(..., **sandbox_kwargs)` rejects overrides of SDK-owned app, network,
ingress, environment, readiness, and ownership-tag fields. Ordinary Modal arguments remain
available when they do not replace those boundaries.

`ComputerSandbox.snapshot_directory(path)` and `ComputerSandbox.mount_image(path, image)` expose
the directory snapshot and restore signatures. `snapshot_filesystem()` is a compatibility helper
when the installed Modal SDK provides it.

`run_modal_daemon_command(computer, command, path=...)` runs an explicit `inherited`, `connect`, or
`target-loopback` path. `run_modal_daemon_command_with_fallback(..., external_runner=...)` may use
the external runner only when endpoint preparation fails before dispatch; it never replays a
command after dispatch begins. `ModalDaemonCommandResult` reports whether fallback was used, a
stable reason, and a sanitized exception type without raw exception text.

Warm-pool browser validation raises `BrowserReadinessError`; first-frame validation raises
`FrameValidationError`. These types remain compatible with `RuntimeError` and `ValueError`, so
orchestration can distinguish expected candidate rejection from unrelated programming errors.

The [Modal deployment guide](modal-deployment.md) covers configuration, image selection, attach and
recovery procedures, runner topology, warm capacity, persistent storage, and cleanup operations.

## Action, budget, and command contracts

`/v1/computer/status` includes a budget snapshot for actions, screenshots, artifact bytes
(including recordings), and recording seconds.

`/v1/actions/run` executes ordered batches with per-action timeouts and a whole-batch duration
limit. Timeout precedence is `action.timeout_ms`, then request `max_action_timeout_ms`, then
daemon default; `screenshot_after` uses request `max_action_timeout_ms`, then daemon default.
Values above the configured daemon maximum are rejected. The whole-batch duration limit is a
hard deadline and stops the batch even when `continue_on_error` is true. Timeout results include
`error_code="timeout"` and an output `scope` of `action` or `batch`. `Idempotency-Key` replays
the original complete batch result without re-executing actions, incrementing budgets, or
duplicating trace/artifact writes; reusing a key with a different request body returns `409`.
`continue_on_error` applies between top-level batch items. A `hold_key` action is treated as one
compound top-level item: nested actions run while the key is held, and the first nested failure
releases the key and fails that `hold_key` item before later nested actions run.
Nested `hold_key` action trees are canonical only through `/v1/actions/run`; the direct
`/v1/keyboard/hold` route is primitive-only and accepts only `key` plus optional `duration_ms`.
Nested trees are preflighted iteratively and fail closed. The default maximum depth is 32 and the
configurable range is `1..128`; the depth limit cannot be disabled. Batch size remains 50 by
default with a hard configuration maximum of 500 actions, including nested actions.
Action budgets count attempted executable desktop actions after validation, including failed
and timed-out actions. Screenshot and zoom actions count against screenshot/artifact budgets
instead, and cursor-position queries do not consume the action budget. Successful action-route
responses include timing metadata as `timing.daemon_ms`, measured inside the daemon for the
batch request. The timing object contains only elapsed milliseconds and no command strings,
stdout/stderr, typed text, clipboard text, screenshots, artifacts, or paths.
`actions.input_rate_limit_per_sec` and `actions.input_rate_limit_burst` configure one daemon-local
token bucket. The defaults are 100 normalized input-work tokens per second and a 400-token
burst. Repeated clicks, long typing, large scrolls, drag paths, hotkeys, and nested `hold_key`
actions cost more than a simple move or click. Screenshots, waits, zooms, and cursor queries use no
input tokens. The daemon computes and reserves the complete recursive batch cost before mutation.
A batch that can fit but lacks current credit returns `429 rate_limited`, `retry_after_ms`, and
`Retry-After`. A batch whose cost exceeds the configured burst returns the non-retryable
`422 input_cost_exceeds_burst`. Neither response executes an action or creates a Step receipt.
Direct desktop mutation routes use the same bucket, so they cannot bypass the trajectory limit.
`screenshot_after` is an implicit trailing screenshot operation. Its screenshot and artifact
budgets are reserved after earlier batch actions complete, immediately before capture, so a budget
failure is returned as a trailing `screenshot_after` result rather than rolling back already
executed actions. Pixel-budget validation is different: because output geometry is known up front,
oversized `screenshot_after` requests fail validation before any batch action executes.
`/v1/commands/run` is serialized under the daemon input lock because callers can run GUI-affecting
tools such as `xdotool`. Command stdout/stderr and process log tails remain available to
authenticated callers for debugging, but known secret-bearing substrings such as bearer tokens,
noVNC URLs, and artifact URIs are sanitized before the daemon returns them.
Command and app-launch vectors default to 65,536 arguments. Each argument is also bounded by the
Linux encoded-byte limit derived from page size; kernel `E2BIG` failures return a sanitized `422`.
Drag paths default to 1,024 points and key/modifier collections default to 64 entries. These
collection caps can be explicitly disabled with zero. Typed and clipboard text rely on the 16 MiB
JSON envelope instead of smaller field caps. Automatic long-text typing selects clipboard before
native keymap expansion; explicit native typing emits bounded chunks.

The daemon applies a 16 MiB default receive ceiling to HTTP bodies and WebSocket messages.
Artifact PUTs are excluded because they stream. Hot-session and observation-stream WebSockets use
global default connection caps of 64 and 16. Limit rejection happens at admission or protocol
boundaries and adds no per-frame application accounting.

`ComputerTrace` represents validated trace entries. Replay skips redacted typed text and emits
results with screenshot bytes and base64 payloads redacted. The [trace and replay guide](trace-replay.md)
owns capture, validation, replay commands, target selection, and failure handling.

Benchmark commands are not part of the SDK contract. See [Benchmarking](benchmarking.md) for
prerequisites, commands, cleanup, output, and reporting rules; see [Performance](performance.md)
for interpretation and design guidance.

Recording metadata includes the output path, `artifact_uri`, size, duration, SHA-256, status,
`ffmpeg_args`, return code, stop method, and bounded ffmpeg diagnostics (`stderr_path`,
`stderr_tail`, `error`) when a recording fails or emits useful stderr. The daemon stores
diagnostics as files and returns only a short tail.

`computer.artifacts.sync()` returns `ArtifactSyncResult`. In the built-in local/daemon store it is
an explicit no-op that reports `persistent=false` unless the store was configured as persistent.
With persistent storage configured, it reports `ok=true` only after the configured persistence
operation succeeds. See [Modal deployment](modal-deployment.md) for Volume versions, mounts,
commit visibility, reload behavior, and concurrency guidance.

For complete route schemas and request/response models, see the generated
[OpenAPI schema](openapi.json). The active product specification is
[modal-computer-use product specification](spec/product-spec.md).
