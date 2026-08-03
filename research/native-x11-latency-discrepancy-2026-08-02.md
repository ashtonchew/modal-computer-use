# Native X11 latency discrepancy: historical 146 ms versus current 32.78 ms

Research date: 2026-08-02.

## Conclusion

The historical `146.33 ms -> 1.15 ms` result is a recoverable, dated same-run diagnostic, not an
untraceable number: commit
[`5ada640b`](https://github.com/ashtonchew/modal-computer-use/commit/5ada640b090d5716c5bc31f7aeeb0fd2c05b6a66)
preserves the dirty worktree used for the final run. The recorded run timeline places the final
XTest command at 2026-07-23 17:23:56 PDT, the matching `xdotool` command at 17:24:21, comparison at
17:25:00, and the WIP snapshot at 17:25:28. **Repository inference:** absent an intervening
source edit in that sequence, `5ada640b` is the exact recoverable run source. The two `/private/tmp`
reports and their three-element arrays were not committed, so a fresh clone still cannot recompute
the historical aggregates.

The clean replication measured `32.7829 ms -> 1.5598 ms` with 30 tracked samples per arm. A later
controlled matrix directly shows that shared-loop subprocess ownership adds about 89.65 ms to the
current `xdotool` case. Together with the earlier PR #17 control, the evidence supports the
structural conclusion that a persistent XTest connection is materially faster than the
compatibility subprocess path. The matrix identifies a large current-source runner effect; it does
not retroactively assign the historical 113.55 ms aggregate difference or restore its samples.

## Measurement record and controls

| Run | Provenance | Samples/arm | XTest mean | `xdotool` mean | Ratio |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-05-22 PR #17 control | PR body; `/tmp` arrays absent | 10 | 1.1 ms | 32.2 ms | about 30x |
| 2026-07-23 diagnostic | source recovered at `5ada640b`; arrays absent | 3 | 1.15 ms | 146.33 ms | 127.7x |
| 2026-08-02 replication | clean SHA; arrays tracked | 30 | 1.5598 ms | 32.7829 ms | 21.017x |

The [historical report](../docs/archive/benchmarks/benchmark-results-2026-07-23-native-x11-input.md)
records one warmup, three measured iterations, forced backends, successful readbacks, identical
conditions for both arms, HTTP/1.1 attested-tunnel ingress, and provider-default placement. The
[replication artifact](../benchmark-data/modal-native-x11-backend-ab-replication-2026-08-02.json)
retains all 30 daemon samples per arm, a clean source SHA, successful readbacks, HTTP/1.1 Connect
ingress, provider-default placement, and `subprocess_backend="isolated-asyncio"`.

[PR #17](https://github.com/ashtonchew/modal-computer-use/pull/17) reports a forced-backend,
10-iteration HTTP/2 attested-tunnel comparison at its shared-loop head: `move_click` daemon means
of 32.2 ms (`xdotool`) and 1.1 ms (XTest). Its artifacts were intentionally kept in `/tmp`, so it is
an aggregate-only control, not an independently recalculable run. It matters because a shared-loop
implementation had already produced a roughly 32 ms `xdotool` mean; loop ownership alone therefore
cannot explain the later 146.33 ms observation.

As a descriptive check on the current sample distribution, all `C(30,3) = 4,060` distinct
three-sample means from the replication's tracked `xdotool` array range from 26.381 to 51.716 ms;
zero are near or above 146.33 ms. This exhaustive recombination is exactly reproducible from the
artifact. It shows that merely reducing the current array to three observations cannot recreate
146.33 ms; because the triplets overlap and all come from one modern run, it is not a probability
estimate and says nothing by itself about the historical run's distribution.

## What the `move_click` case executes

The benchmark contract is two API actions: a move followed by a coordinate-bearing click
([`MOVE_CLICK_ACTIONS`](../src/modal_computer_use/benchmarks/constants.py)). In the `xdotool` arm,
the move launches one `xdotool mousemove` process; the click launches a second process whose single
CLI invocation composes `mousemove` and `click`. Thus the measured case launches **two** processes,
not one. The same contract and composition are present in
[`5ada640b`](https://github.com/ashtonchew/modal-computer-use/blob/5ada640b090d5716c5bc31f7aeeb0fd2c05b6a66/src/modal_computer_use/benchmarks/constants.py)
and its
[`mouse.py`](https://github.com/ashtonchew/modal-computer-use/blob/5ada640b090d5716c5bc31f7aeeb0fd2c05b6a66/src/modal_computer_use/daemon/desktop/mouse.py).

The persistent native arm also handles two API actions. The move action emits motion and calls
`XSync` once; the click action emits motion, button-down, and button-up and calls `XSync` once. The
correct boundary is therefore one native sync per API action/emission—two syncs for this benchmark
case—not one sync for the whole case. A single public `click(x, y)` call would compose the three
events under one sync.

Upstream `xdotool` v3.20160805.1 shows that one CLI context executes its supplied commands, while
libxdo opens a display, queries XTest, populates a keyboard map, emits XTest events, and closes the
display when freed ([`xdotool.c`](https://github.com/jordansissel/xdotool/blob/v3.20160805.1/xdotool.c),
[`xdo.c`](https://github.com/jordansissel/xdotool/blob/v3.20160805.1/xdo.c),
[Debian sources](https://sources.debian.org/src/xdotool/)). The repository image installs distro
`xdotool` through APT, but neither historical run recorded the resolved binary version.

The [XTEST specification](https://www.x.org/releases/X11R7.6/doc/xextproto/xtest.html) defines a
zero event delay as `CurrentTime`. Xlib documents that `XSync` flushes and waits for earlier requests
to be processed, and that normal `XCloseDisplay` performs a final `XSync`
([XSync](https://www.x.org/archive/X11R7.5/doc/man/man3/XSync.3.html),
[XOpenDisplay/XCloseDisplay](https://www.x.org/archive/X11R7.5/doc/man/man3/XOpenDisplay.3.html)).
Consequently, normal `xdotool` teardown gives each process a final synchronization boundary; process
exit alone is not the protocol guarantee.

## Controlled subprocess-runner matrix

The tracked
[three-block matrix](../benchmark-data/modal-native-x11-runner-matrix-2026-08-02.json) holds source,
region (`us-west-2`), resources (4 CPU, 8 GiB), Connect HTTP/1.1 ingress, one warmup, and 30 measured
iterations per cell fixed. Each randomized block runs the four input/runner combinations in fresh
sandboxes. Arithmetic means independently recalculated from the raw arrays are:

| Input | Runner | Block means (ms) | Pooled mean, n=90 |
| --- | --- | --- | ---: |
| `xdotool` | shared `asyncio` | 127.479, 127.006, 113.377 | 122.621 ms |
| `xdotool` | `isolated-asyncio` | 30.869, 28.198, 39.850 | 32.972 ms |
| XTest | shared `asyncio` | 1.075, 1.309, 1.973 | 1.452 ms |
| XTest | `isolated-asyncio` | 1.495, 1.400, 1.556 | 1.484 ms |

For `xdotool`, the paired shared-minus-isolated block differences are 96.610, 98.808, and
73.527 ms; the pooled difference is 89.649 ms. The corresponding XTest difference is -0.032 ms,
with small block differences of -0.421, -0.091, and +0.417 ms that reverse direction. The runner
setting therefore has a large, repeatable effect on the subprocess-backed path in this controlled
environment, not a comparable global effect on native XTest execution.

The effect also scales with the known process count. The pooled shared/isolated means for the
eight-launch `move_click_sequence` are 485.537/124.413 ms, or
`(485.5372995 - 124.4128215) / 8 = 45.1406 ms` per launch. For the two-launch `move_click` case,
`(122.6209781 - 32.9724671) / 2 = 44.8243 ms` per launch. The close per-launch estimates are strong
repository evidence that the controlled delta belongs to repeated subprocess execution.

All gates passed: 12/12 cells completed; every manifest SHA-256 matched its raw artifact; every
artifact, daemon surface, `move_click` case, cursor/typing readback, cleanup, source attribution,
placement, HTTP/1.1 transport, input-backend attribution, and 30-sample count matched the plan; the
target runtime inventory was identical across cells. The preregistered shared-loop plausibility band
was 109.7475-182.9125 ms. Its pooled 122.621 ms result and all three shared-loop block means fell
inside the band, so the planned HTTP/2/`5ada640b` fallback was not triggered.

## Explanatory boundary

Current [`process_runner.py`](../src/modal_computer_use/daemon/desktop/process_runner.py) moves
subprocess work from the request-serving loop to a private standard-asyncio thread. In the tracked
30-sample [subprocess-runner A/B](../benchmark-data/modal-subprocess-runner-ab-1cpu-2026-07-31.json),
shared asyncio measured 56.53 ms daemon p50 for a small shell command versus 8.56 ms on isolated
asyncio. That shell A/B is supporting mechanism evidence; the three-block matrix is the direct
`xdotool` ablation. PR #17's earlier shared-loop 32.2 ms result still prevents treating runner
ownership as a universal or complete explanation across historical environments.

- **Tracked measurement fact:** the replication means, ratio, all 4,060 triplet means, matrix cell
  means, paired contrasts, and per-launch estimates recalculate from tracked arrays.
- **Recoverable historical provenance:** `5ada640b` preserves the source; the dated report and
  recorded timeline preserve aggregate results and timing; the raw three-sample arrays remain absent.
- **Externally documented mechanism:** XTest event semantics and Xlib synchronization support the
  persistent-session/process-lifecycle explanation.
- **Controlled repository result:** on the matrix source and environment, isolated subprocess
  ownership removes an approximately 44.8-45.1 ms cost per `xdotool` launch.
- **Repository inference:** avoiding process/display lifecycles explains the structural XTest
  advantage and the matrix's launch-count scaling.
- **Not established:** the 113.55 ms difference between historical and replicated `xdotool` means
  cannot be exactly apportioned among loop ownership, run variance, cloud scheduling, image/package
  drift, topology, or other changed conditions; the matrix does not supersede either dated run.
