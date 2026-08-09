# Cut over X11 shared-memory screenshot capture

Status: **COMPLETE — promotion rejected; no default cutover**.

Base: `feat/optimized-default-cutover` at `e61191d2d0f21316c34d19eda51c3f83f2d1742c`.
Feature branch: `feat/native-screenshot-capture`.

## Current progress

- [x] Semantic source contract and private module boundary.
- [x] XCB/MIT-SHM capture, fixed-Up level-1 PNG, validation, and deterministic close.
- [x] Generation-scoped readiness, sticky automatic fallback, explicit fail-closed behavior,
  bounded XCB reply waits, and display restart reset/re-probe.
- [x] Managed Image build, universal package source delivery, and real build-time Xvfb canary.
- [x] Full/region/structured/action/hot consumer coverage and existing-path preservation.
- [x] Versioned evidence validator, paired public-call harness, failure matrix, concurrency probe,
  display restart probe, and 10,000-request daemon-local soak.
- [x] First post-implementation two-axis adversarial review; all reported code/harness blockers fixed.
- [x] Second independent two-axis adversarial review; all reported P1 runtime blockers fixed.
- [x] Exact-resource matched Modal promotion attempted from the clean feature branch.
- [x] Fixed gates applied without adjustment: readiness latency and X-server restart failed.
- [x] Production default and canonical documentation retained MSS.
- [ ] Default cutover, release publication, and ready PR — intentionally not performed because the
  fixed promotion decision is reject.

## Decision

The production feature is **X11 shared-memory screenshot capture**. Its source token is
`x11-shm`.

The name describes the actual screenshot source. It does not expose the programming language or
the PNG compression policy. The tested fixed-Up, level-1 PNG policy remains private to the native
implementation. `capture_backend` continues to report the source that produced the frame, such as
`x11-shm`, `mss`, `scrot`, or `maim`.

The production default remains `mss`. `auto` is an opt-in evaluation policy: it selects `x11-shm`
only when the extension and live X11 display pass readiness, and selects MSS for unsupported or
ordinarily failed displays. An explicit `x11-shm` selection fails readiness instead of silently
changing source.

The matched campaign did not satisfy the no-readiness-regression or X-server-restart gates. Those
failures are terminal under the preregistered rule, so this slice is retained as an explicit
capability and benchmark vehicle rather than enabled for every session.

## Contract and scope

The promotion boundary is the complete public call:

```python
await computer.screenshots.full()
```

The required contract is:

- 1024x768 full-resolution lossless PNG;
- cursor hidden;
- identical decoded RGB pixels, dimensions, coordinate space, metadata, and safety behavior;
- the same Modal Image, region, resources, ingress, pooled connection, and SDK call; and
- complete SDK request latency, not encoder or controller latency alone.

The same implementation must benefit eligible shared consumers through the existing screenshot
controller:

- full and regional PNG screenshots;
- raw screenshot after actions and fused action-plus-raw-screenshot;
- hot-session screenshot and action-plus-screenshot operations; and
- structured/artifact hidden-cursor PNG at scale 1 where the existing controller owns the bytes.

Cursor-visible, scaled, JPEG, WebP, and raw-RGB paths keep their existing sources. XDamage,
patches, changed-frame protocols, CDP, and video remain out of scope.

## Deep module and test seams

The existing `X11ScreenshotController` remains the deep module. It owns source selection,
readiness, fallback, metadata, hashing, and existing file/MSS behavior. The private native
implementation has one narrow interface:

```text
X11SharedMemoryScreenshotSession.capture_png(region) -> bytes
X11SharedMemoryScreenshotSession.close() -> None
```

The external test seam is the public screenshot SDK/daemon contract. The internal lifecycle seam
is source resolution/readiness/fallback/close. Rust unit tests cover only native invariants that
cannot be observed without an X server. Do not add a capability registry, a second screenshot
controller, an observation interface, or a generic frame abstraction.

Pre-agreed behavior seams:

1. `await computer.screenshots.full()` and the raw full/region routes.
2. Action-plus-screenshot and hot-session shared controller calls.
3. `/readyz` source selection and explicit-source failure.
4. Sticky automatic fallback after one native generation fails.
5. Backend shutdown and Linux resource cleanup.
6. Modal Image construction and native import/capture canary.

## Fixed promotion gates

Do not weaken these gates after results are visible:

- at least 20% lower complete public-SDK p50;
- no more than 5% p95 regression;
- no more than 10% median payload growth;
- at least 5 ms absolute daemon-side saving, including PNG hashing;
- exact decoded pixel and metadata parity;
- no readiness, fallback, memory, cleanup, or concurrency regression; and
- no replacement samples, hidden retries, or unreported fallback.

