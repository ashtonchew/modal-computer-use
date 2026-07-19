# Modal V2 Candidate Benchmark Methodology

This benchmark answers three separate questions without combining them into one provider ranking:

1. What latency is available through Modal's public Sandbox product path?
2. What changes when V1 and V2 target generation use the same encrypted-tunnel transport?
3. What is possible through an explicitly asymmetric V2 workspace-private direct path?

Modal V2 remains Beta. The benchmark never labels V2 the default or winner. Modal SDK 1.5.2 and
the current V2 documentation report that Connect Tokens are unsupported on V2, so Connect parity is
not claimed.

## Canonical Arms

| Arm | Target backend | Caller | Ingress | Classification |
| --- | --- | --- | --- | --- |
| `v1-connect-product` | V1 | Persistent same-region V2 runner | Sandbox Connect endpoint | Public product path |
| `v1-encrypted-tunnel` | V1 | Persistent same-region V2 runner | Encrypted tunnel plus application bearer | Transport-matched backend arm |
| `v2-encrypted-tunnel` | V2 | Persistent same-region V2 runner | Encrypted tunnel plus application bearer | Transport-matched backend arm |
| `v2-i6pn-direct-optimized` | V2 | Persistent same-region V2 runner | Workspace-private i6pn plus application bearer | Asymmetric optimized candidate |

`target-loopback` is not a canonical arm. It remains a same-container lower-bound diagnostic and
cannot appear in product, backend-causal, or promoted candidate comparisons.

The broker/control caller may allocate targets and the runner, select placement, supply ephemeral
authentication, and clean resources. It is never on the measured action or frame data path. The
runner talks directly to the target through the arm's declared endpoint.

## Matched Controls

Every target arm requests:

- one exact named Chromium image revision;
- 4 CPU cores and 8192 MiB memory;
- the cloud request selected by the placement-capability matrix and broad region `us-west`;
- Chromium prewarm enabled;
- a 1024x768, 96 DPI desktop;
- the same daemon, browser, readiness, click, frame, timeout, retry, and cleanup semantics;
- no warm pool, filesystem snapshot, memory snapshot, provider pool, or replacement sample.

The persistent runner is V2, i6pn-enabled, uses the same named image revision, requests 1 CPU and
1024 MiB, and is reused across the randomized phase. Reusing it keeps runner allocation outside
target lifecycle and warm action boundaries. Its actual cloud and region must be observed.

Modal's documented `us-west` selector is a broad Modal region label, not a concrete AWS region.
Before preregistration, an unmeasured capability matrix evaluates `auto`, `aws`, `gcp`, and `oci`
requests for four roles: V1 target, V2 tunnel target, V2 i6pn target, and V2 i6pn runner. Each probe
uses the exact named image and role resources, records the runtime `MODAL_CLOUD_PROVIDER` and
`MODAL_REGION`, verifies i6pn where required, and performs run-scoped cleanup. The first request
whose four roles share one exact observed cloud and concrete region is eligible; an explicit cloud
request must also be honored.

The matrix performs no latency measurement. Its path, SHA-256, run ID, source commit, image
identity, resources, selected request, and observed placement are bound into preregistration.
Preregistration fails when the matrix is descriptive-only or differs from the harness configuration.
The SHA-256 covers the exact indented JSON bytes written at the recorded path. The pilot
independently requires every runner and target to equal the matrix's concrete observed cloud and
region, even when the cloud request was unconstrained, and suppresses all causal ratios if placement
drifts. This makes the comparison stratum observed and auditable instead of inferred from requested
placement. Azure is not a matrix request because Modal's documented runtime providers are AWS, GCP,
and OCI and the current V1 API rejected the earlier observed V2 Azure placement.

## Measured Boundaries

Each primary sample is one independent target lifecycle:

- `allocation_ms`: immediately before `Sandbox.create` or `Sandbox._experimental_create` until the
  SDK returns the registered Sandbox handle;
- `daemon_ready_ms`: target request start until the persistent runner verifies authenticated
  `/readyz` through the arm's declared transport;
- `browser_ready_ms`: target request start until the runner verifies the configured prewarmed
  Chromium window;
- `first_valid_frame_ms`: target request start until a protected PNG decodes as 1024x768;
- `warm_action_to_frame_ms`: immediately before a correlated click on a persistent observation
  session until a matching changed causal frame arrives in one binary envelope.

The runner also verifies `/healthz`, `/readyz`, `/v1/version`, `/v1/capabilities`, browser identity,
browser prewarm, frame geometry and format, action success, matching causal identifiers, visual
change, no change timeout, and binary-envelope delivery. A failed verification is a failed sample,
not a latency value.

The V1/V2 encrypted-tunnel pair is the only neutral backend comparison. Backend-causal ratios are
suppressed unless image, resources, requested and actual target placement, requested and actual
runner placement, readiness, action, observation, transport, retry, and cleanup controls match.
Connect results remain product-path evidence. i6pn results remain asymmetric candidate evidence even
when faster.

## Sampling And Gates

After an eligible placement matrix is bound, preregistration freezes both schedules before measured
credentialed execution:

- pilot: 5 lifecycle samples per arm, randomized in four-arm blocks with seed `20260719`;
- full: 30 lifecycle samples per eligible arm, randomized in four-arm blocks with seed `20260720`;
- harness retries: 0;
- replacement samples: disabled;
- failures and timeouts: retained at their original schedule positions.

