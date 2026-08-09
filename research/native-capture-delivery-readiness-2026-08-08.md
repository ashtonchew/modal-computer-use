# Native screenshot capture delivery readiness

**Date:** 2026-08-08
**Review target:** `/private/tmp/modal-computer-use-candidate-d-sdk` (`b226e64`,
`7b11efd`; currently rebased as `04ac4ed`, `47b61ab`)
**Review posture:** adversarial delivery/rebase/PR review. No branch, worktree, product
source, remote, or release state was changed by this review.

## Decision

The candidate is useful benchmark evidence, but it is **not ready for a production
cutover or a release PR**. Keep MSS as the only default. Do not advertise the native
arm as a general screenshot or Chromium result. The immediate safe delivery is an
evidence-only, benchmark-scoped PR after the artifact is normalized and its provenance
is made reproducible. A production opt-in requires a separate image/build and runtime
readiness slice, plus Linux resource and failure testing.

## Exact base and ancestry

The refs below were read from the shared repository on 2026-08-08. The branch moved while
this review was in progress; use the full commit IDs when preparing the rebase. At the
pass-2 handoff, the parent agent has pushed the optimized-default branch, so the remote
tip must still be rechecked immediately before any candidate push.

| Ref | Commit / relation | Meaning |
| --- | --- | --- |
| `origin/main` | `a39d127` | Wrong base for this stack; it predates the optimized-default and Computer Step contract. |
| historical `origin/feat/optimized-default-cutover` | `6295a65` | Candidate C/D's original parent before the current branch publication. Do not use it as a new-work base. |
| candidate original C | `b226e64` parent `6295a65` | Benchmark-only Rust PNG harness and Candidate C/C2 experiments. |
| candidate original D | `7b11efd` parent `b226e64` | Native XCB/MIT-SHM benchmark arm, Python wiring, runner, and artifact. |
| candidate rebased C | `04ac4ed` parent `24faf4f` | Rebase-equivalent of `b226e64`. |
| candidate rebased D | `47b61ab` parent `04ac4ed` | Rebase-equivalent of `7b11efd`; current candidate branch tip. |
| current integration branch and observed remote tip | `e61191d` (`feat/optimized-default-cutover`) | Current local/remote tip at pass-2 handoff; ancestry is `ea4c2a2` → `24faf4f` → `f6b9ade` → `e61191d`. |

**Correct immediate target:** rebase the two logical candidate commits onto
`e61191d2d0f21316c34d19eda51c3f83f2d1742c` (or an explicitly published descendant of
that commit). Do not rebase onto `6295a65` or `main` merely because those are the
candidate's old parent refs. If the branch is intentionally frozen before the latest
Computer Step publication, the alternate target is `f6b9ade`, but that must be an
explicit decision; it is not the current HEAD observed here.

The managed-image worktree is a separate, diverged stack: PR #236's local tip is
`cb5c26f`, whose merge base with the current integration branch is `6295a65`. It is not
a descendant of `e61191d`. A production native-image change must either stack on a new
branch that contains both lines or be prepared as a separate PR; silently using
`cb5c26f` as the candidate's base will drop the current Computer Step commits.

The candidate worktree was clean, but its tracked report fails whitespace hygiene:

```text
benchmark-data/native-capture-sdk-ab-30.md:52: new blank line at EOF.
```

Fix that in the evidence PR; do not amend the candidate branch in place during this
review.

## What the commits actually contain

### `b226e64`: Candidate C, not a production backend

`b226e64` adds `native/rust_png`, `daemon/desktop/png_encoder.py`, the 1,000-sample
encoder fixtures, and `scripts/benchmarks/full_screenshot_sdk_harness.py`. The candidate
README explicitly says this is benchmark-only, changes no main-worktree routes/defaults,
and measures daemon conversion/encoding rather than complete SDK/Modal latency
(`benchmark-data/rust-png-candidate-c/README.md:1-7,24-26,61-70,109-124`). The Rust PNG
module is not imported by the screenshot route: the production MSS encoder still calls
`mss.tools.to_png` (`src/modal_computer_use/daemon/desktop/screenshots.py:426-466` in
`47b61ab`).

Do not bring Candidate C's codec, `png_encoder.py`, C/C2 aggregate JSON, or its local
encoder claims into a native-capture production PR. Retain only the generic full-route
SDK harness if it is needed for a separately scoped benchmark.

