# Modal Optimized-Frontier Result, 2026-07-19

## Status

**Rejected at the pilot gate; full lifecycle and throughput phases were not run.** No optimized-
frontier path ratio is eligible.

The predeclared comparison used V1 encrypted tunnel on OCI `us-phoenix-1` and V2 i6pn on Azure
`westus3`. This was a descriptive best-system design and never a backend-causal experiment. The
placement foundation remains the unchanged `descriptive-placement-capability-only` artifact with
`measurement_performed: false`.

## Exact rejection

An initial `aedf44f356a7d42e430999be8d695bb63b79a9e9` pilot command detached from its tool session
before retaining a sample. It remained active after its raw directory was preserved. A replacement
signal-safe harness was frozen at `db25d89291a298351ce69fe78a7a48c191665b24`, but the stale process
and the new process then overlapped and wrote into the same default raw evidence root. Those
concurrent files are explicitly classified as contaminated and cannot be combined or promoted.

The signal-safe run independently retained one V1 primary attempt before it was interrupted to stop
the contamination. Its generation-matched runner verified OCI `us-phoenix-1`, but V1 target
creation timed out before returning a handle. The attempt therefore had no latency samples, 0%
route/frame/action verification, and incomplete direct target cleanup fields. Its exact tagged
cleanup sweep passed with zero remaining resources.

Either condition is sufficient to reject the pilot:

- only 1 of 5 required V1 primary attempts was retained, and it failed with `TimeoutError`;
- V2 primary retained 0 of 5 required attempts;
- two processes contaminated the shared raw checkpoint/output root.

No samples were replaced. The full phase was not started, and throughput was not attempted.

## Pilot score table

Only the `db25d89291a298351ce69fe78a7a48c191665b24` signal-safe evidence is shown as the candidate
pilot. The stale `aedf44f...` rows are retained separately as contaminated diagnostics and are not
scores.

| Arm | Role | Required | Retained | Valid | Verification rate | Eligible |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `v1-encrypted-tunnel-optimized` | Primary | 5 | 1 | 0 | 0% | No: target create timeout |
| `v2-i6pn-direct-optimized` | Primary | 5 | 0 | 0 | Not measured | No: schedule not reached |
| `v1-connect-product` | Diagnostic | 5 | 0 | 0 | Not measured | Not evaluated |
| `v2-encrypted-tunnel-diagnostic` | Diagnostic | 5 | 0 | 0 | Not measured | Not evaluated |

The retained V1 runner placement verification rate was 100% (1/1). Target placement was unobserved
because target creation never returned.

## Latency and percentile tables

The only retained candidate attempt was invalid, so no latency value can enter a distribution.

| Primary arm | Allocation | Daemon ready | Browser ready | First valid frame | Warm action to frame |
| --- | --- | --- | --- | --- | --- |
| V1 optimized | Not reportable | Not reportable | Not reportable | Not reportable | Not reportable |
| V2 optimized | Not measured | Not measured | Not measured | Not measured | Not measured |

| Primary arm | p50 | p95 | ECDF | Bootstrap 95% CI |
| --- | --- | --- | --- | --- |
| V1 optimized | Not reportable | Not reportable | Empty | Not reportable |
| V2 optimized | Not measured | Not measured | Empty | Not measured |

## Full, throughput, and ratio table

| Evidence | V1 optimized | V2 optimized | Result |
| --- | --- | --- | --- |
| Full independent lifecycles | 0/30 | 0/30 | Not run: pilot gate failed |
| Throughput concurrency 1 | Not run | Not run | Lifecycle eligibility absent |
| Throughput concurrency 5 | Not run | Not run | Lifecycle eligibility absent |
| Throughput concurrency 20 | Not run | Not run | Lifecycle eligibility absent |
| Optimized-frontier path ratio | — | — | Not eligible |
| V2 backend causal speedup | — | — | Prohibited by design |

## Cleanup

All known run IDs were explicitly enumerated through both `Sandbox.list()` and
`Sandbox._experimental_list()` after interruption:

| Run ID | Context | Matched before explicit cleanup | Terminated | Failures | Remaining V1 | Remaining V2 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `run_129080cb614240a9` | Initial detached run | 2 in first recovery sweep | 2 | 0 | 0 | 0 |
| `run_b2c2b87d306e4958` | Signal-safe run | 0 after interrupt cleanup | 0 | 0 | 0 | 0 |

The stale process was interrupted and verified absent before the final two-run cleanup audit. That
audit again observed zero tagged survivors for both run IDs in both listing APIs.

## Cost

Modal billed cost was not synchronously attributable and remains **not reconciled**. The strongest
available retained resource-time proxy is:

| Evidence | Requested-resource proxy |
| --- | ---: |
| Signal-safe V1 primary failure | $0.0826900817 |
| Contaminated stale diagnostics (not score evidence) | $0.1991340421 |
| Known retained proxy total | $0.2818241239 |

This is not a bill. It covers requested target and runner CPU/memory resource time with the
documented 1.75x narrow-region multiplier. It excludes unretained work, image builds, actual usage
above requests, control-plane charges, and billing adjustments.

## Provenance

- Foundation commit: `33f056505940123877c4116d40df2c0d95deb3a4`
- Initial harness commit: `aedf44f356a7d42e430999be8d695bb63b79a9e9`
- Signal-safe harness commit and exact image revision:
  `db25d89291a298351ce69fe78a7a48c191665b24`
- Placement artifact: `benchmark-data/modal-v2-placement-capability-2026-07-19.json`
- Placement artifact SHA-256:
  `d5ee2b31d70e924bdd9b24c55c4361e0adee1234c18246b245d0568b8aa89244`
- Initial preregistration SHA-256:
  `0b6614b23a6109c600779b27438b842c7b18b43825637bcbfcf4ad445f6e6c5a`
- Signal-safe preregistration SHA-256:
  `ef63a5ecb301441d8be31a22c58df30c174153ef6ded28ac96f4384df40ba0ad`
- Initial zero-retained checkpoint SHA-256:
  `fb0c0cbb74bb8618bb05359e99ac213c7cbbc759e91dc9f2cef3b640d4deda1e`
- Initial recovery cleanup SHA-256:
  `2d3f2296456bb31cc9090e673c20749889824bdbfe8d29655c96bee57ea1b81a`
- Contaminated stale checkpoint SHA-256:
  `79bd105255370c6f917f4d9407b1628e2502b3f7d2ba8884fa8ba1c55a46cf3d`
- Signal-safe rejected result SHA-256:
  `84212e4fcacf3c84267fa1ec82bd7b71cd72005c8a1c4d5da6dbb52f3c618c87`
- Promoted artifact: none; promotion gates correctly refused partial and contaminated evidence

Raw evidence remains ignored under:

- `benchmark-results/modal-optimized-frontier-2026-07-19-aborted-aedf44f/`
- `benchmark-results/modal-optimized-frontier-2026-07-19-contaminated-concurrent-runs/`

See [the methodology](modal-optimized-frontier-benchmark.md) for the predeclared controls,
asymmetries, lifecycle boundary, and gates.
