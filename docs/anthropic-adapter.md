# Anthropic Adapter

`AnthropicAdapter` converts Anthropic computer actions to the core
[action schema](glossary.md#action-schema). The adapter does not call the Anthropic API.

## Tool versions

Use one of these tool versions:

- Use `computer_20251124` with beta `computer-use-2025-11-24` for new code.
- Use `computer_20250124` with beta `computer-use-2025-01-24` for compatible older models.
- Use `computer_20241022` only for legacy integrations.

`AnthropicAdapter` uses `computer_20251124` by default. Zoom is off by default. Set
`enable_zoom=True` to enable it.

The adapter rejects actions that the selected tool version does not support:

- `computer_20241022` does not support `scroll`, `left_mouse_down`, `left_mouse_up`, `hold_key`,
  `wait`, or `triple_click`.
- `computer_20250124` supports these actions, but it does not support `zoom`.
- `computer_20251124` supports `zoom` when you enable zoom.

The adapter accepts the current Anthropic fields:

- `scroll_direction` gives the scroll direction.
- `scroll_amount` gives the number of scroll clicks.
- `duration` gives the duration in seconds.
- `text` gives the key for `hold_key`.
- `text` can also give a scroll modifier.
- `key` gives an optional click modifier.
- `region` gives a zoom area as `[x0, y0, x1, y1]`.

The adapter converts `duration` to milliseconds. It converts a zoom region to a full-resolution
native region.

The adapter also accepts the older `direction`, `amount`, and `duration_ms` fields. It accepts
`key` for an older `hold_key` action. It also accepts an object-shaped zoom region. Use the current
fields in new code.

## Supported actions

The adapter supports these actions:

- `mouse_move`
- `left_click`
- `right_click`
- `middle_click`
- `double_click`
- `triple_click`
- `left_click_drag`
- `key`
- `type`
- `scroll`
- `left_mouse_down`
- `left_mouse_up`
- `hold_key`
- `wait`
- `screenshot`
- `zoom`
- `cursor_position`

A click without coordinates uses the current cursor position. A drag without `start_coordinate`
uses the current cursor position as its start.

Unknown actions raise `UnsupportedActionError` by default. Set `allow_unknown=True` only when you
must accept an unknown provider action. In this mode, the adapter converts the unknown action to a
zero-duration `wait`. The adapter does not run the unknown desktop action.

The adapter stores redacted provider data in action metadata. A trace can then show the provider
action and the normalized action. The adapter removes typed text from this metadata.

## Tool results

Use `anthropic_tool_result()` to make an Anthropic `tool_result` from a native `Screenshot` or
`ActionResult`:

```python
from modal_computer_use.adapters.anthropic import anthropic_tool_result

shot = computer.screenshots.full()
tool_result = anthropic_tool_result(tool_use_id="toolu_123", result=shot)
```

The helper does not call Anthropic. It does not control the model loop.

Use `anthropic_screenshot_metadata()` to record safe screenshot metadata. The result contains the
dimensions, format, SHA-256, artifact URI, capture time, and coordinate space. It does not contain
image bytes or base64 data.

## Adapter example

```python
from modal_computer_use import ComputerSandbox
from modal_computer_use.adapters.anthropic import AnthropicAdapter

computer = ComputerSandbox.local(token="dev")
computer.wait_until_ready()

adapter = AnthropicAdapter(
    computer,
    tool_version="computer_20251124",
    enable_zoom=True,
)
adapter.apply({"action": "mouse_move", "coordinate": [500, 300]})
adapter.apply({"action": "left_click"})
```

## Coordinate spaces

The screenshot dimensions can differ from the desktop dimensions. In this case, give the adapter a
[`CoordinateSpace`](glossary.md#coordinatespace):

```python
from modal_computer_use import CoordinateSpace

space = CoordinateSpace(
    desktop_width=1440,
    desktop_height=900,
    image_width=720,
    image_height=450,
)
adapter = AnthropicAdapter(
    computer,
    tool_version="computer_20251124",
    enable_zoom=True,
    coordinate_space=space,
)
```

The adapter converts model coordinates to desktop coordinates. It does not change coordinates when
you do not supply a coordinate space.

The optional `before_action` hook receives the normalized native action. The hook can reject the
action before the daemon receives it.

## Messages API loop

Declare the Anthropic computer tool without a custom input schema. Set the display dimensions to the
dimensions of the image that Claude receives:

```python
tool = {
    "type": "computer_20251124",
    "name": "computer",
    "display_width_px": 1280,
    "display_height_px": 800,
    "enable_zoom": True,
}
response = client.beta.messages.create(
    model="claude-sonnet-5",
    betas=["computer-use-2025-11-24"],
    max_tokens=4096,
    tools=[tool],
    messages=messages,
)
```

Keep the complete assistant content in the message history. Run each `tool_use`. Add one matching
`tool_result` for each tool use. Put all tool results in the next user message. Mark a failed action
with `is_error`. Return a screenshot after a graphical action.

Stop when Claude does not request a tool. Set limits for turns, actions, action time, and total time.
See [examples/anthropic_message_server.py](../examples/anthropic_message_server.py) for a complete
loop.

## Safety

Run the desktop in a dedicated virtual machine or container. Give the environment minimum
privileges. Do not give the environment sensitive data unless the task requires that data.

Allow only the required network destinations. Treat page content and screenshots as untrusted
input. Ask for confirmation before an action has a meaningful external result or requires consent.

Anthropic can detect some prompt-injection attempts. This detection does not replace isolation,
allowlists, validation, or confirmation. Record action paths for review. Remove secrets and provider
payloads from records.

Sources:

- [Anthropic Computer use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- [Anthropic tool reference](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference)
- [Anthropic tool-result handling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
