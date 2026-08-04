# Fallback ownership benchmark, 2026-07-24

> **Archive category:** Diagnostic
> **Date or revision:** 2026-07-24; source `5f880c509a13605aca5df5f91fa8a5e56160d03b`
> **Question:** Did the fallback-ownership change preserve correctness and contemporaneous Modal
> performance?
> **Disposition:** The report records implementation validation for the merged change. The later
> [2026-07-26 provider report](benchmark-results-2026-07-26-provider-results.md) retains the
> contemporaneous provider evidence.

This report validates the fallback-ownership cleanup through code revision
`5f880c509a13605aca5df5f91fa8a5e56160d03b` against latest-main revision
`ca64daa1c59eba608ff5dce6becaa2aa32b9e599`.

## Environment and contract

- Modal profile: `auto-alphafold3`
- Modal environment: `main`
- Modal target and runner region: `us-west-2`
- Target: Chromium, 4 CPU, 8192 MiB
- Colocated runner: 1 CPU, 1024 MiB
- Input: forced XTest, rate limit disabled
- Hot-path samples: 30 after one warmup
- Vendor samples: 3 product lifecycles after one warmup
- Provider credentials: existing repository `.env` symlink; values were not logged or tracked

The optimized runs use public daemon and SDK operation boundaries. The `target-loopback` path runs
the benchmark client inside the target sandbox over `127.0.0.1`. This path is a diagnostic lower
bound. It is not a separate product topology. The provider-default comparison uses each provider's
public SDK from the same external caller.

## Headline optimized comparison

This table reports p50. Modal optimized is the 30-sample target-loopback path. Daytona and E2B use
provider-default public SDK paths from three complete product lifecycles. The ratios are descriptive
best-system comparisons. They are not paired causal estimates.

| Operation | Modal optimized | Daytona default | E2B default | Optimized Modal comparison |
| --- | ---: | ---: | ---: | --- |
| Full screenshot | **25.66 ms** | 198.15 ms | 187.30 ms | **7.72x / 7.30x faster** |
| Move + click | **3.78 ms** | 370.30 ms | 220.10 ms | **97.86x / 58.16x faster** |
| Four move/click pairs | **12.60 ms** | 1,462.81 ms | 881.16 ms | **116.13x / 69.95x faster** |
| Type 100 characters | **11.14 ms** | 623.07 ms | 4,299.83 ms | **55.95x / 386.10x faster** |
| Type 1,000 characters | **66.10 ms** | 5,352.52 ms | 41,861.39 ms | **80.97x / 633.28x faster** |
| Command echo | **97.73 ms** | 117.28 ms | **61.26 ms** | Modal **1.20x** faster than Daytona; E2B **1.60x** faster |

## Correctness

Before the change, a Connect target sent its Connect token to the loopback daemon. Every
target-loopback warmup failed authentication. The SDK now keeps a separate daemon bearer for
loopback execution. It does not place that bearer in public metadata or logs.

| Check | Result |
|---|---:|
| Target-loopback smoke | `ok=true` |
| Full target-loopback HTTP + observation run | 0 failures |
| Selected HTTP operations | 30/30 each |
| Selected observation cases | 30/30 each |
| Cursor readback | passed |
| Controlled typing readback | passed |
| Provider comparison | all providers `ok`, 0 failures |

## Contemporaneous main ablation

The first post-change screenshot sample was slower than an earlier baseline. MSS 10.2 maps the
default selector and `xshmgetimage` to the same implementation. Therefore, the decisive check used
immediate back-to-back runs with the same command and resources. The main run used a detached,
clean worktree. Negative deltas are faster.

| Connect operation | Main p50 | Feature p50 | p50 change | Main p95 | Feature p95 | p95 change |
|---|---:|---:|---:|---:|---:|---:|
| Full screenshot | 39.47 ms | 40.99 ms | +3.87% | 42.00 ms | 42.48 ms | +1.14% |
| Move + click | 4.66 ms | 5.01 ms | +7.71% | 5.73 ms | 6.12 ms | +6.93% |
| Four move/click pairs | 10.02 ms | 9.39 ms | **-6.31%** | 13.55 ms | 11.21 ms | **-17.30%** |
| Type 100 characters | 11.29 ms | 11.19 ms | **-0.85%** | 12.71 ms | 12.42 ms | **-2.30%** |
| Type 1,000 characters | 49.65 ms | 53.88 ms | +8.54% | 54.71 ms | 59.74 ms | +9.19% |
| Command echo | 93.66 ms | 101.72 ms | +8.61% | 261.90 ms | 290.01 ms | +10.73% |

