# API

The SDK is a thin Python client over the daemon's HTTP API. Start with:

```python
from modal_computer_use import (
    ComputerConfig,
    ComputerSandbox,
    ComputerSessionHandle,
    DaemonClient,
)
```

Namespaces on `ComputerSandbox`:

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

`ComputerSandbox.session_handle()` returns a frozen, versioned `ComputerSessionHandle` for an
SDK-owned Modal desktop created with an explicit requested region and either `attested-tunnel` or
`connect` ingress and `vnc_mode="off" | "view_only"`. Control-mode noVNC targets cannot produce or
enter a handoff. The v2 handle serializes only its protocol version, sandbox/session identity, app
and Modal environment, `requested_modal_region`, ingress, daemon HTTP version, vnc policy, and
config hash. Sandbox/session identity is hidden
from `repr()` but necessarily remains in JSON or cloudpickle/pickle data so Modal can reconnect.
Endpoint URLs, bearer or Connect credentials, noVNC URLs, tags, prompts, typed text, screenshots,
and artifacts are never fields on the handle.

The handle is routing identity, not a bearer credential or an authorization boundary. Do not log
or publish it. A public HTTP wrapper must authenticate callers and authorize the target before it
invokes the deployed Function; the Function's Modal identity is what resolves fresh access.

For non-Python clients, the
[application-owned run gateway example](../examples/modal_run_gateway.py) provides a bounded
spawn-and-poll HTTP control plane. It accepts only opaque application `desktop_key`, `task_key`,
and required `idempotency_key` values. The host application must inject its principal resolver,
ownership catalogs, atomic durable run store, and one deployed trajectory Function dispatcher.
There is no permissive resolver or in-memory production store. Responses contain only the stable
application run ID and sanitized state; provider call identities, handles, task text, results,
endpoints, and tokens stay private.

The gateway's closed lifecycle is `reserved -> dispatching -> running`, followed by a terminal
state or `cancellation_requested`. Every admitted record has an absolute `deadline_at`; extending
a reconciliation interval never extends that deadline. One application-store admission transaction authoritatively
handles replay, quota, exclusive ownership of a stable application desktop identity, run creation,
and creation of a payload-free pending dispatch intent. Versioned HMAC-SHA256 bindings keep raw
idempotency keys and internal desktop/task identities out of the run record. A matching replay is
returned before capacity checks; mismatched replay, desktop contention, and tenant quota exhaustion
return sanitized errors without writes. A second atomic claim moves both the run and intent before
the sole winner may spawn, including on replay after admission commit. The intent is not an
automatic delivery queue and has no worker or delivered state.
A safe cancellation or expiry before dispatch atomically revokes the still-pending intent while it
releases capacity.
A stale dispatch claim becomes `indeterminate` and is never automatically spawned again. Modal
Function dispatch and durable persistence are not one transaction: the stable application run ID
fences a repeated `borrow_async(run_id=run_id, ...)`, but it cannot reconstruct a missing
FunctionCall identity after a dispatch/persistence gap.

`GET /v1/runs/{run_id}` is a durable read and never polls Modal or advances state. The cancel route
only records `cancellation_requested`, its request time, and a bounded cancellation deadline. A
scheduled application reconciler owns polling and provider cancellation. It keyset-scans a bounded
number of bounded pages and atomically leases each due record; every write requires both the opaque
lease token and expected record version. Overlapping reconciler containers are therefore safe.
Single-container Modal settings are cost controls, not correctness primitives.

Pending polls reset the consecutive provider-error counter. Transiently unavailable polls use
capped exponential backoff and become `indeterminate` at the configured error cap. At the absolute
run deadline, recovery first persists cancellation intent, then polls before requesting
`cancel(terminate_containers=False)`. Cancellation recovery polls before each request and remains
`cancellation_requested` after an accepted or transiently failed request. Missing call identity,
irrecoverable provider ambiguity, an error cap, or the cancellation deadline seals the record as
`indeterminate`; it continues to hold quota and its desktop claim.

