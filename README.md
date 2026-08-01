# modal-computer-use

`modal-computer-use` turns a Modal Sandbox into a remotely controllable Linux desktop. Its typed
Python API covers mouse and keyboard input, screenshots, recordings, windows, artifacts, action
batches, and optional provider adapters.

Use it when computer-use agents or applications need typed, provider-neutral control of Linux
desktops on Modal.

The project provides daemon-first control primitives. Autonomous agent orchestration and model
loops stay in application code or examples.

This is an independent community project for Modal.

## Install from source

Use Python 3.12 or later and `uv`. The package is not on PyPI. Install it from GitHub:

```bash
uv add "modal-computer-use @ git+https://github.com/ashtonchew/modal-computer-use.git@v1.1.0"
```

Add the Modal extra when the application will create Modal Sandboxes:

```bash
uv add "modal-computer-use[modal] @ git+https://github.com/ashtonchew/modal-computer-use.git@v1.1.0"
```

The Modal extra supports the Modal 1.5 line and requires Modal 1.5.2 or later. Contributors should
instead follow the [local development guide](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/local-development.md).
The install commands pin the `v1.1.0` GitHub Release. The project is not published to PyPI.

## Run locally

Start a deterministic mock desktop in one terminal:

```bash
COMPUTER_USE_BACKEND=mock \
COMPUTER_USE_LOCAL_TOKEN=dev \
COMPUTER_USE_REQUIRE_CONNECT_USER=false \
COMPUTER_USE_ARTIFACTS_DIR=/tmp/modal-computer-use/artifacts \
COMPUTER_USE_RECORDINGS_DIR=/tmp/modal-computer-use/recordings \
COMPUTER_USE_TRACE_DIR=/tmp/modal-computer-use/traces \
  uv run computer-use-daemon
```

Connect from another terminal:

```python
from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(base_url="http://127.0.0.1:8080", token="dev")
try:
    computer.wait_until_ready()
    computer.mouse.move(100, 120)
    screenshot = computer.screenshots.full(show_cursor=True)
    print(screenshot.width, screenshot.height, screenshot.sha256)
finally:
    computer.detach()
```

The command prints `1024 768` followed by the screenshot's SHA-256 digest. Press Ctrl-C in the
daemon terminal when finished.

The daemon refuses to start without token or Connect authentication. For a local process that
intentionally has no token, set `COMPUTER_USE_ALLOW_UNAUTHENTICATED_LOOPBACK=true`; that mode may
bind only to loopback.

## Run on Modal

Configure Modal credentials once with `uv run modal setup`. Creating a Sandbox can incur Modal
charges.

Save this example as `quickstart.py`:

```python
from modal_computer_use import ComputerConfig, ComputerSandbox

computer = ComputerSandbox.create(config=ComputerConfig())
try:
    computer.wait_until_ready()
    computer.mouse.move(100, 120)
    screenshot = computer.screenshots.full(show_cursor=True)
    print(screenshot.width, screenshot.height, screenshot.sha256)
finally:
    try:
        computer.terminate(wait=True)
    finally:
        computer.detach()
```

Run the example:

```bash
uv run python quickstart.py
```

This uses an inline desktop image and authenticated daemon access on port `8080`. noVNC is off by
default. The [Modal deployment guide](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/modal-deployment.md)
explains browser profiles, network policy, attach and reuse, Volumes, warm capacity, and cleanup.

## Capabilities

- Typed SDK namespaces for lifecycle, input, display, screenshots, recordings, browser and app
  control, processes, commands, artifacts, and debugging.
- Ordered action batches that stop on the first failure by default or continue when requested.
- Local mock and X11 backends that use the same daemon API as Modal deployments.
- OpenAI, Anthropic, and generic adapters that normalize actions without calling provider APIs or
  importing provider SDKs into core modules.
- Structured logs, traces, budgets, rate limits, secret redaction, and path-safe artifact access.
- Reproducible benchmark tooling with tracked, sanitized evidence separated from raw local output.

The generated [OpenAPI schema](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/openapi.json)
defines the HTTP request and response shapes. The [API guide](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/api.md)
describes SDK semantics and behavioral contracts.

## Documentation

- [Documentation map](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/README.md)
- [Configuration reference](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/configuration.md)
- [Modal deployment](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/modal-deployment.md)
- [Performance](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/performance.md)
- [Troubleshooting](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/troubleshooting.md)
- [Release checklist](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/release-checklist.md)
- [Contributing guide](https://github.com/ashtonchew/modal-computer-use/blob/main/CONTRIBUTING.md)

Provider examples are available for [OpenAI](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/03_openai_computer_loop.py)
and [Anthropic](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/anthropic_message_server.py).

## Security

The daemon can control the desktop, read clipboard contents, and access artifacts. Never expose
its control routes as an unauthenticated public service. Treat bearer tokens, noVNC URLs, typed or
clipboard text, screenshots, recordings, and artifacts as secrets.

See the [security policy](https://github.com/ashtonchew/modal-computer-use/blob/main/SECURITY.md) for
the current reporting process. See
[runtime security](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/security.md) for
authentication, redaction, noVNC, artifact, and provider-adapter guidance.
