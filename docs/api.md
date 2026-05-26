# API

The SDK is a thin Python client over the daemon's HTTP API. Start with:

```python
from modal_computer_use import ComputerSandbox, ComputerConfig, DaemonClient
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
For Modal-created sandboxes, noVNC tunnel URLs are owned by Modal orchestration; use
`ComputerSandbox.debug_urls()` rather than the daemon-only `computer.debug.urls()` helper when you
need to know whether a Modal noVNC URL exists.

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

## Modal attach and reuse

Modal mode supports three explicit attach paths:

```python
ComputerSandbox.attach(sandbox_id="sb-...")
ComputerSandbox.attach(name="desktop-1", app_name="modal-computer-use")
ComputerSandbox.attach(run_id="support-ticket-123")
```

`run_id` is the canonical sandbox lifetime identifier. `request_id` is only a deprecated
configuration alias for compatibility boundaries.

`ComputerSandbox.create(wait=True)` waits for both Modal's sandbox readiness probe, when the
installed Modal SDK exposes it, and the daemon `/readyz` endpoint. `wait=False` returns after the
connect token is created and does not poll daemon desktop readiness.

`ComputerSandbox.snapshot_directory(path)` and `ComputerSandbox.mount_image(path, image)` expose
Modal's documented directory snapshot flow for filesystem state. `snapshot_filesystem()` remains a
compatibility helper for SDKs that expose it, but v1 examples restore directory snapshots by
mounting them into a fresh normal computer-use sandbox instead of using the snapshot image as the
whole desktop image.

Use `attach_or_create` when a caller wants resumable run-scoped sandboxes:

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

Attached `ComputerSandbox.metadata()` returns safe Modal metadata when available: sandbox ID,
app name, name, run ID, owner, creation time, config hash, tags, and artifact directory. It does
not include connect tokens or noVNC URLs produced outside explicit debug helpers.

## Modal manager lifecycle helpers

`ComputerSandboxManager` is a thin Modal orchestration facade. It can create, attach, list,
find by run ID, terminate by sandbox ID, and inspect stale sandboxes for cleanup. It does not own
prompts, provider policies, messages, or task loops.

```python
from modal_computer_use import ComputerSandboxManager

manager = ComputerSandboxManager(app_name="modal-computer-use")
refs = manager.list(owner="alice")
ref = manager.find_by_run_id("support-ticket-123")

plan = manager.cleanup_expired(
    ttl_seconds=3600,
    owner="alice",
    dry_run=True,  # default; does not terminate anything
)
```

Cleanup uses the safe `computer-use.created_at` tag applied at sandbox creation. Missing or
malformed creation timestamps are reported as skipped instead of guessed. Passing
`dry_run=False` terminates only expired listed sandboxes with valid creation metadata and returns
a `SandboxCleanupResult` containing counts plus safe per-sandbox candidate, skipped, and error
items. Cleanup results do not include connect tokens, noVNC URLs, command output, artifact bytes,
or provider credentials.

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
responses include safe timing metadata as `timing.daemon_ms`, measured inside the daemon for the
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

Trace tooling is available through `ComputerTrace` and the `computer-use` CLI. Use
`computer-use trace validate <path>` to validate trace NDJSON and
`computer-use trace replay <path> --dry-run` to produce a replay plan without contacting a
daemon, Modal, provider APIs, screenshots, or artifact storage. Use
`computer-use trace replay <path> --base-url <daemon-url> --token <token>` for local replay, or
`--target-run-id` / `--sandbox-id` for Modal-backed replay. Real replay validates first, skips
redacted typed text, executes supported normalized actions through the target sandbox, redacts
screenshot bytes/base64 in emitted results, and returns nonzero for validation or action failures.

Benchmark tooling is available through `computer-use benchmark report` and
`computer-use benchmark action-batch`. Use
`computer-use benchmark report --mock-local --iterations 5` for an in-process release-style
report, or pass `--base-url`, optional `--token`, and optional `--output` for an already-running
daemon. The report includes action-batch, move+click, full screenshot, compressed screenshot, and
100-character typing, and recording start/stop benchmarks, plus explicit `not_measured` entries
for Modal/Sandbox.exec unless requested, cold create, and warm attach cases. Add
`--include-sandbox-exec --sandbox-id <id>` with `--base-url` to attach to an existing Modal
Sandbox and compare the daemon move+click hot path with a live `Sandbox.exec` `xdotool`
move+click command. This mode never creates a sandbox. It returns nonzero when any warmup or
measured iteration fails. Use `--modal-region`, `--resource-profile`, `--browser`, `--gpu`, and
`--image-profile` to attach caller-supplied environment labels to the report; these flags are
metadata only and do not create or modify Modal resources. Recording benchmark output includes
safe metadata such as status, format, size, duration, and stop method, but not recording bytes,
raw paths, artifact URIs, stdout, stderr tails, raw command strings, or ffmpeg argv. Reported
`base_url` values strip userinfo, query strings, and fragments before JSON is printed or written.
Action benchmark cases also include `daemon_samples_ms`, `daemon_summary_ms`,
`overhead_samples_ms`, `overhead_summary_ms`, and `attribution`. Missing daemon timing is reported
as unavailable for compatibility with old daemons; malformed timing is a structured failure.

`computer-use benchmark sdk --create-modal-sandbox --surfaces daemon-http` is the explicit
mode that creates a fresh Modal CUA sandbox for benchmarking. In that mode `--gpu`, `--browser`,
`--resource-profile`, `--modal-region`, `--modal-cpu`, and `--modal-memory-mib` are applied to the
created `ComputerConfig`; the run records cold create-to-ready metadata, executes the warm daemon
cases, then terminates and detaches the sandbox.
For latency-sensitive Modal sandboxes, prefer the config surface rather than passthrough Modal
kwargs:

```python
from modal_computer_use import ComputerConfig, ComputerSandbox