### `7b11efd`: D vertical slice and its public surface

The D commit adds:

- a PyO3/XCB/MIT-SHM extension (`native/native_capture`), compiled only by the benchmark
  runner;
- lazy Python selection and attribution in `daemon/desktop/native_capture.py` and
  `screenshots.py`;
- `ActionConfig.screenshot_capture_backend` plus
  `COMPUTER_USE_SCREENSHOT_CAPTURE` projection/validation;
- a public SDK A/B runner and 30-sample artifact/report; and
- tests for selector, fallback, attribution, and close behavior.

The selector is defaulted to MSS and the native extension is loaded lazily
(`src/modal_computer_use/config.py:173-188`,
`src/modal_computer_use/daemon/desktop/native_capture.py:1-79`). Explicit native arms
fail closed when the extension is absent; `auto` may fall back to MSS. The native fast
path is narrower than the name suggests: it is used for complete PNG bytes when scale is
`1.0` and the cursor is hidden; raw RGB capture remains `mss-raw`, and non-PNG,
scaled, cursor-visible, and file paths stay on existing MSS/scrot/maim behavior
(`screenshots.py:205-328,422-466`). The route adds/retains a
`x-computer-use-capture-backend` attribution header
(`daemon/routes/screenshots.py:59-62,95-110`).

That is a valid benchmark seam, but it is a new public configuration and environment
contract even though comments call it “benchmark-only.” Before treating it as a
supported SDK feature, decide whether to keep it internal to the runner or document it
as experimental with capability and image requirements. `/v1/capabilities` currently
does not report native-capture availability.

## Rebase and conflict map

The candidate's rebased branch already includes `ea4c2a2` and `24faf4f`; do not replay
those commits a second time. The likely textual conflict is
`benchmark-data/README.md`: current `e61191d` adds the Computer Step evidence section,
while `7b11efd` adds the native-capture bullet. Resolve by preserving both and making the
native entry explicitly non-promotable until it has a validator. Other review hotspots:

- `docs/benchmarking.md` and `docs/v2-release-candidate.md`: preserve immutable optimized
  default/Computer Step history; append a new native-capture experiment rather than
  rewriting prior reports.
- `src/modal_computer_use/daemon/app.py`, `daemon/settings.py`, `daemon/desktop/x11.py`,
  and `sandbox.py`: verify the candidate's new selector is still compatible with the
  current settings/config projection after the rebase.
- `docs/configuration.md` and `configuration_reference.py`: keep the selector's default
  and failure semantics identical; do not turn a benchmark arm into an unqualified
  default.
- If stacking on managed-image PR #236, reconcile its `uv`-locked image/runtime changes
  with the current `e61191d` line instead of choosing one side silently.

After the rebase, run `git diff --check`, inspect the full diff against `e61191d`, and
confirm that no `b226e64`-only production-looking files were pulled in accidentally.

## Adversarial readiness findings

### Blocking correctness and release issues

1. **The extension is absent from normal images.** The current inline image recipe only
   installs desktop/runtime packages (`src/modal_computer_use/image.py:11-73`); it does
   not install Rust, `libxcb`/MIT-SHM runtime and development packages, or build/copy
   `_native_capture.so`. The benchmark runner creates a special image, installs Rust
   1.91.0, builds in `/opt/native-capture`, and copies the `.so` into the package
   (`scripts/benchmarks/native_capture_sdk_runner.py:41-99`). An explicit native selector
   on a normal inline or managed image therefore fails closed or is unavailable. A
   production opt-in needs a reproducible Linux build in every image variant that claims
   support, or the selector must remain benchmark-only.

2. **Linux resource lifecycle still needs release evidence, but the earlier FD-leak
   allegation is withdrawn.** The candidate lock resolves `xcb` 1.7.0. Its
   `xcb_send_request_with_fds` contract says that descriptors sent with a request become
   owned by XCB and are closed eventually; the generated `shm::AttachFd` request uses that
   ownership path (the pinned binding source is `xcb-1.7.0/src/ffi/base.rs:230-261`). Do
   **not** add a second successful-path `libc::close` (that would risk a
   double close). The candidate still has no Linux FD/RSS soak, attach-error/connection
   failure test, or assertion that the pinned XCB binding preserves this ownership across
   the production build. `NativeCaptureSession.close()` only marks the Rust object closed;
   XShm detach/flush happens in Rust `Drop` (`native/native_capture/src/lib.rs:372-381,384-422`).
   Verify deterministic object destruction, detach, mmap/unmap, and bounded descriptors in
   the target image before long-lived sandbox use.

