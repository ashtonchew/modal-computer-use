# Modal deployment

Use an async owner and one placed Modal Function for the primary path. Set the same exact requested
region on both sides. Also set the Modal environment, Function and Sandbox resources, images,
timeouts, retries, and capacity in application code so operators can inspect the cost-bearing
choices.

The lifecycle order is:

1. Enter `AsyncComputerSandbox.create(...)` once.
2. Call `owner.session_handle()` once after the desktop is ready.
3. Dispatch the versioned session handle to an application-owned Modal Function.
4. Enter one `borrow_async()` context for the complete repeated trajectory.
5. Use `computer.step()` for each ordered action array and immediate post-action frame.
6. Wait for the Function result or reach an explicit cancellation outcome.
7. Let the borrower release its lease.
8. Leave the owner context so it terminates the desktop and closes its client.

The owner must outlive the dispatched Function. Do not return from the owner context after spawn
unless the application has durably transferred lifecycle ownership and defined recovery. A
borrower always detaches; it never terminates the owner's desktop.

The immediate frame returned by `computer.step()` is not application readiness. Keep readiness
checks in the application model loop. First-visual-change observation is a separate experimental
Interface.

Placement checks fail before desktop mutation when the Function region is missing, mismatched, or
unverifiable. The SDK does not silently degrade to an external laptop caller. The handle does not
contain a reusable bearer token. Borrow entry resolves fresh client access and verifies the live
desktop, protocol, policy, and requested placement.

Native async creation remains cancellation-safe. It does not reduce cold allocation or desktop
startup time. Keep cold allocation, Function dispatch, borrow entry, warm operations, and cleanup
as separate timing fields.

Warm capacity is off in the primary example. `min_containers=0` allows the Function to scale to
zero, and the owner creates a Sandbox on demand. Positive Function minimums and Sandbox warm pools
are explicit cost-bearing choices. Configure them only when the operator accepts the idle cost.

Use the executable
[`modal_function_session_handoff.py`](../examples/modal_function_session_handoff.py) example as the
local source of truth. Deploy it in the explicit environment named by the example:

```bash
uv run modal deploy --env main examples/modal_function_session_handoff.py
```

This command can create billable resources when the deployed Function is invoked. Do not run it or
the example without operator authorization.

Use the [deployment guide](https://modal-computer-use.mintlify.app/operate/deploy) for the public
guide.

## Low-level compatibility

Local clients, direct daemon and REST access, attach flows, synchronous ownership, and recovery
tools remain supported. They are useful for debugging and compatibility. They do not establish the
placed default trajectory on their own.
