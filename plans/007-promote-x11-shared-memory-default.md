# Promote X11 shared-memory screenshot capture safely

Status: **IN PROGRESS — MSS remains the default until every fixed gate passes**.

## Objective

Make `screenshot_capture_source="auto"` safe to enable for every managed session while preserving
the public screenshot contract and the complete Sandbox lifecycle. The promotion boundary remains
the literal public call:

```python
await computer.screenshots.full()
```

at 1024x768, hidden cursor, full-resolution lossless PNG, with identical decoded pixels,
dimensions, coordinate space, metadata, fallback, readiness, cleanup, and safety behavior.

## Existing stack: reuse, do not rebuild

1. PR #233 upgrades the Modal dependency.
2. PR #234 owns the optimized placed trajectory and Computer Step defaults.
3. PR #236 owns uv-locked managed Image releases, immutable manifests, exact-ID canaries, and
   release orchestration.
4. PR #238 owns managed Image lifecycle evidence.
5. PR #239 owns opt-in X11 shared-memory screenshot capture, its packaged Rust source, the pinned
   managed-Image build, a real Xvfb/MIT-SHM build canary, selectors, fallbacks, consumers, and the
   rejected promotion harness.

No new wheel format, companion native package, Image release framework, build canary, screenshot
registry, codec abstraction, or observation protocol belongs in this work.

## New behavior stack

### R1 — readiness attribution and parity

Branch: `perf/x11-shm-readiness-parity`, based on PR #239.

Interface seams:

- public fresh `AsyncComputerSandbox.create(...)` through authenticated `/readyz`;
- daemon lifespan publication of operational readiness;
- existing `DesktopBackend.ready()` capability verification; and
- the versioned X11 screenshot promotion artifact.

Requirements:

- [ ] retain paired per-sample SDK startup stages for both MSS and automatic X11 shared memory;
- [ ] retain failed readiness observations rather than only an aggregate rejected decision;
- [ ] distinguish Modal allocation/TCP/tunnel work, browser fixture readiness, and daemon backend
      readiness without exposing secrets or a new stable SDK schema;
- [ ] after Xvfb starts, overlap only independent browser startup and backend readiness work;
- [ ] join both tasks before the daemon lifespan yields or readiness is published;
- [ ] keep the full hidden PNG and cursor-visible canaries;
- [ ] retain the warmed screenshot session for the first public request;
- [ ] preserve MSS behavior and the generation-aware readiness cache; and
- [ ] keep the fixed candidate startup-p95 limit at MSS p95 plus at most 5%.

Stop conditions:

- any readiness safety check is removed or replaced by import-only evidence;
- startup work races display/window-manager availability;
- readiness can become true before browser/backend tasks finish;
- cancellation leaks either startup task;
- an optimization changes public screenshot semantics; or
- the exact-resource readiness p95 still exceeds the fixed limit.

### R2 — display-generation recovery

Branch: `fix/x11-display-generation-recovery`, based on R1.

Deep module seam:

```python
await backend.invalidate_display_generation()
await daemon_readiness(request, force=True)
```

The interface is semantic: it does not expose XCB handles, XIDs, MIT-SHM slots, controller lists,
or public generation tokens. The backend owns which daemon clients belong to the outgoing display
generation. Lifecycle routes own authorization, the exclusive mutation admission, readiness
invalidation, the full-stack `Supervisor` mutation, and the bounded forced daemon-readiness probe
after the mutation.

Requirements:

- [ ] invalidate the readiness/display epoch exactly once before process mutation;
- [ ] best-effort release held input state, then clear generation-bound logical state;
- [ ] close clipboard ownership, native and MSS screenshot sessions, EWMH/window Xlib state, and
      XTest/Xlib input state while the old X server is alive;
- [ ] attempt every close and preserve the first sanitized failure;
- [ ] treat a named Xvfb restart as a complete display-stack restart, including the window manager
      and VNC dependents;
- [ ] never reuse an old XCB connection, XID, atom cache, MIT-SHM attachment, or MSS instance;
- [ ] after start, force daemon readiness and return success only after input, window manager,
      `xdpyinfo`, hidden full PNG, cursor-visible paths, and configured VNC dependents pass;
- [ ] keep readiness false and return 503 on failed reconstruction;
- [ ] reject restart with typed `display_restart_busy`/409 while an active recording, observation
      websocket, or HTTP observe-change request owns a live X display;
