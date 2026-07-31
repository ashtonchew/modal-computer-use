# Anthropic Adapter

`AnthropicAdapter` converts Anthropic computer actions to the core
[action schema](glossary.md#action-schema). The adapter does not call the Anthropic API.

Install the Modal and Anthropic extras before running the executable example:

```bash
uv sync --extra modal --extra anthropic
```

Set `ANTHROPIC_API_KEY`. Set `ANTHROPIC_MODEL` only when you need to override the example's
default model.

## Tool versions

Use one of these current API tool versions:

- Use `computer_20251124` with beta `computer-use-2025-11-24` for new code.
- Use `computer_20250124` with beta `computer-use-2025-01-24` for compatible older models.

The adapter also accepts `computer_20241022` for legacy integrations. Do not use this adapter-only
compatibility option in new API requests.

`AnthropicAdapter` uses `computer_20251124` by default. Zoom is off by default. Set
`enable_zoom=True` to enable it.

The adapter rejects actions that the selected tool version does not support:

- `computer_20241022` supports the base action set.
- `computer_20250124` adds enhanced actions, but it does not support `zoom`.
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
fields in new code. The application-owned batch schema described below exposes only current fields.

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

Keep Anthropic's provider-owned hosted `computer` tool. Do not declare an input schema for it.
Anthropic owns that schema and the computer-use classifier behavior.

The executable example also declares an application-owned custom tool named `computer_batch`.
That custom tool has an explicit, version-gated schema. It does not inherit the hosted computer
tool's schema or classifier behavior.

Use `computer_batch` for a predictable sequence whose actions do not depend on intermediate visual
outcomes:

```python
batch_result = adapter.apply_many(
    [
        {"action": "left_click", "coordinate": [420, 280]},
        {"action": "type", "text": "Example"},
        {"action": "key", "text": "TAB"},
    ],
    continue_on_error=False,
    screenshot_after=True,
)
```

One custom tool invocation becomes one ordered daemon batch. Execution stops on the first error.
The tool result labels each completed or failed sub-action. It preserves native screenshot and zoom
images. It adds the fused post-batch frame when the final native image does not already provide the
needed observation. The fused frame is immediate; it does not prove that the application settled.

All batch coordinates refer to the screenshot observed before the batch. The model cannot inspect
an intermediate screen and replan inside the custom tool call. Keep the hosted `computer` tool for
exploratory navigation, sensitive steps, recovery, and any action whose successor depends on the
intermediate screen.

Declare both tools in the Messages API request:

```python
tools = [
    {
        "type": "computer_20251124",
        "name": "computer",
        "display_width_px": 1280,
        "display_height_px": 800,
        "enable_zoom": True,
    },
    computer_batch_tool,
]
response = client.beta.messages.create(
    model="claude-sonnet-5",
    betas=["computer-use-2025-11-24"],
    max_tokens=4096,
    tools=tools,
    messages=messages,
)
```

Keep the complete assistant content in message history. Continue the tool loop only when
`stop_reason` is `tool_use`. Add one matching `tool_result` for each tool use, including failures.
Put all tool results together in the next user message.

Execute multiple hosted computer calls against the shared screen sequentially in assistant-content
order. Preserve their tool-use IDs and return their results in the same order. Do not combine
separate hosted calls into one daemon batch. Parallelize another tool only when it is read-only and
independent of the shared screen.

Return screenshot data after graphical actions. Reuse native screenshot or zoom output instead of
capturing it again. Return `cursor_position` as text.

Stop when Claude does not request a tool. Set distinct limits for turns, trajectory actions,
recursive batch actions, action time, and total time. Count every provider response as one turn.
If the final allowed response requests a tool, stop before executing it because the loop cannot
return the required tool result within its turn budget. See
[the executable Anthropic example](../examples/anthropic_message_server.py).

## Modal deployment

For a latency-sensitive deployment, use one target browser Sandbox and one user-owned Modal
Function invocation for the provider loop. Borrow one session connection for the whole trajectory.
Keep provider credentials and model state in that Function.

Select a common requested region for the Function and Sandbox only when measurements support it.
A common requested region is a scheduling request. It does not promise the same host or availability
zone.

Keep `attested-tunnel` as the default ingress. Use the
[session-handoff example](../examples/modal_function_session_handoff.py) to pass target ownership to
the Function without copying a full Function scaffold into the provider loop. See
[Modal deployment](modal-deployment.md) for lifecycle and ingress constraints. See
[performance](performance.md) for measurement boundaries and capacity tradeoffs.

## Safety

Run the desktop in a dedicated virtual machine or container. Give the environment minimum
privileges. Do not give the environment sensitive data unless the task requires that data.

Allow only the required network destinations. Treat page content and screenshots as untrusted
input. Ask for confirmation before an action has a meaningful external result or requires consent.

Anthropic can detect some prompt-injection attempts for the hosted computer tool. This detection
does not replace isolation, allowlists, validation, or confirmation. It does not automatically
extend to `computer_batch`. Record action paths for review. Remove secrets and provider payloads
from records.

Sources:

- [Anthropic Computer use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- [Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)
- [Anthropic tool-result handling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
- [Anthropic computer batch reference implementation](https://github.com/anthropics/claude-quickstarts/blob/main/computer-use-best-practices/computer_use/tools/batch.py)
- [Anthropic computer and browser use best practices](https://claude.com/blog/best-practices-for-computer-and-browser-use-with-claude#batch-tools)
