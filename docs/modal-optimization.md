# Optimize Modal for production

Use this guide after a Modal desktop works from end to end. Measure startup and warm-operation
latency separately. Keep the default configuration until measurements identify a bottleneck.

This guide covers the main production choices. See [Performance](performance.md) for detailed
latency mechanisms and benchmark records.

## Start with one repeated loop

Create the desktop once and keep one connection for the full agent trajectory. The
[Modal Function session-handoff example](../examples/modal_function_session_handoff.py) is the
ownership pattern:

1. The application creates and owns the desktop.
2. The application passes a `ComputerSessionHandle` to a deployed Modal Function.
3. The Function opens one `borrow_async()` context around the repeated observe, model, and action
   loop.
4. The Function detaches when the trajectory ends.
5. The application terminates the desktop after the Function reaches a terminal result.

This shape pays startup once and keeps the connection, lease, and daemon state available between
turns. The application still owns admission, provider calls, deadlines, cancellation, and final
cleanup.

Read [Hand a desktop to a Modal Function](modal-deployment.md#hand-a-desktop-to-a-modal-function)
for deployment and failure behavior. The [API handoff contract](api.md#modal-function-session-handoff)
defines ownership and recovery.

### Keep async orchestration responsive

Use `AsyncComputerSandbox` when the application already runs on asyncio. In a deployed Modal
Function, keep one `borrow_async()` context open for the full trajectory.

Native async keeps the event loop available during Modal and daemon I/O. The async APIs also
provide cancellation-aware cleanup. Caller placement and session reuse reduce warm-path latency.

The [async owner example](../examples/async_modal_owner.py) shows native async creation and cleanup.
Read [Native async provisioning](modal-deployment.md#native-async-provisioning) for ownership and
cancellation details.

## Place the caller near the Sandbox

Run the [Modal region benchmark](benchmarking.md#run-a-modal-sdk-benchmark) from the real
application caller. Use the measured region selector in the separate placement requests for the
desktop and deployed Function. Modal applies each request independently. Host and availability-zone
placement remain unspecified.

Use one of these caller shapes:

- Prefer the deployed Function handoff for a repeated application trajectory.
- Use the [co-located runner example](../examples/modal_colocated_runner.py) for a bounded command
  that starts its own nearby runner.

Keep brokers, durable stores, and public gateways away from the screenshot and action hot path.
They can own admission, recovery, and audit records without relaying every frame.

## Reduce work in each turn

Choose the smallest public operation that preserves the required behavior:

- [Batch ordered actions](performance.md#batch-actions) in one request when each action can run
  without an intermediate model decision.
- Use a [binary screenshot path](performance.md#screenshot-hot-paths) when the next model call only
  needs image bytes.
- Use fused action and screenshot capture when the immediate frame is the required observation.
- Use [visual-change observation](experimental-visual-change-observation.md#choose-a-synchronization-method)
  when the first changed pixels define the boundary. Keep application readiness checks for semantic
  states such as a completed save or a loaded page.

Measure the complete caller-observed request. Daemon-only timing omits transport, authentication,
and response processing.

## Prepare the browser and image

Put stable system and Python dependencies in the Modal Image. Put frequently changed application
files in later image layers so Modal can reuse earlier layers.

The [browser profile example](../examples/browser_profile.py) shows browser prewarm and explicit
resource profiles. Enable prewarm after browser startup appears in the measured boundary. Add CPU,
memory, or GPU resources after the workload shows sustained demand.

Filesystem snapshots can preserve prepared files between desktops. GUI processes and browser
sessions start with each desktop. See [Filesystem snapshots](performance.md#filesystem-snapshots)
for the supported behavior.

## Choose warm capacity

Function warm capacity and desktop warm pools solve different waits:

- Positive Function capacity reduces the wait for a deployed Function container.
- A desktop [warm pool](../examples/04_warm_pool.py) reduces the wait for a ready Sandbox.

Both choices add idle cost. Warm capacity changes request-to-ready time. Warm action latency follows
the connection, request, and daemon paths described above.

Read [Warm capacity](modal-deployment.md#warm-capacity) for Function and Sandbox ownership. Measure
pool hit rate, cold fallback rate, remaining lifetime, and cost before increasing capacity.

## Measure the complete boundary

Record enough context to reproduce each result:

- caller location and topology;
- requested and observed placement;
- ingress, image revision, resources, and browser setup;
- cold or warm state and the exact timer boundary;
- sample count, p50, p95, failures, cleanup, and cost.

Use at least 30 measured samples when reporting p95. Keep raw samples and record the clean evidence
revision. The [Benchmarking](benchmarking.md) guide defines maintained commands, artifact status,
statistics, cost accounting, and publication rules.

See the [July 30 warm-operation report](benchmark-results-2026-07-30-warm-paths.md) for a complete
example. Its optimized measurements used a synchronous co-located client. Native async improves
orchestration behavior and has a separate measurement scope.

## Related guides

- [Modal deployment](modal-deployment.md): lifecycle, handoff, caller placement, and warm capacity.
- [Performance](performance.md): implementation mechanisms, A/B evidence, and detailed tuning.
- [API](api.md): ownership, async surfaces, session handles, and daemon contracts.
- [Experimental visual-change observation](experimental-visual-change-observation.md):
  action-to-first-change semantics and limits.
