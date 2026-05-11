# Modal Deployment

`ComputerSandbox.create()` lazily imports Modal, builds or accepts a Modal `Image`, and starts:

```bash
python -m modal_computer_use.daemon
```

The daemon listens on port `8080`. Readiness should target `/readyz`, not only `/healthz`.

Current Modal docs state that Sandbox Connect Tokens authenticate HTTP/WebSocket requests to a server listening on port `8080`, and that outbound network restrictions use `block_network` and `cidr_allowlist`. noVNC should be exposed only with explicit `encrypted_ports=[6080]`.

`modal.NetworkFileSystem` is intentionally unused. Persistent artifacts should use Modal Volumes in user configuration or examples, then call `computer.artifacts.sync()` when immediate visibility is required.
