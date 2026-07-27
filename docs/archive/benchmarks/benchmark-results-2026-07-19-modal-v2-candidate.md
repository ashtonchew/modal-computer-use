# Modal V2 Candidate Result, 2026-07-19

> **Archive category:** Diagnostic
> **Date or revision:** 2026-07-19; source `ccf756154c8d7aa157ca6844b80d3042ea718df4`
> **Question:** Could V1 and V2 be placed in one observed provider-region stratum for a causal
> latency comparison?
> **Disposition:** No tested placement satisfied the prerequisite. The capability matrix remains
> descriptive; performance sampling did not begin.

## Status

**Descriptive placement-capability result; performance comparison ineligible.** The corrected clean
harness and named image revision were `ccf756154c8d7aa157ca6844b80d3042ea718df4`.
An unmeasured matrix evaluated `auto`, `aws`, `gcp`, and `oci` requests across a V1 target, V2
encrypted-tunnel target, V2 i6pn target, and separate V2 i6pn runner. It found no request where all
four roles shared one exact observed cloud and concrete region.

The preregistration gate therefore stopped the workflow before all latency measurement. No pilot,
full lifecycle, throughput, ratio, winner, default, or Connect-parity result exists.

## Placement Matrix

| Cloud request | V1 target | All three V2 roles | Exact common placement | Decision |
| --- | --- | --- | --- | --- |
| `auto` | OCI `us-phoenix-1` | Azure `westus3` | No | Descriptive only |
| `aws` | AWS `us-west-2` | Azure `westus3` | No; V2 did not honor request | Descriptive only |
| `gcp` | GCP `us-west3` | Azure `westus3` | No; V2 did not honor request | Descriptive only |
| `oci` | OCI `us-phoenix-1` | Azure `westus3` | No; V2 did not honor request | Descriptive only |

All 16 probes created their Sandbox, observed runtime placement, and passed run-scoped cleanup.
Both i6pn roles also verified an i6pn address. The capability matrix explicitly records
`measurement_performed: false`.

## Performance Table

| Arm | Pilot | Full | Allocation | First valid frame | Warm action | Eligibility |
| --- | --- | --- | --- | --- | --- | --- |
| `v1-connect-product` | Not run | Not run | Not measured | Not measured | Not measured | Ineligible |
| `v1-encrypted-tunnel` | Not run | Not run | Not measured | Not measured | Not measured | Ineligible |
| `v2-encrypted-tunnel` | Not run | Not run | Not measured | Not measured | Not measured | Ineligible |
| `v2-i6pn-direct-optimized` | Not run | Not run | Not measured | Not measured | Not measured | Ineligible |

There is consequently no defensible V1-versus-V2 speedup number. The earlier approximately 15 ms
V2 screenshot observation remains lower-bound evidence from a different, non-comparable path; this
matrix neither validates nor contradicts that latency.

## Conclusion

V2 is a promising optimized candidate, but this account's current V2 placement behavior prevents a
causal V1/V2 backend comparison. The V2 tunnel, V2 i6pn target, and V2 runner consistently colocated
on Azure `westus3`, which is useful for a V2-only optimized-path experiment. It cannot be compared
as a backend-generation ratio against V1 until Modal exposes a common placement or V1 supports that
same Azure stratum.

The correct next options are:

1. Ask Modal to enable or document a common V1/V2 provider-region placement for this workspace.
2. Report a separately labeled V2-only optimized distribution on Azure `westus3`, with no V1 ratio.
3. Rerun this capability gate after a Modal SDK or backend placement change before attempting the
   four-arm benchmark.

## Provenance

- Source and image revision: `ccf756154c8d7aa157ca6844b80d3042ea718df4`
- Capability artifact SHA-256: `d5ee2b31d70e924bdd9b24c55c4361e0adee1234c18246b245d0568b8aa89244`
- Tracked artifact: `benchmark-data/modal-v2-placement-capability-2026-07-19.json`
- Raw artifact: `benchmark-results/modal-v2-candidate-2026-07-19/diagnostics/placement-capability.json`
- Cleanup: 16 of 16 probes reported successful run-scoped cleanup
- Modal SDK: `1.5.2`
- Billed cost: not reconciled

See [the methodology](modal-v2-candidate-benchmark.md) for boundaries, gates, classifications, and
reproduction commands.
