# Modal Optimized-Frontier Result, 2026-07-19

## Status

**Rejected at the clean pilot gate; full lifecycle and throughput phases were not run.** No
optimized-frontier path ratio is eligible.

The predeclared comparison used V1 encrypted tunnel on OCI `us-phoenix-1` and V2 i6pn on Azure
`westus3`. This is a descriptive best-system design, not a backend-generation causal experiment.
The unchanged placement foundation remains classified `descriptive-placement-capability-only` with
`measurement_performed: false`.

## Exact rejection

The final clean pilot used source and image revision
`f8d24e63e9cadaf3224da464dd517f98ca020258`. It completed all 20 preregistered independent
lifecycles with zero retries and no replacement samples. Both primary arms failed the requirement
for exactly five valid, fully verified samples:

- V1 encrypted-tunnel optimized: 2/5 valid; three samples timed out at
  `observation_stream` while awaiting the initial stream frame.
- V2 i6pn optimized: 3/5 valid; one sample failed semantic validation at
  `warmup_action_frame` and one at `measured_action_frame`.

The V2 failures were safely retained as `ValueError`. The exact semantic invariant was not encoded
by the frozen measurement commit. Three separately labeled post-pilot V2 diagnostics passed, so no
narrower cause is asserted. The runner now classifies future fixed-message validation failures
without exposing credentialed output.

Earlier rejected diagnostics identified and corrected deterministic harness defects before this
clean pilot: overlapping raw writers, unsealed process termination, a transport-insensitive daemon
bind address, V2 private readiness ownership, and discarded observation-delta baselines. Those
earlier rows remain ignored and are not mixed into the final pilot.

## Pilot score table

| Arm | Role | Attempted | Valid | Verification rate | Cleanup rate | Gate result |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `v1-encrypted-tunnel-optimized` | Primary | 5 | 2 | 40% | 100% | Rejected: 3 initial-frame timeouts |
| `v2-i6pn-direct-optimized` | Primary | 5 | 3 | 60% | 100% | Rejected: 2 semantic frame failures |
| `v1-connect-product` | Diagnostic | 5 | 4 | 80% | 100% | Diagnostic only; 1 initial-frame timeout |
| `v2-encrypted-tunnel-diagnostic` | Diagnostic | 5 | 4 | 80% | 100% | Diagnostic only; 1 initial-frame timeout |

Every valid row passed all 11 verification fields: health, readiness, version, capabilities,
browser prewarm, first frame, action, causal frame, changed frame, binary envelope, and runner
placement. Every attempted row observed its declared target and runner placement.

## Primary percentile table

Percentiles use only valid samples. The small retained counts make these descriptive diagnostics,
not an eligible comparison.

| Primary arm | Metric | n | p50 ms | p95 ms | Bootstrap 95% CI for p50 ms | Bootstrap 95% CI for p95 ms |
| --- | --- | ---: | ---: | ---: | --- | --- |
| V1 optimized | Allocation | 2 | 3540.888 | 4139.490 | 2875.774–4206.001 | 2875.774–4206.001 |
| V1 optimized | Daemon ready | 2 | 9298.022 | 9490.865 | 9083.753–9512.291 | 9083.753–9512.291 |
| V1 optimized | Browser ready | 2 | 9419.236 | 9614.520 | 9202.254–9636.218 | 9202.254–9636.218 |
| V1 optimized | First valid frame | 2 | 9510.768 | 9708.381 | 9291.197–9730.339 | 9291.197–9730.339 |
| V1 optimized | Warm action to frame | 2 | 14.470 | 20.553 | 7.712–21.228 | 7.712–21.228 |
| V2 optimized | Allocation | 3 | 2280.578 | 2891.225 | 2065.779–2959.075 | 2065.779–2959.075 |
| V2 optimized | Daemon ready | 3 | 9924.379 | 17894.419 | 7846.336–18779.979 | 7846.336–18779.979 |
| V2 optimized | Browser ready | 3 | 10134.300 | 18339.226 | 8092.376–19250.885 | 8092.376–19250.885 |
| V2 optimized | First valid frame | 3 | 10316.448 | 18777.258 | 8267.649–19717.348 | 8267.649–19717.348 |
| V2 optimized | Warm action to frame | 3 | 49.748 | 112.055 | 47.723–118.978 | 47.723–118.978 |

The complete valid warm-action samples were V1 `{7.712, 21.228}` ms and V2
`{47.723, 49.748, 118.978}` ms. They are not divided into a ratio because the lifecycle gate
failed.

## Diagnostic percentile table