Any failed gate means no default cutover. MSS remains the rollback source.

## Slice 0 — isolate and rebase

- [x] Create `feat/native-screenshot-capture` in the isolated worktree.
- [x] Rebase Candidate C/D evidence onto the current optimized-default integration tip.
- [x] Preserve the Computer Step evidence during the benchmark index conflict.
- [ ] Recheck the remote optimized-default tip immediately before push and rebase again if needed.
- [x] Remove Candidate C encoder code and artifacts from the production diff.
- [x] Retain only evidence and harness code that directly supports the X11 shared-memory slice.

## Slice 1 — semantic source contract

Red:

- configuration accepts `auto`, `mss`, and `x11-shm` only;
- the production default remains `mss`; `auto` and `x11-shm` are opt-in;
- response attribution reports the source that actually produced the frame;
- no public name contains `native`, `rust`, `fixed-up`, `adaptive`, or a compression library; and
- explicit `x11-shm` cannot silently return MSS.

Green:

- replace the benchmark `native-xcb-*` selector with `screenshot_capture_source`;
- keep the source resolver and private bridge local to the screenshot feature; and
- use `_modal_computer_use_x11_shm` and `X11SharedMemoryScreenshotSession` only as private
  implementation names.

## Slice 2 — native session correctness and cleanup

Red:

- zero/overflow/out-of-root regions fail before an X request;
- unsupported MIT-SHM version, root geometry, visual, depth, byte order, masks, stride, or reply
  depth fail source readiness;
- the production method has no encoder-policy argument and emits RGB8 lossless PNG;
- one shared-memory slot is sufficient under the existing input lock;
- close is idempotent and performs checked detach while the X server generation is live;
- failure paths release mappings and owned resources; and
- known RGB fixtures decode exactly for full and regional captures.

Green:

- rename and reduce the Rust crate to the one production fixed-Up path;
- retain pinned Cargo.lock and Rust MSRV/toolchain metadata;
- use one checked FD-backed MIT-SHM slot and reusable RGB scratch;
- validate each GetImage reply depth and byte count; and
- expose only dimensions, capture PNG, and deterministic close to Python.

## Slice 3 — readiness, fallback, and lifecycle

Red:

- `/readyz` executes a real hidden-cursor native GetImage/PNG capture before selecting `x11-shm`;
- invalid PNG signature or dimensions fail the readiness probe;
- unavailable/unsupported native under `auto` selects MSS once with a bounded sanitized reason;
- an ordinary non-timeout runtime native failure closes and quarantines the source for the
  session generation;
- later requests do not retry a quarantined native source;
- the request that detects an ordinary failure returns through MSS with `mss-fallback`
  attribution;
- a native setup or reply deadline fails closed without calling another client against the same
  unresponsive X server, and display restart clears that timeout quarantine;
- explicit `x11-shm` fails readiness/capture without fallback; and
- shutdown closes the native session and MSS exactly once.

Green:

- resolve the source after Xvfb starts;
- keep a sticky session-resolution record in the controller;
- open native only for eligible requests or its readiness probe;
- preserve existing locks, budgets, cursor metadata, and file fallbacks; and
- record hash time and include it in controller total timing.

The current synchronous call is not a new regression: MSS is also synchronous and the authoritative
input lock serializes screenshot work. Do not add a worker pool or cancellation protocol in this
slice without measured evidence.

## Slice 4 — runtime Image delivery

Keep the public SDK distribution as one universal Hatchling wheel plus one sdist.

Red:

- the wheel and sdist contain Cargo.toml, Cargo.lock, and Rust sources for the private extension;
- every standard, Firefox, and Chromium Modal Image recipe that claims automatic capture support
  builds the same locked crate;
- the Image build pins Rust 1.91, Python 3.12, target architecture, and Cargo lock;
- the build copies the extension to a private top-level import path and verifies import;
- an Xvfb canary performs a real capture before a named Image is published; and
- a custom Image without the extension falls back under `auto` and fails clearly under explicit
  `x11-shm`.

Green:

- store native build source inside package data so installed SDKs do not require a repository CWD;
- add one Image helper in `image.py`, the existing Modal-only seam;
- add source with `copy=True`, build with `cargo build --locked --release`, and remove build-only
  files/tooling in the same Image layer where practical;
- preserve runtime `libxcb` discovery through the crate's dynamic-loading feature; and
- record source revision, Cargo.lock hash, toolchain, Image identity, and native marker in release
  provenance.

Named revision Images are the production delivery path. Inline Images use the same cached recipe.
A separate companion wheel is a future image-size/build optimization, not part of this slice.

## Slice 5 — shared-consumer parity

Add behavior tests through the existing controller/routes for:

