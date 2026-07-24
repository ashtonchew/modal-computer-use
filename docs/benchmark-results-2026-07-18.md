# Rejected Provider Benchmark Diagnostic, 2026-07-18

This run is rejected and is not a current provider reference. Review found that external-provider
create-to-first-screenshot samples included teardown latency, while Modal samples did not, and the
Daytona screenshot payload size represented base64 transport text rather than decoded PNG bytes.
The artifact remains tracked as provenance for the rejected result; do not use its ratios or payload
sizes for product claims.

See the [corrected retained reference](benchmark-results-2026-07-18-provider-default.md) for the successful
rerun from the committed harness. This rejected artifact remains only as historical provenance.

## Provenance

- Tracked sanitized report: `benchmark-data/provider-compare-2026-07-18.json`
- Raw local report: `benchmark-results/candidates/provider-compare-live-20260718-v3.json`
- Raw report SHA-256: `1cb34cfe01c5eea1b341e583fcbb9de8c8854019a577475b37ba01c7ac437fc0`
- Harness commit: `86c15252cc5be188f2b79fddad98a438a0331e85`
- Artifact status: `rejected`; the original harness emitted `ok=true`, but review invalidated the
  cross-provider lifecycle and screenshot-payload claims
- Sampling as emitted: one warmup and three measured iterations; external teardown was incorrectly
  included in lifecycle timing
- Desktop: `1024x768`; Modal used Chromium, the browser resource profile, HTTP/1.1, and the
  attested encrypted tunnel

The tracked report removes ephemeral ingress URLs, Modal run IDs, and Modal sandbox IDs. It keeps
all timing samples, summaries, provider metadata, case definitions, verification results, raw
artifact hash, harness commit, rejection reason, and legacy sanitizer provenance.

The command was:

```sh
uv run computer-use benchmark compare \
  --create-modal-sandbox \
  --provider modal-daemon \
  --provider daytona \
  --provider e2b \
  --modal-ingress attested-tunnel \
  --resource-profile browser \
  --browser chromium \
  --iterations 3 \
  --env-file /Users/ashtonchew/projects/modal-computer-use/.env \
  --output benchmark-results/candidates/provider-compare-live-20260718-v3.json \
  --json
```

## Rejected Observations

The following values are retained only to explain the rejected artifact. They must not be cited as a
provider ranking. Lifecycle rows are asymmetric because teardown was included only for external
providers; Daytona payload accounting also used transport text length rather than decoded bytes.

| Case | Modal p50 | Daytona p50 | E2B p50 | Modal vs Daytona | Modal vs E2B |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to first screenshot | 10980.9 ms | 10925.3 ms | 1445.9 ms | 0.99x | 0.13x |
| Provider-default full screenshot | 120.7 ms | 218.3 ms | 188.8 ms | **1.81x** | **1.56x** |
| Move and click | 71.9 ms | 352.6 ms | 245.5 ms | **4.90x** | **3.41x** |
| Four move/click pairs | 77.6 ms | 1445.6 ms | 1050.1 ms | **18.64x** | **13.54x** |
| Type 100 characters | 786.3 ms | 637.4 ms | 4208.9 ms | 0.81x | **5.35x** |
| Type 1000 characters | 6735.2 ms | 5355.3 ms | 42087.7 ms | 0.80x | **6.25x** |
| Command echo | 144.8 ms | 118.7 ms | 61.0 ms | 0.82x | 0.42x |

The emitted warm action data suggested that four move/click pairs cost only 5.6 ms more than one
move/click at p50 because the daemon accepts the sequence as one batch. The equivalent public SDK
sequence is 18.64x slower on Daytona and 13.54x slower on E2B.

The emitted fused click-and-screenshot path was 118.1 ms p50. It has no directly equivalent
fused case in this provider-default report, so it is recorded as a Modal capability rather than a
cross-provider win.

Typing and command observations remain historical diagnostics only; rerun the corrected harness
before making comparative claims.

## Screenshot Scope

`screenshot_full` is the canonical binary screenshot API path for the Modal SDK. It is not a
normalized-pixel visual benchmark. The reported Daytona payload size is invalid because it counted
base64 transport text instead of decoded PNG bytes. Even after that correction, provider-default
screenshots support API-latency claims only, not identical-pixel encoder or visual claims.

Within Modal, the binary path was 120.7 ms p50 while the structured JSON/base64 compatibility path
was 252.8 ms p50, making the binary path 2.09x faster. The daemon itself spent 22.7 ms p50 capturing
and encoding; the remaining typical latency was transport and client overhead.

Use a deterministic synthetic-canvas run for an identical-content visual comparison. The May 22
synthetic-canvas run remains a separate historical visual diagnostic and is not replaced by this
provider-default report.

## Startup Scope

The artifact contains three fresh lifecycle attempts per provider, but external samples included
cleanup latency and are therefore not comparable to Modal. The emitted lifecycle ranking is rejected.

That result is fair as a product-level "create to first successful screenshot" metric, but it is
not a normalized infrastructure boot comparison:

| Provider | Startup model | Snapshot/template | Readiness endpoint |
| --- | --- | --- | --- |
| Modal | Sandbox image plus daemon startup | No | Raw screenshot after daemon and attested ingress readiness |
| Daytona | Managed default snapshot | Yes | Computer Use start plus first full screenshot |
| E2B | Desktop template snapshot | Yes | First sandbox screenshot |

E2B's lead is real for the out-of-box product experience and is also explained by a materially
different startup model. A separate normalized startup experiment would require equivalent prepared
desktop images or snapshots on all providers.

## Result Status

| Result | Status | Reason |
| --- | --- | --- |
| 2026-07-18 v3 | Rejected | External lifecycle samples included teardown; Daytona bytes used base64 transport length |
| 2026-07-18 v2 | Superseded candidate | Correct behavior, but run from an uncommitted harness tree |
| 2026-07-18 v1 | Rejected | Modal lifecycle had one sample while Daytona and E2B had three |
| 2026-05-13 provider compare | Superseded | One iteration, old cold definition, and ambiguous screenshot path |
| 2026-05-17 provider compare | Historical diagnostic | Older ingress and screenshot semantics; useful only with its original scope |
| 2026-05-22 synthetic canvas | Historical visual diagnostic | Separate normalized visual workload; not comparable to provider-default screenshots |

Do not combine rows from different statuses or workloads into one provider ranking.
