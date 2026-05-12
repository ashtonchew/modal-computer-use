# Modal Deployment

`ComputerSandbox.create()` lazily imports Modal, builds or accepts a Modal `Image`, and starts:

```bash
python -m modal_computer_use.daemon
```

The daemon listens on port `8080`. Modal readiness uses `modal.Probe.with_tcp(8080)` to
wait until the daemon port is accepting connections; SDK/client readiness should still
target `/readyz`, not only `/healthz`.
Local repository commands still use `uv run`; the sandbox image command stays `python -m`
because the Modal image API installs the package into the image runtime.

Current Modal docs state that Sandbox Connect Tokens authenticate HTTP/WebSocket requests to a server listening on port `8080`, and that outbound network restrictions use `block_network` and `cidr_allowlist`. noVNC should be exposed only with explicit `encrypted_ports=[6080]`.
Sandbox tags are applied after creation with `Sandbox.set_tags()` and then used for
`Sandbox.list(tags=...)` attach/recovery flows.

`modal.NetworkFileSystem` is intentionally unused. Persistent artifacts should use Modal Volumes in user configuration or examples, then call `computer.artifacts.sync()` when immediate visibility is required.