The `RunStore` contract is intentionally incompatible with the former `reserve_if_absent` example.
Adapters need an explicit migration, drain, or backfill. Existing SHA-only rows cannot safely infer
desktop claims and must not be upgraded implicitly. Terminal `succeeded`, `failed`, and `cancelled`
transitions release quota and the desktop claim exactly once; `indeterminate` retains both.
For HMAC rotation, add the replacement as active and retain every referenced prior key for
verification. Migrate, drain, or expire all rows and tombstones using a retiring version, verify no
references remain, and only then remove that key. A missing retained key version is an internal
configuration/migration failure: admission fails closed with no writes rather than treating an old
identity as new. The example intentionally provides no migration worker or database adapter.

The store contract also owns operator recovery and retention. `SAFE_RELEASE` and `SAFE_REPLACE`
require a sealed `indeterminate` record, expected version, and non-empty actor, reason, and audit
identity. Replacement atomically seals the old record and creates a successor with a fresh run and
idempotency identity; it never reopens the ambiguous run. There is deliberately no production admin
HTTP route. Retention may compact only released `succeeded`, `failed`, or `cancelled` records after
the configured interval. Active, leased, cancellation, unresolved-audit, and all `indeterminate`
records are protected. Replay tombstones must remain authoritative through their fencing window,
and admission must consult them before quota or desktop acquisition.

The gateway dispatcher calls the user-owned trajectory Function with
`(handle, task, run_id, deadline_at)`. The deadline is the original timezone-aware absolute value
from admission, so Function retries or container replacement cannot silently restart the wall-clock
budget.

Use the native-async borrow context inside an async user-owned Modal Function:

