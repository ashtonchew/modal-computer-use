# OpenAI Adapter

`OpenAIAdapter` translates OpenAI-style computer-use action JSON into the core [action schema](glossary.md#action-schema). It does not call the OpenAI API.

## Current OpenAI integration

New integrations should use the GA Responses API computer tool:

```python
response = client.responses.create(
    model="gpt-5.6",
    tools=[{"type": "computer"}],
    input="Complete the task. Use the computer tool for UI interaction.",
)
```

For every returned `computer_call`, execute every item in `actions[]` in order, capture one updated
screenshot, return a `computer_call_output`, and continue with `previous_response_id`. Stop when the
response has no `computer_call`, and enforce an application-level turn/action/time budget. The
runnable implementation is [examples/03_openai_computer_loop.py](../examples/03_openai_computer_loop.py).

The legacy `computer-use-preview` model, `computer_use_preview` tool, one-action response shape,
display fields, and required `truncation="auto"` are not used by the current cookbook.

## Supported actions

`click`, `double_click`, `scroll`, `type`, `keypress`, `drag`, `move`, `wait`, `screenshot`.

The adapter accepts current provider fields:

- `keys` carries click, double-click, drag, move, and scroll modifiers.
- `keypress.keys` is executed sequentially, not as a simultaneous hotkey.
- drag paths accept both `[x, y]` pairs and `{x, y}` objects.
- `wheel`, `back`, and `forward` buttons map to native X11 buttons.
- pixel-like scroll deltas are converted to bounded wheel clicks and both axes are preserved.

`normalize()` represents exactly one native action. Provider actions that expand to multiple native
actions, such as a multi-key keypress or two-axis scroll, must use `apply_many()`.

Unknown actions raise `UnsupportedActionError` by default. Pass `allow_unknown=True` only for an
intentional compatibility mode; unknown payloads become a zero-duration native `wait` action with
redacted provider-action metadata instead of a desktop action.

Normalized actions include redacted provider provenance under metadata so daemon traces can record
both `provider_action` and the native `normalized_action`. The adapter redacts typed text before
placing provider payloads in metadata.

## Screenshot output helper

`openai_computer_call_output(screenshot, call_id=...)` builds only the provider-shaped
`computer_call_output` item from a native `Screenshot`. It does not call OpenAI and it does not
own the model loop.

```python
from modal_computer_use.adapters.openai import openai_computer_call_output

shot = computer.screenshots.full()
input_item = openai_computer_call_output(shot, call_id="call_123")
```

Use `openai_screenshot_metadata(shot)` when you need to keep dimensions, format, SHA-256,
artifact URI, capture time, and coordinate-space metadata in your own logs or traces. The metadata
helper intentionally omits raw bytes and base64 image data.

## Example

```python
from modal_computer_use import ComputerSandbox
from modal_computer_use.adapters.openai import OpenAIAdapter

computer = ComputerSandbox.local(token="dev")
computer.wait_until_ready()

adapter = OpenAIAdapter(computer)
adapter.apply({"type": "click", "x": 500, "y": 300, "button": "left"})
adapter.apply({"type": "type", "text": "hello"})
```

## Coordinate spaces

If you downscaled a 1440×900 desktop screenshot to 720×450 before sending it to the model, pass a [`CoordinateSpace`](glossary.md#coordinatespace) so the adapter translates model coordinates back to the desktop grid. The adapter never silently rescales.

```python
from modal_computer_use import CoordinateSpace

space = CoordinateSpace(
    desktop_width=1440, desktop_height=900,
    image_width=720, image_height=450,
)
adapter = OpenAIAdapter(computer, coordinate_space=space)
```

The `before_action` hook, when provided, sees the normalized native action after this transform
and can deny execution before the action is sent to the daemon.

## Safety boundary

Run the desktop in an isolated least-privilege sandbox. Treat screenshots, page content, PDFs,
emails, chats, and tool outputs as untrusted input; only direct user instructions grant permission.
Use domain and action allowlists, stop on suspected prompt injection or phishing, and confirm at the
point of risk before destructive, authenticated, financial, external-communication, permission, or
sensitive-data actions. Typing sensitive data counts as transmission. Keep turn/action/time budgets,
fail closed on unknown actions, and redact screenshots, typed text, tokens, and URLs from logs.

Canonical source: [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use).
