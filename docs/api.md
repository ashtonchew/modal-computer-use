# API

The public SDK starts with:

```python
from modal_computer_use import ComputerSandbox, ComputerConfig, DaemonClient
```

Main namespaces:

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

The daemon exposes `/healthz`, `/readyz`, `/v1/version`, `/v1/capabilities`, and `/v1/*` primitive routes.

`/v1/computer/status` includes a budget snapshot for actions, screenshots, artifact bytes
(including recordings), and recording seconds.

`/v1/actions/run` executes ordered batches with per-action timeouts. Timeout precedence is
`action.timeout_ms`, then request `max_action_timeout_ms`, then daemon default. Values above
the configured daemon maximum are rejected. `Idempotency-Key` replays the original complete
batch result without re-executing actions, incrementing budgets, or duplicating trace/artifact
writes; reusing a key with a different request body returns `409`.

Recording metadata includes the output path, `artifact_uri`, size, duration, SHA-256, status,
ffmpeg argv, return code, stop method, and bounded ffmpeg diagnostics (`stderr_path`,
`stderr_tail`, `error`) when a recording fails or emits useful stderr. The daemon stores
diagnostics as files and returns only a short tail.
