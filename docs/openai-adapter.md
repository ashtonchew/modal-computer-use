# OpenAI adapter

Keep the OpenAI client and model loop in the application-owned, explicitly placed Modal Function.
Pass a versioned session handle into that Function and enter one `borrow_async()` context around the
complete loop. Each turn should call the semantic `screenshots.full()` method. The returned
byte-backed `Screenshot` converts to provider base64 only at the adapter boundary.

Preserve each ordered model action array as one `actions.run([...])` batch. Do not replay the batch
after dispatch may have started. The core package does not import OpenAI and does not own prompts,
model calls, or confirmation policy.

Use [`03_openai_computer_loop.py`](../examples/03_openai_computer_loop.py) as the executable local
example. Use the
[OpenAI integration guide](https://modal-computer-use.mintlify.app/integrate/openai) for the public
tutorial.
