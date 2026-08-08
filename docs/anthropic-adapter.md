# Anthropic adapter

Keep the Anthropic client and message loop in the application-owned, explicitly placed Modal
Function. Pass a versioned session handle into that Function and enter one `borrow_async()` context
around the complete loop. Each turn should call the semantic `screenshots.full()` method. The
returned byte-backed `Screenshot` converts to provider base64 only at the adapter boundary.

Preserve each ordered model action array as one `actions.run([...])` batch. Do not replay the batch
after dispatch may have started. The core package does not import Anthropic and does not own
messages, model calls, or confirmation policy.

Use [`anthropic_message_server.py`](../examples/anthropic_message_server.py) as the executable local
example. Use the
[Anthropic integration guide](https://modal-computer-use.mintlify.app/integrate/anthropic) for the
public tutorial.
