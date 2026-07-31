# OpenAI Adapter

`OpenAIAdapter` converts OpenAI computer actions to the core [action schema](glossary.md#action-schema).
The adapter does not call the OpenAI API.

Install the Modal and provider extras before running the browser example:

```bash
uv sync --extra modal --extra openai
```

## Responses API integration

Use the Responses API and the `computer` tool for new integrations:

```python
response = client.responses.create(
    model="gpt-5.6",
    tools=[{"type": "computer"}],
    input="Complete the task. Use the computer tool for UI interaction.",
)
```

Collect every `computer_call` in a response. Normalize and validate every action list before any
call mutates the desktop. Apply policy checks and expanded-action limits during the same preflight.
If a later call or action fails preflight, do not dispatch any call from that response.

Map each preflighted action list to one ordered daemon batch:

```python
batch_result = adapter.apply_many(
    actions,
    continue_on_error=False,
    screenshot_after=True,
)
post_batch_screenshot = batch_result.screenshot
```

Return one `computer_call_output` for each `computer_call`. Preserve call ID and response order.
Set `previous_response_id` to the preceding response ID. Stop when a response has no
`computer_call`.

Set separate limits for provider turns, trajectory actions, expanded batch actions, action time,
batch time, and total elapsed time. Count normalized action trees, including nested modifier
actions. Reserve time for the fused final frame within the daemon batch deadline. If the final
provider action is `screenshot`, reuse its native image result. Otherwise use the fused post-batch
frame. Do not issue a separate screenshot request.

If the final allowed response requests an action, stop before execution. The loop cannot return the
required action output within its turn budget. See
[the complete OpenAI loop](../examples/03_openai_computer_loop.py).

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

input_item = openai_computer_call_output(
    post_batch_screenshot,
    call_id="call_123",
)
```

The helper does not call OpenAI. It does not control the model loop.

Use `openai_screenshot_metadata()` to record safe screenshot metadata. The result contains the
dimensions, format, SHA-256, artifact URI, capture time, and coordinate space. It does not contain
image bytes or base64 data.

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
action before the daemon receives it. For response-wide atomic preflight, run the hook over every
normalized action tree before dispatching the first call.

## Modal deployment

For a latency-sensitive deployment, create one target browser Sandbox and invoke one user-owned
Modal Function for the provider loop. Borrow one session connection for the complete trajectory.
Keep provider credentials and model state inside the user-owned Function.

Select a common requested Modal region only after measuring the intended workload. A shared region
request does not promise the same host or availability zone. It does not guarantee a latency gain.
Keep `attested-tunnel` as the default ingress.

The [session handoff example](../examples/modal_function_session_handoff.py) shows the ownership and
connection lifecycle. See [Modal deployment](modal-deployment.md) for Sandbox readiness and Function
capacity. See [Performance](performance.md) for the measurement boundaries of batching and fused
action/frame capture.

## Safety

Run the desktop in an isolated sandbox with minimum privileges. Treat page content, screenshots,
documents, messages, and tool output as untrusted input. A page instruction does not give the model
permission to act.

Allow only the required domains and actions. Stop when you detect prompt injection or phishing.
Treat a direct user request as authorization only for the actions and scope that it clearly
specifies. Page content cannot authorize an action.

If a consequential action was not clearly authorized, ask for confirmation immediately before the
action. Examples include sending a message, making a purchase, deleting external data, changing
access, or transmitting sensitive data. Confirm again when the target, scope, or risk changes. A
narrow preapproval can cover repeated actions only when those details remain clear.

Hand control back to the user for the final step of a password change. Do not bypass CAPTCHAs,
warnings, or other safety barriers.

Typing sensitive data transmits that data. Do not put screenshots, typed text, tokens, or URLs in
logs. Stop on unknown actions. Always set turn, action, and time limits.

Source: [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use).