| Diagnostic arm | Metric | n | p50 ms | p95 ms |
| --- | --- | ---: | ---: | ---: |
| V1 Connect | Allocation | 4 | 3307.329 | 3503.450 |
| V1 Connect | Daemon ready | 4 | 8534.688 | 8689.607 |
| V1 Connect | Browser ready | 4 | 8665.464 | 8815.270 |
| V1 Connect | First valid frame | 4 | 8763.634 | 8911.593 |
| V1 Connect | Warm action to frame | 4 | 14.196 | 34.834 |
| V2 encrypted tunnel | Allocation | 4 | 2197.909 | 10142.557 |
| V2 encrypted tunnel | Daemon ready | 4 | 15513.700 | 16280.413 |
| V2 encrypted tunnel | Browser ready | 4 | 15766.733 | 16534.586 |
| V2 encrypted tunnel | First valid frame | 4 | 16001.706 | 17294.276 |
| V2 encrypted tunnel | Warm action to frame | 4 | 13.055 | 25.476 |

The complete valid warm-action samples were V1 Connect
`{8.503, 8.531, 19.860, 37.476}` ms and V2 encrypted tunnel
`{9.947, 11.607, 14.504, 27.412}` ms. Diagnostic paths cannot substitute for primary arms.

## Full, throughput, and ratio table

| Evidence | V1 optimized | V2 optimized | Result |
| --- | --- | --- | --- |
| Pilot independent lifecycles | 2/5 valid | 3/5 valid | Rejected |
| Full independent lifecycles | 0/30 | 0/30 | Not run: pilot gate failed |
| Throughput concurrency 1 | Not run | Not run | Lifecycle eligibility absent |
| Throughput concurrency 5 | Not run | Not run | Lifecycle eligibility absent |
| Throughput concurrency 20 | Not run | Not run | Lifecycle eligibility absent |
| Optimized-frontier path ratio | — | — | Not eligible |
| V2 backend causal speedup | — | — | Prohibited by design |

## Cleanup

Every one of the 20 final pilot lifecycles terminated its target and runner, detached its target,
and completed its run-tagged sweep. The final phase sweep enumerated both `Sandbox.list()` and
`Sandbox._experimental_list()` and observed zero resources before and after cleanup.

| Run ID | Source | Retained attempts | Phase cleanup | Remaining V1 | Remaining V2 |
| --- | --- | ---: | --- | ---: | ---: |
| `run_1a5a60b346c24cb6` | Final clean pilot | 20 | Passed | 0 | 0 |
| `run_25620820b4624db9` | Rejected delta-baseline diagnostic | 2 | Passed | 0 | 0 |
| `run_e0d1469fef294bf6` | Rejected readiness diagnostic | 5 | Passed | 0 | 0 |
| `run_b2c2b87d306e4958` | Earlier signal-safe diagnostic | 1 | Passed | 0 | 0 |
| `run_129080cb614240a9` | Earlier detached-run recovery | Contaminated | Passed | 0 | 0 |

All ad hoc path diagnostics also ended with both listing APIs at zero.

## Cost

Modal billed cost was not synchronously attributable and remains **not reconciled**. The strongest
available final-pilot proxy is **$0.6031404664**. It covers requested target and runner CPU/memory
resource-seconds with the documented 1.75x narrow-region multiplier; it excludes actual usage above
requests, control-plane charges, and billing adjustments.

The three post-pilot V2 semantic diagnostics added a separately labeled proxy of $0.0393841309.
Earlier rejected and contaminated diagnostics are not added to the final-pilot cost.

## Provenance

- Foundation commit: `33f056505940123877c4116d40df2c0d95deb3a4`
- Final clean pilot source and exact image revision:
  `f8d24e63e9cadaf3224da464dd517f98ca020258`
- Placement artifact: `benchmark-data/modal-v2-placement-capability-2026-07-19.json`
- Placement artifact SHA-256:
  `d5ee2b31d70e924bdd9b24c55c4361e0adee1234c18246b245d0568b8aa89244`
- Final preregistration SHA-256:
  `6a37ac8ae351418ca815e4593d23892c26fdb9874225e3dc38a2d12082ddb4da`
- Final pilot checkpoint SHA-256:
  `537c886b83152352011b113cd2c9fdaa2d6279dd587f18e9a3fcb740d0a04032`
- Final rejected raw result SHA-256:
  `6e471e773f190185cb060a5593115121c3a33d1b14ac1ede7f28af4423fcda02`
- Final raw result:
  `benchmark-results/modal-optimized-frontier-2026-07-19/rejected/pilot.json`
- Promoted artifact: none; promotion gates correctly reject incomplete primary lifecycles

Raw credentialed evidence remains ignored under `benchmark-results/`, including the final run and
the separately named earlier rejected/contaminated directories. See
[the methodology](modal-optimized-frontier-benchmark.md) for the predeclared controls,
asymmetries, lifecycle boundary, and gates.