The preregistered screenshot gate allowed the larger of 10% or 2 ms at p50. It allowed the larger
of 15% or 3 ms at p95. The feature run passed both limits. Each selected p50 remained within 10% of
the contemporaneous main run.

## Optimized target-loopback results

| Operation or causal observation | p50 | p95 | Valid |
|---|---:|---:|---:|
| Full screenshot | 25.66 ms | 27.86 ms | 30/30 |
| Move + click | 3.78 ms | 4.76 ms | 30/30 |
| Four move/click pairs | 12.60 ms | 23.79 ms | 30/30 |
| Type 100 characters | 11.14 ms | 16.02 ms | 30/30 |
| Type 1,000 characters | 66.10 ms | 93.50 ms | 30/30 |
| SDK-default action → changed frame | 12.53 ms | 33.85 ms | 30/30 |
| Auto-signal action → changed frame | 33.33 ms | 38.06 ms | 30/30 |

The final two rows are the clean-worktree, post-review confirmation at the code revision named at
the start of this report. The ignored raw artifact is
`benchmark-results/fallback-ownership-pr-head-loopback-observation-20260724.json`.
It reports `ok=true`, zero failures, and `git_worktree_clean=true`.

The preceding clean confirmation at `6f06fc50e589fee11e477d536e994a4d07416bce` measured 13.05/41.92
ms for SDK-default p50/p95 and 32.28/44.97 ms for auto-signal. At PR head, the two p50 values
changed by -4.0% and +3.3%. Both p95 values improved. This result supports the no-regression
conclusion. The changes between these revisions affect warm-pool and readiness-failure ownership,
not the daemon input or capture hot path. Both runs completed 30/30 samples.

## Same-run provider comparison

Values are p50. Ratios above `1.00x` mean Modal is faster for that operation.

| Public SDK operation | Modal XTest | Daytona | E2B | Modal vs Daytona | Modal vs E2B |
|---|---:|---:|---:|---:|---:|
| Product create → first screenshot | 11,683.64 ms | 10,598.13 ms | **1,423.70 ms** | 0.91x | 0.12x |
| Full screenshot | **118.18 ms** | 198.15 ms | 187.30 ms | **1.68x** | **1.58x** |
| Move + click | **70.55 ms** | 370.30 ms | 220.10 ms | **5.25x** | **3.12x** |
| Four move/click pairs | **73.35 ms** | 1,462.81 ms | 881.16 ms | **19.94x** | **12.01x** |
| Type 100 characters | **75.26 ms** | 623.07 ms | 4,299.83 ms | **8.28x** | **57.14x** |
| Type 1,000 characters | **107.05 ms** | 5,352.52 ms | 41,861.39 ms | **50.00x** | **391.04x** |
| Command echo | 99.75 ms | 117.28 ms | **61.26 ms** | **1.18x** | 0.61x |

This is not a universal provider ranking. E2B is faster for cold product startup and command echo.
Modal is faster for screenshot, daemon-native input, and batching. The provider sample is complete
but small (`n=3`). The results are descriptive and are not paired causal effects.

The sanitized candidate artifact is
`benchmark-data/provider-compare-fallback-ownership-2026-07-24-candidate.json`. The raw report
contains credentials and endpoints, so it remains ignored at
`benchmark-results/candidates/provider-compare-fallback-ownership-20260724.json`. The sanitized
artifact records its SHA-256. The provider comparison ran at `6d3d22d`. Later commits affect
orchestration failure ownership, typed readiness errors, tests, and documentation. They do not
change daemon input, capture, or vendor-adapter hot paths.

## Observed harness limitations

- A long combined Connect and target-loopback job lost its target before the final diagnostic
  `Sandbox.exec`. The separate path runs completed. Therefore, the provider and optimized tables
  keep the paths independent.
- The Connect observation stream twice timed out during setup for the auto-signal case. Other
  Connect observation cases completed 30/30, and the same auto-signal behavior completed 30/30 over
  target-loopback. The failed attempts are retained and not averaged into the successful data.
- Historical and contemporaneous hosts had different stable screenshot floors. The performance
  conclusion uses the immediate main-versus-feature ablation. It does not use the faster older
  baseline.