computer = ComputerSandbox.create(
    config=ComputerConfig(
        runtime={"modal_region": "us-west"},
        ingress="attested-tunnel",
    )
)
```

`runtime.modal_region=None` leaves placement to Modal. A region only affects newly created
sandboxes; attaching or reusing an existing sandbox cannot relocate it. Because the region lives in
`ComputerConfig`, it participates in the existing config-hash mismatch protection for reuse flows.
Use `computer-use benchmark modal-region-ab` to compare fresh `daemon-transport-floor` runs across
repeatable `--modal-region` values while holding ingress, HTTP version, image, and resource knobs
fixed.
The `type_100_chars` benchmark reports only safe request metadata: `character_count` and `method`.
Use `computer-use benchmark action-batch --mock-local --iterations 5` to run only the action-batch
benchmark against an in-process mock daemon, or pass `--base-url` and optional `--token` for an
already-running daemon. The command emits JSON and returns nonzero when any warmup or measured
iteration fails. It compares one five-action batch request with five separate action requests;
`Sandbox.exec` is explicitly marked `not_measured` in this version.

Recording metadata includes the output path, `artifact_uri`, size, duration, SHA-256, status,
ffmpeg argv, return code, stop method, and bounded ffmpeg diagnostics (`stderr_path`,
`stderr_tail`, `error`) when a recording fails or emits useful stderr. The daemon stores
diagnostics as files and returns only a short tail.

`computer.artifacts.sync()` returns `ArtifactSyncResult`. In the built-in local/daemon store it is
an explicit no-op that reports `persistent=false` unless the store was configured as persistent.
When `storage.persist_artifacts=True` sets `COMPUTER_USE_ARTIFACTS_PERSISTENT=true`, the daemon
runs Modal's documented Volume v2 `sync <artifacts_dir>` mountpoint command and reports `ok=true`
only if that command succeeds. Local orchestration can then verify data through
`Volume.read_file` or CLI `modal volume get`. Modal Volume v1 is not a supported immediate-sync
target for this package. Already-mounted reader containers must use `Volume.reload()` /
`Sandbox.reload_volumes()` before checking for committed changes. Reload can fail with open files,
and concurrent writes to the same Volume paths are last-writer-wins, so production artifact paths
should be run-scoped.

For the full route schemas and request/response models, see [spec/modal_computer_use_spec_v7.md](spec/modal_computer_use_spec_v7.md).
