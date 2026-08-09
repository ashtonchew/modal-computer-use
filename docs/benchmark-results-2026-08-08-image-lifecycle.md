# Standard Image lifecycle results, 8 August 2026

This report compares the current inline Image recipe with one verified standard Managed Image
Release. It measures Modal Sandbox creation through the first valid frame. It does not measure Image
build time or warm daemon operations.

## Result

The managed release improved the typical lifecycle in this run. Its p50 was 1.38 seconds lower than
the inline recipe, an 8.95% reduction. Its p95 was 1.41 seconds lower, a 3.15% reduction.

The paired median improvement was stable in the bootstrap analysis. The paired mean was not stable
because both arms had large tail outliers. Treat this result as evidence to advance the managed
standard Image to the next release gate. Do not treat it as sufficient evidence to make every Image
variant the default.

| Metric | Inline recipe | Managed exact ID | Managed change |
| --- | ---: | ---: | ---: |
| p50 | 15.47 s | 14.08 s | −1.38 s (−8.95%) |
| p95 | 44.68 s | 43.27 s | −1.41 s (−3.15%) |
| Mean | 19.35 s | 18.46 s | −0.89 s (−4.62%) |

The paired median delta was −1.36 seconds. Its bootstrap 95% confidence interval was −1.98 to
−0.44 seconds. The paired mean delta was −0.89 seconds. Its bootstrap 95% confidence interval was
−6.53 to +4.81 seconds.

## Method

- Source revision: `4cb098207053931c2e6e693ce87f7f6e16ab215a`
- Caller topology: one external SDK process
- Caller label: `codex-desktop-local-process`
- Modal Workspace: `modal-ai-hackathon`
- Modal Environment: `main`
- Requested and observed placement: `aws/us-west-2`
- Managed Image reference:
  `modal-computer-use-standard:4cb098207053931c2e6e693ce87f7f6e16ab215a`
- Managed Modal object ID: `im-PUBK2dnCEAaheWFqgQ9EgM`
- Image Builder Version: `2025.06`
- Modal SDK: `1.5.3`
- uv: `0.12.3`
- Target resources: 1 CPU and 2048 MiB, with limits equal to requests
- Schedule: one warmup pair and 30 measured pairs
- Ordering: deterministic paired interleaving
- Concurrency: one target
- Retries and replacement samples: zero

All 60 measured lifecycles succeeded. Every measured target returned the expected placement and a
valid first frame. Every cleanup succeeded. The artifact contains no failed sample.

## Cost

The measured target resource-duration estimate was $0.1217. The preregistered target ceiling was
$1.0305. These values use public rates recorded on 8 August 2026 and include the narrow-region
multiplier. They exclude Image build, canary, the external caller, control-plane charges, and billing
adjustments.

The final source-layer build took 1.41 seconds after Modal reused the earlier dependency layers. The
full final publication and canary command took 22.82 seconds. Build timing is deployment evidence,
not part of the Sandbox lifecycle comparison.

## Evidence

- [Managed Image Release manifest](../benchmark-results/image-lifecycle/4cb098207053931c2e6e693ce87f7f6e16ab215a/standard-image-release.json)
- [Pilot artifact](../benchmark-results/image-lifecycle/4cb098207053931c2e6e693ce87f7f6e16ab215a/pilot.json)
- [Primary artifact](../benchmark-results/image-lifecycle/4cb098207053931c2e6e693ce87f7f6e16ab215a/primary.json)

## Decision boundary

This result supports the expected value of a Managed Image Release for the standard variant: users
can skip the inline recipe's caller-local source transfer and resolve an exact prebuilt Image.

The result does not prove the following claims:

- Firefox and Chromium variants have the same lifecycle change.
- Managed Images reduce warm action or screenshot latency.
- The paired mean improvement is stable under heavy lifecycle tails.
- Release publication, rollback, and availability are ready for an automatic default in every
  Workspace and Environment.

Keep the inline recipe as an explicit development path. Advance the standard managed release through
availability, rollback, and release-order checks before changing the SDK default.
