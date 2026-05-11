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
