# Migrate from v1 to v2

Version 2 is a semantic-version major release. It changes the primary Modal interface and its
lifecycle contract. It does not remove the daemon's JSON/base64 or REST compatibility routes.

Use `AsyncComputerSandbox.create()` in an async context for the primary owner. Call
`owner.session_handle()`, pass the handle to the placed Function, and enter
`handle.borrow_async()` once around that Function's complete trajectory.
The primary `create()` validates an explicit Modal environment and exact region before any Modal
lookup or desktop allocation. Use `AsyncComputerSandbox.create_unplaced()` only for intentional
low-level compatibility work that does not need a session handoff.

## Migration table

| Version 1 pattern | Version 2 default | Required migration |
| --- | --- | --- |
| A laptop or other external process owns `ComputerSandbox.create()` and calls the daemon for every model turn. | An async owner creates the desktop once and sends its versioned `ComputerSessionHandle` to an explicitly placed, application-owned Modal Function. | Move the model trajectory into that Function. Keep provider SDK imports and model calls in application code, not in core. |
| Each remote operation creates or attaches to its own desktop/client context. | The Function calls `borrow_async(handle)` exactly once around the whole trajectory. | Hoist borrowing outside the model-turn loop and release it only after the trajectory ends. |
| Region may be absent or broad, and a mismatched caller can continue over ingress. | Primary `AsyncComputerSandbox.create()` requires an explicit environment and exact region such as `us-west-2` before allocation; the Function, observed Function runtime, and Sandbox must then match that region. | Select an exact region for both resources and make environment, CPU, memory, image, timeout, retries, scaling limits, and capacity inspectable. Use `create_unplaced()` only for an intentional low-level path without handoff. |
| Async creation may use tunnel ingress, control VNC, or warm-pool tagging even though those modes cannot produce the default handoff. | Primary `AsyncComputerSandbox.create()` rejects these modes before Modal work. | Use `create_unplaced()` for an intentional low-level owner, or select attested-tunnel/connect ingress, off/view-only VNC, and default ownership tags. |
| `screenshots.full(storage="inline")` returns a JSON/base64-backed `Screenshot`. | The same semantic method uses the raw binary response and returns `Screenshot(bytes=...)`. | Prefer `as_bytes()` or `to_base64()` instead of reading `data_base64` directly. JSON serialization of `bytes` uses Base64URL. |
| A provider loop calls `actions.run(...)` and then `screenshots.full()` after each model action array. | The borrowed `computer.step(...)` Interface sends the ordered array and returns one `ComputerStepResult` with `actions`, `screenshot`, and `timing`. | Replace the two calls with one step. Use its immediate post-action `screenshot` for the next model turn. Do not treat the frame as application readiness or replay a step after a possible dispatch. |
| Provider examples may send model actions one at a time. | One ordered model `actions[]` becomes one `computer.step(...)` request. | Preserve model order, choose continuation explicitly, use the returned immediate screenshot, and never replay automatically after possible dispatch. |
| Cleanup commonly relies on the outer owner context only. | The borrowed client and lease close first; the owner then detaches or terminates according to ownership. | Keep the owner alive until the placed Function reaches a terminal result, including cancellation cleanup. |
| The main quickstart presents direct namespace calls as the performance path. | The placed owner-to-handle-to-Function trajectory is the primary documented path. | Use the low-level primitive SDK only when local, direct REST, idempotency, debugging, or compatibility behavior is intentional. |

## Screenshot compatibility

`computer.step()` requires the versioned `computer-step-envelope-v1` capability. Borrow preflight
fails before mutation when the daemon does not support it. The SDK does not silently fall back to
the old two-request path.

The public `screenshots.full()` method keeps its typed return. Only the inline transport and payload
representation change. A v2 byte-backed `Screenshot` supports:

- `Screenshot.bytes` for the optional direct bytes field;
- `Screenshot.as_bytes()` for a representation-independent byte result;
- `Screenshot.to_base64()` for a provider boundary that requires standard base64;
- `Screenshot.data_base64` only as a compatibility field that may be absent.

```python
screenshot = await computer.screenshots.full()
image_bytes = screenshot.bytes
portable_bytes = screenshot.as_bytes()
provider_base64 = screenshot.to_base64()
```

Do not assume `Screenshot.data_base64` is populated. `to_base64()` returns an existing base64 value
or encodes inline bytes when the caller asks for it. JSON serialization still emits a safe
base64 representation for the bytes field when a caller explicitly serializes the model.

JSON/base64 and REST routes remain available for direct clients and compatibility. The v2 SDK does
not remove those daemon routes. It also does not silently fall back from a rejected binary response
to JSON after a request has been dispatched.

`screenshots.full_bytes()` remains available for an explicit low-level bytes-only client. It is not
the primary screenshot interface because it omits the semantic `Screenshot` metadata contract.

Warm capacity remains off unless an operator enables it. Function minimums and Sandbox warm pools
are separate, inspectable cost choices; they are not part of article parity.

## Input rate-limit migration

The old `actions.input_rate_limit_per_sec=20` default counted flat actions in a rolling one-second
window. The new default interprets that field as a token-bucket refill rate and sets it to `100`.
`actions.input_rate_limit_burst=400` is new. The daemon now charges normalized input-work tokens
and reserves a complete recursive batch before mutation.

If you set the old field explicitly, review both values. For an intentionally restrictive profile,
set both the refill and burst. Do not assume that `20` with the new default burst reproduces the old
boundary. A transient `rate_limited` response is safe because no action in that request ran. An
`input_cost_exceeds_burst` response cannot succeed after waiting; change the explicit capacity or
the application-owned request shape.

## Lifecycle and failure behavior

The owner creates the desktop once. A Function borrows it once for one trajectory. The borrow uses
one pooled async client and releases its lease before owner cleanup. A validation or placement
failure occurs before mutation. A lost response after possible mutation is not replayed
automatically; observe or recover according to the reported outcome.

Native async provisioning makes cancellation cleanup safe. It does not shorten cold allocation or
desktop startup. Measure cold allocation, Function dispatch, borrow entry, repeated warm operation
time, and cleanup separately.

## Rollback

Roll back the package and runtime artifacts as one compatible release set. Do not configure the v2
client to downgrade silently to the external v1 topology. Keep the v1 documentation version
available during the migration window so operators can restore the last compatible instructions.
