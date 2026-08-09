# Candidate-D native XCB/MIT-SHM capture: production-readiness review

**Review date:** 2026-08-08
**Requested candidate:** `/private/tmp/modal-computer-use-candidate-d-sdk`, commit `7b11efd`
**Current inspected worktree:** `/private/tmp/modal-computer-use-candidate-d-sdk`, `47b61ab`
**Public contract under review:** `await computer.screenshots.full()` (hidden cursor, full-size PNG)

## Decision

Do not make Candidate-D the default capture arm for every session. It is a useful benchmark
control and a promising opt-in Linux/Xvfb experiment, but it has release-blocking gaps in
readiness, distribution, format negotiation, lifecycle recovery, and failure handling. The
measured 30-sample result is performance evidence only; it is not operational evidence for a
default.

The candidate worktree was rebased after the requested commit. The native implementation and its
tests are unchanged between `7b11efd` and the inspected `47b61ab`; the line references below use
the inspected files. The local integration branch (and the current local tracking view of origin)
is now `e61191d`; `f6b9ade` is an earlier ancestor. This report is a review of the requested
candidate, not a claim that later root commits fix any finding. Rebase/delivery decisions should
target `e61191d`, not the candidate's stale `24faf4f`/`6295a65` ancestry.

### Evidence reviewed

- `AGENTS.md`, `CONTEXT.md`, and the consolidated research notes, especially
  [`current-architecture-rust-seams-2026-08-08.md`](current-architecture-rust-seams-2026-08-08.md),
  [`near-instant-general-screenshot-2026-08-08.md`](near-instant-general-screenshot-2026-08-08.md),
  and [`native-general-screenshot-candidate-expansion-2026-08-08.md`](native-general-screenshot-candidate-expansion-2026-08-08.md).

- All Candidate-D Python/Rust code, packaging/image definitions, routes, supervisor/lifecycle
  code, tests, benchmark runners, raw benchmark JSON, and the benchmark report.
- Targeted Python tests: `tests/test_native_capture.py`, `tests/test_screenshot_namespace.py`,
  and `tests/test_x11_backend.py` — **94 passed, 1 skipped**. These are fakes and route tests;
  they do not load the Rust extension or an X server.
- Host `cargo test` for the PyO3 crate: linking fails on macOS with unresolved Python symbols
  under the extension-module configuration. A Linux cross-build also needs explicit PyO3 cross
  interpreter configuration. This is a distribution/test-workflow gap, not proof that the Linux
  crate cannot compile.
- The Modal benchmark: 30 samples/arm, fixed 1024x768 fixture, one process/connection per arm,
  zero observed fallbacks, and successful context cleanup. It does not exercise real Chromium,
  concurrency, cancellation, resize/restart, long soaks, RSS, FD count, SHM resource count, or
  failure injection.

Severity used below: **P0** means a default cutover must stop; **P1** means a release must not
ship the arm broadly until the fix and gate exist; **P2** means a bounded follow-up needed before
calling the implementation production quality. “Fix” items are intentionally local to the
controller, native session, packaging, or tests; they do not propose a control-plane rewrite.

## P0 — default-cutover blockers

### P0-1 — readiness can report healthy without testing the native path

**Evidence:** `src/modal_computer_use/daemon/desktop/screenshots.py:140-158` calls
`capture_bytes()` with `show_cursor=True`. Native is eligible only when the cursor is hidden and
the format/scale are PNG/1.0 (`screenshots.py:422-424,536-538`), so this probe uses the MSS/file
path even after `_ensure_native()` constructs the Rust session. An explicit native arm can pass
`/readyz`, then the first public `full()` (which asks for a hidden cursor) reaches
`native_capture.capture_png()` and fails. `ReadinessCache` then caches that false success for its
TTL (`daemon/readiness.py:22-45`).

**Impact:** readiness is an advertised safety gate, but it does not establish that the selected
backend can capture the public contract. The first user call can become a 500 (explicit arm) or a
surprising MSS fallback (auto arm), and a cache can continue to report ready while the native arm
is disabled. This is a correctness and availability failure, not merely a diagnostic omission.

**Bounded fix/gate:** make the probe invoke the exact hidden-cursor full-PNG native-eligible path;
validate PNG signature, decoded dimensions, and selected backend; then cache readiness only after
that result. On a probe error, explicit selection must fail readiness with the native error and
auto must atomically mark native unavailable and select MSS for the session. Add a real Linux/Xvfb
test that starts the extension, calls `/readyz`, then calls the literal public SDK method.

### P0-2 — the normal package/image does not ship or build the extension

**Evidence:** the wheel is Hatchling-only (`pyproject.toml:1-3,61-71`) and includes Python
packages, but no maturin/setuptools-rust build hook, native artifact, platform tag, or ABI policy.
The managed image installs X utilities and `libx11-6` but does not install/build the Rust crate or
declare the XCB/Shm runtime/development dependencies (`src/modal_computer_use/image.py:11-31,49-73`).
Only the disposable benchmark image manually installs build tools and XCB packages, runs rustup,
builds Cargo, and copies `lib_native_capture.so` into the Python package
(`scripts/benchmarks/native_capture_sdk_runner.py:41-99`).

