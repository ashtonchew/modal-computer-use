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
instead, and cursor-position queries do not consume the action budget.

Trace tooling is available through `ComputerTrace` and the `computer-use` CLI. Use
`computer-use trace validate <path>` to validate trace NDJSON and
`computer-use trace replay <path> --dry-run` to produce a replay plan without contacting a
daemon, Modal, provider APIs, screenshots, or artifact storage. Both commands emit JSON and
return nonzero for invalid traces.

Recording metadata includes the output path, `artifact_uri`, size, duration, SHA-256, status,
ffmpeg argv, return code, stop method, and bounded ffmpeg diagnostics (`stderr_path`,
`stderr_tail`, `error`) when a recording fails or emits useful stderr. The daemon stores
diagnostics as files and returns only a short tail.

For the full route schemas and request/response models, see [spec/modal_computer_use_spec_v6.md](spec/modal_computer_use_spec_v6.md).
