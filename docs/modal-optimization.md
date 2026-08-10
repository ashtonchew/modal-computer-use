# Optimize Modal for production

Use one placed trajectory for the primary SDK path. There is no `optimized=True` flag, hidden
environment setting, or performance-profile toggle.

## Establish the trajectory

1. An async owner creates one desktop in an exact requested region.
2. The owner produces a versioned session handle.
3. An application-owned Modal Function in the same requested region receives the handle.
4. The Function enters one `borrow_async()` context around the complete model loop.
5. One pooled async HTTP client carries every operation for that borrow.
6. The borrower releases the lease before the owner cleans up the desktop.

Follow the executable
[`modal_function_session_handoff.py`](../examples/modal_function_session_handoff.py) example for
the full lifecycle. Provider model calls remain in application code.

## Use the model-loop path

Send one ordered action batch through `await computer.step(actions)`. The daemon validates the full
batch before mutation, runs actions in order, and captures an immediate post-action frame. The
versioned response returns action results, a byte-backed `Screenshot`, and timing metadata.

The immediate post-action frame does not establish application readiness. Keep workload-specific
readiness checks in the application. First visual change remains experimental and has a separate
[observation contract](experimental-visual-change-observation.md).

Use `await computer.screenshots.full()` for the initial observation or screenshot-only work. Inline
full screenshots use the binary response and return a typed `Screenshot`. Call `as_bytes()` when an
integration needs bytes and `to_base64()` when a provider payload needs base64.

The daemon prefers persistent MSS capture for cursor-hidden screenshots and persistent XTest input.
The daemon falls back to a compatibility backend only before capture or input has produced an
uncertain result. The SDK never replays a mutation after dispatch may have started.

## Keep cost choices visible

Set the region, Function and Sandbox resources, images, timeouts, retries, and capacity in
application code. The SDK has no universal values for those choices. The primary example keeps warm
capacity off with `min_containers=0`.

Native async provisioning improves cancellation and cleanup behavior. It does not shorten cold
allocation or desktop startup. Report allocation, Function dispatch, borrow entry, warm operations,
and cleanup separately.

[Performance](performance.md) covers tuning and measurement boundaries. [Benchmarking](benchmarking.md)
covers commands and evidence rules.

## Low-level compatibility

Direct daemon clients, local clients, synchronous ownership, attach flows, REST and idempotency
routes, JSON/base64 screenshots, and `full_bytes()` remain supported. They serve explicit local,
debugging, recovery, and compatibility workflows. They do not establish the placed trajectory on
their own, and the SDK does not silently downgrade `computer.step()` to two requests.

Use the [public performance guide](https://modal-computer-use.mintlify.app/operate/performance) for
the hosted workflow.