**Impact:** a normal installation has no `_native_capture` module. `auto` silently measures MSS;
explicit native fails at startup. A copied untagged `.so` also has no promise for CPython minor
versions, architecture, libc, or system libxcb ABI. Promoting this arm as a default would make
success depend on an undocumented benchmark-only image recipe.

**Bounded fix/gate:** choose one supported distribution contract: build manylinux/musllinux
platform wheels with a pinned Rust/PyO3 toolchain and an explicit `abi3` policy, or build the
extension in the managed image with pinned packages and fail image construction if import/smoke
capture is unavailable. Add x86_64/aarch64 and supported CPython import/capture smoke jobs, verify
dynamic dependencies, and make the release image—not the benchmark runner—the source of the
artifact. Keep auto fallback, but do not call the native arm default until the artifact is present
on every supported image.

### P0-3 — format selection is a hard-coded Linux subset while public configuration permits more

**Evidence:** `native/native_capture/src/lib.rs:278-286` accepts only little-endian, 32-bit pixels,
depth 24/32, TrueColor, and exact BGRX masks. `connect()` also requires MIT-SHM version at least
1.2 and Linux FD-backed slots (`lib.rs:195-248,384-428`). The public `DesktopConfig` accepts
`display_depth` 8–32 (`src/modal_computer_use/config.py:22-36`), while daemon settings expose the
native selector without validating the desktop depth against the native capability
(`daemon/settings.py:149-155,222-232`). X servers may be remote or configured with a different
visual/depth even when the process is Linux.

**Impact:** explicit native selection fails for valid SDK configurations (16/30-bit or a
non-TrueColor visual), remote X displays where MIT-SHM is unavailable, and non-Linux supported
environments. If someone changes the default selector, this is a session-wide outage. If auto
selection is intended to protect users, its capability probe and sticky fallback are not yet
complete (see P1-2).

**Bounded fix/gate:** negotiate and record the root geometry, visual, depth, byte order, masks,
and MIT-SHM capability before selecting native; select native only for an explicitly supported
tuple and otherwise use MSS. Either constrain a native-only deployment's configuration with a
clear validation error or implement the missing visual/depth conversion. Add a capability matrix
for Xvfb depth 16/24/30/32, TrueColor/DirectColor, byte order, absent/old MIT-SHM, local/remote
display, and Linux architectures. The default must remain MSS for unsupported tuples until every
public configuration is covered.

### P0-4 — native startup/capture failures are not a safe default fallback

**Evidence:** selection intentionally raises for explicit native when the module is missing
(`daemon/desktop/native_capture.py:58-79`). A capture failure closes the native object, but
re-raises for every non-`auto` request (`screenshots.py:227-237`). Auto merely falls through to MSS
for that call; `_capture_resolution` remains native, so each later request calls `_ensure_native()`
again and retries the failing arm. Startup fallback stores a reason, but no sticky disable or
capability state is exposed (`screenshots.py:100-130`, `native_capture.py:68-79`).

**Impact:** making native the default equivalent to making transient XCB/SHM failures user-visible
500s. Auto can oscillate between a dead X server and MSS, adding latency and hiding a persistent
degradation. Fallback can also occur after the native session has returned an invalid/corrupt
frame, without proving that the native session is quarantined for the rest of the generation.

**Bounded fix/gate:** define policy per session generation: after attach, X error, timeout, format,
or validation failure, close the session, mark native unavailable with a bounded reason, and use
MSS for all subsequent eligible calls. Explicit mode should fail readiness/startup, not fail the
first user frame after readiness. Emit a backend state and fallback reason in sanitized metrics;
add fault-injected tests for startup, first frame, mid-run X disconnect, and repeated calls.

## P1 — release blockers for any broad opt-in

### P1-1 — stale XCB connection and SHM attachments survive Xvfb restart/resize

**Evidence:** `NativeCaptureSession` caches `Connection`, root XID, width/height, and visual
format (`native/native_capture/src/lib.rs:121-133,195-306`). The controller holds it for the
backend lifetime (`screenshots.py:78-138`). `/v1/computer/restart` only restarts the supervisor
(`daemon/routes/lifecycle.py:60-66`), and the supervisor can stop/start Xvfb without notifying the
controller (`daemon/supervisor.py:77-109`). App shutdown stops the supervisor before
`backend.close()` (`daemon/app.py:110-127`), so native detach is sent after the X server is gone.

**Impact:** after an Xvfb restart, the cached connection can be closed, root IDs can be invalid,
and attached segments refer to the old server. A changed root geometry/visual is not re-queried;
the region bounds and stride are based on stale values. Resize, restart, and supervisor recovery
are normal lifecycle operations, not exotic tests.

**Bounded fix/gate:** give the supervisor a generation callback or have the controller invalidate
and close native before stop/restart, then reconnect and re-probe after Xvfb start. Query root
geometry/visual every generation (and recover on a resize/X error). Close/detach while the X
server is alive; wait for checked detach/flush or explicitly record that the server generation is
already dead. Add restart, resize, stop/start, and shutdown-order integration tests.

