# OpenAI Adapter

`OpenAIAdapter` translates OpenAI-style computer-use action JSON into the core [action schema](glossary.md#action-schema). It does not call the OpenAI API.

## Supported actions

`click`, `double_click`, `scroll`, `type`, `keypress`, `drag`, `move`, `wait`, `screenshot`.

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