- full raw PNG and regional raw PNG;
- structured inline and artifact hidden-cursor PNG;
- action-plus-raw-screenshot and screenshot-after-actions;
- hot-session screenshot and action-plus-screenshot;
- ineligible cursor-visible, scaled, JPEG, WebP, and raw-RGB paths;
- region edges and coordinate metadata;
- automatic fallback attribution on every eligible surface; and
- explicit-source failure without mutation replay.

Do not add native raw-pixel or observation-delta behavior.

## Slice 6 — Linux operational gates

Run in the exact production-style Modal Image:

1. Real Chromium fixture at 1024x768x24 with deterministic local content.
2. At least 100 alternating MSS/`x11-shm` public calls after warmup.
3. Concurrency 1, 2, 4, and 8 through the public SDK; verify input-lock serialization and bounded
   p95 instead of claiming native parallelism.
4. 10,000 daemon-local captures while sampling RSS, `/proc/self/fd`, mappings, fallback count,
   and cleanup.
5. Region/full alternation and maximum supported screenshot size within the configured pixel and
   memory budget.
6. Injected extension load, constructor, attach, GetImage, encode, invalid-result, and close
   failures.
7. X server loss/restart behavior: automatic mode uses MSS for ordinary native failures, but a
   bounded X-server reply timeout fails closed because every capture source shares that display;
   display restart clears the quarantine and restores attributed `x11-shm`. Explicit mode fails
   clearly without fallback.
8. Terminal daemon/Sandbox cleanup with no survivor and stable resource counts.

Retain failures. Do not replace samples.

## Slice 7 — matched public promotion evidence

The retained artifact must have a versioned schema and validator. It records:

- exact source and runner revisions;
- clean-worktree status;
- requested and observed Modal placement;
- Image name/object identity and native build provenance;
- CPU, memory, display, browser, ingress, HTTP, pooled-client, and warmup configuration;
- deterministic alternating schedule and every raw sample;
- payload, controller capture/encode/hash/total, complete SDK timing, source attribution, failures,
  fallback, and cleanup;
- decoded pixel and metadata parity; and
- the fixed gate decision with confidence intervals.

Do not retain app URLs, tokens, local absolute paths, screenshot bytes, or private endpoints.

## Slice 8 — chronological documentation cutover

Preserve historical optimized-default and Computer Step evidence unchanged. Append the new slice in
this order:

1. architecture: define X11 shared-memory screenshot capture and its controller ownership;
2. configuration: document `mss | auto | x11-shm`, production default and opt-in semantics;
3. performance and benchmarking: link the validated matched result and its limits;
4. troubleshooting/security: unsupported display, build/import, fallback, and secret-safe
   attribution;
5. release candidate/checklist: native Image provenance, canary, rollback, and publication order;
6. changelog/migration: state that screenshot semantics are unchanged and MSS is the fallback; and
7. README/docs index: mention the faster default only after every gate passes.

Runtime Image first, package second, hosted documentation last. Do not publish documentation that
selects an unavailable Image or extension.

## Slice 9 — two-axis adversarial review

Pass 1 is complete and captured in the dated naming, production-readiness, delivery, and packaging
research notes.

After implementation, run Pass 2 against `e61191d...HEAD`:

- Standards axis: AGENTS.md, architecture rules, locality, modularity, naming, resource ownership,
  security, and smell baseline.
- Spec axis: every contract, gate, consumer, failure, packaging, evidence, documentation, and
  rollback item in this plan.

Fix every P0/P1 finding, rerun the full matrix, and repeat the two-axis review until both axes are
clean.

## Slice 10 — verification, commits, and PR

Required local verification:

```text
uv run ruff check .
uv run mypy src
uv run pytest
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
uv build
clean wheel and sdist install/import probes
OpenAPI and hosted-documentation checks
```

Required live verification was attempted from the exact feature branch. Readiness latency exceeded
the fixed regression allowance and X-server restart recovery failed, so Slice 6/7 rejected the
candidate. The validator did not emit a publishable success artifact for the operationally failed
run; no private run URL or synthetic success artifact is retained.

Use small conventional commits by behavior. A future evidence-only draft may be stacked against
the updated `feat/optimized-default-cutover` branch and should match PR #234's structure: `Summary`,
`Context`, `Description`/`Contract`, measured result, `Test Plan`, limitations, rollback, and stack.
Do not mark a production/default PR ready from this rejected result.

## Rollback

Rollback has been applied at the product-policy layer: MSS is the default screenshot source in SDK
configuration and the daemon. The optional implementation, tests, and rejected evidence remain for
future evaluation. No historical benchmark artifact or published package is rewritten.
Explicit `x11-shm` remains diagnostic only if its readiness contract still works. Do not rewrite
historical artifacts or published package files.
