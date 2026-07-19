# Modal V2 Candidate Result, 2026-07-19

## Status

The four-arm harness and preregistration protocol are implemented. Live evidence must be generated
from the clean committed harness and exact published named image before any score is added here.

No V2 default, winner, Connect-parity, or backend-causal performance claim is currently made by
this document. If pilot placement, verification, cleanup, or comparison gates fail, the exact
candidate/rejected reason will be recorded here and the raw artifact will remain ignored under
`benchmark-results/modal-v2-candidate-2026-07-19/`.

## Classification

| Arm | Evidence class | Comparable use |
| --- | --- | --- |
| `v1-connect-product` | Public product path | User-available Connect endpoint distribution |
| `v1-encrypted-tunnel` | Matched backend arm | V1 side of transport-matched generation evidence |
| `v2-encrypted-tunnel` | Matched backend arm | V2 side of transport-matched generation evidence |
| `v2-i6pn-direct-optimized` | Asymmetric candidate | Workspace-private optimized distribution only |

See [the methodology](modal-v2-candidate-benchmark.md) for frozen controls, boundaries, schedules,
decision gates, statistics, provenance, and reproduction commands.

## Result Table

Live values are intentionally absent until credentialed execution completes.

| Arm | Pilot | Full | Allocation p50/p95 | Browser-ready p50/p95 | Warm action p50/p95 | Eligibility |
| --- | --- | --- | --- | --- | --- | --- |
| `v1-connect-product` | Pending | Pending | Pending | Pending | Pending | Pending |
| `v1-encrypted-tunnel` | Pending | Pending | Pending | Pending | Pending | Pending |
| `v2-encrypted-tunnel` | Pending | Pending | Pending | Pending | Pending | Pending |
| `v2-i6pn-direct-optimized` | Pending | Pending | Pending | Pending | Pending | Pending |

## Cost And Limitations

The preregistered public-rate ceiling is `$10.00`. Reported cost remains partial until delayed Modal
billing can be reconciled; target CPU and memory are included while runner compute, control-plane
work, and billing adjustments are explicitly excluded from per-trial estimates.

V2 is Beta, V2 Connect Tokens are unsupported in Modal SDK 1.5.2, i6pn is workspace-private and
region-scoped, exact actual cloud/region may differ from requested broad placement, and the runner's
V2 backend is held constant rather than treated as a public product default.
