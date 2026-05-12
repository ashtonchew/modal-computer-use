# Anthropic Adapter

`AnthropicAdapter` translates Anthropic-style computer-use action JSON into the core [action schema](glossary.md#action-schema). It does not call the Anthropic API.

## Tool versions

- `computer_20241022`
- `computer_20250124` (recommended for new code)
- `computer_20251124`

The newer versions add actions; older versions stay supported for compatibility with existing agent harnesses.

Version gates fail closed. `computer_20241022` rejects enhanced input actions such as
`scroll`, `left_mouse_down`, `left_mouse_up`, `hold_key`, `wait`, and `triple_click`.
`computer_20250124` accepts those enhanced actions but rejects `zoom`. `computer_20251124`
accepts `zoom` when zoom is enabled.

## Supported actions

`mouse_move`, `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`, `left_click_drag`, `key`, `type`, `scroll`, `left_mouse_down`, `left_mouse_up`, `hold_key`, `wait`, `screenshot`, `zoom`, `cursor_position`.

Coordinate-less click actions operate at the current cursor. Drag actions without a `start_coordinate` are sent as destination-only drags so the daemon uses the current cursor as the start.

Unknown actions raise `UnsupportedActionError` by default. Pass `allow_unknown=True` only for an
intentional compatibility mode; unknown payloads become a zero-duration native `wait` action with
redacted provider-action metadata instead of a desktop action.

Normalized actions include redacted provider provenance under metadata so daemon traces can record
both `provider_action` and the native `normalized_action`. The adapter redacts typed text before
placing provider payloads in metadata.

## Tool result helper

`anthropic_tool_result(tool_use_id=..., result=...)` builds an Anthropic `tool_result` from a
native `Screenshot` or safe `ActionResult` summary. It does not call Anthropic and it does not own
the model loop.

```python
from modal_computer_use.adapters.anthropic import anthropic_tool_result

shot = computer.screenshots.full()
tool_result = anthropic_tool_result(tool_use_id="toolu_123", result=shot)
```

Use `anthropic_screenshot_metadata(shot)` when you need to preserve native screenshot metadata
outside the provider payload. The metadata helper includes dimensions, format, SHA-256, artifact
URI, capture time, and coordinate-space metadata, and intentionally omits raw bytes and base64
image data.

## Example

```python
from modal_computer_use import ComputerSandbox
from modal_computer_use.adapters.anthropic import AnthropicAdapter

computer = ComputerSandbox.local(token="dev")
computer.wait_until_ready()

adapter = AnthropicAdapter(computer, tool_version="computer_20250124")
adapter.apply({"action": "mouse_move", "coordinate": [500, 300]})
adapter.apply({"action": "left_click"})
```

## Coordinate spaces

If the screenshot you sent the model was downscaled from the desktop's native resolution, pass a [`CoordinateSpace`](glossary.md#coordinatespace) so the adapter translates coordinates back. The adapter never silently rescales.

```python
from modal_computer_use import CoordinateSpace

space = CoordinateSpace(
    desktop_width=1440, desktop_height=900,
    image_width=720, image_height=450,
)
adapter = AnthropicAdapter(computer, tool_version="computer_20250124", coordinate_space=space)
```

The `before_action` hook, when provided, sees the normalized native action after this transform
and can deny execution before the action is sent to the daemon.