3. **The tracked artifact does not meet repository provenance shape.**
   `native-capture-sdk-ab-30.json` has only `arms`, `benchmark`, `cleanup`,
   `fallback_counts`, `payload`, `public_call`, `sample_count_per_arm`, and
   `warmup_iterations`. It lacks the accepted artifacts' `schema_version`, `status`,
   `configuration`, `provenance`, `schedule`, source/image identity, raw SHA binding,
   and gate decision. No native-capture validator or pinning test exists under `scripts`
   or `tests`. `benchmark-data/README.md` says tracked artifacts must pass a validator
   and provenance gate. Until a native-specific schema/validator is added, keep the raw
   result under ignored `benchmark-results/` or label the tracked file as unpromotable
   descriptive evidence.

4. **The report is not reproducible as committed.** Its reproduction block hard-codes a
   private `PYTHONPATH`, a developer `.venv` path, a private output path, and a live Modal
   app URL (`benchmark-data/native-capture-sdk-ab-30.md:5-15`). Scrub those values and
   record source SHA, clean-worktree state, exact image identity/digest, requested and
   observed placement/resources, runner SHA, arm schedule, and raw-artifact SHA. Treat
   app URLs and any bearer-bearing output as sensitive; never copy them into logs or
   public docs.

5. **The evidence fixture is not Chromium.** The report says the desktop is a deterministic
   core-X11 browser-layout fixture held by a live Xlib owner, explicitly “not Chromium”
   (`native-capture-sdk-ab-30.md:28-33`). The measured values therefore support the
   vertical slice only. They do not establish browser compositor parity, resize behavior,
   concurrency safety, or production Chromium performance.

6. **The native Rust code has no direct Linux integration tests.** The Python tests use
   fakes (`tests/test_native_capture.py`); `cargo check --offline` passes on macOS but
   only emits dead-code warnings for Linux-only fields/functions. The target image/OS,
   real Xvfb, MIT-SHM masks/stride, resize, region bounds, concurrent capture, server
   restart, attach failure, and cleanup paths remain unverified.

7. **The “benchmark-only” selector is still SDK/API drift.** It is serialized by
   `ComputerConfig`, exported through `sandbox.py`, validated by daemon settings, and
   listed in `docs/configuration.md`. That is a compatibility commitment even with an
   MSS default. Either keep the option private to benchmark image construction or add
   an explicit experimental contract, capabilities/readiness behavior, image support
   matrix, and rollback story.

### What is already useful evidence

The candidate's runner uses one matched image/configuration per arm, us-west-2, 1 CPU,
2048 MiB, attested ingress, pooled SDK clients, the public `await
computer.screenshots.full()` call, and 30 samples after three warmups
(`scripts/benchmarks/native_capture_sdk_runner.py:103-237`). It checks decoded RGB parity,
dimensions, cursor/coordinate metadata, attribution, and cleanup. The report records:

- MSS complete-SDK p50/p95: 24.89575 / 26.36447 ms;
- adaptive native p50/p95: 10.92440 / 13.32610 ms, payload -55.01%;
- fixed-Up native p50/p95: 10.42470 / 12.25229 ms, payload +4.18%;
- zero observed fallbacks and successful cleanup on this fixture
  (`native-capture-sdk-ab-30.md:35-51`).

Those numbers are a good preregistered vertical-slice lead, not a release gate. The
repository's general policy requires at least 30 complete interleaved samples per arm,
fixed caller/target/region/resources/image/ingress/HTTP/input/screenshot/action/client
topology, retained sanitized observations/failures/cleanup, attribution, no replacements
or retries, and a paired confidence interval (`docs/benchmarking.md:7-23,57-81`). The
native run still needs a validator-compatible artifact and the missing operational arms
listed above.

Targeted local evidence observed during this review:

- with `PYTHONPATH` set to the candidate source/scripts, the candidate harness and native
  selector tests pass: **18 passed**;
