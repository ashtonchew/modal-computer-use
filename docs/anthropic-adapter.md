# Anthropic Adapter

`AnthropicAdapter` translates Anthropic-style computer-use action JSON into the core [action schema](glossary.md#action-schema). It does not call the Anthropic API.

## Tool versions

- `computer_20241022`
- `computer_20250124` (recommended for new code)
- `computer_20251124`

The newer versions add actions; older versions stay supported for compatibility with existing agent harnesses.

## Supported actions

`mouse_move`, `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`, `left_click_drag`, `key`, `type`, `scroll`, `left_mouse_down`, `left_mouse_up`, `hold_key`, `wait`, `screenshot`, `zoom`, `cursor_position`.

Coordinate-less click actions operate at the current cursor. Drag actions without a `start_coordinate` are sent as destination-only drags so the daemon uses the current cursor as the start.

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