### P1-2 — MIT-SHM/XCB protocol and reply validation are incomplete

**Evidence:** `GetImage` waits synchronously and checks only `reply.size()` against the requested
slot (`native/native_capture/src/lib.rs:332-358`). It does not check reply depth, visual, byte
order, bits-per-pixel, scanline pad, or server-provided row stride. The BGRA conversion is hard
coded (`lib.rs:110-119`) after an initial root-format check; it assumes the format cannot change.
The segment is attached writable (`read_only:false`) and mapped read-only, which is appropriate for
server writes, but the lifetime/format contract is not revalidated per reply.

**Impact:** a server or root visual change can produce a correctly sized but incorrectly interpreted
PNG. The Python adapter accepts any non-empty `bytes` (`daemon/desktop/native_capture.py:99-114`),
and SDK validation checks hashes/metadata but does not decode PNG pixels. This is silent visual
corruption, potentially worse than an explicit fallback.

**Bounded fix/gate:** validate every reply's depth/visual/format/stride and the exact region byte
count; reject any mismatch before reading the slot. Validate PNG signature and decode dimensions
at the controller boundary (or use a cheap bounded decoder in a test/shadow path), and quarantine
native on mismatch. Compare every returned frame against MSS for representative fixtures and
verify region edge/overflow cases.

