<div align="center">
  <img src="./docs/assets/modal-computer-use-logo.png" width="160" alt="Modal Computer Use logo">
  <h1>modal-computer-use</h1>
</div>

`modal-computer-use` turns a Modal Sandbox into a remotely controllable Linux desktop through a
typed, provider-neutral Python SDK and an in-Sandbox daemon.

This is an independent project using Modal.

## Quick start

Use Python 3.12 or later and `uv`. Install the Modal extra from PyPI:

```bash
uv add "modal-computer-use[modal]"
```

The Modal extra supports the Modal 1.5 line and requires Modal 1.5.2 or later.

Save this as `quickstart.py`. Choose one exact Modal region for both the Function and the Sandbox.
The resource values are application choices, not SDK defaults.

```python
import uuid

import modal

from modal_computer_use import AsyncComputerSandbox, ComputerConfig, ComputerSessionHandle

APP_NAME = "computer-use-quickstart"
REGION = "us-west-2"

app = modal.App(APP_NAME)
function_image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "modal-computer-use[modal]"
)


@app.function(
    image=function_image,
    region=REGION,
    cpu=1.0,
    memory=2048,
    min_containers=0,
    retries=0,
    timeout=900,
)
async def trajectory(handle: ComputerSessionHandle, run_id: str) -> tuple[int, int]:
    async with handle.borrow_async(run_id=run_id, function_region=REGION) as computer:
        result = await computer.step(
            [
                {"type": "move", "x": 320, "y": 240},
                {"type": "click", "x": 320, "y": 240},
            ],
            continue_on_error=False,
        )
        return result.screenshot.width, result.screenshot.height


@app.local_entrypoint()
async def main() -> None:
    config = ComputerConfig(
        runtime={"modal_environment": "main", "modal_region": REGION},
        resources={"profile": "browser", "cpu": 1.0, "memory_mib": 2048},
        browser={"kind": "chromium"},
    )
    async with AsyncComputerSandbox.create(config=config, app_name=APP_NAME) as owner:
        size = await trajectory.remote.aio(
            owner.session_handle(), f"quickstart_{uuid.uuid4().hex}"
        )
        print(*size)
```

Run it:

```bash
uv run modal run --env main quickstart.py
```

This is the optimized default topology. The async owner creates one desktop and produces a
versioned handle. The application-owned Modal Function enters one borrow for the whole trajectory.
That borrow reuses one pooled async HTTP client. `computer.step()` sends the ordered action batch
and returns its immediate, byte-backed post-action screenshot in one request. The Function releases
the lease before the owner terminates the Sandbox. Warm capacity is
off because `min_containers=0`; enable paid idle capacity only after you measure the tradeoff.

## Core API

`AsyncComputerSandbox` plus `ComputerSessionHandle.borrow_async()` is the primary Modal trajectory
Interface. Keep the provider model loop in your application-owned Modal Function. Use one borrow
around the repeated model loop. Use `computer.step()` for each ordered action array and its
immediate post-action frame.

The synchronous SDK, direct daemon clients, attach flows, REST routes, and idempotency tools remain
available as low-level compatibility surfaces. Use them for local control, direct-daemon work,
debugging, recovery, or an application that owns its own lifecycle. They do not establish the
article-backed placed topology by themselves.

| Task | Representative API |
| --- | --- |
| Own and hand off | `AsyncComputerSandbox.create()`, `owner.session_handle()`, `handle.borrow_async()` |
| Act and observe | `computer.step()` |
| Create or attach at the low level | `ComputerSandbox.create()`, `ComputerSandbox.attach()`, `AsyncComputerSandbox.attach()` |
| Acquire by name | `ComputerSandbox.attach_or_create(name=...)`, `AsyncComputerSandbox.attach_or_create(name=...)` |
| Input | `computer.mouse.move()`, `computer.keyboard.type()`, `computer.clipboard.get_text()` |
| Observe | `computer.screenshots.full()`, `computer.display.info()`, `computer.windows.list()` |
| Browser and apps | `computer.browser.open_url()`, `computer.apps.launch()` |
| Execute | `computer.actions.run()`, `computer.commands.run()` |
| Files and recordings | `computer.artifacts.download()`, `computer.recordings.start()` |
| Operate | `computer.lifecycle.status()`, `computer.processes.logs(name)` |

Action batches validate the full request before execution. They stop on the first error by default,
can opt into `continue_on_error`, and can capture a trailing screenshot in the same request.
The trailing-screenshot option is a retained low-level capability. The borrowed `computer.step()`
Interface is the default model-loop path. It returns `ComputerStepResult.actions`,
`ComputerStepResult.screenshot`, and timing metadata.

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

