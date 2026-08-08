# Modal optimization

The optimized default is one complete placed trajectory. It is not a flag or a hidden environment
setting. The application establishes this path:

1. An async owner creates one desktop with an exact requested region and explicit resources.
2. The owner produces a versioned session handle.
3. An application-owned Modal Function in the same requested region receives the handle.
4. The Function enters one `borrow_async()` context around the complete model trajectory.
5. One pooled async HTTP client carries screenshots and ordered action batches for that borrow.
6. The borrower releases its lease before the owner cleans up the desktop.

See the executable
[`modal_function_session_handoff.py`](../examples/modal_function_session_handoff.py) example. It
keeps the application-owned model loop outside the core package.

## Screenshot path

`await computer.screenshots.full()` is the primary screenshot Interface. For inline storage, it
uses the binary HTTP response and returns a byte-backed `Screenshot`. The public model keeps width,
height, format, hash, capture time, coordinate space, and cursor metadata. Use `Screenshot.bytes`
or `Screenshot.as_bytes()` for bytes. Use `Screenshot.to_base64()` only when a provider payload
needs base64.

The JSON/base64 and REST routes remain available. `screenshots.full_bytes()` remains an explicit
low-level compatibility method. Do not use it in the primary trajectory: it discards the typed
metadata contract.

Cursor-hidden PNG capture uses the persistent MSS/XShm session and in-memory encoding when the
native display connection is available. Cursor-visible capture and failed display connections use
the bounded documented fallback. The daemon does not launch one screenshot process or write one
temporary file per successful native frame.

## Input and batching

The daemon prefers its persistent XTest/Xlib/XKB input session. It flushes and synchronizes native
input before it reports success. One lock serializes keyboard, pointer, drag, and batch state.

Send a model-produced ordered action array in one `actions.run([...])` request. The daemon validates
the complete batch before mutation, runs actions in order, and stops on the first failure unless the
application explicitly sets continuation. A fallback can run only before any native event is
emitted. Never replay a mutation after dispatch may have started.

## Authentication and transport

An attested-tunnel borrow exchanges authentication once and reuses the client state. Each request
still crosses authenticated Modal ingress. This path reduces repeated authentication work; it does
not remove ingress routing.

## Cost choices

Region, Function CPU and memory, Sandbox CPU and memory, images, timeouts, retries, and capacity are
explicit application choices. The SDK does not define one universal value. Warm capacity is off in
the primary example. A positive Function `min_containers` or a Sandbox warm pool creates optional
idle cost and needs its own lifecycle policy.

Native async provisioning makes cancellation and cleanup safe. It does not shorten cold allocation
or desktop startup. Report these phases separately:

- cold allocation and desktop startup;
- Function dispatch;
- borrow entry and authentication;
- repeated warm operation time;
- lease release and owner cleanup.

Post-action first-visual-change observation is experimental. XDamage is a wake-up hint, and
full-resolution pixel comparison verifies a changed frame. Polling remains the fallback. A changed
frame is not application readiness.

Use the [performance guide](https://modal-computer-use.mintlify.app/operate/performance) for the
public guide.

The repository [benchmarking procedure](benchmarking.md) remains canonical for measurements and
evidence.

## Low-level compatibility

Use direct daemon clients, local clients, synchronous ownership, attach, REST/idempotency routes,
and `full_bytes()` when you need those specific primitives. These surfaces remain supported. They
do not silently place an external caller into the optimized topology and are not the documented
default trajectory.
