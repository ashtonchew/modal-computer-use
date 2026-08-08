# Troubleshooting

Use these checks for the optimized default before you inspect low-level daemon behavior.

## Handoff fails before the trajectory starts

Check that the owner and Function use the same Modal environment and exact requested region. Check
that the owner produced a current versioned handle and that the Function passed its region to
`borrow_async()`. A missing, mismatched, or unverifiable placement must fail before desktop
mutation. Do not work around the error with an external-caller fallback.

## Borrow fails or loses readiness

Use one `borrow_async()` context for the complete trajectory. Do not reuse a borrow context or a
sealed run ID. Check daemon health, readiness, version, capabilities, and the live session policy.
The SDK closes any partially created client when borrow entry fails.

If the error names `computer-step-envelope-v1`, the installed daemon cannot support the primary
`computer.step()` Interface. Install a compatible runtime artifact. Do not work around the error by
issuing separate action and screenshot requests in the optimized path.

If a mutation may have reached the daemon but its response was lost, do not repeat it. Use the
reported receipt outcome. A read-only observation can show a later visible state, but it cannot
prove that an invisible action succeeded or that the application is ready.

## Screenshot validation fails

The inline `screenshots.full()` path validates the binary body and typed metadata headers. A missing
or inconsistent format, size, hash, geometry, coordinate space, timestamp, or cursor field fails the
request. The SDK does not silently retry through the JSON/base64 route after dispatch. Check that
the installed daemon and client have a compatible screenshot protocol.

Cursor-hidden requests normally use the persistent native capture session. Cursor-visible requests
and failed display connections can use the bounded fallback. An ordinary Xlib failure should fail
one request, not terminate the daemon.

## Latency is higher than expected

Separate cold allocation, desktop startup, Function dispatch, borrow entry, and warm operation
time. Native async provisioning protects cancellation and cleanup; it does not shorten cold
allocation. Confirm exact requested and observed placement, resources, image revision, ingress,
HTTP version, capture and input backends, warmup, and connection reuse before comparing results.

Warm capacity is off in the primary example. Enabling positive Function minimums or a Sandbox warm
pool can reduce some cold waits but creates explicit idle cost. It does not change the repeated warm
operation contract.

The article's 47.10 ms value is arithmetic over separate 37.25 ms warm raw-screenshot and 9.85 ms
click medians. It is not a measured fused turn and is not a latency promise for `computer.step()` or
a different topology.

## A changed frame arrives too early

First-visual-change observation is experimental. XDamage only signals where to look; the daemon
uses full-resolution pixel verification and can fall back to polling. A verified first change is
not application readiness or visual stability. Use an application-specific readiness condition
before the next dependent action.

Use the [troubleshooting guide](https://modal-computer-use.mintlify.app/operate/troubleshooting) for
the public guide.

## Low-level compatibility

For direct REST, local daemon, attach, idempotency, or debugging work, use the low-level clients and
routes explicitly. Their availability does not mean that they provide the placed primary topology.