See the [API reference](https://modal-computer-use.mintlify.app/reference/overview) for namespace
semantics and the generated
[OpenAPI schema](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/openapi.json) for
HTTP request and response shapes.

## How it works

The primary path has two lifecycle scopes. The async owner creates and owns the Modal Sandbox. It
waits while the placed Function uses the versioned handle. The Function enters one exclusive
borrow, runs the complete trajectory over one pooled client, and releases the borrow. The owner then
terminates the Sandbox, including on ordinary exceptions and cancellation.

Native async provisioning keeps cancellation and cleanup safe. It does not shorten cold allocation
or desktop startup. Measure cold allocation, Function dispatch, borrow entry, and repeated warm
operation time separately.

`ComputerSandbox.attach_or_create(name=...)` and its async counterpart acquire a compatible live
Sandbox with that app-scoped name, or create one if it is missing. If the call creates the Sandbox,
leaving the block terminates it. If the Sandbox already existed, leaving the block keeps it running.

A daemon inside the Sandbox executes desktop actions, captures screenshots and recordings, runs
commands, and reads or writes files through `computer.artifacts`.

The daemon admits input through one token bucket for the desktop. The default refills 100
normalized input-work tokens per second and allows a 400-token burst. A complete ordered batch
reserves its cost before the first mutation. This keeps the limit away from normal optimized Step
loops and prevents a rate-limit boundary from partially executing a batch.

## Performance

[![Warm-operation p50 latency on July 30, 2026. Modal optimized recorded the lowest p50 in each of six displayed rows; configurations and caller topologies differed.](https://raw.githubusercontent.com/ashtonchew/modal-computer-use/main/docs/assets/warm-operation-p50-2026-07-30.svg)](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/benchmark-results-2026-07-30-warm-paths.md)

The figure shows July 2026 p50 latency for six computer-use cases, based on 30 successful samples
per cell. Lower is better. Warm-operation latency starts after the desktop and client connection
are ready. The article's opening 47.10 ms figure is arithmetic over separate 37.25 ms raw-screenshot
and 9.85 ms click medians. It is not a measured fused turn and is not a latency promise for
`computer.step()`.

A separate preregistered same-topology benchmark measured the fused `computer.step()` path at
44.29 ms p50 and 52.57 ms p95 across 100 paired samples. The prior two-request path measured
47.14 ms and 58.22 ms. See the [Computer Step promotion report](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/benchmark-results-2026-08-08-computer-step.md).

The [benchmark results](https://modal-computer-use.mintlify.app/benchmarks/current-results) give p95
values and explain how each path was configured and measured.

## Examples

| Workflow | Example |
| --- | --- |
| Run the complete optimized default trajectory | [`modal_function_session_handoff.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/modal_function_session_handoff.py) |
| Configure and prewarm a browser | [`browser_profile.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/browser_profile.py) |
| Acquire one named desktop from async code | [`async_named_desktop.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/async_named_desktop.py) |
| Attach without taking lifecycle ownership | [`attach_existing_sandbox.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/attach_existing_sandbox.py) |
| Capture and download a recording | [`recording_lifecycle.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/recording_lifecycle.py) |
| Persist artifacts with a Modal Volume | [`volume_artifacts.py`](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/volume_artifacts.py) |
| Run an application-owned model loop | [OpenAI](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/03_openai_computer_loop.py) · [Anthropic](https://github.com/ashtonchew/modal-computer-use/blob/main/examples/anthropic_message_server.py) |

## Documentation

| Guide | What it covers |
| --- | --- |
| [Public documentation](https://modal-computer-use.mintlify.app) | Installation, tasks, integrations, operations, benchmarks, and API reference. |
| [Quickstart](https://modal-computer-use.mintlify.app/start/quickstart) | Create a browser desktop and save a screenshot. |
| [Benchmarks](https://modal-computer-use.mintlify.app/benchmarks/overview) | Current results, evidence limits, and reproducibility. |
| [API reference](https://modal-computer-use.mintlify.app/reference/overview) | Entry points, configuration, namespaces, errors, models, and OpenAPI. |
| [Version 2 migration](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/migration-v2.md) | Exact v1-to-v2 replacements, screenshot payload changes, compatibility, and rollback. |
| [Contributing](https://github.com/ashtonchew/modal-computer-use/blob/main/CONTRIBUTING.md) | Development setup, required checks, and pull request expectations. |

## Local development

See the [local development guide](https://github.com/ashtonchew/modal-computer-use/blob/main/docs/local-development.md)
for daemon startup, mock and X11 backends, synchronous and async clients, authentication, and
repository checks.

## Security

The daemon can control the desktop and access clipboard contents, screenshots, recordings, and
artifacts. Do not expose it without authentication.

See the [security policy](https://github.com/ashtonchew/modal-computer-use/blob/main/SECURITY.md) for
reporting vulnerabilities and the
[runtime security guide](https://modal-computer-use.mintlify.app/operate/security)
for deployment guidance.
