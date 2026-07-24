# Fallback ownership benchmark, 2026-07-24

This report validates the fallback-ownership cleanup through code revision
`5f880c509a13605aca5df5f91fa8a5e56160d03b` against latest-main revision
`ca64daa1c59eba608ff5dce6becaa2aa32b9e599`.

## Environment and contract

- Modal profile: `auto-alphafold3`; environment: `main`
- Modal target and runner region: `us-west-2`
- Target: Chromium, 4 CPU, 8192 MiB
- Colocated runner: 1 CPU, 1024 MiB
- Input: forced XTest, rate limit disabled
- Hot-path samples: 30 after one warmup
- Vendor samples: 3 product lifecycles after one warmup
- Provider credentials: existing repository `.env` symlink; values were not logged or tracked

The optimized runs use public daemon/SDK operation boundaries. `target-loopback` executes the
benchmark client inside the target sandbox over `127.0.0.1`; it is a diagnostic lower bound, not a
separate product topology. The neutral provider comparison uses each provider's public SDK from the
external caller.

## Correctness

Before the change, a Connect-ingress target passed its Connect token to the loopback daemon. Every
target-loopback warmup failed authentication. After the change, SDK-owned targets retain a private
daemon bearer for loopback execution without placing it in public metadata or logs.

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

The first post-change screenshot sample was slower than a baseline captured earlier in the day.
Because MSS 10.2 maps the default and `xshmgetimage` selectors to the same implementation, the
decisive check was an immediate back-to-back run from a detached clean main worktree with the same
command and resources. Negative deltas are faster.

| Connect operation | Main p50 | Feature p50 | p50 change | Main p95 | Feature p95 | p95 change |
|---|---:|---:|---:|---:|---:|---:|
| Full screenshot | 39.47 ms | 40.99 ms | +3.87% | 42.00 ms | 42.48 ms | +1.14% |
| Move + click | 4.66 ms | 5.01 ms | +7.71% | 5.73 ms | 6.12 ms | +6.93% |
| Four move/click pairs | 10.02 ms | 9.39 ms | **-6.31%** | 13.55 ms | 11.21 ms | **-17.30%** |
| Type 100 characters | 11.29 ms | 11.19 ms | **-0.85%** | 12.71 ms | 12.42 ms | **-2.30%** |
| Type 1,000 characters | 49.65 ms | 53.88 ms | +8.54% | 54.71 ms | 59.74 ms | +9.19% |
| Command echo | 93.66 ms | 101.72 ms | +8.61% | 261.90 ms | 290.01 ms | +10.73% |

The preregistered screenshot gate allowed the larger of 10% or 2 ms at p50 and the larger of 15%
or 3 ms at p95. The feature run passed both. All selected operation p50 values remained within 10%
of the contemporaneous main run.

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
the start of this report. The raw, ignored artifact is
`benchmark-results/fallback-ownership-pr-head-loopback-observation-20260724.json`.
It reports `ok=true`, zero failures, and `git_worktree_clean=true`.

The preceding clean confirmation at `6f06fc50e589fee11e477d536e994a4d07416bce` measured 13.05/41.92
ms for SDK-default p50/p95 and 32.28/44.97 ms for auto-signal. At PR head, the p50 changes were
-4.0% and +3.3%, respectively, while both p95 values improved. This supports the intended
no-regression conclusion; the review-only changes between those revisions affect warm-pool and
readiness-failure ownership rather than the daemon input/capture hot path. Both runs completed
30/30.

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

This is not a universal provider ranking. E2B is substantially faster for cold product startup and
command echo; Modal's advantage is concentrated in screenshot and daemon-native input/batching.
The provider samples are complete but small (`n=3`) and are descriptive rather than paired causal
effects.

The secret-safe candidate artifact is
`benchmark-data/provider-compare-fallback-ownership-2026-07-24-candidate.json`. The credential- and
endpoint-bearing raw report remains ignored at
`benchmark-results/candidates/provider-compare-fallback-ownership-20260724.json` and is bound by
SHA-256 in the sanitized artifact. That provider comparison was recorded at `6d3d22d`; subsequent
commits affect orchestration failure ownership, typed readiness errors, tests, and documentation,
not daemon input, capture, or vendor-adapter hot paths.

## Observed harness limitations

- A long combined Connect plus target-loopback job lost its target before the final diagnostic
  `Sandbox.exec`. Separate path runs completed; provider and optimized tables therefore keep the
  paths independent.
- The Connect observation stream twice timed out during setup for the auto-signal case. Other
  Connect observation cases completed 30/30, and the same auto-signal behavior completed 30/30 over
  target-loopback. The failed attempts are retained and not averaged into the successful data.
- Historical and contemporaneous hosts showed materially different stable screenshot floors.
  Performance conclusions use the immediate main-vs-feature ablation, not the more favorable older
  baseline.
