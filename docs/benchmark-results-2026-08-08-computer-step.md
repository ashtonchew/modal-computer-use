# Computer Step promotion result — 2026-08-08

Status: **eligible; promote**

This report records the first promotion measurement for the stable `computer.step(actions)`
Interface. It measures one ordered action batch followed by its immediate screenshot as one fused
HTTP operation. It does not measure first visual change, application readiness, or task success.

## Result

| Same-topology arm | Warm action-to-frame p50 | Warm action-to-frame p95 |
| --- | ---: | ---: |
| Prior public: `actions.run()` then `screenshots.full()` | 47.14 ms | 58.22 ms |
| Candidate default: `computer.step()` | **44.29 ms** | **52.57 ms** |

The candidate improved the paired median by 3.37 ms. The paired bootstrap 95% confidence interval
for candidate minus prior was -4.66 to -2.65 ms. The candidate also passed the preregistered rule
that its p95 must not exceed the prior p95.

The historical article value of 47.10 ms was 37.25 ms plus 9.85 ms from separate screenshot and
click medians. It was not a measured fused turn. The new 44.29 ms value is a true fused
action-to-immediate-frame measurement, but it comes from a new experiment and must not be
presented as a remeasurement of the historical arithmetic.

## Fixed configuration

- Source commit: `f6b9adeee54f584c345c813750758b7c7b5db744`
- Caller topology: one application-owned Modal Function
- Function and Sandbox placement: requested and observed `aws/us-west-2`
- Function resources: 1 CPU, 2048 MiB; minimum containers 0
- Sandbox resources: 1 CPU, 2048 MiB; warm-pool capacity 0
- Ingress and HTTP: attested tunnel over HTTP/1.1
- Client lifetime: one `borrow_async()` context and one pooled async HTTP client
- Input: persistent XTest; product rate limit retained at 20 events per second
- Screenshot: PNG, quality 90, scale 1, hidden cursor, daemon processing, inline raw-binary
- Action: one left click at the preregistered coordinate
- Pacing: 125 ms after each arm, outside the measured boundary, to remain below the retained
  rolling input limit
- Retries and replacement samples: 0

Each arm reset the pointer and captured an untimed baseline before measurement. A valid sample
required a byte-valid screenshot, the measured post-action cursor coordinate, and a later capture
timestamp from the same daemon. This proves action-before-capture ordering without waiting for a
browser paint or comparing clocks across hosts.

## Sampling and lifecycle

The run retained 100 interleaved paired samples per arm after two warm-up pairs. Both artifacts
record zero failures. Cleanup succeeded with zero survivors.

Cold allocation and setup were paid once and excluded from warm action-to-frame latency:

- Sandbox allocation: 1182.79 ms
- Sandbox startup and attestation: 9195.55 ms
- Mutation-free Function placement probe: 5229.78 ms
- Trajectory borrow setup: 1229.61 ms

The Function placement probe and the measurement invocation were separate Function calls. The
measurement invocation received the versioned handle and owned the entire interleaved trajectory.

## Evidence

- [Prior public raw artifact](../benchmark-data/computer-step-prior-public-2026-08-08.json)
- [Candidate raw artifact](../benchmark-data/computer-step-candidate-2026-08-08.json)
- [Promotion decision](../benchmark-data/computer-step-promotion-decision-2026-08-08.json)

The artifacts contain sanitized numeric observations and configuration only. Historical reports
and artifacts remain unchanged.
