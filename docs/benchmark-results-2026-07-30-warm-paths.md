# Warm-operation benchmark results, 2026-07-30

**Evidence status:** eligible

## What this report measures

Warm-operation timers start after the desktop and client connection are ready. They measure one
selected public SDK or daemon request from its caller. Desktop creation and cleanup sit outside the
timer.

This report combines two eligible artifacts from July 30, 2026. Modal optimized used a Modal
Function with the same requested region as its target and a tuned daemon configuration. The four
default paths used external callers and provider-default settings.

All cells contain 30 successful samples. p50 is the median. p95 uses linear interpolation on the
sorted values at rank `0.95 * (n - 1)`. Ratios divide each path's p50 by the Modal optimized p50 for
the same case.

## Results

| Case | Modal optimized p50 / p95 | Modal default p50 / p95 / ratio | Daytona default p50 / p95 / ratio | E2B default p50 / p95 / ratio | Tzafon default p50 / p95 / ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot, provider-native format | 37.25 / 48.76 ms | 115.80 / 132.91 ms / 3.11x | 563.57 / 603.79 ms / 15.13x | 198.78 / 223.20 ms / 5.34x | 154.25 / 192.53 ms / 4.14x |
| One click on the screen | 9.85 / 16.85 ms | 214.09 / 218.19 ms / 21.73x | 386.40 / 394.19 ms / 39.22x | 209.86 / 213.42 ms / 21.30x | 130.27 / 170.55 ms / 13.22x |
| Four ordered clicks | 12.52 / 22.07 ms | 230.10 / 235.09 ms / 18.37x | 1,546.74 / 1,577.44 ms / 123.50x | 860.68 / 897.95 ms / 68.72x | 458.03 / 499.49 ms / 36.57x |
| Type 100 characters | 15.76 / 28.15 ms | 259.67 / 270.18 ms / 16.48x | 805.55 / 812.84 ms / 51.11x | 4,083.30 / 4,156.65 ms / 259.08x | 85.16 / 101.65 ms / 5.40x |
| Type 1,000 characters | 53.35 / 79.69 ms | 263.95 / 269.71 ms / 4.95x | 5,528.38 / 5,554.88 ms / 103.63x | 40,914.66 / 41,374.28 ms / 766.95x | 185.03 / 188.37 ms / 3.47x |
| Non-login shell command | 11.69 / 14.12 ms | 72.64 / 158.22 ms / 6.21x | 285.33 / 294.57 ms / 24.40x | 55.90 / 69.27 ms / 4.78x | 31.73 / 33.35 ms / 2.71x |

The ratios compare complete measured paths. Configuration, caller placement, screenshot format, and
request shape vary across columns.

## Path configuration

| Path | Caller and configuration |
| --- | --- |
| Modal optimized | One Modal Function and its targets requested `us-west-2`. The Function and target each requested 1 CPU and 2 GiB. The path used attested-tunnel ingress, HTTP/1.1, XTest input, zero input pacing, and an isolated asyncio subprocess backend. |
| Modal default | An external caller used the public SDK, standard resources, default input pacing, and the attested-tunnel daemon path. |
| Daytona default | An external caller used Daytona SDK 0.175.0 and its provider-default computer-use path. |
| E2B default | An external caller used E2B Desktop SDK 2.3.1 and its provider-default computer-use path. |
| Tzafon default | An external caller used Tzafon SDK 2.44.1 and its provider-default computer-use path. |

The optimized artifact records an observed cloud and region match for every target. A shared region
request controls Modal scheduling. Host and availability-zone placement remain unspecified.

## Measurement boundaries

- Full screenshots use each provider's public native format. Tzafon returned 1280x720 JPEG. The
  other paths returned 1024x768 PNG.
- Modal optimized, Modal default, and Tzafon sent four ordered clicks in one request. Daytona sent
  four SDK requests. E2B sent four SDK requests through eight transport calls.
- Modal optimized typed through XTest keystrokes with zero delay. The default paths used their
  recorded public SDK behavior.
- The shell case requested `sh -c "printf '42\n'"` or its provider equivalent. Every successful
  sample returned exit code 0 and exact stdout `"42\n"`.

Warm-operation timers include transport, authentication, request handling, execution, and response
collection. They exclude target creation and cleanup.

## Native async scope

The optimized July 30 harness used synchronous `ComputerSandbox` and `DaemonClient` objects inside
the co-located Modal Function. It reused one warm client and connection for the measured path.

These measurements predate the native async owner and daemon client APIs. Those APIs keep an
asyncio event loop responsive during Modal and daemon I/O and use the SDK's async cancellation and
cleanup paths. This report covers the synchronous co-located path. A separate benchmark must
compare async and sync.

## Evidence and reproducibility

The figure and tables use these tracked sanitized artifacts:

- [Modal optimized samples](../benchmark-data/modal-optimized-provider-2026-07-30.json), produced
  from clean evidence harness `a49767175fb99f9c06ca042a0703eb31d43ffa2d`.
- [Provider-default samples](../benchmark-data/provider-compare-coordinate-command-2026-07-30.json),
  produced from clean evidence harness `7cb39f7eb49e599567d303cd021c797873d4b483`.

One documentation, example, and test commit separates the two harness revisions. The `src/` diff
between them is empty.

Run the deterministic figure check from the repository root:

```bash
uv run python scripts/render_readme_benchmark_figure.py --check
```

See [Benchmarking](benchmarking.md) for the maintained commands, evidence policy, and publication
rules.
