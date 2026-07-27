# Documentation Archive

The archive preserves context. Current product contracts, procedures, and benchmark results remain
in the maintained documentation. Start at the [documentation map](../README.md).

Archive status depends on evidence and relevance, not age alone:

- **Superseded:** a named successor owns the current answer.
- **Rejected:** review found a method, evidence, or eligibility failure that prevents the stated
  claim.
- **Incomplete:** a required gate or measurement phase did not finish.
- **Diagnostic:** the document records investigation evidence that is not a product result.
- **Historical:** the document accurately records an older system or workload but no longer
  describes the maintained one.

Each archived document begins with a notice containing:

- **Category:** one category from the list above.
- **Date or revision:** enough information to identify the recorded state.
- **Question:** the design or evidence question the document addressed.
- **Disposition:** why it is archived and its successor when one exists.

Do not silently revise archived evidence to match current behavior. Add a short correction or
disposition note when later context is necessary.

Current product contracts remain in [API](../api.md), [configuration](../configuration.md), and the
[active specification](../spec/modal_computer_use_spec_v7.md). Current benchmark procedure remains
in [benchmarking](../benchmarking.md). Machine-readable current evidence remains in
[`benchmark-data/`](../../benchmark-data/).

## Superseded specifications

- [Specification v5](spec/modal_computer_use_spec_v5.md) records the architecture before the
  UV-first tooling revision. Specification v6 superseded it.
- [Specification v6](spec/modal_computer_use_spec_v6.md) records the UV-first design before the
  active v7 contract.

## Archived benchmarks

Superseded reports:

- [Provider-default reference, 2026-07-18](benchmarks/benchmark-results-2026-07-18-provider-default.md)
  predates Tzafon and the current small-sample display policy.
- [Tzafon provider comparison, 2026-07-24](benchmarks/benchmark-results-2026-07-24-tzafon.md)
  predates the eligibility-gated combined artifact.

Rejected reports:

- [Provider benchmark diagnostic, 2026-07-18](benchmarks/benchmark-results-2026-07-18.md)
  used asymmetric lifecycle and screenshot-payload boundaries.
- [Modal optimized-frontier result, 2026-07-19](benchmarks/benchmark-results-2026-07-19-modal-optimized-frontier.md)
  failed the clean pilot gate.

Diagnostic reports:

- [Early provider comparison, 2026-05-17](benchmarks/benchmark-results-2026-05-17.md)
  retains ingress, payload, and 10-iteration context.
- [Provider screenshot payload investigation, 2026-05-19](benchmarks/benchmark-results-2026-05-19.md)
  explains format and byte-accounting differences.
- [Modal V2 candidate result, 2026-07-19](benchmarks/benchmark-results-2026-07-19-modal-v2-candidate.md)
  retains the placement-capability matrix that stopped performance sampling.
- [Native X11 input result, 2026-07-23](benchmarks/benchmark-results-2026-07-23-native-x11-input.md)
  records implementation-validation evidence.
- [Fallback ownership result, 2026-07-24](benchmarks/benchmark-results-2026-07-24-fallback-ownership.md)
  records cleanup and runner-path investigation.

Historical protocols:

- [Modal V2 candidate methodology](benchmarks/modal-v2-candidate-benchmark.md) defines the matched
  placement and promotion gates used by its diagnostic result.
- [Modal optimized-frontier methodology](benchmarks/modal-optimized-frontier-benchmark.md) defines
  the asymmetric best-system protocol used by its rejected result.
