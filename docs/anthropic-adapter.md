# Anthropic Adapter

`AnthropicAdapter` supports versioned computer-use action normalization:

- `computer_20241022`
- `computer_20250124`
- `computer_20251124`

Supported actions include `mouse_move`, `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`, `left_click_drag`, `key`, `type`, `scroll`, `left_mouse_down`, `left_mouse_up`, `hold_key`, `wait`, `screenshot`, `zoom`, and `cursor_position`.

Coordinate-less click actions operate at the current cursor. Drag actions without a start coordinate are sent as destination-only drags so the daemon/backend can use the current cursor.
