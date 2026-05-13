# Provider Benchmark Results, 2026-05-13

Five live runs were executed from branch `feat/provider-benchamark` in worktree
`/Users/ashtonchew/projects/modal-computer-use/.worktrees/provider-benchamark`.

Each run used one warmup iteration and one measured iteration per provider. Raw result JSON files are
stored locally under `benchmark-results/` and are gitignored.

## Summary

| Provider | Runs | Cold ready mean | Screenshot mean | Move/click mean | Sequence mean | Type 100 mean | Type 1000 mean | Command mean | Readback | Cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| Daytona | 5/5 ok | 8571ms | 139ms | 242ms | 977ms | 583ms | 5258ms | 55ms | cursor ok; type ok | estimated $0.000769 avg |
| E2B | 5/5 ok | 1902ms | 399ms | 332ms | 1198ms | 4275ms | 42695ms | 97ms | cursor ok; type ok | estimated $0.003480 avg |
| Modal daemon | 5/5 ok | 8597ms | 350ms | 318ms | 1105ms | 838ms | 6747ms | 102ms | cursor ok; type ok | billing $0.006650 avg |

## Timing Ranges

| Provider | Cold ready | Screenshot | Move/click | Sequence | Type 100 | Type 1000 | Command |
|---|---:|---:|---:|---:|---:|---:|---:|
| Daytona | 8432-8629ms | 93-184ms | 215-270ms | 860-1088ms | 554-639ms | 5232-5289ms | 33-68ms |
| E2B | 1563-2338ms | 293-656ms | 288-448ms | 1165-1251ms | 4243-4301ms | 42382-42978ms | 87-110ms |
| Modal daemon | 7341-10531ms | 219-537ms | 223-384ms | 829-1320ms | 722-1153ms | 6447-7350ms | 60-143ms |

## Per-Run Costs

| Run file | Modal billing | Daytona estimate | E2B estimate |
|---|---:|---:|---:|
| `provider-compare-live-full-20260513T175503Z.json` | $0.004445 | $0.000771 | $0.003480 |
| `provider-compare-live-full-20260513T175811Z.json` | $0.002974 | $0.000766 | $0.003655 |
| `provider-compare-live-full-20260513T180127Z.json` | $0.002810 | $0.000776 | $0.003424 |
| `provider-compare-live-full-20260513T180438Z.json` | $0.011949 | $0.000770 | $0.003458 |
| `provider-compare-live-full-20260513T180738Z.json` | $0.011072 | $0.000761 | $0.003382 |

Modal billing values are reconciled from `modal.billing.workspace_billing_report` with benchmark App
tags. They are report-window costs, not public-rate estimates.

## Readback

All five runs passed readback for all three providers:

- `cursor_position: ok`
- `type_text: ok`

## Interpretation

- E2B has the fastest cold create-to-ready time in these runs.
- Daytona has the fastest screenshot, move/click, command, and default 1000-character typing among
  provider-native paths.
- Modal is competitive on warm primitives while keeping the project-owned daemon as the source of
  truth for validation, redaction, readback, and behavior.
- E2B default typing is slow because its SDK default path uses delayed GUI key events. This is a real
  provider-default result; tuned E2B typing should be measured as a separate metric if needed.
