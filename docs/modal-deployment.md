# Modal Deployment

`ComputerSandbox.create()` lazily imports Modal, builds or accepts a Modal `Image`, and starts the daemon inside the sandbox:

```bash
python -m modal_computer_use.daemon
```

## Readiness

The daemon listens on port `8080`. Modal waits for the port to accept connections via `modal.Probe.with_tcp(8080)`. SDK clients should poll `/readyz` rather than relying on the TCP probe, because `/healthz` only confirms the daemon process is alive, not that the desktop is up.

The image launch command stays `python -m modal_computer_use.daemon`. Local repo work uses `uv run computer-use-daemon`. They differ because Modal installs the package into the image runtime; `uv run` is for the editable repo checkout.

## Sandbox configuration

Per current Modal docs, configure the Sandbox with:

- **Connect Tokens** authenticate HTTP and WebSocket requests to the daemon on port `8080`. See [security.md](security.md).
- **Network restrictions** use `block_network` and `cidr_allowlist` to limit outbound traffic.
- **noVNC** is exposed only with explicit `encrypted_ports=[6080]`. Do not expose it on the public internet; use it only when you need manual debugging through an access-controlled tunnel.
- **Tags** are applied after creation with `Sandbox.set_tags()` and used for `Sandbox.list(tags=...)` attach and recovery flows.

`modal.NetworkFileSystem` is intentionally unused. Persistent artifacts should use Modal Volumes in user configuration or examples; call `computer.artifacts.sync()` when the file needs to be visible from outside the sandbox immediately.

## Authentication

For local Modal smoke tests, prefer Modal's native local auth:

```bash
uv sync --extra modal
uv run modal token new
uv run pytest -m modal
```

The Modal SDK reads credentials from `~/.modal.toml`, or from `MODAL_CONFIG_PATH` if set. In CI, use a Modal service user and expose `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` as CI secrets. The repository does not auto-load `.env` for Modal SDK auth; `.env` files are better used through `modal.Secret.from_dotenv()` when creating remote runtime secrets.

The noVNC view-only smoke test is opt-in because it creates a tunnel:

```bash
MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 uv run pytest tests/test_modal_integration.py -q
```

The test checks daemon readiness and process state without printing noVNC URLs or tokens.