An arm advances only when its pilot has exactly five attempts, every attempt is valid and verified,
all requested controls match, actual target and runner cloud/region are observed, no retry occurred,
target plus phase-scoped runner cleanup succeeded, and a final run-scoped sweep enumerates both V1
and V2 Sandboxes with zero remaining resources. The encrypted-tunnel backend comparison adds an
exact cross-arm actual placement and control-signature gate.

The full phase is not a repair pass. It cannot replace pilot samples or silently drop failed arms.
Only a result with complete 5-per-arm pilot evidence, 30-per-arm full evidence, passing verification
and cleanup, and complete throughput evidence can be sanitized into `benchmark-data/`.

## Distributions And Throughput

Every metric reports sorted raw samples, p50, p95, an ECDF-ready rank/probability table, and a
deterministic percentile-bootstrap 95% confidence interval. The bootstrap uses 2,000 resamples and a
frozen seed. Pilot and full distributions remain separate.

After full lifecycle gates pass, a separately labeled minimal-container allocation diagnostic runs
V1 and V2 at concurrency 1, 5, and 20. It caches one App and named Image handle and uses Modal's async
create API. Concurrency 50 is disabled unless both the explicit flag and preregistered cost ceiling
allow it. Every allocation is cleaned and records actual placement. Throughput rows never substitute
for the independent lifecycle distributions. The current harness preregisters a `$20.00`
worst-case public-rate ceiling for the required 1, 5, and 20 batches, tags every allocation with an
exact run ID, and requires a final run-scoped cleanup sweep before promotion.

## Provenance And Artifacts

Raw credentialed evidence remains ignored:

```text
benchmark-results/modal-v2-candidate-2026-07-19/preregistration.json
benchmark-results/modal-v2-candidate-2026-07-19/diagnostics/placement-capability.json
benchmark-results/modal-v2-candidate-2026-07-19/candidates/pilot.json
benchmark-results/modal-v2-candidate-2026-07-19/candidates/full.json
benchmark-results/modal-v2-candidate-2026-07-19/rejected/pilot.json
benchmark-results/modal-v2-candidate-2026-07-19/rejected/full.json
benchmark-results/modal-v2-candidate-2026-07-19/checkpoints/pilot.json
benchmark-results/modal-v2-candidate-2026-07-19/checkpoints/full.json
```

The raw and promoted artifacts record source commit, preregistration digest, the complete sanitized
placement-capability evidence and byte digest, Modal and package versions, image identity, requested
and actual placement, transports, resources, prewarm, versions, verification, failures, retries,
cleanup, estimated partial cost, explicit unreconciled Modal billed-cost status, and every
asymmetric optimization. A failed execution is moved from the requested `candidates/`
path into `rejected/` before it is written. The artifact never
records endpoint URLs, bearer values, private i6pn addresses, screenshots, clipboard or typed text,
stdout, or stderr.

Only a complete gate-passing result can produce:

```text
benchmark-data/modal-v2-candidate-results-2026-07-19.json
```

Candidate and rejected evidence stays under ignored `benchmark-results/` with an exact reason.
After every retained lifecycle, the runner atomically replaces a provenance-bound checkpoint. It
writes a final checkpoint after runner cleanup. `SIGINT`/`KeyboardInterrupt` seals the retained rows
as a rejected artifact and exits `130`; it never resumes or substitutes those rows into a later run.

## Reproduction

Run from the exact clean committed harness and published image revision:

```bash
SOURCE_SHA="$(git rev-parse HEAD)"

uv run python scripts/publish_modal_images.py --revision "$SOURCE_SHA"

uv run python scripts/probe_modal_v2_candidate_placement.py \
  --source-sha "$SOURCE_SHA" --image-revision "$SOURCE_SHA"

uv run python scripts/run_modal_v2_candidate_benchmark.py preregister \
  --source-sha "$SOURCE_SHA" --image-revision "$SOURCE_SHA" \
  --placement-capability \
    benchmark-results/modal-v2-candidate-2026-07-19/diagnostics/placement-capability.json

uv run python scripts/run_modal_v2_candidate_benchmark.py pilot \
  --source-sha "$SOURCE_SHA"

uv run python scripts/run_modal_v2_candidate_benchmark.py full \
  --source-sha "$SOURCE_SHA"

uv run python scripts/sanitize_modal_v2_candidate_benchmark.py \
  benchmark-results/modal-v2-candidate-2026-07-19/candidates/full.json \
  benchmark-data/modal-v2-candidate-results-2026-07-19.json \
  --preregistration benchmark-results/modal-v2-candidate-2026-07-19/preregistration.json \
  --raw-artifact-path benchmark-results/modal-v2-candidate-2026-07-19/candidates/full.json
```

Add `--check` to the final command to verify deterministic regeneration.

Official capability references:

- [V2 Sandboxes](https://modal.com/docs/guide/sandbox-v2)
- [Cluster networking](https://modal.com/docs/guide/private-networking)
- [Sandbox networking and security](https://modal.com/docs/guide/sandbox-networking)
- [Region selection](https://modal.com/docs/guide/region-selection)
- [Environment variables](https://modal.com/docs/guide/environment_variables)
- [Sandbox resources and pricing](https://modal.com/docs/guide/sandbox-resources)
