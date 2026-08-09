# OpenAI adapter

Keep the OpenAI client and model loop in the application-owned, explicitly placed Modal Function.
Pass a versioned session handle into that Function and enter one `borrow_async()` context around the
complete loop. Send each response-wide preflighted action array through `computer.step()`. Use the
returned byte-backed `ComputerStepResult.screenshot` as the next provider observation and convert
it to provider base64 only at the adapter boundary.

Preserve each ordered model action array as one step. Preflight all calls in one OpenAI response
before the first step. Preserve call IDs and response order. Do not replay the step
after dispatch may have started. The core package does not import OpenAI and does not own prompts,
model calls, or confirmation policy.

Use [`03_openai_computer_loop.py`](../examples/03_openai_computer_loop.py) as the executable local
example. Use the
[OpenAI integration guide](https://modal-computer-use.mintlify.app/integrate/openai) for the public
tutorial.
