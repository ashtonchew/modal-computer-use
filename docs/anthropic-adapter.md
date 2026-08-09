# Anthropic adapter

Keep the Anthropic client and message loop in the application-owned, explicitly placed Modal
Function. Pass a versioned session handle into that Function and enter one `borrow_async()` context
around the complete loop. Send each ordered action array through `computer.step()`. Use the returned
byte-backed screenshot as the next provider observation and convert it to provider base64 only at
the adapter boundary.

Preserve each ordered model action array as one step. Keep cursor-position queries action-only.
Screenshot and zoom actions remain valid step actions. Preserve their native image blocks and
suppress only a duplicate final step frame when the last action already supplies that image. Keep
nested image order. Do not replay the step
after dispatch may have started. The core package does not import Anthropic and does not own
messages, model calls, or confirmation policy.

Use [`anthropic_message_server.py`](../examples/anthropic_message_server.py) as the executable local
example. Use the
[Anthropic integration guide](https://modal-computer-use.mintlify.app/integrate/anthropic) for the
public tutorial.
