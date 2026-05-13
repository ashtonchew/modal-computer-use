# Provider Benchmark Results Interpretation

This benchmark compares this project's daemon-first Modal computer-use path with provider-native
Daytona and E2B desktop APIs. It is a primitives benchmark, not a full agent-loop benchmark.

## What Is Measured

- `cold_create_to_ready`: create a sandbox/desktop and wait until screenshots are usable.
- `screenshot_full`: one full-screen screenshot round trip.
- `move_click`: one deterministic mouse move and click.
- `move_click_sequence`: four deterministic move/click pairs.
- `type_100_chars`: one deterministic 100-character GUI typing action.
- `type_1000_chars`: one deterministic 1000-character GUI typing action.
- `command_echo`: one small shell command round trip.
- `readback`: proof probes for cursor position and typed text.

Warm primitive cases reuse one already-ready sandbox per provider run. Cold start is measured
separately so action timings do not include sandbox creation.

## Fairness Notes

- Modal is daemon-first: Modal runs this project's daemon, and the daemon owns primitive execution,
  validation, timing attribution, redaction, and readback.
- Daytona and E2B are provider-API-first: the benchmark calls their SDK computer-use primitives
  directly.
- The benchmark records provider defaults unless a case name explicitly says otherwise. This keeps
  the comparison honest for the default developer experience.
- Screenshot timings are payload-size and network-round-trip sensitive.
- GUI typing is intentionally measured as GUI typing, not file writes, clipboard injection, or shell
  text insertion.

## E2B Typing

E2B's `type_1000_chars` result is expected to be much slower than Modal and Daytona when using the
default E2B Desktop SDK typing path.

Installed SDK checked during the run:

- `e2b-desktop==2.3.1`
- `Sandbox.write(text, *, chunk_size=25, delay_in_ms=75)`

The SDK implementation splits text into chunks and runs:

```python
xdotool type --delay {delay_in_ms} -- {chunk}
```

That delay is passed to `xdotool type`, so it slows the generated key events. A live probe showed:

| E2B typing mode | Time |
|---|---:|
| default 100 chars | 4306ms |
| default 1000 chars | 42704ms |
| tuned 1000, `chunk_size=100`, `delay_in_ms=1` | 2305ms |
| tuned 1000, `chunk_size=1000`, `delay_in_ms=0` | 496ms |

Interpretation: the ~42s E2B `type_1000_chars` measurement is provider-default behavior, not a
cold-start or benchmark setup bug. If tuned typing matters, add a separate metric such as
`type_1000_chars_tuned` and record the exact E2B parameters rather than changing the default metric.

## Cost Fields

- Daytona and E2B `cost_estimate` values are public-rate estimates based on encoded default resource
  assumptions and measured sandbox lifetime.
- Modal `cost_estimate` remains `partial` for default resources because Modal bills on the higher of
  requested resources or actual usage, and the SDK does not expose resolved billed CPU/memory for a
  just-created sandbox.
- Modal actual cost is captured separately through `billing_reconciliation`, using Modal billing
  reports filtered by benchmark App tags.
- Modal billing reconciliation is delayed and bucketed by full report intervals, so fresh runs can
  initially show `not_available_yet`.

Do not treat estimates or billing reconciliation as invoice truth. Credits, discounts, reservations,
and report-window attribution can change invoice-level interpretation.
