# Version 2 release candidate

Status: live-verified branch candidate; not published.

This record binds the package, daemon, protocol, documentation, benchmark, and rollback contracts
for version 2.0.0. It does not authorize a live Modal run, artifact publication, package
publication, or hosted-documentation publication.

## Release identity

The candidate identity is `2.0.0`. The following files must agree before a release tag is created:

- `pyproject.toml`
- `src/modal_computer_use/_version.py`
- `docs/openapi.json`
- the daemon `/v1/version` response
- `CHANGELOG.md`

The release tag is `v2.0.0`. Create it only on the exact verified `origin/main` commit and only
after the worktree is clean. The existing release-candidate checker enforces those conditions.

The daemon keeps API version `v1`. Version 1.1 clients can continue to use the tested REST and JSON
contracts. The version 2 placed trajectory verifies its required capabilities before it acquires a
lease. Package-major equality is not a protocol check.

## Migration contract

The exact migration table is in the version 2.0.0 section of `CHANGELOG.md` and in the version 2
migration guide. The table covers caller placement, one trajectory borrow, binary-backed semantic
screenshots, action batching, mutation ambiguity, cleanup order, and the explicit low-level
compatibility path.

No `optimized=True` flag, performance profile, or hidden environment variable selects between two
defaults. The primary documentation establishes the placed topology. Missing or unverifiable
placement fails before lease acquisition or desktop mutation.

## Benchmark state

Historical reports and artifacts are immutable. They remain evidence for the configurations that
produced them.

The [2026-08-08 optimized-default report](benchmark-results-2026-08-08-optimized-default.md)
records an eligible preregistered, interleaved, same-topology promotion result from runtime source
commit `31bcafefbba2ba75653075a04b12ce2eb816c838`. Its two arms each retained 30 successful samples,
and cleanup recorded no survivors. The article's opening 47 ms figure remains arithmetic over
separate warm screenshot and click medians. It is not a measured fused turn.

Unit tests and the historical cross-provider table do not promote the new default. A release must
record cold startup separately from dispatch, borrow, and repeated warm operations.

## Publication order

Use this order: runtime artifacts → package → hosted documentation.

1. Publish and verify the exact release-matched runtime artifacts. Record their immutable names,
   revisions, requested resources, region constraints, and rollback targets.
2. Build the wheel and source distribution once from the tagged source. Verify and publish those
   exact bytes to TestPyPI and then PyPI.
3. Verify a clean production-package installation outside the checkout.
4. Publish the GitHub Release with the same distributions and checksums.
5. Update the hosted-documentation checks to install the available package version. Preview and
   publish the documentation only after the package is available.

Do not reorder these steps. Do not publish hosted instructions that install an unavailable package.

This article-parity candidate does not require a managed or heavier named release Image. Its
canonical examples make the inline Image recipe explicit. The runtime-artifact stage must record
that no separate artifact publication is required unless the final approved configuration adds
one. Adding a named Image later is a separate provenance, correctness, cost, and benchmark change.

## Selected rollback procedure

The operator-selected rollback target for the public SDK is `modal-computer-use==1.1.0`. The
documentation rollback target is the annotated tag `docs-v1.1.0-last-known-good` described in the
hosted-documentation release record. No named runtime Image is part of this candidate's required
release set.

Use an explicit rollback:

1. Stop package, runtime-artifact, and documentation promotion.
2. Preserve failure evidence and identify the affected source, package, image, and documentation
   revisions.
3. Restore the documentation in a reviewed commit. Keep both major versions selectable and make
   version 1 the recommended version while version 2 is unavailable.
4. Tell operators to pin `modal-computer-use==1.1.0`. If an operator separately selected a named
   runtime artifact, restore its recorded compatible revision. Do not mutate or replace published
   version 2 files.
5. Verify the pinned package, retained runtime artifact, production documentation, and cleanup path.
6. Record the incident, rollback revisions, deployment IDs, and verification results.

The version 2 optimized runtime never silently downgrades to version 1 or to an external laptop
caller. It continues to fail closed when its required placement, handoff, or protocol prerequisites
are absent.

## Candidate gates

The candidate may be tagged only when all offline checks pass from a clean checkout of the exact
main revision. Live or billable gates require separate authorization.

- Run the full lint, type, test, OpenAPI, documentation, example, and import-boundary checks.
- Build one wheel and one source distribution, then install and probe both outside the checkout.
- Verify `/healthz`, `/readyz`, `/v1/version`, and `/v1/capabilities` from the installed daemon.
- In the release Image, run
  `test_x11_clipboard_daemon_child_preserves_long_text_and_restores_state`. Also run the protected
  `test_modal_release_image_x11_clipboard_ownership_smoke` against a real Modal Sandbox. Neither
  test may skip. Together they prove real Xvfb and xclip selection ownership, replacement, and
  cleanup. The protected smoke passed on 2026-08-08.
- Verify that the release bundle contains the exact approved bytes and checksums.
- Run the protected placed-trajectory smoke only with explicit authorization. It passed on
  2026-08-08 with exact `us-west-2` placement.
- Run `scripts/run_optimized_default_promotion.py` from the exact runtime commit with explicit
  authorization. It passed on 2026-08-08 for runtime commit `31bcafefbba2ba75653075a04b12ce2eb816c838`.
  The retained evidence commit changes documentation and benchmark files only.
- Record whether the approved configuration requires runtime artifacts. If it does, record their
  exact revisions before publication.
- Confirm that the hosted documentation preview and rollback version selector pass before its
  publication.

Do not call the candidate released until the production package, immutable GitHub Release, and
hosted documentation have each passed their post-publication checks.
