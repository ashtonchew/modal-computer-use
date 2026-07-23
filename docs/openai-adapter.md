# OpenAI Adapter

`OpenAIAdapter` converts OpenAI computer actions to the core [action schema](glossary.md#action-schema).
The adapter does not call the OpenAI API.

## Responses API integration

Use the Responses API and the `computer` tool for new integrations:

```python
response = client.responses.create(
    model="gpt-5.6",
    tools=[{"type": "computer"}],
    input="Complete the task. Use the computer tool for UI interaction.",
)
```

Process each `computer_call` as follows:

1. Run each item in `actions[]` in the given order.
2. Capture the screen after the actions finish.
3. Return one `computer_call_output` for the call.
4. Set `previous_response_id` to the ID of the previous response.
5. Stop when the response has no `computer_call`.

Set limits for turns, actions, action time, and total time. See
[examples/03_openai_computer_loop.py](../examples/03_openai_computer_loop.py) for a complete loop.

Do not use the deprecated `computer-use-preview` model or the `computer_use_preview` tool in new
code. The current tool does not use the preview display fields, preview action shape, or
`truncation="auto"`.

## Supported actions

The adapter supports these actions:

- `click`
- `double_click`
- `scroll`
- `type`
- `keypress`
- `drag`
- `move`
- `wait`
- `screenshot`

The adapter accepts these OpenAI fields:

- Use `keys` for click, double-click, drag, move, and scroll modifiers.
- Use `keypress.keys` for a sequence of key presses.
- Use `[x, y]` pairs or `{x, y}` objects for drag paths.
- Use `wheel`, `back`, or `forward` for the related X11 mouse buttons.
- Use `scroll_x` and `scroll_y` for scroll distance.

The adapter preserves both scroll axes. It converts the scroll distance to wheel clicks.

`normalize()` returns one native action. Use `apply_many()` when one provider action creates more
than one native action. A multi-key keypress and a two-axis scroll are examples.

Unknown actions raise `UnsupportedActionError` by default. Set `allow_unknown=True` only when you
must accept an unknown provider action. In this mode, the adapter converts the unknown action to a
zero-duration `wait`. The adapter does not run the unknown desktop action.

The adapter stores redacted provider data in action metadata. A trace can then show the provider
action and the normalized action. The adapter removes typed text from this metadata.

## Screenshot output

Use `openai_computer_call_output()` to make a provider response from a native `Screenshot`:

```python
from modal_computer_use.adapters.openai import openai_computer_call_output

shot = computer.screenshots.full()
input_item = openai_computer_call_output(shot, call_id="call_123")
```

The helper does not call OpenAI. It does not control the model loop.

Use `openai_screenshot_metadata()` to record safe screenshot metadata. The result contains the
dimensions, format, SHA-256, artifact URI, capture time, and coordinate space. It does not contain
image bytes or base64 data.

## Adapter example

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

The screenshot dimensions can differ from the desktop dimensions. For example, you can reduce a
1440×900 screenshot to 720×450 before you send it to the model. In this case, give the adapter a
[`CoordinateSpace`](glossary.md#coordinatespace):

```python
from modal_computer_use import CoordinateSpace

space = CoordinateSpace(
    desktop_width=1440,
    desktop_height=900,
    image_width=720,
    image_height=450,
)
adapter = OpenAIAdapter(computer, coordinate_space=space)
```

The adapter converts model coordinates to desktop coordinates. It does not change coordinates when
you do not supply a coordinate space.

The optional `before_action` hook receives the normalized native action. The hook can reject the
action before the daemon receives it.

## Safety

Run the desktop in an isolated sandbox with minimum privileges. Treat page content, screenshots,
documents, messages, and tool output as untrusted input. A page instruction does not give the model
permission to act.

Allow only the required domains and actions. Stop when you detect prompt injection or phishing.
Ask for confirmation immediately before an action that can:

- delete or change external data;
- use an authenticated account;
- send a message;
- make a purchase or financial transaction;
- change access or permissions;
- transmit sensitive data.

Typing sensitive data transmits that data. Do not put screenshots, typed text, tokens, or URLs in
logs. Stop on unknown actions. Always set turn, action, and time limits.

Source: [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use).
