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
`computer.step()` interface. Install a compatible runtime artifact. Do not work around the error by
issuing separate action and screenshot requests in the optimized path.

If a mutation may have reached the daemon but its response was lost, do not repeat it. Use the
reported receipt outcome. A read-only observation can show a later visible state, but it cannot
prove that an invisible action succeeded or that the application is ready.

## Screenshot validation fails

The inline `screenshots.full()` path validates the binary body and typed metadata headers. A missing
or inconsistent format, size, hash, geometry, coordinate space, timestamp, or cursor field fails the
request. The SDK does not silently retry through the JSON/base64 route after dispatch. Check that
the installed daemon and client have a compatible screenshot protocol.

Cursor-hidden lossless PNG requests use X11 shared-memory capture in a managed Image after its
extension and live display pass readiness. Under `auto`, an unavailable or failed source is
quarantined for the current X-server generation and subsequent eligible captures report
`mss-fallback` instead of repeatedly reopening it. Restarting the display clears the quarantine
and re-probes the source. If the X server itself misses the bounded reply deadline, the request
fails closed: MSS, `scrot`, and `maim` depend on that same display and are not safe fallbacks while
it is unresponsive. Explicit `x11-shm` fails readiness. Cursor-visible, scaled, JPEG,
WebP, and raw-RGB requests keep their existing capture paths.

If `/readyz` reports an X11 shared-memory screenshot probe failure, verify that the runtime Image
contains `_modal_computer_use_x11_shm`, that Xvfb is 24-bit TrueColor at the configured dimensions,
and that the server advertises MIT-SHM 1.2 FD attachment. Use `mss` as the explicit rollback source;
do not change PNG format, dimensions, cursor semantics, or SDK routes to work around readiness.

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

## Input is rate limited

Read `retry_after_ms` and the `Retry-After` header from a `429 rate_limited` response. The daemon
rejected the complete request before mutation. Wait in application code only when it is safe to
issue a new operation; the SDK does not replay mutations automatically.

An `input_cost_exceeds_burst` response is different. Waiting cannot make that request fit. Raise
`actions.input_rate_limit_burst` explicitly or split an application-owned batch only when doing so
preserves its required semantics. Configure both refill and burst values for an intentionally
restrictive demo or untrusted workload. The defaults are tuned to stay outside normal serialized
Step loops, but rate limiting remains resource protection rather than an approval policy.

Use the [troubleshooting guide](https://modal-computer-use.mintlify.app/operate/troubleshooting) for
the public guide.

## Low-level compatibility

For direct REST, local daemon, attach, idempotency, or debugging work, use the low-level clients and
routes explicitly. Their availability does not mean that they provide the placed primary topology.
