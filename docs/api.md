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
- `computer.browser`: `open_url`, `status`
- `computer.apps`: `launch`, `open_artifact`
- `computer.commands`: `run`
- `computer.input`: `release_all`
- `computer.lifecycle`: `start`, `stop`, `restart`, `status`
- `computer.processes`: `status`, `restart`, `logs`, `stderr`, `errors`
- `computer.session`: `metadata`, `refresh`
- `computer.debug`: `urls`, `vnc_url`

The daemon exposes `/healthz`, `/readyz`, `/v1/version`, `/v1/capabilities`, and `/v1/*` primitive routes.

## Modal attach and reuse

Modal mode supports three explicit attach paths:

```python
ComputerSandbox.attach(sandbox_id="sb-...")
ComputerSandbox.attach(name="desktop-1", app_name="modal-computer-use")
ComputerSandbox.attach(run_id="support-ticket-123")
```

`run_id` is the canonical sandbox lifetime identifier. `request_id` is only a deprecated
configuration alias for compatibility boundaries.

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

Attached `ComputerSandbox.metadata()` returns safe Modal metadata when available: sandbox ID,
app name, name, run ID, config hash, tags, and artifact directory. It does not include connect
tokens or noVNC URLs produced outside explicit debug helpers.

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
Action budgets count attempted executable desktop actions after validation, including failed
and timed-out actions. Screenshot and zoom actions count against screenshot/artifact budgets
instead, and cursor-position queries do not consume the action budget. Successful action-route
responses include safe timing metadata as `timing.daemon_ms`, measured inside the daemon for the
batch request. The timing object contains only elapsed milliseconds and no command strings,
stdout/stderr, typed text, clipboard text, screenshots, artifacts, or paths.

Trace tooling is available through `ComputerTrace` and the `computer-use` CLI. Use
`computer-use trace validate <path>` to validate trace NDJSON and
`computer-use trace replay <path> --dry-run` to produce a replay plan without contacting a
daemon, Modal, provider APIs, screenshots, or artifact storage. Both commands emit JSON and
return nonzero for invalid traces.

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
measured iteration fails. Recording benchmark output includes safe metadata such as status,
format, size, duration, and stop method, but not recording bytes, raw paths, artifact URIs,
stdout, stderr tails, raw command strings, or ffmpeg argv.
Action benchmark cases also include `daemon_samples_ms`, `daemon_summary_ms`,
`overhead_samples_ms`, `overhead_summary_ms`, and `attribution`. Missing daemon timing is reported
as unavailable for compatibility with old daemons; malformed timing is a structured failure.
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
The SDK does not claim Modal Volume data is visible outside the sandbox unless a concrete Volume
sync/commit path is configured and reported by the daemon.

For the full route schemas and request/response models, see [spec/modal_computer_use_spec_v6.md](spec/modal_computer_use_spec_v6.md).