- [ ] rerun configured browser startup (`browser_open_url_on_start` or browser prewarm) after the
      display is ready, while making no claim to restore arbitrary app processes, browser page
      state, or application-owned state; and
- [ ] leave hot-session sockets connected but make their post-restart operations observe the
      generation/readiness gate.

Failure semantics:

- no automatic action replay;
- no partially published generation;
- no fallback to a stale X client;
- cleanup failure does not skip the remaining cleanup operations; and
- a failed restart remains observable as not-ready until a later successful lifecycle operation.

Stop conditions:

- implementation introduces a generic X-resource registry;
- restart logic moves into the screenshot module;
- old XCB/MSS/Xlib clients are reconnected instead of replaced;
- browser page-state preservation or an XDamage protocol is added;
- a worker/cancellation framework is introduced only for this feature; or
- recovery cannot be bounded and proven through the public lifecycle interface.

### R3 — evidence, default, and canonical documentation

Branch: `perf/x11-shm-default-promotion`, based on R2. Create and publish this PR only after R1 and
R2 are clean and the exact campaign completes.

This PR contains evidence and product-policy changes, not another implementation redesign:

- [ ] at least 100 paired, interleaved public-SDK samples per source;
- [ ] exact managed Image, region, CPU, memory, ingress, fixture, and pooled SDK call;
- [ ] candidate arm requests `auto` and proves `capture_backend=x11-shm` without fallback;
- [ ] at least 20% lower public-SDK p50;
- [ ] no more than 5% public-SDK p95 regression;
- [ ] no more than 10% median payload growth;
- [ ] at least 5 ms daemon-side saving including PNG hash;
- [ ] exact full and edge-region decoded pixel/metadata parity;
- [ ] readiness p95 no worse than MSS by more than 5%;
- [ ] restart, concurrency, failure, cleanup, memory, FD, mmap, and 10,000-capture soak gates;
- [ ] a retained revision-bound artifact with confidence intervals and a fixed decision; and
- [ ] zero surviving benchmark Sandboxes.

If any gate fails, R3 records the rejection, leaves MSS as default, and does not perform the
canonical documentation cutover.

## TDD seams

Tests are written red-to-green at these behavior interfaces:

1. daemon lifespan does not publish readiness until browser and backend preparation both finish;
2. fresh public create-to-ready evidence retains paired stage attribution;
3. public lifecycle start/stop/restart and named Xvfb restart use the display-generation seam;
4. backend generation invalidation closes every daemon-owned X client even when one close fails;
5. forced post-start daemon readiness controls the route result and readiness cache;
6. full/region/action/hot screenshot consumers preserve exact behavior after recovery; and
7. active observation/recording ownership follows the typed-busy contract, while configured browser
   startup is rerun and arbitrary app/page state is not restored.

Tests do not assert private call counts except where ordering is itself the lifecycle contract.
Native Linux invariants remain in Rust/Xvfb tests; public route and SDK behavior remains in Python
integration tests.

## Verification order

For every behavior PR:

```text
focused red/green tests
ruff check .
mypy src
pytest -m 'not modal'
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --offline
wheel and sdist build/install/import checks when package inputs change
```

Then run two independent adversarial passes:

1. standards: locality, semantic naming, deep interface, cleanup, and scope;
2. specification: exact gates, public screenshot contract, lifecycle recovery, and evidence truth.

Only the final evidence PR runs the billable exact-resource Modal promotion campaign.

## Documentation cutover

R1 and R2 update only implementation-local architecture, configuration details, troubleshooting,
and their tests. They must continue to say MSS is the production default.

After a passing R3 campaign, update in one commit:

- `ActionConfig` and daemon default to `auto`;
- configuration reference and environment-variable documentation;
- architecture and product specification source-selection/fallback language;
- performance documentation with the new retained complete-request evidence;
- troubleshooting for automatic fallback, timeout, and display-generation recovery;
- release checklist, changelog, migration notes, and hosted-documentation handoff; and
- rollback instructions selecting `mss` without changing the public screenshot call.

Historical rejected artifacts and research remain unchanged.

## Merge and rebase order

The intended review order is #233 → #234 → #236 → #238 → #239 → R1 → R2 → R3. Before each
dependent PR becomes ready, rebase it on the actual merged parent and rerun its complete checks.
Resolve the known `image.py`, `sandbox.py`, distribution-smoke, and Modal-foundation overlaps by
preserving #236's uv-locked release machinery and #239's feature-local X11 shared-memory build
helper/canary. Do not duplicate either implementation.