- targeted Ruff and mypy checks pass in the shared development environment;
- `cargo check --offline --manifest-path native/native_capture/Cargo.toml` passes on macOS,
  with warnings for Linux-only/dead fields;
- the full repository-required `uv sync --extra dev --extra modal --frozen`, Linux build,
  full test suite, and Modal smoke were not completed. An earlier `uv run pytest` attempt
  used a `0.8.15` executable against this repository's required `0.12.3`; rerun from a
  clean, pinned environment before using that result as release evidence.

## Chronological documentation and artifact cutover

Do not edit the historical optimized-default or Computer Step reports/artifacts. The
current release notes establish that history is immutable and that publication order is
runtime artifacts → package → hosted docs (`docs/v2-release-candidate.md:37-78`,
`docs/hosted-documentation-release.md:73-88`). A native capture rollout should proceed in
this order:

1. **Evidence-only stage.** Add a dated methodology/report and sanitized native artifact
   with a validator/provenance record. Link it from `benchmark-data/README.md` as
   “benchmark-only; not a production default.” State the exact scope (full PNG, scale 1,
   cursor hidden; raw/other formats unchanged), fixture limitation, and numerical/operational
   gates. Do not alter `docs/benchmark-results-2026-08-08-optimized-default.md`, the
   Computer Step report, or their JSON inputs.
2. **Runtime/image stage.** If promotion is approved, add the Linux extension build and
   runtime dependencies to the exact inline/named image variants that support the selector.
   Publish/verify immutable image names/revisions, source SHA, toolchain, package ABI, and
   rollback image before changing user-facing defaults. This stage is separate from the
   current article-parity release, which explicitly has no managed-image requirement.
3. **Opt-in API stage.** Document the experimental selector, image requirement,
   `/healthz`/`/readyz` fail-closed behavior, `auto` fallback attribution, capability
   reporting, supported screenshot options, and compatibility/rollback. Update the checked
   OpenAPI/configuration references only with the tested behavior.
4. **Operational promotion stage.** Run the exact clean release commit against matched
   Chromium and deterministic X11 fixtures, including concurrency/resize/soak and
   failure-recovery gates. Publish a new dated native-capture promotion report only after
   those pass. Then build/install the package and publish hosted documentation; never make
   docs install or select an image/package that has not been published.

The rollback must be explicit: restore MSS as the selector/default, pin the prior package
and image revisions, retain failed evidence, and revert docs in a reviewed commit. Follow
the existing release rollback discipline (no silent downgrade and no rewriting published
artifacts) in `docs/v2-release-candidate.md:80-102`.

## Required release and benchmark evidence before promotion

### Benchmark evidence

- A preregistered native-specific schema/validator that rejects missing provenance,
  placement drift, image/source mismatch, retries/replacements, invalid frames,
  attribution mismatch, and failed cleanup.
- At least 30 complete, deterministic interleaved samples per arm, with failures retained
  and no replacement/retry; report paired bootstrap 95% intervals in addition to p50/p95.
- Same public pooled SDK call and raw binary route; same region, caller Function,
  resolution/depth, CPU/memory, ingress, HTTP version, input backend, action payload,
  warmup, and client reuse. Record requested and observed placement.
- Matched **Chromium** fixture plus the current core-X11 fixture; decoded RGB and metadata
  parity, cursor semantics, coordinate space, dimensions, format/scale matrix, and
  capture-backend attribution on every sample.
- Native session soak with repeated capture, concurrent requests, region/resize changes,
  Xvfb restart/extension loss, XShm attach failure, native capture failure, `auto` fallback,
  explicit-native fail-closed behavior, process RSS, open-FD count, slot exhaustion, and
  terminal cleanup.
- Sanitized artifact binds the runner/source/image/toolchain and raw SHA; no private paths,
  app URLs, tokens, screenshot bytes, or unreviewed endpoint data.

### Release evidence

- `uv sync --extra dev --extra modal --frozen`, OpenAPI export/check, Ruff, mypy, full
  pytest, repository hygiene/import-boundary scans, wheel/sdist build and clean install
  probes, following `docs/release-checklist.md:6-18,75-87`.
- Linux CI/Modal image build from a clean exact commit, with a reproducible Rust/PyO3
  toolchain and pinned dependency/build outputs. Verify `_native_capture.so` loads in
  every advertised image/profile and that MSS-only images still work.
