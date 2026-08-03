# modal-computer-use

`modal-computer-use` turns a Modal Sandbox into a remotely controllable Linux desktop through a
typed, provider-neutral Python SDK and an in-Sandbox daemon.

This is an independent project using Modal.

## Quick start

Use Python 3.12 or later and `uv`. Install the Modal extra from PyPI:

```bash
uv add "modal-computer-use[modal]"
```

The Modal extra supports the Modal 1.5 line and requires Modal 1.5.2 or later.

Save this as `quickstart.py`:

```python
from modal_computer_use import (
    BrowserConfig,
    ComputerConfig,
    ComputerSandbox,
    ResourceConfig,
)

config = ComputerConfig(
    resources=ResourceConfig(profile="browser"),
    browser=BrowserConfig(kind="chromium"),
)

with ComputerSandbox.create(config=config) as computer:
    computer.browser.open_url("https://example.com")
    computer.mouse.move(320, 240)
    screenshot = computer.screenshots.full()
    screenshot.save("screenshot.png")
    print(screenshot.width, screenshot.height, screenshot.sha256)
```

Run it:

```bash
uv run python quickstart.py
```

When the `with` block ends, the SDK terminates the Sandbox and closes the connection.

## Core API

`ComputerSandbox` is the primary synchronous entry point. `AsyncComputerSandbox` provides native
async Modal creation and attachment with the same ownership rules; see the
[async owner example](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/async_modal_owner.py).
`AsyncDaemonClient` connects to an existing daemon without blocking the event loop.

| Task | Representative API |
| --- | --- |
| Create or connect | `ComputerSandbox.create()`, `ComputerSandbox.attach()`, `AsyncComputerSandbox.create()` |
| Input | `computer.mouse.move()`, `computer.keyboard.type()`, `computer.clipboard.get_text()` |
| Observe | `computer.screenshots.full()`, `computer.display.info()`, `computer.windows.list()` |
| Browser and apps | `computer.browser.open_url()`, `computer.apps.launch()` |
| Execute | `computer.actions.run()`, `computer.commands.run()` |
| Files and recordings | `computer.artifacts.download()`, `computer.recordings.start()` |
| Operate | `computer.lifecycle.status()`, `computer.processes.logs(name)` |

Action batches validate the full request before execution. They stop on the first error by default,
can opt into `continue_on_error`, and can capture a trailing screenshot in the same request.

Inside an active `ComputerSandbox`:

```python
batch = computer.actions.run(
    [
        {"type": "move", "x": 320, "y": 240},
        {"type": "click", "x": 320, "y": 240},
    ],
    screenshot_after=True,
)
```

`batch.screenshot` contains the trailing observation when the batch succeeds.

See the [API guide](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/api.md) for
namespace semantics and the generated
[OpenAPI schema](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/openapi.json) for
HTTP request and response shapes.

## How it works

`ComputerSandbox.create()` starts a new Modal Sandbox. If you use it in a `with` block, the SDK
terminates the Sandbox automatically when the block ends. `ComputerSandbox.attach()` connects to an
existing Sandbox. Leaving an attached `with` block closes the SDK connection but keeps the Sandbox
running.

A daemon inside the Sandbox executes desktop actions, captures screenshots and recordings, runs
commands, and reads or writes files through `computer.artifacts`.

## Examples

| Workflow | Example |
| --- | --- |
| Configure and prewarm a browser | [`browser_profile.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/browser_profile.py) |
| Attach without taking lifecycle ownership | [`attach_existing_sandbox.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/attach_existing_sandbox.py) |
| Capture and download a recording | [`recording_lifecycle.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/recording_lifecycle.py) |
| Persist artifacts with a Modal Volume | [`volume_artifacts.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/volume_artifacts.py) |
| Hand a desktop to a Modal Function | [`modal_function_session_handoff.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/modal_function_session_handoff.py) |
| Run an application-owned model loop | [OpenAI](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/03_openai_computer_loop.py) · [Anthropic](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/anthropic_message_server.py) |

Provider adapters translate actions and screenshot results. They do not call provider APIs or move
the model loop into the core package.

## Documentation

- [Documentation map](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/README.md)
- [Modal deployment](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/modal-deployment.md) ·
  [Configuration](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/configuration.md) ·
  [Artifacts](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/artifacts.md) ·
  [Troubleshooting](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/troubleshooting.md)
- [OpenAI adapter](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/openai-adapter.md) ·
  [Anthropic adapter](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/anthropic-adapter.md)
- [Contributing guide](https://github.com/ashtonchew/modal-computer-use/blob/main/CONTRIBUTING.md)

## Local development

See the [local development guide](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/local-development.md)
for daemon startup, mock and X11 backends, synchronous and async clients, authentication, and
repository checks.

## Security

The daemon can control the desktop and access clipboard contents, screenshots, recordings, and
artifacts. Do not expose it without authentication.

See the [security policy](https://github.com/ashtonchew/modal-computer-use/blob/main/SECURITY.md) for
reporting vulnerabilities and the
[runtime security guide](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/security.md)
for deployment guidance.
