# Modal Optimized-Frontier Benchmark Methodology

This benchmark compares each Modal Sandbox generation's fastest supported, predeclared production
path. It is a **descriptive best-system comparison**, not a backend-generation experiment.

The placement-capability foundation observed V1 on OCI `us-phoenix-1` and V2 on Azure `westus3`.
No tested cloud selector produced a common provider-region stratum. The benchmark therefore reports
an **optimized-frontier path ratio** and never a V2 backend causal speedup.

## Predeclared arms

| Arm | Role | Target | Runner | Placement | Ingress and measured data path |
| --- | --- | --- | --- | --- | --- |
| `v1-encrypted-tunnel-optimized` | Primary | V1 | V1 | OCI `us-phoenix-1` | Encrypted tunnel, direct runner-to-target |
| `v2-i6pn-direct-optimized` | Primary | V2 | V2 | Azure `westus3` | Workspace-private i6pn, direct runner-to-target |
| `v1-connect-product` | Diagnostic | V1 | V1 | OCI `us-phoenix-1` | Connect endpoint, direct runner-to-target |
| `v2-encrypted-tunnel-diagnostic` | Diagnostic | V2 | V2 | Azure `westus3` | Encrypted tunnel, direct runner-to-target |

The primary arms are frozen before measured execution. Diagnostic failures are retained and reported
but cannot be substituted into the primary comparison. Connect Tokens remain unsupported on V2 in
the current official V2 feature table, so there is no invented V2 Connect arm.

## Locality and ownership

The benchmark keeps Modal-specific creation, listing, and termination in `sandbox.py`. Feature
methodology, schema, statistics, classifications, and gates live in
`modal_optimized_frontier.py`; live orchestration lives in its execution companion. The CLI scripts
only coordinate clean-commit preregistration, checkpointing, execution, and sanitization.

The broker creates resources, supplies ephemeral authentication, and requests cleanup. It is never
on the measured action or frame path. Primitive action, observation, readiness, and frame behavior
remains daemon-owned.

## Matched controls and declared asymmetries

Every arm uses the same exact named Chromium image revision, 4 physical CPU cores, 8192 MiB memory,
Chromium prewarm, 1024x768 at the image's configured DPI, readiness routes, click coordinates,
changed causal PNG frame validation, binary-envelope observation, timeouts, zero retries, no sample
replacement, no pools, and no snapshots. Every runner uses the same image revision with 1 CPU and
1024 MiB memory.

The result records every unavoidable primary-arm asymmetry:

- backend generation;
- cloud provider;
- concrete provider region;
- runner generation;
- ingress;
- transport.

Those differences are the optimized systems being described. They are also why the ratio cannot be
interpreted as the isolated effect of the V2 backend.

## Independent lifecycle boundary

One sample creates a new generation-matched runner, observes and verifies its placement, creates a
new target, observes and verifies its placement, measures the target, terminates and detaches the
target, terminates the runner, and performs a run-tagged cleanup sweep. No runner or target is reused
between samples.

Measured target metrics are:

- `allocation_ms`: immediately before the target create call until the registered handle returns;
- `daemon_ready_ms`: target request start until the separate runner verifies authenticated
  `/readyz`;
- `browser_ready_ms`: target request start until configured Chromium prewarm is verified;
- `first_valid_frame_ms`: target request start until a protected PNG decodes as 1024x768;
- `warm_action_to_frame_ms`: correlated click dispatch until the matching changed causal frame is
  reconstructed from a binary envelope.

Every valid sample also verifies `/healthz`, `/v1/version`, `/v1/capabilities`, browser identity,
prewarm, frame geometry, action success, causal identifiers, visual change, and measured runner
placement. Verification failure produces a failed sample rather than a latency value.

## Placement and provenance

Preregistration binds the unchanged tracked placement artifact
`benchmark-data/modal-v2-placement-capability-2026-07-19.json`, whose SHA-256 is
`d5ee2b31d70e924bdd9b24c55c4361e0adee1234c18246b245d0568b8aa89244`. Its classification remains
`descriptive-placement-capability-only` with `measurement_performed: false`.

That earlier matrix selects the two separately observed frontiers; it does not certify current live
placement. Every new lifecycle independently rejects drift from OCI `us-phoenix-1` for V1 or Azure
`westus3` for V2, and rejects runner-target non-colocation within either arm. The preregistration
records the new clean source commit and exact named image revision separately from the foundation's
source and image identity.

## Sample, cleanup, cost, and promotion gates

The pilot randomizes five independent lifecycles for each of four arms. Full execution is forbidden
unless both primary arms have exactly five valid, fully verified, retry-free, cleanup-complete
samples with exact controls and placement. If eligible, the full schedule randomizes 30 independent
lifecycle samples for each primary arm only.

Each lifecycle and each phase cleanup enumerates both `Sandbox.list()` and
`Sandbox._experimental_list()`. Promotion requires zero tagged survivors and zero termination
failures. Interrupted and failed executions retain checkpoints and sanitized failure metadata under
ignored `benchmark-results/`; they cannot be promoted.

After the full lifecycle gate passes, a separately labeled minimal-container allocation throughput
diagnostic runs at concurrency 1, 5, and 20 for each primary generation and its predeclared
placement. It cannot replace lifecycle evidence. A preregistered public-rate ceiling stops it before
execution if its worst-case estimate exceeds $20.

Each lifecycle records the strongest synchronously available cost proxy: requested target and runner
CPU/memory resource-seconds with Modal's documented 1.75x narrow-region multiplier. Actual Modal
billed cost remains explicitly unreconciled unless attributable billing data becomes available.

Only a complete result with all pilot, full, throughput, provenance, cost, and cleanup gates can be
sanitized into `benchmark-data/`. The promoted comparison uses the label
`optimized-frontier-path-ratio`, with direction V1 optimized p50 divided by V2 optimized p50.

## Reproduction

Run from the exact clean committed harness:

```bash
SOURCE_SHA="$(git rev-parse HEAD)"

uv run python scripts/publish_modal_images.py --revision "$SOURCE_SHA"

uv run python scripts/run_modal_optimized_frontier_benchmark.py preregister \
  --source-sha "$SOURCE_SHA"

uv run python scripts/run_modal_optimized_frontier_benchmark.py pilot \
  --source-sha "$SOURCE_SHA"

uv run python scripts/run_modal_optimized_frontier_benchmark.py full \
  --source-sha "$SOURCE_SHA"

uv run python scripts/sanitize_modal_optimized_frontier_benchmark.py \
  benchmark-results/modal-optimized-frontier-2026-07-19/candidates/full.json \
  benchmark-data/modal-optimized-frontier-results-2026-07-19.json \
  --preregistration \
    benchmark-results/modal-optimized-frontier-2026-07-19/preregistration.json \
  --raw-artifact-path \
    benchmark-results/modal-optimized-frontier-2026-07-19/candidates/full.json
```

Add `--check` to the sanitizer command to verify deterministic regeneration.

Current official capability references:

- [V2 Sandboxes](https://modal.com/docs/guide/sandbox-v2)
- [Cluster networking](https://modal.com/docs/guide/private-networking)
- [Sandbox networking and security](https://modal.com/docs/guide/sandbox-networking)
- [Region selection](https://modal.com/docs/guide/region-selection)
- [Environment variables](https://modal.com/docs/guide/environment_variables)
- [Sandbox lifecycle](https://modal.com/docs/guide/sandboxes)
- [Sandbox resources and pricing](https://modal.com/docs/guide/sandbox-resources)