- Installed daemon probes `/healthz`, `/readyz`, `/v1/version`, and `/v1/capabilities`;
  readiness/capabilities must make native availability and explicit failure visible without
  leaking secrets.
- Protected Modal smoke at exact requested/observed us-west-2 placement, no retries, and
  terminal owner/image cleanup. Retain deployment/image IDs privately and sanitized
  provenance publicly.

## Do not import from the benchmark prototype

- Candidate C's `native/rust_png`, `png_encoder.py`, C/C2/zlib variant artifacts, or any
  daemon-only encoder ratios as production code or SDK latency claims.
- The hard-coded developer paths, output locations, Modal app URL, or any raw/private
  benchmark response.
- The “not Chromium” fixture as evidence of browser correctness or a default performance
  guarantee.
- A native default, implicit `auto` fallback for explicit native requests, or a capability
  claim for images that do not contain the extension.
- The public selector/env without deciding its support level, image contract, capability
  response, and rollback semantics.
- Any default image/package publication that has not passed the Linux build, FD/RSS soak,
  cleanup, and clean-distribution checks.
- Rewrites to historical optimized-default/Computer Step docs or artifacts, external
  provider/model loops, or changes that violate the repository's Modal-boundary/import
  rules.

## Proposed delivery shape

The prior PRs establish a useful convention: narrowly scoped semantic commits and draft
PR bodies with `Summary`/`Context` or `Why`, `Validation`, benchmark impact, and explicit
limitations. See [PR #234](https://github.com/ashtonchew/modal-computer-use/pull/234),
[PR #236](https://github.com/ashtonchew/modal-computer-use/pull/236),
[PR #238](https://github.com/ashtonchew/modal-computer-use/pull/238), and the merged native
X11 precedent [PR #124](https://github.com/ashtonchew/modal-computer-use/pull/124).

### Recommended evidence PR (base `e61191d`)

Title: `perf(benchmarks): record native screenshot capture candidate`

Keep the PR draft and split commits by behavior:

1. `perf(benchmarks): add pooled full-screenshot SDK harness` — retain only the generic
   harness and its focused tests; no Candidate C route/default code.
2. `perf(benchmarks): add native XCB capture runner` — runner, fixture, and explicit arm
   configuration; keep the build isolated to the benchmark image.
3. `docs(benchmarks): publish native capture evidence` — sanitized artifact, provenance,
   validator/pinning test, methodology report, and a “not production-ready” limitation.

Suggested body:

```markdown
## Summary
Records an opt-in native XCB/MIT-SHM screenshot experiment. MSS remains the default.

## Context
The public pooled `screenshots.full()` path is measured end to end under one matched
Modal topology. This is a benchmark slice, not a production image or default cutover.

## Method
State source/image/region/resources/ingress/client, arm order, warmups, sample count,
fixture, raw route, estimator, provenance, and cleanup.

## Results
Report p50/p95, payload, daemon stages, paired intervals, attribution, failures, and
cleanup for every arm.

## Limitations / follow-up
Not Chromium; no default change; Linux image, FD/RSS soak, concurrency, resize, and
failure-recovery gates remain before an opt-in production slice.

## Validation
Focused tests, artifact validator, diff check, clean pinned checks; list unrun Modal or
Linux gates explicitly.
```

### Separate production PR (only after image/runtime evidence)

Title: `feat(daemon): add opt-in native XCB screenshot capture`

Base it on a branch containing the current `e61191d` line and the approved managed-image
runtime contract (or make image support a prerequisite PR); do not silently base it on
diverged `cb5c26f`. Use scoped commits such as:

1. `feat(daemon): add opt-in native capture contract` (Python selector, route attribution,
   capabilities/readiness semantics, no default change);
2. `feat(images): ship native capture runtime` (Linux build/dependencies/toolchain,
   immutable image manifests and rollback target);
3. `test(daemon): verify native parity and resource cleanup` (real Xvfb/Linux, failure,
   concurrency, resize, FD/RSS/soak tests);
4. `docs(configuration): document native capture support` (experimental status and image
   matrix); and
5. `docs(benchmarks): publish native promotion decision` (only after the complete gate).

Use PR sections matching #236/#124: `## Summary`, `## Why`/`## Context`, `## Contract`,
`## Validation`, `## Benchmark impact`, `## Rollback`, and `## Stack`. Explicitly state
that MSS is unchanged and that native scope is limited to the tested screenshot options.

## Source index

- Candidate original commits: `b226e64` and `7b11efd` in
  `/private/tmp/modal-computer-use-candidate-d-sdk`; rebased equivalents `04ac4ed` and
  `47b61ab`.
- Current integration tip: `e61191d`; previous pacing fix `f6b9ade`; candidate rebase
  base `24faf4f`; old remote base `6295a65`; main `a39d127`.
- Candidate implementation: `native/native_capture`,
  `src/modal_computer_use/daemon/desktop/native_capture.py`, `screenshots.py`, `x11.py`,
  `daemon/settings.py`, `config.py`, `sandbox.py`, and
  `scripts/benchmarks/native_capture_sdk_runner.py`.
- Repository contracts: `docs/benchmarking.md`, `docs/release-checklist.md`,
  `docs/v2-release-candidate.md`, `docs/hosted-documentation-release.md`, and
  `benchmark-data/README.md` at `e61191d`.
- Prior delivery conventions: PR #234 (optimized-default), #236 (managed images),
  #238 (lifecycle benchmark), and #124 (native X11 input).

## Pass 2 addendum — corrected runtime risks and final delivery sequence

This addendum supersedes the FD-leak sentence in the first pass and cross-checks the
semantic naming review in
`research/native-screenshot-semantic-naming-2026-08-08.md` against the candidate's
runtime behavior.

### Corrections and runtime implications

1. **Do not close an `AttachFd` descriptor twice.** The candidate's lockfile resolves
   `xcb` 1.7.0. The XCB binding documentation for `xcb_send_request_with_fds` says that
   descriptors sent with a request are owned by XCB and closed eventually; the generated
   `shm::AttachFd` request is the relevant path (pinned source:
   `xcb-1.7.0/src/ffi/base.rs:230-261`). The successful-path `libc::close` concern
   from pass 1 was incorrect and is withdrawn. The remaining release question is
   deterministic `Drop`/detach/flush and bounded mmap/FD behavior in the target Linux
   image, not a hand-added close after `AttachFd`.

2. **Readiness does not prove a native frame.** `X11ScreenshotController.probe()` calls
   `_ensure_native()` and then `capture_bytes()` with `show_cursor=True`
   (`daemon/desktop/screenshots.py:140-158`). The native fast path is deliberately
   ineligible when the cursor is visible, so readiness exercises extension/session
   construction and an existing MSS/file capture, not a native XShm `GetImage` plus PNG
   encode. An explicit native selector can therefore pass readiness and fail on its first
   eligible capture. Keep this fail-closed behavior explicit; either add a targeted,
   side-effect-safe native probe or state that readiness only proves constructor
   availability and retain a first-capture failure gate.

3. **`auto` can retry a broken native session forever.** A runtime `NativeCaptureFailed`
   closes the session and falls through to MSS for that request, but the auto resolution
   remains native. On the next request `_ensure_native()` constructs another native session
   and repeats the failure (`daemon/desktop/screenshots.py:219-254`). Startup
   `NativeCaptureUnavailable` latches to MSS, but runtime failure does not. Before an
   opt-in production release, latch the runtime fallback (or use a bounded circuit
   breaker), expose the transition in attribution/metrics, and add a test proving that a
   broken native arm cannot add one failed startup/`GetImage` attempt to every request.
   Benchmark evidence must count these failures and must not silently treat repeated MSS
   fallback samples as native samples.

4. **Close is deferred, not an explicit XShm detach.** Rust `close()` only marks the
   session closed; `NativeCaptureSession::Drop` sends `shm::Detach` and flushes. Python
   drops its wrapper after closing the controller, which is normally deterministic under
   CPython but is not a documented resource contract. The production slice needs an
   explicit cleanup test in the target Linux image (including interpreter shutdown,
   session replacement after runtime failure, X-server loss, and outstanding slot
   references) and should either make the deferred ownership clear or add a deterministic
   native shutdown operation.

5. **The existing capture/controller lock serializes the operation.** The native session
   is not a concurrent-throughput primitive: the controller/lease capture lock means
   callers are serialized around capture. The candidate's 30-sample A/B is valid for the
   single-request latency claim it measures, but it says nothing about parallel captures,
   queueing, or slot scaling. Keep lock-wait timing in any future benchmark and compare
   both arms under the same lock. One slot is sufficient for the current public route; treat
   the two-slot implementation as an unproven future direct-call/throughput experiment. Do
   not call it “concurrent” or use this artifact as a throughput result.

6. **The candidate pulls in all of Candidate C unnecessarily.** The rebased branch carries
   the entire `b226e64` codec experiment (Rust PNG crate, encoder module, four aggregate
   JSON files, README, and C/C2 scripts) even though D only needs the generic
   `measure_full_screenshot_arms` helper. The smallest evidence change extracts that helper
   and its focused tests, then drops `native/rust_png`, `png_encoder.py`, C/C2/zlib
   artifacts, and local codec claims. This prevents a benchmark-only codec from looking
   like a supported daemon backend and makes review/rebase conflicts tractable.

### Naming decision after the semantic review

The naming review's first capability-only proposal (`lossless-frame`) is not suitable for
the current response header: `capture_backend` is operational attribution and must retain
the actual source (`mss`, `x11-shm`, `scrot`, `maim`, or an explicit fallback). The revised
recommendation is the one to use for any unmerged production-facing surface:

```text
benchmark selector:  screenshot_capture_source = auto | mss | x11-shm
actual attribution:  capture_backend = x11-shm | mss-fallback | mss | ...
codec detail:        encoding_policy = level1-up | level1-adaptive (artifact only)
```

`native-xcb-adaptive` and `native-xcb-fixed-up` combine implementation, transport, and
PNG filter policy. They should not become stable SDK values. Because the candidate is not
merged, rename the selector before it leaves the benchmark branch (or keep it entirely
private to the runner); do not preserve an unstable `ActionConfig`/environment contract
just to avoid a pre-release compatibility alias. Keep the implementation seam local:
the smallest change can leave the code in `daemon/desktop/screenshots.py` and use a
private `X11SharedMemoryScreenshotSession.capture_png(...)` only where the PNG/scale/cursor
contract is proven. Do not add the larger capability registry or rename every screenshot
consumer in the evidence PR.

This naming decision does not authorize a broad refactor. It is a boundary rule for the
next production slice: source attribution remains truthful, codec experiments remain
benchmark metadata, and no name implies readiness, changed pixels, zero-copy ownership,
or a particular transport to SDK callers.

### Smallest viable PR shape

The smallest reviewable delivery is **one benchmark/evidence PR**, not an update to the
optimized-default implementation PR and not a production image PR:

1. Start from the exact current tip of the optimized-default branch after its own release
   review. Extract the generic pooled full-screenshot harness and focused tests only.
2. Add the D native seam needed to run the A/B, but keep it explicitly opt-in and
   benchmark-scoped. Rename the selector to `screenshot_capture_source`/`x11-shm` (or keep
   the setting private to the runner); do not add a stable default or claim image support.
3. Add the native runner and a **new validator-compatible** sanitized artifact/report.
   Record source/image/toolchain, observed placement, lock/queue semantics, sample
   schedule, first-capture behavior, fallback counts, and cleanup. Do not import any C
   codec implementation or C aggregate data.
4. Add one focused methodology/benchmark README entry saying “benchmark-only; not a
   production default.” Do not touch optimized-default/Computer Step historical reports,
   `CHANGELOG.md`, package version, or hosted release docs.

This PR may contain the Python selector/native bridge because the runner needs a real
daemon arm, but it must not contain image publication, wheel packaging, a default switch,
or a readiness/capabilities promise. If the selector cannot be kept private, mark the
configuration experimental and pin its exact supported image contract in the report.

A later **production PR** is required for the smallest safe runtime change: package/build
the extension in supported Linux images/wheels, fix/latch runtime fallback, define
deterministic close/readiness semantics, and add real Linux Xvfb/MIT-SHM/Chromium,
concurrency/lock, resize, failure, FD/RSS, and soak tests. It should not carry Candidate C
files or rewrite benchmark history.

### PR #234 versus a new stacked PR

Do **not** update PR #234 with native capture. PR #234's title/body and release evidence
are for the placed optimized SDK default; adding a benchmark-only native arm would blur
the default contract and invalidate its focused review. At pass-2 handoff, the current
local and remote branch both point to `e61191d`; if either moves before publication,
repeat the freeze/push check below. The order is:

1. Finish and review the optimized-default/Computer Step work on
   `feat/optimized-default-cutover`; run its full release checks from the moving HEAD.
2. Verify that exact reviewed tip is pushed and update PR #234 (or replace it with a fresh
   PR if the branch/base policy requires). Record the resulting remote SHA; do not use the
   stale `6295a65` base for new work.
3. Create a new draft benchmark PR from the resulting `feat/optimized-default-cutover`
   tip. The PR is logically stacked on #234, with title
   `perf(benchmarks): record native screenshot capture candidate`; its base is the updated
   optimized-default branch, not `main` and not the diverged managed-image tip.
4. Keep PR #236's managed-image changes separate. If the later production native PR needs
   managed images, first rebase/update #236 onto the accepted #234 tip, then stack the
   production native PR on that combined image/runtime base. Do not use `cb5c26f` directly
   while it still diverges at `6295a65`.

### Final exact rebase, push, PR, and documentation sequence

The following is the handoff sequence for the operator who will publish; it is not an
instruction to mutate refs during this review.

**A. Freeze and publish the base**

```sh
git fetch origin
git switch feat/optimized-default-cutover
base_sha="$(git rev-parse HEAD)"
git diff --check
# run the repository release checklist from this exact clean commit
git push origin feat/optimized-default-cutover
```

Verify on GitHub that the branch/PR #234 base now points to `base_sha`. At pass-2 handoff
this check should read `e61191d`; if local HEAD has moved beyond it, use the newly recorded
full SHA in every subsequent artifact and PR description; never infer it from a short branch
name. If the remote already equals the clean `base_sha`, no second push is needed.

**B. Rebase and curate the candidate**

From the candidate repository, replay only the two logical D commits onto that published
base (the current candidate parent to exclude is `24faf4f`):

```sh
git fetch origin
git rebase --onto "${base_sha}" 24faf4f feat/native-screenshot-capture
```

Before creating the PR, split/curate the result: retain the generic full-route harness,
the D bridge/runner/tests, and the normalized artifact/report; drop every C codec/source
file and scrub private paths/app URLs. Resolve `benchmark-data/README.md` by preserving
the current Computer Step section and appending a native “not promotable” entry. Run
`git diff --check`, the artifact validator, focused tests, and the complete release checks
from a clean checkout of the rebased SHA.

**C. Publish the evidence PR**

```sh
git switch -c perf/native-capture-evidence "${base_sha}"
# apply the curated D-only commits/files
git push -u origin perf/native-capture-evidence
```

Open a **draft** PR with base `feat/optimized-default-cutover`, title
`perf(benchmarks): record native screenshot capture candidate`, and sections
`Summary`, `Context`, `Method`, `Results`, `Limitations / follow-up`, and `Validation`.
State explicitly: MSS remains the default; the fixture is not Chromium; the native arm
is not shipped in normal images/wheels; readiness does not execute native `GetImage`; auto
runtime retry/close behavior is not a production guarantee; and no C codec files are
included.

**D. Only after evidence is accepted, prepare production runtime**

1. Rebase/update managed-image PR #236 onto the accepted #234 tip, preserving its
   `uv`-locked runtime contract and release manifests.
2. Build a separate production PR for the opt-in native source: pinned Linux toolchain and
   wheel/image artifact, capabilities/readiness semantics, latched fallback, deterministic
   close, and real Xvfb/Chromium/lock/resize/failure/FD/RSS/soak tests. Keep MSS default.
3. Run the complete native promotion gate from the exact clean production SHA, publish
   immutable runtime/image artifacts first, then build/verify the package, then publish
   hosted docs. Do not add user docs that select an unpublished image or package.
4. If any production gate fails, restore MSS and the previous package/image revisions,
   preserve evidence, and revert docs in a reviewed commit. No silent fallback or history
   rewrite.

The final documentation cutover is therefore: **#234 optimized-default base → evidence
PR/report → #236 managed-image base (if needed) → production native runtime/image → package
→ hosted docs**. Only the last two stages can change user-facing support claims; the
evidence PR remains a dated, immutable benchmark record.
