# Modal V2 Candidate Result, 2026-07-19

## Status

**Rejected before measurement.** The clean harness and named image revision were
`2749f72c9201a8a34bbce0bcf91f504069a3401a`. The final preregistered pilot requested the common
placement `aws/us-west`; its persistent V2 runner reported `CLOUD_PROVIDER_AZURE/westus3`.
The placement preflight stopped before the first target, retained zero trials, emitted no ratios,
and completed its run-ID-scoped cleanup with zero remaining Sandboxes.

Exact result reason: `pilot placement preflight failed before measurement: requested runner cloud
aws; observed CLOUD_PROVIDER_AZURE`.

An additional capability probe found that V2 accepts `azure/us-west`, but V1 rejects Azure as an
unknown cloud provider. A common explicit requested and actual provider therefore was not
established for the required V1/V2 arms. No V2 default, winner, Connect-parity, backend-causal, or
asymmetric performance claim is made.

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

| Arm | Pilot | Full | Allocation p50/p95 | Browser-ready p50/p95 | Warm action p50/p95 | Eligibility |
| --- | --- | --- | --- | --- | --- | --- |
| `v1-connect-product` | 0, preflight stop | Not run | Not measured | Not measured | Not measured | Rejected |
| `v1-encrypted-tunnel` | 0, preflight stop | Not run | Not measured | Not measured | Not measured | Rejected |
| `v2-encrypted-tunnel` | 0, preflight stop | Not run | Not measured | Not measured | Not measured | Rejected |
| `v2-i6pn-direct-optimized` | 0, preflight stop | Not run | Not measured | Not measured | Not measured | Rejected |

Full lifecycle and throughput phases were ineligible and were not run. No sanitized artifact was
promoted to `benchmark-data/`.

## Provenance

- Preregistration SHA-256: `cac19aa0d594ea47a91d979fa00c8b67445b5b25d83beaf7861e7d9c2238c765`
- Rejected result SHA-256: `fefe0742228e1c316d1d49aa1e45d1763c69bf57f56fbf9fb09f606312fc0216`
- Final checkpoint SHA-256: `145e02169bb46fbcf6de39259a0c19189f74edeb10217b7858db5042bbbd1b4f`
- Cloud compatibility diagnostic SHA-256:
  `4f5ecdf0919f159fe570196460f3dd1c1aa55288169221c573daa5433139ae52`
- Raw paths: `benchmark-results/modal-v2-candidate-2026-07-19/preregistration.json`,
  `benchmark-results/modal-v2-candidate-2026-07-19/rejected/pilot.json`, and
  `benchmark-results/modal-v2-candidate-2026-07-19/checkpoints/pilot.json`
- Cleanup evidence: zero matched leftovers, zero termination failures, zero remaining Sandboxes;
  the benchmark App reported zero tasks after verification.

## Cost And Limitations

The preregistered public-rate ceiling was `$10.00`. No target was created, so the artifact's partial
target CPU/memory estimate is `$0.00`. Persistent-runner compute, capability probes, control-plane
work, and billing adjustments are not included. Modal billed cost remains unreconciled.

V2 is Beta, V2 Connect Tokens are unsupported in Modal SDK 1.5.2, i6pn is workspace-private and
region-scoped, and the observed V2 provider did not honor the explicit common AWS request. This is a
placement-compatibility rejection, not a performance result.