```python
FUNCTION_REGION = "us-west"


async def trajectory(handle: ComputerSessionHandle, task: str, run_id: str) -> None:
    async with handle.borrow_async(
        run_id=run_id, function_region=FUNCTION_REGION
    ) as computer:
        for _ in range(3):
            screenshot = await computer.screenshots.full()
            action = await application_model_call(task, screenshot)
            await computer.actions.run([action])
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
The original `ComputerSandbox` owner can inspect `recovery_status()` and call
`acknowledge_recovery(incident_id=...)`; attached objects without its private owner proof fail
closed. Exclusive lease ownership prevents overlapping trajectories for one desktop but is not a
claim that one desktop is safe for multiple tenants.

Keyboard typing accepts `method="auto" | "keystrokes" | "clipboard" | "xdotool"`.
`keystrokes` is the canonical direct-input behavior and uses the configured native or compatibility
input adapter; `auto` uses clipboard paste for long or active-layout-unmapped Unicode text.
`xdotool` remains a legacy explicit compatibility request. The daemon default input adapter is
`auto`, which prefers the persistent XTest/XKB path and falls back before emission when necessary.
For Modal-created sandboxes, noVNC tunnel URLs are owned by Modal orchestration; use
`ComputerSandbox.debug_urls()` rather than the daemon-only `computer.debug.urls()` helper when you
need to know whether a Modal noVNC URL exists.

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

The SDK supports four explicit attach paths:

```python
ComputerSandbox.attach(sandbox_id="sb-...")
ComputerSandbox.attach(name="desktop-1", app_name="modal-computer-use")
ComputerSandbox.attach(run_id="support-ticket-123")
ComputerSandbox.attach(base_url="https://daemon.example", token="...")
```

`run_id` is the canonical sandbox lifetime identifier. `request_id` is only a deprecated
configuration alias for compatibility boundaries.

`ComputerSandbox.create(wait=True)` waits for both Modal's sandbox readiness probe, when the
installed Modal SDK exposes it, and the daemon `/readyz` endpoint. `wait=False` returns after the
connect token is created and does not poll daemon desktop readiness.

`ComputerSandbox.attach_or_create(...)` exposes resumable run-scoped attachment:

```python
computer = ComputerSandbox.attach_or_create(
    run_id="support-ticket-123",
    config=ComputerConfig(),
    reuse="by_run_id",  # "by_run_id", "by_name", or "never"
)
```

For backward compatibility, `reuse=True` maps to `"by_run_id"` and `reuse=False` maps to
`"never"`. `reuse="by_name"` requires `name=...`. Missing run ID or name matches create a new
sandbox when creation arguments are supplied. Ambiguous run ID matches raise a structured
`SandboxAmbiguousError` instead of selecting an arbitrary sandbox.

When reusing an existing sandbox and the sandbox has a `computer-use.config_hash` tag, the SDK
compares it with `compute_config_hash(config)`. Mismatches fail closed with `ConfigConflictError`
by default. Pass `on_config_mismatch="reuse"` only when the caller intentionally accepts the
existing sandbox configuration.

`attach(...)` is non-blocking by default because callers may attach only to inspect metadata or
terminate a sandbox. Pass `wait=True` to poll `/readyz` after attaching. `attach_or_create(...)`
defaults to `wait=True` for both reused and newly-created sandboxes, so resumable workflows get a
desktop-ready handle unless they explicitly pass `wait=False`.
If readiness times out, `attach(...)` closes the client it created but does not terminate the
existing target. This cleanup applies equally to Modal handles and direct `base_url` attachments.
The readiness timeout remains the primary error if client cleanup also fails; only the cleanup
exception type is attached as diagnostic context.

Attached `ComputerSandbox.metadata()` returns Modal metadata when available: sandbox ID,
app name, name, run ID, owner, creation time, config hash, tags, and artifact directory. It does
not include connect tokens or noVNC URLs produced outside explicit debug helpers.

`ComputerSandboxManager` is a thin Modal orchestration facade. It can create, attach, list,
find by run ID, terminate by sandbox ID, and inspect stale sandboxes for cleanup. It does not own
prompts, provider policies, messages, or task loops.
`cleanup_expired(ttl_seconds=..., owner=None, dry_run=True)` returns a `SandboxCleanupResult` with
candidate, skipped, and error items. Missing or malformed creation timestamps are skipped;
`dry_run=False` terminates only expired listed sandboxes with valid creation metadata.

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
Action budgets count attempted executable desktop actions after validation, including failed
and timed-out actions. Screenshot and zoom actions count against screenshot/artifact budgets
instead, and cursor-position queries do not consume the action budget. Successful action-route
responses include timing metadata as `timing.daemon_ms`, measured inside the daemon for the
batch request. The timing object contains only elapsed milliseconds and no command strings,
stdout/stderr, typed text, clipboard text, screenshots, artifacts, or paths.
`actions.input_rate_limit_per_sec` maps to `COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC` and enforces a
simple per-daemon rolling one-second action limit. The limit applies to `/v1/actions/run` and
direct desktop-affecting mutation routes, including mouse, keyboard, clipboard writes/clears,
windows, apps, browser, and commands; failures return `rate_limited` without executing the
over-limit action.
`screenshot_after` is an implicit trailing screenshot operation. Its screenshot and artifact
budgets are reserved after earlier batch actions complete, immediately before capture, so a budget
failure is returned as a trailing `screenshot_after` result rather than rolling back already
executed actions. Pixel-budget validation is different: because output geometry is known up front,
oversized `screenshot_after` requests fail validation before any batch action executes.
`/v1/commands/run` is serialized under the daemon input lock because callers can run GUI-affecting
tools such as `xdotool`. Command stdout/stderr and process log tails remain available to
authenticated callers for debugging, but known secret-bearing substrings such as bearer tokens,
noVNC URLs, and artifact URIs are sanitized before the daemon returns them.

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
[modal-computer-use specification v8](spec/modal_computer_use_spec_v8.md).