The MIT-SHM protocol requires capability/version discovery, server-defined image layout, and
ordering/lifetime around `GetImage`, detach, and unmap; see the [MIT-SHM specification](https://xorg.freedesktop.org/archive/X11R7.7/doc/xextproto/shm.html),
[XShm manual](https://www.x.org/archive/X11R7.5/doc/man/man3/XShm.3.html), and
[XCB Shm API](https://xcb.freedesktop.org/manual/group__XCB__Shm__API.html). Those requirements
are why a size-only check is not sufficient.

### P1-3 — blocking XCB wait and PNG encode have no explicit operation deadline

**Evidence:** `capture_png()` runs the synchronous PyO3 method; Rust calls
`Connection::wait_for_reply()` and encodes before returning (`native/native_capture/src/lib.rs:332-358`
and `lib.rs:143-175`). There is no XCB poll deadline, cancellation flag, or worker boundary. The
daemon route's budget/lock surrounds the await (`daemon/routes/execution.py:75-126`), but cannot
interrupt a blocking call that is already inside the event-loop thread.

**Impact:** a wedged X server, broken socket, or pathological PNG operation can hold the daemon
event loop and the screenshot/input readiness lock, delaying health, actions, cancellation, and
other sessions. Existing single-request benchmark latency does not exercise this failure mode.
The existing route-level `asyncio.wait_for()` around action-plus-screenshot can only observe a
timeout once a synchronous PyO3 call yields; it cannot interrupt that call.
MSS also performs synchronous capture, so this is not evidence of a baseline MSS regression; it is
the native arm's separate XCB connection/resource failure mode that needs an explicit policy before
it can become the default.

**Bounded fix/gate:** do not move this PyO3 object to `asyncio.to_thread()` as a first fix: the
class is explicitly unsendable, and moving the XCB connection/slot lease would create a new
ownership and cancellation problem. Preserve the existing route/input lock. First add a fault
injection test that stops or wedges Xvfb and measures event-loop/health behavior. If the production
contract requires a hard per-capture deadline, implement XCB polling/timeout and connection
quarantine inside the Rust owner thread; define what cancellation means after `GetImage` is
emitted (never replay a frame whose server request may still be live). A worker is a later design
only after the native object has an explicit Send/ownership contract. Add hung-X, cancellation, and
supervisor-recovery tests before allowing a default cutover.

### P1-4 — two-slot `Arc` ownership is implicit and untested under shared consumers

**Evidence:** Python always requests two slots (`native_capture.py:85-95`). Rust considers a slot
free solely when `Arc::strong_count() == 1` (`native/native_capture/src/lib.rs:361-369`); the frame
lease is an `Arc` dropped after encode (`lib.rs:55-70`). There is no explicit lease type or
per-session mutex in the native class. However, the public screenshot route takes
`ready_input_lock` (`daemon/routes/execution.py:103-117`), and the action/fused screenshot path
holds the same `state.input_lock` for both action dispatch and the post-action capture
(`daemon/actions/batch.py:291-299,582-635`). Thus normal full, region, hot, observation, and
fused route calls are serialized today; the report has no evidence of a current two-call slot race.
The untested surfaces are direct controller/native calls and readiness probing concurrent with a
capture.

**Impact:** two slots double root-sized SHM memory and expand lifetime/race surface without evidence
that they improve the serialized public one-shot call. A retained frame or an unsupported direct
concurrent caller can still exhaust both slots and receive an error rather than wait. A slot must
stay leased through PNG row conversion and output ownership; accidental early release would allow
server overwrite.

**Bounded fix/gate:** for the current public route, one slot plus the existing route lock is
sufficient and is the minimal safe production shape; do not add a second async lock or worker just
to prove concurrency that the route forbids. Either construct one slot now or explicitly document
the two-slot experiment as benchmark-only. If a future direct/concurrent surface is required,
introduce an explicit RAII lease and bounded wait, then test concurrent full/region/fused/hot/
observation calls and retained-frame lifetimes. Keep the route lock authoritative.

### P1-5 — resource cleanup is not deterministic or observable (but successful AttachFd is not an automatic FD leak)

**Evidence:** `Drop` sends checked `shm::Detach` but ignores `check_request()` and `flush()` results
(`native/native_capture/src/lib.rs:372-381`). Python `close()` marks the wrapper closed before
calling extension cleanup and suppresses future retries (`native_capture.py:116-125`). The Rust
`close` method only sets a flag (`lib.rs:135-141`); actual detach/unmap waits for object
destruction. App shutdown closes the backend after stopping Xvfb as noted in P1-1.

The `AttachFd` path explicitly closes the fd on `ftruncate`, `mmap`, and attach-check failures
(`lib.rs:384-422`). On a successful `xcb_send_request_with_fds*`, XCB owns the sent descriptors and
closes them eventually; the [XCB API declaration](https://codebrowser.dev/qt6/include/xcb/xcbext.h.html#87)
documents that ownership transfer. Therefore the review does **not** claim that every successful
attach leaks a memfd. The remaining risk is failure/teardown observability, delayed closure, and
server-side SHM resource lifetime. One small explicit failure hole remains: if
`NonNull::new(mapped)` returns `None` at `lib.rs:411`, the `?` path returns without closing the
already-open fd or unmapping; this should be cleaned up even though a null `mmap` result is unusual.

**Impact:** a failed detach can be invisible; a wrapper close cannot be retried after a transient
error; and a process can accumulate mappings, XCB socket FDs, or server SHM segments across
reconnects. No benchmark records `/proc/self/fd`, RSS, mapped bytes, or X resource counts.

**Bounded fix/gate:** make close idempotent but report/record cleanup status, detach before Xvfb
stop, flush and await checked errors while the connection is alive, and unmap only after the last
frame lease. Run a Linux soak with per-iteration FD/RSS/`/proc/$pid/maps` and X resource checks,
including attach failure, server death, cancellation, repeated restart, and process shutdown.

### P1-6 — native is opened for ineligible variants and shared consumers are only partially covered

**Evidence:** `capture_bytes()` calls `_ensure_native()` before checking whether the request can use
native (`screenshots.py:213-224`). The native condition checks only cursor-hidden and PNG/scale 1
(`screenshots.py:220-224`); it does **not** check the internal `prefer_native_png` flag. Consequently
the public structured/artifact `capture()` path can also run native for a hidden-cursor PNG even
though it calls `capture_bytes()` with the default `prefer_native_png=False` (`screenshots.py:160-203`).
Cursor-visible, JPEG/WebP, and scaled variants bypass native; `capture_raw_pixels()` remains MSS
(`screenshots.py:292-329`). The native controller is reachable from full/region raw, action raw
screenshot, hot, and observation paths through `X11DesktopBackend` (`daemon/desktop/x11.py:1009-1046`),
but there are no native route tests for that matrix.

**Impact:** an otherwise valid JPEG, cursor-visible, or scaled session can fail merely because an
optional native constructor was attempted; structured/artifact PNG can unexpectedly use native and
therefore inherit its lifecycle/fallback behavior. Resources are consumed even when native cannot
accelerate the request. More importantly, one consumer may return native pixels while another
silently uses MSS, with differences in cursor, region, metadata, and fallback semantics.

**Bounded fix/gate:** check eligibility before opening native and decide explicitly whether
structured/artifact PNG is in scope; either pass a capability-level preference through the common
controller or keep those routes on MSS. Define a backend matrix for full, region, fused action+raw,
hot-session, observation/keyframe, structured JSON, artifact storage, cursor-visible, scaled,
JPEG/WebP, and raw pixels. Add route-level parity/fallback tests for every eligible consumer.

### P1-7 — public region/geometry and memory limits are not coupled to native allocation

**Evidence:** Rust validates `u16` coordinates and cached root bounds
(`native/native_capture/src/lib.rs:310-331`), but Python daemon settings do not validate native
dimensions/depth as strictly as `DesktopConfig`; the native session allocates two root-sized slots
at startup (`lib.rs:288-306`). The SDK maximum resolution is 8.3 megapixels
(`config.py:28-35`), yet there is no native-specific memory budget.

**Impact:** at the maximum desktop size, two four-byte slots are approximately 66.4 MB (63.3 MiB) before the
RGB conversion, PNG output, Python response, and XCB/PyO3 overhead. Regions and malformed direct
daemon settings can exercise integer/stride edges. The benchmark's 1024x768 fixture does not
represent this memory pressure.

**Bounded fix/gate:** derive a checked native allocation budget from the configured pixel limit;
reject or fall back before attach when it would exceed the process budget. Use one slot until
throughput evidence justifies two. Test max-size full/region captures, repeated calls, OOM/attach
failure, RSS ceiling, and slot reuse.

### P1-8 — packaging/ABI/portability and test workflow are not reproducible

**Evidence:** Cargo uses semver dependencies (`pyo3 = 0.24.1`, `xcb = 1.5`, and `png`), but there
is no lock/build step in the Python package. The benchmark assumes Rust 1.91 and Debian packages
(`native_capture_sdk_runner.py:41-99`). `cargo test` for an extension-module crate fails to link
on macOS unless the documented PyO3 test configuration is used; cross-compiling needs a selected
target/Python ABI configuration.

**Impact:** a developer can get green Python fakes while the production wheel is missing or linked
against a different ABI. Linux glibc/aarch64, Python minor-version, libxcb version, and future
PyO3 changes are untested. This is a release reproducibility risk independent of screenshot
correctness.

**Bounded fix/gate:** pin Cargo dependencies/toolchain, choose `abi3` or per-CPython tags, define
the supported Linux/libc/architecture matrix, and provide a build/test command that works in CI
and the managed image. Run an actual Xvfb integration test against the built artifact; retain pure
Python tests for fallback and route contracts. PyO3's [building and distribution guide](https://pyo3.rs/main/building-and-distribution)
documents interpreter configuration, extension tags, and extension-module linker/test caveats.

### P1-9 — benchmark methodology establishes speed, not production safety

**Evidence:** `benchmark-data/native-capture-sdk-ab-30.md` and
`scripts/benchmarks/full_screenshot_sdk_harness.py:40-173` correctly time the literal public
`await computer.screenshots.full()`, decode parity through a callback, verify route/metadata/hash,
and require 30 samples. The runner uses one deterministic core-X11 fixture, one 1024x768 root,
one CPU, and separate arms; it does not use real Chromium and does not measure concurrency,
restart, cancellation, RSS/FD/SHM, or injected fallback. The daemon writes `total_ms` before the
`CapturedScreenshot` constructor computes SHA-256 (`screenshots.py:277-284`), so the reported
daemon total excludes hashing even though the public SDK timer includes receipt/validation.

**Impact:** the p50/p95/payload improvement can be real for the measured fixture and still miss
visuals, resource leaks, or tail latency under the supported workload. The “daemon save” is not a
complete daemon-stage attribution, and zero fallback in 30 clean calls is not a reliability rate.

**Bounded fix/gate:** time hash through response construction, randomize/interleave arms in the
same session where possible, and report confidence intervals plus CPU/RSS/FD/SHM/fallback/error
counts. Add real Chromium/browser-layout and image-heavy fixtures; run full/region/fused/hot/raw
matrix, resize/restart, cancellation, failure-injection, and a multi-hour soak. Keep the existing
promotion thresholds (complete SDK p50/p95, payload, daemon absolute saving, exact parity) and
make operational gates mandatory rather than treating the 30 clean samples as sufficient.

## P2 — bounded correctness, observability, and maintainability gaps

### P2-1 — adapter validation is weaker than its error message

`NativeCaptureSession.capture_png()` accepts any non-empty `bytes` and calls it a PNG
(`daemon/desktop/native_capture.py:99-114`). It does not check the PNG signature, IHDR dimensions,
color type, or decoded size. SDK validation checks the body hash and metadata but not image decode.
Add a bounded signature/IHDR check on the daemon path and independent decoder tests for malformed,
truncated, wrong-dimension, and wrong-color-type outputs. Do not log screenshot bytes.

### P2-2 — module import failures can escape auto fallback

`_load_module()` catches only `ImportError`, `ModuleNotFoundError`, and `OSError`
(`native_capture.py:132-144`). An ABI initialization exception, unexpected `RuntimeError`, or
loader-specific exception can escape selection instead of resolving to MSS. Catch and classify
known extension-load failures, preserve the exception type/reason in sanitized diagnostics, and
test bad ABI, missing shared library, and constructor exceptions separately.

### P2-3 — close state prevents retry and hides cleanup errors

The Python wrapper sets `_closed=True` before invoking native close
(`native_capture.py:116-125`); if close raises, a second close is a no-op. Rust's `close` only
flips a flag and relies on `Drop` for detach. Use a two-phase close state (`closing`/`closed`) or
an explicit result so callers can retry/record cleanup while maintaining idempotence.

### P2-4 — no native capability/fallback telemetry

Response attribution gives a backend string, but the controller does not expose selected format,
native generation, fallback reason, attach/reply error class, slot wait, or resource counts. The
benchmark's fallback counter only looks for the substring `fallback`
(`full_screenshot_sdk_harness.py:121-132`) and can miss command backends such as `maim`/`scrot`.
Add structured, secret-free counters and timing fields for native ready/active/fallback/disabled,
X error, encode, slot wait, and cleanup outcomes. Keep display names, tokens, screenshot bytes,
and typed text out of logs per `AGENTS.md`.

### P2-5 — no visual/cursor/region parity contract beyond the one fixture

Native intentionally excludes cursor composition, scale, non-PNG formats, and raw pixel callers;
the Python route still reports cursor metadata. There is no test that a cursor-visible frame,
cursor position, region coordinate space, or structured/artifact response remains identical when
the native session is present. Add explicit tests that native initialization cannot alter those
paths and that hidden-cursor native output has exact RGB parity with MSS for text, alpha-like UI,
browser, image-heavy, and high-entropy fixtures.

### P2-6 — direct API and error boundaries are under-specified

The PyO3 class is marked unsendable and stores a mutable XCB connection/encoder
(`native/native_capture/src/lib.rs:121-133`), but the Python wrapper does not document that direct
calls must be serialized. Region coordinates are narrowed to XCB's signed/unsigned widths in Rust;
the route-level pixel budget and region validation are the only current guard. Define the supported
ownership/threading contract, return typed error categories (unsupported format, unavailable
display, timeout, corrupt reply, resource exhaustion), and test all overflow/zero/negative cases.

### P2-7 — performance tuning is coupled to payload policy without a content distribution

Adaptive filtering and fixed-Up are separate native arms. The benchmark shows fixed-Up lower latency
but a larger payload; neither arm has a real desktop/content distribution or a confidence interval
for that tradeoff. Keep both as named experiment arms until the production fixture corpus and
transport cost policy establish the acceptable payload bound. Do not silently switch compression
policy as part of a default cutover.

### P2-8 — tracked benchmark report exposes private run identity and developer paths

The candidate report `benchmark-data/native-capture-sdk-ab-30.md:5-15` records a private
`/private/tmp` source path, a developer virtualenv path, and a Modal app URL/resource identifier.
It does not expose screenshot bytes or bearer tokens, but these values violate the repository's
sanitized-artifact convention and make the evidence non-reproducible for another checkout. They
also create avoidable resource-discovery and provenance risk if the report is published broadly.

Remove private URLs and machine paths from tracked Markdown, retain source/image/toolchain/raw
artifact identity as sanitized hashes or stable public revisions, and keep deployment IDs and raw
logs in the ignored/private benchmark-results area. Add a sanitizer/pinning test before treating
the result as promotion evidence.

## Consumer and lifecycle coverage matrix

| Surface/variant | Native currently eligible? | Readiness/fallback/test status | Promotion requirement |
| --- | --- | --- | --- |
| `await computer.screenshots.full()` raw PNG, hidden cursor | Yes, intended path | Fakes plus one clean 30-sample Modal fixture; probe does not exercise it | P0-1 probe, Linux integration, failure/restart/soak gates |
| Region raw PNG | Yes in controller | No real XCB edge/stride/visual tests | Region parity, bounds/overflow, resize tests |
| Action + raw screenshot / raw after action | Through shared controller | No native route-level matrix | Lock/cancellation/ordering and parity tests |
| Hot session / observation keyframe | Through shared controller where raw PNG | No concurrency or retained-frame tests | Multi-client slot/lock/stream soak |
| Structured JSON/base64 or artifact | Hidden-cursor PNG/scale 1 may use native; other variants do not | No route-level native matrix; `prefer_native_png` is ignored by the native condition | Decide scope, then parity/fallback and lazy-eligibility tests |
| Cursor-visible | No | Probe mistakenly uses this path and can mask native failure | Separate cursor/file fallback contract |
| Scaled, JPEG, WebP, raw pixels | No | Native session may still be opened | Ensure no native startup side effect; parity tests |
| Resize/restart/stop-start | No safe generation handling | Cached connection/root/format | Invalidate/reconnect and shutdown-order tests |
| Depth/visual/remote/non-Linux | Rejected or unavailable | No public capability matrix | Explicit capability negotiation and fallback |

## Promotion gates

Candidate-D should remain benchmark-only until all of the following are demonstrated in CI and the
managed runtime image:

1. The built artifact is present and importable on every supported Python/Linux architecture and
   libc; dynamic XCB/Shm dependencies are pinned and audited.
2. `/readyz` executes the same hidden-cursor full-PNG native path as the public SDK call and
   reports native readiness truthfully.
3. A generation-aware controller reconnects after Xvfb restart/resize and detaches before server
   shutdown; cleanup errors are observable.
4. Native validates root/reply visual, depth, byte order, masks, stride, dimensions, PNG signature,
   and geometry, with quarantine-to-MSS behavior for any mismatch.
5. Explicit and auto selection have documented failure policy, sticky fallback, bounded reasons,
   and no retry storm.
6. Native calls have a cancellation/deadline strategy that cannot wedge the daemon event loop or
   input/readiness locks.
7. Full, region, fused, hot, observation, cursor, structured, artifact, scaled, JPEG/WebP, and
   raw-pixel consumers have route-level parity and fallback tests.
8. Concurrent calls, retained frame leases, attach failure, X disconnect, malformed replies,
   cancellation, OOM, and repeated restart are covered by integration tests.
9. A Linux soak records stable RSS, `/proc/$pid/fd`, mappings, and X SHM resources; no monotonic
   growth or unreleased segments are observed. The soak should verify the successful XCB FD handoff
   does not grow descriptors while also checking all failure paths.
10. The public A/B is interleaved over real Chromium and representative fixtures with corrected
    daemon timing (including SHA), confidence bounds, payload policy, and the existing exact-pixel
    and complete-SDK promotion thresholds.

## Primary sources and design references

- [MIT-SHM protocol specification](https://xorg.freedesktop.org/archive/X11R7.7/doc/xextproto/shm.html) —
  extension/version capability, server image layout, read/write attachment, and detach/unmap
  ordering.
- [XShm manual](https://www.x.org/archive/X11R7.5/doc/man/man3/XShm.3.html) — attach, image-read,
  synchronization, and detach semantics.
- [XCB Shm API](https://xcb.freedesktop.org/manual/group__XCB__Shm__API.html) and
  [XCB core API](https://xcb.freedesktop.org/manual/group__XCB__Core__API.html) — request/reply,
  checked-error, flush, and connection behavior.
- [XCB `xcb_send_request_with_fds` declaration](https://codebrowser.dev/qt6/include/xcb/xcbext.h.html#87) —
  successful sent-FD ownership transfers to XCB and descriptors are eventually closed; this is why
  this report treats FD cleanup as a failure/teardown/observability gate rather than claiming an
  unconditional memfd leak.
- [PyO3 building and distribution](https://pyo3.rs/main/building-and-distribution) — interpreter
  configuration, extension tags, and extension-module linker/test constraints.
- Repository constraints and prior evidence: [`AGENTS.md`](../AGENTS.md),
  [`CONTEXT.md`](../CONTEXT.md), [`current-architecture-rust-seams-2026-08-08.md`](current-architecture-rust-seams-2026-08-08.md),
  [`near-instant-general-screenshot-2026-08-08.md`](near-instant-general-screenshot-2026-08-08.md),
  and [`native-general-screenshot-candidate-expansion-2026-08-08.md`](native-general-screenshot-candidate-expansion-2026-08-08.md).

## Pass 2 — cross-review corrections and minimum production slice

This pass rechecked the first draft against the delivery/provenance review and the semantic
naming review. It narrows two recommendations so the report does not prescribe a larger
concurrency or threading redesign than the current routes need.

### Corrections and scope decisions

1. **Structured/artifact PNG correction.** The native branch is selected by
   `screenshots.py:220-224` from cursor/format/scale only; it does not test
   `prefer_native_png`. Therefore `X11ScreenshotController.capture()` can use native for a
   hidden-cursor PNG in structured or artifact storage even though that call passes
   `prefer_native_png=False` (`screenshots.py:160-203`). The corrected P1-6 above no longer claims
   those paths always use MSS. Before promotion, make this intentional: either include them in the
   native capability matrix and parity tests, or make eligibility depend on the preference/capability
   requested by the route.

2. **One slot is sufficient for the current public route.** A normal screenshot is wrapped by
   `run_screenshot_capture_with_timing()` and `ready_input_lock`
   (`daemon/routes/execution.py:89-117`). An action/fused screenshot holds the same
   `state.input_lock` across action dispatch and post-action capture
   (`daemon/actions/batch.py:291-299,582-635`). Hot/observation capture calls use that same
   screenshot wrapper. Consequently, public capture calls are serialized and there is no evidence
   that two slots improve the target call. The two-slot `Arc::strong_count()` scheme remains an
   untested direct-call/retained-frame behavior, but it is not a demonstrated route-level race.
   The minimal production shape is one slot plus the existing authoritative route lock; a second
   slot is a future throughput experiment.

3. **A worker thread is not the default timeout fix.** `NativeCaptureSession` is an unsendable
   PyO3 class holding an XCB connection and mmap leases (`native/native_capture/src/lib.rs:121-133`),
   so moving it with `asyncio.to_thread()` would require a new ownership and cancellation contract.
   Existing MSS capture is also synchronous. The must-have is an explicit policy and fault test for
   a wedged X server, not an automatic thread migration. If a hard deadline is required, implement
   polling/timeout and connection quarantine inside the native owner, or redesign the native object
   as a worker-owned resource with a tested abort protocol. The exact requirement is recorded in
   P1-3; do not claim that `asyncio.wait_for()` interrupts a synchronous PyO3 call.

4. **FD wording remains corrected.** Successful `xcb_send_request_with_fds*` transfers descriptor
   ownership to XCB, which eventually closes sent descriptors. The remaining P1-5 issue is ignored
   detach/flush/error state, delayed object destruction, server SHM lifetime, and the absence of FD
   soak evidence—not an unconditional memfd leak.

5. **Benchmark publication is a separate delivery gate.** The candidate JSON contains sample arms
   and cleanup but not the repository's accepted schema/provenance/source-image identity,
   schedule, raw-artifact binding, or gate decision. The delivery review found no native-specific
   validator/pinning test. Keep this evidence explicitly unpromotable until a validator and
   reproducible, sanitized provenance record exists; remove its private app URL and local machine
   paths before publication. This does not change the runtime P0/P1 findings.

### Must-fix versus future work

The bounded set required before a production opt-in—and therefore before any default change—is:

1. **Truthful readiness:** `X11ScreenshotController.probe()` must exercise the hidden-cursor,
   full-PNG native-eligible path, validate the result, and make auto fallback sticky for the X
   server generation.
2. **A real artifact:** build/import the extension in the managed image or publish tested wheels,
   with pinned Rust/PyO3/XCB dependencies and an architecture/libc/CPython support matrix.
3. **Capability negotiation:** select native only after MIT-SHM version, root geometry, visual,
   depth, byte order, masks, and stride are proven; unsupported settings must use MSS or fail
   readiness with a documented error.
4. **Generation-aware lifecycle:** invalidate/reopen the controller on supervisor start/restart,
   query geometry/format for each generation, detach while Xvfb is alive, and record cleanup
   failures.
5. **Reply/output validation:** validate reply format/depth/visual/stride/size and PNG signature/
   dimensions; quarantine native on any mismatch rather than returning plausible corrupt pixels.
6. **Failure policy:** startup/attach/X disconnect/reply/timeout failures close the session, record a
   bounded reason, and select MSS for the remainder of the generation in auto mode. Explicit mode
   must fail readiness rather than the first post-ready frame.
7. **Serialized ownership:** use one native slot for the currently serialized public route (or
   retain two only as a measured, documented experiment); ensure readiness probing cannot race a
   capture. Do not add a redundant worker or pool without a failing concurrency test.
8. **Resource and failure evidence:** Linux Xvfb integration plus attach/visual/reply/X-disconnect,
   restart/resize, cancellation policy, malformed PNG, and close-order tests; then an RSS/FD/mmap/
   SHM resource soak with no monotonic growth. The soak must measure successful FD handoff and all
   failure paths.
9. **Consumer contract:** explicitly decide whether structured/artifact hidden-cursor PNG joins
   the native capability. Test full, region, fused action+raw, hot, observation, cursor-visible,
   scaled, JPEG/WebP, structured, artifact, and raw-pixel behavior.
10. **Evidence provenance:** add a native benchmark schema/validator, source/image/toolchain
    identity, interleaved schedule, retained failures, and a gate decision before tracking the
    artifact as promotion evidence.

These are deliberately local seams: controller (`screenshots.py`), adapter/selection
(`native_capture.py`), Rust session (`native/native_capture/src/lib.rs`), supervisor lifecycle,
managed image/build, route locks, and benchmark validator. They do not require changing the SDK
method, lease/receipt protocol, or model-loop ownership.

The following are **future nice-to-haves**, not reasons to delay the minimal one-slot opt-in once
the must-fix list passes:

- a two-slot or queueing pool for genuinely concurrent direct/stream consumers;
- a worker-owned XCB connection or fully nonblocking XCB implementation if the fault test proves
  event-loop stalls and a hard deadline is required;
- a capability-level public token such as `lossless-frame`, with `xcb`, MIT-SHM, PNG filter, and
  `adaptive`/`fixed-up` retained only as private implementation/benchmark dimensions;
- native raw-pixel, cursor-composition, JPEG/WebP, or scaled output; and
- richer per-stage metrics (slot wait, X reply, encode, cleanup) after the minimum backend state,
  fallback reason, and resource counters are present.

### Exact seams and tests for the next bounded slice

| Seam | Required test/evidence |
| --- | --- |
| `X11ScreenshotController.probe`, `_ensure_native`, `capture_bytes`, `close` | Linux/Xvfb test: start supervisor, run readiness, call the literal public full raw PNG, then repeat after injected native failure; assert backend, dimensions, pixels, sticky fallback, and cleanup. Include structured/artifact PNG decision. |
| `NativeCaptureSession.connect`, `capture_region`, `Drop`/close | Rust integration on the target image: depth/visual/byte-order matrix, region corners/overflow, malformed size/depth/visual replies, attach failure, X disconnect, detach error, and PNG IHDR/pixel parity. |
| `Supervisor.start/restart/stop` and `X11DesktopBackend.ready` | Restart/resize generation test: old session is invalidated before Xvfb stop, a new session is probed after start, and shutdown detaches while the server is alive. |
| `daemon/routes/execution.py` and `daemon/actions/batch.py` locks | Concurrent public full/region/fused/hot/observation calls assert max one native capture in flight; readiness probe cannot overlap capture. This test justifies one slot; do not infer concurrency from the two-slot implementation. |
| `native_capture.py` selector/import/fallback | Missing module, bad ABI exception, unsupported visual, attach error, first-frame error, mid-generation X error, and repeated auto calls assert one bounded fallback reason and no retry storm; explicit mode fails readiness. |
| `pyproject.toml`, `image.py`, benchmark image | Build the exact managed/runtime image, import the extension, run one Xvfb capture, inspect dynamic dependencies, and repeat on each supported architecture/CPython/libc. |
| Native benchmark runner/artifact | Validator rejects missing schema/provenance/source-image/toolchain/raw SHA/schedule/failure data; interleaved real Chromium and deterministic fixtures retain failures and resource samples. |
| Linux resource soak | Repeated create/capture/close/restart/attach-failure cycles record `/proc/$pid/fd`, RSS, mappings, and X SHM resources; successful AttachFd descriptors eventually return to baseline and failed detach is visible. |
