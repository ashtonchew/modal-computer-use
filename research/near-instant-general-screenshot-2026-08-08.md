# Native acceleration for the general full-screenshot path

- Research date: 2026-08-08
- Repository revision inspected: `6295a65829e1`
- Rust PNG evidence worktree: `/private/tmp/modal-computer-use-rust-png`, revision `aaa2f80b1c1e`
- Candidate-C vertical slice: `/private/tmp/modal-computer-use-candidate-c`, revision `97d39e0`
- Candidate-D vertical slice: `/private/tmp/native-capture-slice`, revision `87c59a2`
- Candidate-D SDK slice: `/private/tmp/modal-computer-use-candidate-d-sdk`, revision `7b11efd`
- MSS-main control: `/private/tmp/mss11-control`, revision `6fa1c61`
- libdeflate control: `/private/tmp/libdeflate-slice`, revision `3a498c0`

## Decision summary

The optimization target is exactly:

```python
shot = await computer.screenshots.full()
```

with a 1024×768, full-resolution, lossless PNG; hidden cursor; unchanged decoded pixels,
dimensions, coordinate space, metadata, validation, and safety behavior; and the same Modal image,
region, resources, ingress, pooled connection, and public call.

The corrected thesis is narrower than “rewrite the daemon in Rust” and lower than an encoder-only
adapter:

```text
X11 root drawable
  -> persistent MSS-main or Rust XCB/MIT-SHM capture session
  -> leased BGRA/BGRX slot
  -> row-oriented channel conversion + PNG filtering/DEFLATE
  -> SHA-256 of the exact PNG bytes
  -> existing Python route, response, transport, and SDK validation
```

Python should continue to own Modal orchestration, lifecycle, leases, authentication, request
validation, budget and input locks, receipts, route schemas, fallbacks, metadata, and provider
adapters. Rust is a bounded data-plane module.

The current Rust worktree is useful evidence, not a cutover candidate. A newer candidate-C slice
now borrows MSS 10.2's `shot.raw` and streams one RGB row into the PNG encoder instead of building a
full RGB frame. In an audited, interleaved 1,000-call local run, C was pixel-exact and faster than A
and B on all five fixtures. On the browser-like repository fixture, however, A was 5.89 ms p50 and C
was 3.00 ms: a **2.89 ms** saving, below the fixed 5 ms daemon-side gate. The adjacent fixed-Up arm
was 2.74 ms but was not interleaved with A. A later interleaved custom fixed-Up C2 arm removed one
more row copy yet saved only 4.13 ms versus A. Encoder surgery alone still cannot satisfy the gate
from ordinary browser-like local evidence.

**Recommendation today: fixed-Up is the leading cutover candidate, but do not change defaults until
the remaining operational gates pass.** The exact public A/B now times the literal
`await computer.screenshots.full()` call through the existing route, ingress, pooled client, body
receipt, second SDK hash, metadata validation, and typed result. On 30 calls per arm, MSS was
24.896 / 26.364 ms p50/p95; adaptive was 10.924 / 13.326 ms; fixed-Up was
10.425 / 12.252 ms. Fixed-Up reduces complete p50 58.13%, improves p95 53.53%, saves 13.225 ms
daemon-side, grows median payload only 4.18%, returns exact pixels/metadata, uses the requested
backend on every sample, records zero fallbacks, and cleans up successfully. Adaptive also passes
all measured performance gates but has a less stable/content-dependent filter decision, so retain
it as the comparison arm rather than the first default candidate. The fixture is deterministic
core-X11 browser-like content, not Chromium; memory/FD soak, concurrency, cancellation, failure
fallback, and a real Chromium desktop remain unmeasured operational gates. Reject
candidate E for now: the existing OpenSSL-backed Python SHA is sub-millisecond for ordinary PNG
payloads, while the standalone slice's generic Rust SHA was slower. Ship only if every
preregistered gate passes.

XDamage, tile deltas, patch reconstruction, CDP, DOM/accessibility observations, and video codecs
are not part of this recommendation. They are isolated in the final appendix.

## Contract and evidence rules

The primary benchmark starts immediately before the `await` and stops only after the SDK returns a
validated, typed `Screenshot`. It therefore includes request construction, pooled HTTP, daemon
readiness and locking, capture, encoding, response construction, Modal ingress, body receipt, the
SDK's second SHA-256 pass, metadata validation, and model construction.

The fixed promotion gates are:

| Gate | Requirement |
| --- | --- |
| Complete public-SDK p50 | At least 20% lower |
| Complete public-SDK p95 | No more than 5% higher |
| Median PNG payload | No more than 10% larger |
| Daemon-side absolute saving | At least 5 ms |
| Parity | Exact decoded pixels, dimensions, coordinate space, metadata semantics, and safety behavior |
| Operations | No readiness, fallback, memory, cleanup, or concurrency regression |

The gates must not be weakened after results are visible. PNG byte identity is not required: PNG
filtering and DEFLATE can produce different legal byte streams. The daemon header must remain the
SHA-256 of the exact returned PNG, and the SDK must independently verify it.

Evidence is labeled as one of:

- **Measured, matching operation:** the complete 1024×768 full-PNG raw request or a stage header
  from that request.
- **Measured, adjacent:** a local microbenchmark or a different public surface useful for sizing a
  stage, but not proof of the headline call.
- **Estimated:** a range derived from measured stages. Estimates are not promotion evidence.
- **Unmeasured:** a gap that the A/B harness must instrument.

The extended primary-source candidate survey is in
[`native-general-screenshot-candidate-expansion-2026-08-08.md`](native-general-screenshot-candidate-expansion-2026-08-08.md).

## 1. Exact general-path route map

### Public async call

`AsyncScreenshotsNamespace.full()` defaults to `format="png"`, `scale=1.0`,
`show_cursor=False`, `processing="auto"`, and `storage="inline"`. Inline storage takes the raw
binary route:

1. [`namespaces/screenshots.py`](../src/modal_computer_use/namespaces/screenshots.py) builds the
   request payload.
2. It awaits `AsyncDaemonClient.post_bytes_with_headers("/v1/screenshots/full/raw", ...)`.
3. [`client.py`](../src/modal_computer_use/client.py) dispatches through `_request` and returns
   `response.content` and headers.
4. [`transports/http.py`](../src/modal_computer_use/transports/http.py) uses one long-lived
   `httpx.AsyncClient`; HTTPX clients reuse pooled connections rather than opening a connection per
   call ([HTTPX client documentation](https://www.python-httpx.org/advanced/clients/)).

### Daemon request and serialization

The `/v1/screenshots/full/raw` handler in
[`daemon/routes/screenshots.py`](../src/modal_computer_use/daemon/routes/screenshots.py):

1. requires inline storage and validates options and the pixel budget;
2. enters `run_screenshot_capture`, which checks readiness and budget, takes the ready input lock,
   reserves the screenshot, invokes the operation, enforces the budget, and marks the desktop ready;
3. calls `backend.screenshot_bytes(options, include_cursor_position=True,
   prefer_native_png=True)`;
4. builds the `x-computer-use-*` headers; and
5. returns a Starlette `Response` with the PNG bytes as its body.

Starlette 1.5.1's `Response.render()` returns an existing `bytes` or `memoryview` rather than
encoding it again, and its ASGI call sends that body object
([upstream source](https://github.com/Kludex/starlette/blob/1.5.1/starlette/responses.py)). That
does not prove the server, kernel, tunnel, or client makes no copies; those later boundaries remain
unmeasured.

### Screenshot controller

`X11DesktopBackend.screenshot_bytes()` delegates to `X11ScreenshotController.capture_bytes()` in
[`daemon/desktop/screenshots.py`](../src/modal_computer_use/daemon/desktop/screenshots.py):

1. `_MSSCaptureSession` reuses one `mss.MSS(display=..., backend="xshmgetimage")` object.
2. `_mss.grab()` captures either the full desktop or requested region.
3. For cursor-hidden, PNG, scale-1, `prefer_native_png=True`, `_encode_mss_png()` reads
   `capture.shot.rgb` and calls `mss.tools.to_png(..., level=1)`.
4. Python builds `CoordinateSpace`.
5. The full raw route separately queries cursor position even though the cursor is not drawn; that
   is required response metadata and must remain.
6. `CapturedScreenshot` computes SHA-256 over the encoded PNG.

The current `timings_ms["total_ms"]` is assigned before the `CapturedScreenshot` constructor
evaluates `sha256_bytes(data)`. The public timing header therefore does **not** include the daemon's
PNG hash. That instrumentation gap must be fixed in the benchmark branch before attributing all
daemon time.

### SDK receipt and validation

After HTTPX has collected the body into `response.content`, `_screenshot_from_binary_response()`
in [`namespaces/screenshots.py`](../src/modal_computer_use/namespaces/screenshots.py) validates:

- non-empty body and exact `image/png` media type;
- positive width and height and exact body length;
- a 64-character lowercase SHA-256 header and an independent SHA-256 of the returned PNG;
- timezone-aware capture time;
- coordinate-space dimensions;
- exact requested cursor visibility and bounded cursor position;
- finite, nonnegative timing metadata; and
- non-empty capture-backend attribution.

Only then does it construct the byte-backed `Screenshot`. It does not decode the PNG. This entire
validation step is inside the headline latency boundary.

### Shared general consumers

A controller-level native session can benefit every eligible raw-PNG call without changing route
or SDK contracts:

| Consumer | Shared call |
| --- | --- |
| Default typed full screenshot | `/v1/screenshots/full/raw` -> `screenshot_bytes(..., prefer_native_png=True)` |
| Full raw bytes | Same route and controller call |
| Regional raw bytes | `/v1/screenshots/region/raw` -> same controller with a region |
| Fused action plus raw screenshot | `/v1/actions/run/raw-screenshot` -> ordered actions -> `screenshot_bytes(..., prefer_native_png=True)` |
| Raw screenshot-after actions | `run_with_screenshot_bytes()` -> same controller |
| Hot-session screenshot | Hot WebSocket request -> same controller |
| Hot action plus screenshot | Hot ordered action path -> same `run_with_screenshot_bytes()` capture |

Structured JSON/base64 or artifact paths call `backend.screenshot()` without
`prefer_native_png=True`; cursor-visible, scaled, JPEG/WebP, file fallback, and mock-backend paths
also bypass the eligible seam. They remain on the existing implementation unless separately
specified and tested. The primary contract in this note is the default inline call.

## 2. MSS 10.2 and the actual copy chain

MSS 10.2 already contains the important persistent setup. Its XCB/XShm backend allocates a
root-sized memfd/mmap, attaches it once, and reuses it. It queries the MIT-SHM extension and falls
back to XGetImage when shared memory is unavailable. The release's local 4K tight-loop benchmark
improved from 46.2 to 9.48 ms, but that is an upstream environment-specific capture benchmark, not
this SDK's latency ([MSS 10.2 release notes](https://python-mss.readthedocs.io/latest/release-history/v10.2.0.html)).

The remaining per-request copies are explicit in upstream source:

1. `xcb_shm_get_image` writes into the reused mmap.
2. `_grab_xshmgetimage()` copies the requested range into a fresh `bytearray` with
   `bytearray(memoryview(self._buf)[:new_size])`
   ([MSS 10.2 XShm source](https://github.com/BoboTiG/python-mss/blob/v10.2.0/src/mss/linux/xshmgetimage.py)).
3. `ScreenShot.rgb` allocates `width * height * 3`, performs the BGR-to-RGB slice assignments, and
   freezes the result as `bytes`. `ScreenShot.bgra` also returns `bytes(self.raw)`, which is another
   full-frame copy
   ([MSS 10.2 screenshot source](https://github.com/BoboTiG/python-mss/blob/v10.2.0/src/mss/screenshot.py)).
4. `mss.tools.to_png()` constructs filter-0 scanlines with a per-row list and `b"".join`, calls
   native zlib at level 1, then joins PNG chunks into the output
   ([MSS 10.2 PNG source](https://github.com/BoboTiG/python-mss/blob/v10.2.0/src/mss/tools.py)).

For 1024×768 this means approximately:

| Object | Size |
| --- | ---: |
| XShm BGRA/BGRX slot | 3,145,728 bytes |
| MSS Python `bytearray` snapshot | 3,145,728 bytes |
| Optional `.bgra` `bytes` result | 3,145,728 bytes |
| Packed RGB staging | 2,359,296 bytes |
| Filter bytes plus RGB scanlines | 2,360,064 bytes |
| Typical measured PNG body | about 343–392 KiB |

Not all objects are simultaneously live in every arm, and the PNG/zlib libraries have their own
scratch state. Peak RSS and allocation counts must be measured rather than inferred from this
ledger.

The MIT-SHM protocol itself only changes how the X server and client share image storage;
`GetImage` still reads the drawable into that storage, and the client must manage attachment,
format, and lifetime ([MIT-SHM specification](https://xorg.freedesktop.org/archive/X11R7.7/doc/xextproto/shm.html),
[XShm manual](https://www.x.org/archive/X11R7.5/doc/man/man3/XShm.3.html)). A Rust session can remove
MSS/Python snapshots, not the X server's framebuffer read.

## 3. What the Rust PNG worktree proves—and does not prove

The worktree adds a PyO3 crate using `png 0.18.1`, a Python encoder-selection seam, route
attribution, tests, and a local benchmark. The default remains `python`; `auto` and `rust` are
experimental selections.

### Eligible worktree paths

The Rust arm is selected only when all of these are true:

- the X11/MSS fast path succeeded;
- the caller passed `prefer_native_png=True`;
- the format is PNG;
- scale is 1.0;
- the cursor is hidden; and
- startup encoder resolution selected Rust.

That covers default inline `screenshots.full()`, `full_bytes()`, regional raw bytes, raw action
screenshot-after, and hot raw screenshot operations. It bypasses structured JSON/artifact/auto
storage, cursor-visible capture, scaled output, JPEG/WebP, MSS failure/file fallback, and mock
backends.

### Copies that remain

The branch calls `memoryview(capture.shot.bgra)`. In MSS 10.2, `.bgra` is `bytes(self.raw)`, so the
branch pays both the XShm-to-`bytearray` snapshot and a new 3.0 MiB BGRA `bytes` copy before PyO3
sees the frame. The Rust function then:

1. validates and borrows the contiguous PyBuffer;
2. allocates a full 2.25 MiB RGB `Vec` and pushes three bytes per pixel;
3. lets the `png` crate filter and compress it into another `Vec`; and
4. calls `PyBytes::new(py, &output)`, which copies the Rust vector into Python-owned bytes.

PyO3 documents that `PyBuffer::as_slice()` can borrow a compatible C-contiguous buffer, while
`PyBytes::new()` copies its input; it also warns that borrowed buffers can be mutated by Python or
another extension, so a detached-GIL native consumer needs exclusive ownership
([PyBuffer](https://pyo3.rs/main/doc/pyo3/buffer/struct.pybuffer),
[PyBytes](https://docs.rs/pyo3/latest/pyo3/types/struct.PyBytes.html)).

The current arm therefore proves a codec seam and decoded-pixel parity. It does not prove native
capture ownership or zero-copy processing.

### Local encoder-only results

The 1,000-call, 50-warmup, 1024×768 local artifact
`/private/tmp/rust-png-screen-1000.json` reported:

| Fixture | Python p50 | Rust p50 | Direct p50 saving | Payload delta |
| --- | ---: | ---: | ---: | ---: |
| Flat UI | 5.70 ms | 2.03 ms | 3.66 ms | +3.27% |
| Text UI | 6.78 ms | 2.74 ms | 4.03 ms | -54.12% |
| Image-heavy | 11.68 ms | 4.86 ms | 6.82 ms | -82.94% |
| High entropy | 49.73 ms | 16.13 ms | 33.59 ms | +0.06% |

All decoded-pixel checks passed. The selected Rust arm used adaptive filtering plus level-1
DEFLATE, whereas MSS uses filter 0 plus zlib level 1; payloads are therefore intentionally not byte
identical. Flat and text fixtures missed the fixed 5 ms absolute saving gate. The benchmark records
`passes=false` and `cutover=false`, and it did not measure the complete public call.

That original branch is stale relative to the inspected repository. The separate candidate-C slice
was instead built directly on revision `6295a65829e1`; its final benchmark commit is `97d39e0`, leaves
production defaults and routes untouched, and passed its focused Python, Ruff, Cargo-check, and
benchmark-contract verification. Its harness times the literal default call with
`processing="auto"`, performs independent decoded-pixel parity after the timed await, and fails on
cleanup errors. Candidate C is deliberately not wired into a live daemon in that slice, so it has
no Modal public-call result.

## 4. Measured bottleneck table

The table deliberately does not make an additive model from unrelated artifacts.

| Stage | Evidence | p50 / p95 or range | What is actually included |
| --- | --- | ---: | --- |
| Complete raw full-PNG, same-region Modal runner | Measured, matching raw operation; 30 calls | 38.60 / 40.05 ms | Pooled HTTP/1.1, 1024×768 391,587-byte PNG, daemon and residual; `screenshot_api="raw_bytes"`, not the typed async `full()` validation |
| Backend timing within that run | Measured, matching stage header | 24.07 / 24.58 ms | Current-style capture-controller total; last call had 0.96 ms capture and 21.71 ms encode |
| Post-backend residual within that run | Measured subtraction in the same artifact | 14.87 / 16.12 ms | Readiness/lock and route work not in backend header, daemon hash, ASGI/server, ingress, body transfer, HTTPX; not “pure network” |
| Complete optimized public screenshot | Measured, adjacent historical public call; 30 calls | 37.25 / 48.76 ms | Same-region Function/target, attested tunnel, pooled HTTP/1.1, 1024×768 PNG; synchronous SDK, predates exact async typed call |
| External public full-PNG | Measured, matching raw operation but different caller topology | 84.24 / 90.13 ms | Same 391,582-byte payload; backend 22.76 / 23.27 ms and residual 61.27 / 67.08 ms |
| Representative controller capture | Measured, matching raw operation | 0.88–1.19 ms examples | XShm request/reply **plus MSS snapshot and Python object**, not pure XShm |
| Representative controller encode | Measured, matching raw operation | 19.15–21.71 ms examples | `.rgb` materialization, filter-0 scanline construction, and zlib level 1 combined |
| MSS raw RGB copy | Measured, adjacent observation instrumentation | about 2.5–3.8 ms | Full BGRA-to-RGB copy on other full-frame samples; not isolated in canonical raw route |
| Rust/Python encode microbench | Measured, adjacent local fixture | table above | Deterministic BGRA input; excludes X11, route, Modal, body transfer, and SDK |
| Candidate-C interleaved local slice | Measured, adjacent; 1,000 calls/arm | Browser-like A 5.89 / 9.55 ms; C 3.00 / 4.72 ms | Complete encoder call only; exact parity; capture/route/transport/SDK excluded |
| Candidate-C2 fused fixed-Up slice | Measured, adjacent; 1,000 calls/arm | In its interleaved run A 7.62 / 13.13 ms; C 4.12 / 7.09 ms; C2 3.49 / 6.12 ms | C2 removes another row copy but gains only 0.62 ms p50 over C; A→C2 saving 4.13 ms still misses the 5 ms gate; +6.90% payload and exact parity |
| Whole-buffer libdeflate 1.25 slice | Measured, adjacent; 1,000 calls/arm | A 6.43 / 8.78 ms; C2 2.46 / 3.80 ms; LD-Up 2.75 / 4.41 ms; LD-Adaptive 5.43 / 7.72 ms | LD-Up saves only 3.68 ms vs A and is slower than C2, though payload is −4.18%; adaptive saves only 1.00 ms. Exact parity; reject |
| MSS main zero-copy build-versus-buy control, Modal browser-like Xvfb | Measured, adjacent; 100 alternating calls/arm after 10 warmups | MSS 10.2 12.591 / 13.732 ms; pinned main 11.557 / 12.675 ms | Canonical `grab→shot.rgb→to_png→SHA`, identical 46,061-byte PNG/SHA, 1.033 ms p50 saving, clean FD/lease close; no route, transport, or SDK |
| Candidate-D native AttachFd feasibility run, Modal flat Xvfb | Measured, adjacent; 100 calls after 10 warmups | Capture 0.583 / 0.733 ms; encode 1.133 / 1.325 ms; capture+encode+SHA 1.737 / 2.021 ms | First actual 1024×768 XCB/MIT-SHM 1.2 run on one CPU in `us-west-2`; 13,173-byte flat PNG; no browser, route, transport, SDK, or control |
| Candidate-D versus MSS 10.2, same Modal container and flat pixels | Measured, adjacent; 100 calls/arm after 10 warmups | MSS 11.023 / 13.030 ms; D full-buffer 2.415 / 2.524 ms; D row-stream 3.133 / 4.558 ms | Stage-only and sequential, not public SDK: full-buffer D saves 8.608 ms but payload grows 27%; row-stream D saves 7.890 ms with +4.94% payload; exact decoded parity |
| Corrected Candidate-D versus MSS 10.2, same Modal container and browser-like pixels | Measured, adjacent; 100 calls/arm after 10 warmups | MSS 7.918 / 9.616 ms; D adaptive 2.255 / 7.082 ms; D fixed-Up 1.974 / 2.852 ms | `-noreset`, live fixture owner, per-arm probes, exact parity. Adaptive saves 5.663 ms with −55.01% payload; fixed-Up saves 5.944 ms with +4.18%; sequential stage-only, not SDK |
| Same-region HTTP body floor | Measured, adjacent transport endpoint; 10 calls/size | 0 B 2.89 ms; 50 KiB 4.03 ms; 250 KiB 5.91 ms p50 | Pooled HTTP/1.1 binary response; no screenshot capture or SDK validation |
| Daemon PNG SHA | Required; local adjacent sizing only | about 0.15 ms p50 at 391,587 B; about 0.91 ms at 2.25 MiB | Current controller timing stops before this hash; live one-CPU value remains unmeasured |
| Cursor-position query | Required but not summarized in retained full-path artifact | unknown | Included in controller operation for `/full/raw` despite hidden rendered cursor |
| Starlette response dispatch | Measured, adjacent local synthetic | about 0.002 ms p50 | No-op ASGI send only; excludes Uvicorn, socket, tunnel, and body transfer |
| SDK response validation | Measured, adjacent local synthetic 391 KiB body | about 0.22 ms p50, 0.75 ms p95 | Header parsing, second SHA, model validation; excludes HTTP receive |
| Exact async typed `await screenshots.full()`, MSS arm | Measured, matching public operation; 30 calls after 3 warmups | 24.896 / 26.364 ms | Same image/config/region/ingress, deterministic browser-layout pixels, pooled client, complete typed SDK await; 46,061-byte PNG |
| Exact async typed call, D adaptive | Measured, matching public operation; 30 calls | 10.924 / 13.326 ms | Complete p50 −56.12%, p95 −49.45%, controller-header median 2.522 ms versus A 15.365 ms, payload −55.01%, exact parity, zero fallback |
| Exact async typed call, D fixed-Up | Measured, matching public operation; 30 calls | 10.425 / 12.252 ms | Complete p50 −58.13%, p95 −53.53%, controller-header median 2.141 ms, payload +4.18%, exact parity, zero fallback; leading candidate |

Primary retained artifacts are
[`native-x11-colocated-us-west-2-2026-07-24-final.json`](../benchmark-results/native-x11-colocated-us-west-2-2026-07-24-final.json),
[`modal-optimized-provider-2026-07-30.json`](../benchmark-data/modal-optimized-provider-2026-07-30.json),
and [`provider-compare-coordinate-command-2026-07-30.json`](../benchmark-data/provider-compare-coordinate-command-2026-07-30.json).
The newer optimized-default benchmark times a screenshot followed by an action; it is deliberately
not used as evidence for this one-call headline.

The new public A/B retains every raw sample and uses linear interpolation for p95. All arms shared
one revisioned image, `us-west-2`, one CPU, 2,048 MiB, attested ingress, the same default public
payload, and one pooled async client per warm sandbox. Every observation was 1024×768, hidden
cursor, identical coordinate space, exact decoded RGB, and a self-consistent response/SDK SHA.
The run is recorded in [Modal](https://modal.com/apps/modal-ai-hackathon/main/ap-C6uzJeomVTNw41xadzROjM).

Payload size is part of complete latency, not merely a guardrail. The adjacent same-region transport
artifact rose by about 3.0 ms between 0 and 250 KiB. A linear extrapolation to the observed 391,587-
byte PNG is roughly 7–8 ms total HTTP response time in that old topology. This is an estimate from a
different endpoint/revision, not a subtraction from the canonical call, but it explains why the
codec must be selected on `encode + transfer + SDK`, not encoder time alone. A filter that costs an
extra millisecond but halves the PNG may win the public await; a faster arm that grows the body over
10% fails regardless.

## 5. Remaining allocation and ownership map

| Boundary | A: MSS Python | B: current Rust worktree | Desired C/D |
| --- | --- | --- | --- |
| Persistent XCB connection | MSS owns/reuses | MSS owns/reuses | Rust session owns/reuses |
| Persistent XShm storage | MSS one root-sized mmap | Same | Rust 1–2 refcounted slots |
| XShm -> Python snapshot | Fresh 3.0 MiB `bytearray` | Still present | Removed |
| `.bgra` materialization | Not on A's `.rgb` path | Fresh 3.0 MiB `bytes` | Removed |
| BGRA -> RGB | Python 2.25 MiB staging | Rust 2.25 MiB `Vec` | Convert one row directly into encoder scratch |
| PNG scanlines | Python full filter-0 buffer | `png` crate internal plus full RGB input | Row streaming; bounded previous/current/filter rows |
| PNG output | Python `bytes` | Rust `Vec` then copied to `PyBytes` | One owned output plus at most one explicit Python ownership transfer |
| Daemon hash | Separate pass over PNG | Separate pass over PNG | Keep the existing optimized SHA pass unless live attribution reverses the current rejection of E |
| Starlette body | Reuses Python `bytes` reference | Same | Same public response |
| HTTP client body | HTTPX-owned `bytes` | Same | Same |
| SDK hash | Separate trusted pass | Same | Same; must not be removed |

The `png` crate exposes a `StreamWriter` that accepts partial row data and keeps bounded row state
([official API](https://docs.rs/png/latest/png/struct.StreamWriter.html)). Chromium provides a
production precedent for accepting BGRA pixels directly in a fast Rust-backed PNG path
([Chromium PNG codec](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/ui/gfx/codec/png_codec.cc)).
These sources justify the experiment, not a performance claim on this image.

### Candidate-C local vertical-slice result

The candidate-C worktree rebased the encoder experiment onto revision `6295a65829e1`, added a
`PyBuffer`-borrowed row-streaming encoder, and ran A/B/C in a fixed rotating order for 1,000 calls
per arm after 50 warmups. B includes the MSS 10.2 `ScreenShot.bgra = bytes(raw)` copy; C borrows
`shot.raw`. All arms return complete 1024×768 lossless RGB PNGs. These macOS measurements exclude
XShm capture, route/ASGI work, Modal transport, and SDK validation, so they are sizing evidence only.

| Fixture | A p50 / p95 | B p50 / p95 | C adaptive p50 / p95 | C p50 saving vs A | C payload delta | Pixel parity |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Flat UI | 5.10 / 10.15 ms | 2.56 / 5.25 ms | 1.56 / 3.82 ms | 3.54 ms | +3.66% | Exact |
| Text UI | 4.51 / 8.44 ms | 2.45 / 4.58 ms | 1.56 / 3.19 ms | 2.95 ms | −53.96% | Exact |
| Image-heavy synthetic | 11.65 / 15.64 ms | 4.72 / 6.90 ms | 3.65 / 5.51 ms | 8.00 ms | −82.88% | Exact |
| High entropy | 49.54 / 105.05 ms | 14.26 / 30.09 ms | 12.75 / 26.50 ms | 36.78 ms | +0.36% | Exact |
| Repository browser-like UI | 5.89 / 9.55 ms | 3.91 / 5.46 ms | 3.00 / 4.72 ms | **2.89 ms** | +7.21% | Exact |

The same run measured the 3.0 MiB `.bgra` copy itself at only 0.043–0.054 ms p50. The improvement
from B to C is therefore mostly removal of full-RGB staging and a different row-oriented encode
shape, not removal of `.bgra` alone. The browser-like fixture is the local stop case: C fails the
5 ms absolute daemon gate even before demanding a 20% complete-request win. An adjacent fixed-Up
variant measured 2.74 ms p50 and +7.22% payload on that fixture, but it was not interleaved with A
and is not promotion evidence.

Candidate C2 tested the next copy below `png::StreamWriter`: a custom, safe fixed-Up writer that
converts BGRA directly into filtered rows and feeds level-1 zlib, retaining only the previous RGB
row, filtered row, compressed stream, and final PNG. In a separate 1,000-call interleaved run on the
same repository browser fixture, A was 7.620 / 13.134 ms p50/p95, C adaptive was
4.116 / 7.091 ms, and C2 was 3.492 / 6.121 ms. C2 improved C by only 0.623 ms p50 and 0.969 ms p95;
its A→C2 saving was 4.128 ms, still below the 5 ms daemon gate. Payload was +6.90% versus A and
decoded pixels were exact. This is a bounded safe improvement, but it closes off “remove one more
row copy” as the missing primary win.

### MSS-main zero-copy build-versus-buy control

The isolated `/private/tmp/mss11-control` slice pinned upstream commit `c845c854` and ran it beside
MSS 10.2 in separate persistent workers against the same deterministic browser-like X11 fixture.
After 10 warmups, the controller alternated 100 canonical samples per arm. The timed operation was
exactly `grab → shot.rgb → mss.tools.to_png(level=1) → SHA`; a separate `.bgra` lease diagnostic was
kept out because it perturbed caches and is not part of the current route.

MSS 10.2 measured 12.591 / 13.732 ms p50/p95 and pinned main measured
11.557 / 12.675 ms. Main therefore saved 1.033 ms p50 (8.21%); the paired median delta was
−0.971 ms. Both produced the identical 46,061-byte PNG and SHA. Capture itself improved from
0.975 / 1.342 ms to 0.726 / 1.064 ms. The old-frame lease remained unchanged across the next grab,
close with a live view reported no error, and both workers returned to their baseline nine file
descriptors; measured peak RSS growth was about 12.6 MiB for 10.2 versus 9.3 MiB for main.

This is a clean negative control for dependency-only acceleration: it does not approach the fixed
5 ms daemon gate. A diagnostic `.bgra` read suggests the leased-buffer boundary itself removes only
about 0.25 ms on this fixture. Feeding that lease into a native encoder remains architecturally
valid, but the local C/C2 and this control say the result must come primarily from the codec, not
from replacing MSS capture ownership.

### Candidate-D native-capture feasibility result

The standalone candidate-D slice uses Rust `xcb 1.7`, MIT-SHM 1.2 `AttachFd`, a persistent XCB
connection, two refcounted memfd/mmap slots, reply-fenced capture, visual/depth/mask/stride
validation, reusable PNG scratch, and explicit detach/unmap cleanup. It compiled for
`x86_64-unknown-linux-gnu` and then ran inside a Modal Function with one CPU, 2,048 MiB memory,
`us-west-2`, and Xvfb `1024x768x24`.

In the first feasibility run, after 10 warmups, 100 native full-frame samples measured
0.583 / 0.733 ms capture p50/p95,
1.133 / 1.325 ms encode, and 1.737 / 2.021 ms capture+encode+SHA. A 256×256 region measured
0.238 / 0.320 ms complete. Dimensions and the X11 visual format were validated. This establishes
that the exact FD-backed XShm mechanism works under Modal's runtime.

A second same-container run added persistent MSS 10.2 on the same unchanged flat pixels. MSS
capture+RGB/PNG+SHA was 11.023 / 13.030 ms p50/p95; native full-buffer capture+PNG+SHA was
2.415 / 2.524 ms, an 8.608 ms p50 stage saving. Exact decoded RGB parity passed. But the native
full-buffer PNG was 13,173 bytes versus MSS's 10,372 bytes, a 27.0% increase that fails the fixed
payload gate. The native row-streaming arm was 3.133 / 4.558 ms and 10,884 bytes: a 7.890 ms p50
saving with +4.94% payload on this fixture. The arms ran in the same image/container but in
sequential blocks, not an interleaved schedule.

This is valuable stage evidence: the combined native capture/encoder can clear the 5 ms daemon
gate on favorable content, while capture ownership by itself accounts for only about 0.72 ms of
the observed p50 delta (MSS capture 1.158 ms versus native 0.438 ms). Most of the flat-frame win is
encoding. It still does **not** establish an SDK win: the screen was flat Xvfb content, the run
excluded the Python route, HTTP transfer, SDK receipt, and real browser content, and the
row-streaming codec has already shown payload cliffs on other fixtures.

The first browser-like follow-up exposed a benchmark bug rather than admissible evidence. Xvfb
could reset the root drawable after the short-lived fixture client disconnected. One run retained
the intended 46 KiB layout, later runs silently captured the 10–13 KiB default root, and another
run produced cross-arm pixel mismatches. A fixture probe taken only immediately after drawing was
insufficient because the reset could happen before a later arm. All numerical browser-D results
from that harness are withdrawn.

The corrected run must launch Xvfb with reset disabled or retain one fixture-owner X connection
until every arm completes, probe stable pixels before and after every arm, and reject the artifact
unless all decoded images match. This incident also strengthens the public benchmark rule: every
sample needs content identity evidence, not just dimensions and a valid PNG.

That corrected run is now complete. It used `-noreset`, retained one owner connection, recorded the
same four expected pixel probes before and after MSS and every native arm, and decoded every native
PNG against the MSS output:

| Arm | Stage p50 / p95 | PNG bytes | p50 saving vs MSS | Payload delta | Stage verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| MSS 10.2 | 7.918 / 9.616 ms | 46,061 | — | — | Control |
| D adaptive | 2.255 / 7.082 ms | 20,724 | 5.663 ms | −55.01% | Pass |
| D fixed-Up | 1.974 / 2.852 ms | 47,988 | 5.944 ms | +4.18% | Pass |
| D streaming/fdeflate | 2.576 / 3.403 ms | 59,511 | 5.342 ms | +29.20% | Reject: payload |

The arms still ran as sequential persistent blocks rather than an interleaved public schedule, and
the 46 KiB control body is smaller than the retained real-browser 343–392 KiB bodies. Adaptive and
fixed-Up have nevertheless cleared the two daemon-stage gates on stable browser-like pixels and are
the only D codecs that should advance to the literal public call. The committed artifact is
`/private/tmp/native-capture-slice/modal-results-browser-final.txt` at `87c59a2`; the bounded run is
recorded in [Modal](https://modal.com/apps/modal-ai-hackathon/main/ap-cBME1qtEpuqNW0WAeIy9uO).

### Exact public-SDK vertical-slice result

The isolated `/private/tmp/modal-computer-use-candidate-d-sdk` worktree then integrated the same
persistent native session behind an explicit non-default capture selection. Python retained the
existing readiness, screenshot budget and input lock, cursor-position metadata, timestamps,
coordinate-space construction, daemon SHA and headers, Starlette response, pooled HTTP client, SDK
SHA verification, and typed `Screenshot`. Explicit native arms fail closed; only `auto` may fall
back. Native initialization is deferred until after the supervisor starts Xvfb.

The Modal runner created three warm Sandboxes from the same image and configuration, painted the
same deterministic core-X11 browser layout while retaining an owner connection, and invoked the
literal no-argument public call. It used three warmups then 30 alternating calls per arm. Decoding
and pixel comparison happened after the timed await.

| Arm | Complete SDK p50 / p95 | Controller `total_ms` header median | Median PNG | Complete p50 delta | Payload delta | Verdict so far |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| A: MSS 10.2 | 24.896 / 26.364 ms | 15.365 ms | 46,061 B | — | — | Control |
| D adaptive | 10.924 / 13.326 ms | 2.522 ms | 20,724 B | −56.12% | −55.01% | Pass measured gates |
| D fixed-Up | 10.425 / 12.252 ms | 2.141 ms | 47,988 B | −58.13% | +4.18% | Pass measured gates; leading arm |

The controller-header median saving is 12.843 ms for adaptive and 13.225 ms for fixed-Up, above the
5 ms daemon-side gate even though the header excludes the later daemon SHA described earlier. Both
p95s improve rather than regress. Every sample reported the exact requested capture
backend; fallback counts were zero; cleanup succeeded; dimensions, coordinate space, cursor-hidden
semantics, headers and decoded pixels matched. No screenshot bytes, tunnel token, or URL secret is
retained in the artifact.

This satisfies the performance, payload, and semantic gates on this fixture. It is not yet a
production cutover authorization because the operational clause also requires no memory,
concurrency, failure, readiness, or cleanup regression. The run proves normal readiness and one
clean teardown, but not a long soak, FD/SHM/RSS stability, concurrent slot pressure, cancellation,
X-server restart/resize, or fallback behavior. It also uses a deterministic X11 approximation of a
browser rather than Chromium itself.

The SDK slice is committed at `7b11efd` on top of its candidate-C staging commit `b226e64`. Its
verification completed with 2,239 Python tests passed and 14 skipped, 18 focused native-capture
tests passed, Ruff and mypy clean, host and Linux cross-target release Cargo checks clean, and a
successful Linux release build in the Modal image. Cross-target `cargo test` cannot link Python
symbols from the macOS host; the Python contract tests and real Modal extension import/capture cover
that boundary. No generated extension, Cargo target directory, screenshot bytes, or credentials are
tracked.

## 6. Bounded candidates A–E

The labels below follow the preregistered comparison exactly.

| Candidate | Definition | Copies removed | Current evidence | Rank |
| --- | --- | --- | --- | ---: |
| A | Existing Python MSS 10.2 encoder | None; control | Complete raw-path measurements exist | Control |
| B | Current Rust BGRA→PNG after MSS capture | Python `.rgb` and MSS scanline list; adds `.bgra` copy and Rust RGB staging | Faster locally, but common fixtures miss 5 ms gate; no Modal A/B | 3 |
| C | Rust fused processing while still receiving MSS-owned BGRA | Avoid `.bgra` copy by borrowing MSS 10.2 `shot.raw`; stream rows to PNG; avoid full RGB staging; MSS XShm snapshot remains | Interleaved local A/B/C: exact parity and faster on all fixtures, but browser-like fixture saves only 2.89 ms p50 | 2, retained diagnostic |
| D-control | Pinned MSS 11/main XShm slot lease feeding the existing Python path | Removes MSS snapshot; retains upstream capture validation/fallback | Modal browser-like canonical stage 12.591→11.557 ms p50, only 1.033 ms; identical payload/SHA and clean closure | Completed negative control; do not ship dependency solely for latency |
| D | Persistent Rust XCB/XShm buffer→PNG without MSS snapshot | Same ownership target as D-control, with a coarse native session | Exact public call: fixed-Up 24.896→10.425 ms p50, +4.18% payload; adaptive 10.924 ms, −55.01%; exact parity, zero fallback, clean teardown | 1; fixed-Up leads, pending operational gates |
| E | C/D plus fused PNG hash | Avoids a separate daemon read pass over encoded PNG; SDK hash remains | Python/OpenSSL SHA p50 about 0.15 ms for 392 KiB locally; generic Rust SHA regressed badly on 2.36 MiB | Reject unless new attribution reverses this |

Candidate C should be measured before a larger XCB implementation because it can determine how much
of the remaining win comes from row streaming versus capture ownership. It must use
`memoryview(shot.raw)`, not `shot.bgra`, keep the `ScreenShot` alive, and never release the GIL while
another owner can mutate that `bytearray`.

Candidate D is a valid module seam, but native ownership is not independently valuable enough.
MSS main commit `c845c854` already implements the difficult Python 3.12+ two-slot/finalizer-backed
XShm lease. Its 11.0 page is a draft with a placeholder release date, and the completed pinned
control saved only 1.033 ms p50. Recreating MSS in Rust merely to return Python BGRA bytes would
reintroduce the copy and fail the gate; D must remain a coarse capture-plus-codec operation to have
a plausible benefit.

Candidate E is semantically valid because the response hash is over PNG bytes, but current sizing
rejects it. A local OpenSSL-backed `hashlib.sha256` pass was about 0.15 ms p50 for a 391,587-byte
body and about 0.91 ms for 2.25 MiB. The standalone Rust slice's generic `sha2` arm was much slower
on its large synthetic PNG. Keep the existing daemon and SDK hashes; revisit only with exact Linux
stage attribution and a hardware-accelerated implementation.

### Encoder bakeoff inside C/D

The encoder is a parameter inside the candidate, not the public interface. Compare on the same live
frames:

- `png` level 1 with `NoFilter` as the closest MSS filter-0 control;
- `png` level 1 with adaptive filtering (the current worktree selection);
- `png` level 1 with fixed `Up`, matching Chromium/Skia's current low-compression Rust PNG mapping;
- `fdeflate` ultra-fast with a payload guard;
- the opt-in `zlib-rs` backend as a rejected/control arm, not an assumed improvement;
- a whole-buffer libdeflate zlib stream if retaining one prefiltered buffer is faster overall; and
- libspng progressive rows as a C-library comparison.

`libdeflate` is explicitly whole-buffer and does not provide streaming, so its speed can cost the
full scanline allocation ([official project/API](https://github.com/ebiggers/libdeflate)). libspng
supports progressive row encoding and encode-to-buffer, but its input conversion and ownership
must be profiled ([official encoder documentation](https://libspng.org/docs/encode/)). No upstream
benchmark substitutes for the matched Modal result.

The interleaved local C run used the default `miniz_oxide` backend. A separate `zlib-rs` build kept
decoded parity but grew the flat fixture by 66.6% and the browser-like fixture by 17.4%, failing the
10% payload gate. `fdeflate` likewise produced unacceptable growth on several fixtures. Chromium's
first-party Skia encoder maps its low-compression Rust PNG arm to `Level1WithUpFilter`
([implementation](https://skia.googlesource.com/skia.git/+/a02df043b36900afe1a5a967670bd7b339063501/src/encode/SkPngRustEncoderImpl.cpp)); fixed-Up therefore remains the serious live arm alongside adaptive level 1.

The standalone libdeflate 1.25 control used the official whole-buffer zlib API with one complete
2,360,064-byte filtered frame. In its 1,000-call interleaved browser-fixture run, LD-Up was
2.752 / 4.407 ms p50/p95 and 118,873 bytes: 0.292 ms slower than C2, 10.36% smaller than C2, but only
a 3.678 ms saving versus A. LD-Adaptive was 5.433 / 7.723 ms and saved only 0.997 ms versus A.
Both were pixel-exact; bounded flat/text/image/entropy checks showed additional cliffs. The smaller
LD-Up body is interesting for transfer, but it misses the non-negotiable 5 ms daemon gate before
transport, so libdeflate is rejected for cutover.

MSS's draft 11.0 documentation describes Python 3.12+ zero-copy Linux screenshot buffers and a
two-slot ownership pool, with an upstream 4K read-all improvement from 22.64 to 18.59 ms
([draft release notes](https://python-mss.readthedocs.io/latest/release-history/v11.0.0.html)). Its
release date is still a placeholder and the repository is locked to 10.2.0. The pinned experiment
above is not production dependency evidence or a complete-request result, but it replaces the
earlier byte-scaled guess: snapshot/ownership changes plus the existing Python codec saved about
1.03 ms p50 on the actual 1024×768 browser-like control, far below the 5 ms gate.

## 7. Realistic lower-bound latency budget

### Why 1–5 ms complete is not credible

The fastest selected Rust encoder measured 2.03–4.86 ms p50 on ordinary local fixtures and 16.13 ms
on high entropy. Those numbers exclude X11 capture, Python/Rust ownership transfer, the PNG output
copy, both hashes, route work, body transport, and SDK validation. The new fixed-Up public result is
10.425 ms p50 with a 2.141 ms median daemon total. Median subtraction is not a causal sum, but the
remaining roughly 8.28 ms shows that even a fictional zero-time native stage would not make this
topology a 1–5 ms complete call.

PNG is necessarily a full-frame operation here. For every one-shot call, the X server must populate
about 3.0 MiB of pixels, the encoder must inspect and filter all 786,432 pixels, DEFLATE must produce
a full lossless stream, and the existing HTTP/SDK path must receive and validate the complete PNG.
No prior frame exists to amortize that work.

### Budget ranges

The first three rows remain planning ranges; the final rows now include matching public evidence:

| Component | Evidence-backed planning range |
| --- | ---: |
| Native XShm request/reply into an owned slot | 0.46–0.63 ms p50 on the two Modal fixtures; retain 0.5–1.5 ms as a planning range for real browser variance |
| Row conversion, filtering, and PNG compression | 1.1–2.0 ms p50 on deterministic flat/browser-like Modal fixtures; 2–15 ms is prudent for real UI and entropy variance |
| Output ownership, daemon hash, cursor query, metadata | Included in new D daemon median 2.141–2.522 ms; finer split remains unmeasured |
| Same-topology non-daemon remainder | Roughly 8.28 ms by fixed-Up median subtraction and 9.53 ms for A; includes route/ingress/body/SDK and is not a pure transport clock |
| Complete deterministic browser-layout call | Fixed-Up 10.425 / 12.252 ms and adaptive 10.924 / 13.326 ms, measured |
| Complete real Chromium/UI planning range | Roughly 15–30 ms p50 and 20–40 ms p95 until the actual browser run; estimate only |

The exact current MSS baseline is 24.896 ms p50, so the preregistered 20% threshold is 19.917 ms.
Both D arms clear it by a wide margin. The older 37–39 ms artifacts remain useful topology history,
but they no longer determine this fixture's gate. The gap now is generality and operations, not
whether native capture can move the public latency number.

“Almost instantaneous” for this unchanged public contract can now mean a stable warm complete call
around 10–15 ms on simple UI and, provisionally, 15–30 ms on a real browser—not 1–5 ms and not
action-to-delta latency. Only the first range has matching public evidence today.

## 8. Native capture module design

The Python-facing interface should stay coarse:

```text
NativeCaptureSession
├── capture_full_png()   -> NativePngFrame
├── capture_region_png() -> NativePngFrame
├── capture_full_bgra()  -> NativeBgraLease
└── apply_damage()       -> optional future consumer, not required to ship
```

`NativePngFrame` should contain owned PNG bytes, width, height, capture-backend attribution, native
stage timings, and optionally the SHA-256 of those bytes. Python creates `CoordinateSpace`, queries
cursor position where the route requires it, sets public timestamps with the existing semantics,
constructs `CapturedScreenshot`, and builds the response headers.

`NativeBgraLease` is not a naked pointer. It owns or pins a slot, exposes a read-only contiguous
buffer plus width, height, stride, channel masks/order, and generation, and releases the slot
exactly once. It is primarily for parity tests and future native consumers; the general PNG fast
path should not bounce through Python.

### Session ownership

- One XCB connection and root drawable per controller/session.
- Query MIT-SHM version and attach support at initialization.
- Prefer the Rust `xcb` crate with its `shm` feature because it is a safe interface over libxcb and
  exposes `QueryVersion`, attach/create-segment, and `GetImage`
  ([xcb crate](https://docs.rs/xcb/latest/xcb/shm/)).
- Allocate one slot for strictly serialized capture; use two bounded slots if encoding may overlap
  a later capture. Do not add an unbounded pool.
- Each slot has generation, state (`free`, `capturing`, `encoding`), refcount/lease, dimensions,
  stride, and XShm attachment identity.
- A slot cannot be overwritten while any encoder or Python lease can read it.

FFmpeg's XCB grabber is the clearest primary precedent: it keeps an XCB connection and
`AVBufferPool`, gets a refcounted SHM buffer, runs `xcb_shm_get_image`, and returns packet data that
points at the slot until the buffer reference is released
([FFmpeg xcbgrab source](https://www.ffmpeg.org/doxygen/8.0/xcbgrab_8c_source.html)). Borrow the
ownership pattern, not an FFmpeg subprocess or video pipeline.

### Capture and encode transaction

1. Validate the full or regional rectangle against the configured desktop.
2. Acquire a slot under the native session mutex.
3. Submit and await the XShm `GetImage` reply.
4. Validate reply depth/visual plus server byte order, bits per pixel, masks, stride, and length.
5. Feed BGRX/BGRA rows directly to the PNG writer. Convert only into bounded row scratch.
6. If E is selected, update SHA-256 over the exact PNG bytes as the writer emits them.
7. Transfer the finished owned PNG to Python, release the slot in `finally`, and return attribution
   and timings.

Cursor rendering stays off for the primary path. Cursor **position metadata** remains a Python/X11
backend query exactly where the current route performs it.

### Lifecycle and failure behavior

- Readiness must probe actual capture and decoded-pixel self-test, not importability alone.
- If native initialization/self-test fails in `auto`, choose MSS before serving requests and expose
  the fallback reason.
- Explicit native benchmark mode fails closed so defects cannot hide in fallback samples.
- A production fallback may recapture through the existing complete MSS/file path only before any
  response bytes are committed; it must never mix pixels, hash, timing, or metadata from two arms.
  Record the fallback and trip a process-lifetime circuit breaker for later requests.
- Resize, root visual change, or X-server restart invalidates all slots and the connection. Drain
  leases, rebuild once, or use the existing fallback.
- Detach SHM, close descriptors/mmaps and XCB, and release all slots idempotently on controller
  close, startup failure, cancellation, and process shutdown.
- Preserve current screenshot budget, ready-input lock, cancellation, timeout, and file-fallback
  behavior. No partial PNG may be returned.

## 9. Local Linux experiment plan

Use an isolated Codex worktree based on the current revision. Do not continue implementation on the
stale encoder branch without rebasing its useful pieces and rechecking cleanup behavior.

### Benchmark fixtures and clocks

Run under Linux/Xvfb at 1024×768 with the exact image libraries and CPU/memory limit used on Modal.
Use deterministic flat UI, text-heavy UI, image-heavy UI, and incompressible/noisy frames, plus the
actual browser/desktop fixture used by the live benchmark.

Add benchmark-only clocks around:

- readiness and input-lock wait;
- XCB request/reply;
- MSS snapshot (A–C only);
- `.bgra`/`.rgb` copy;
- row conversion and filter selection;
- DEFLATE;
- PNG output ownership transfer;
- daemon PNG SHA;
- cursor-position query;
- header and `Response` construction;
- ASGI response start/body send;
- HTTPX first byte and complete body;
- SDK SHA/header/model validation; and
- the entire public await.

The current controller `total_ms` must be extended to cover its hash or accompanied by a separate
hash clock. Do not rename the old field in a way that makes old and new artifacts look comparable.

### Sequence

Completed adjacent evidence:

1. Rebased the encoder seam onto the current revision and ran interleaved A/B/C and A/C/C2 local
   controls with 1,000 calls per arm and exact decoded-pixel parity.
2. Ran pinned MSS main against 10.2 on browser-like Modal pixels; its 1.033 ms p50 win rejects a
   dependency-only cutover.
3. Proved Rust D's AttachFd session on Modal and compared it with MSS on flat pixels; one native
   codec passes the daemon and flat-payload gates.
4. Rejected the first D browser-like artifact after discovering that Xvfb could reset the root
   between short-lived clients, then reproduced it correctly with reset disabled, a live owner,
   per-arm probes, and exact parity. Adaptive and fixed-Up pass the stable stage gates.
5. Rejected whole-buffer libdeflate: fixed-Up saves only 3.68 ms versus A and adaptive only 1.00 ms.
6. Ran the literal public SDK A/B. Both D arms pass all measured performance/payload/semantic gates;
   fixed-Up is the leading candidate.

Next experiments, in order:

1. Repeat at least 100 alternating exact public calls on a real Chromium desktop state, retaining
   decoded-pixel identity and payload distribution.
2. Run concurrency 1/2/4/8, a 10,000-capture soak with RSS/FD/SHM tracking, cancellation, slow
   clients, X restart/resize, capability failure, fallback, and cleanup tests.
3. If every operational gate passes, rebase the isolated slice, choose fixed-Up as the initial
   candidate, retain MSS fallback/rollback, run the full repository suite, and only then change the
   default. Keep adaptive available for continued A/B rather than selecting it automatically.
4. Keep E and libdeflate rejected unless new live attribution becomes material. Do not merge or
   change defaults if any fixed gate fails.

Record p50/p95, CPU time, allocations, bytes copied by stage where measurable, payload size, RSS
and RSS growth, open descriptors/SHM segments, fallback count, and exact decoded RGB.

### Local parity and fault matrix

- Full and edge-touching regional captures.
- Every pixel channel on red/green/blue/alpha-sentinel patterns.
- Row padding, byte order, masks, and non-default stride.
- Hidden rendered cursor and preserved cursor-position metadata.
- MIT-SHM absent, version too old, attach denied, `GetImage` error, and XGetImage/file fallback.
- X server restart, root resize, visual/depth mismatch, and out-of-bounds region.
- Encoder error/OOM, output ownership error, cancellation during capture/encode/send.
- Slot exhaustion, concurrent requests, slow client, client disconnect, and controller close with a
  live lease.

## 10. Matched Modal A/B and cutover plan

Build one revisioned Modal image containing all benchmark arms so image, packages, fonts, X server,
browser, and native libraries are identical. Select the arm only through benchmark configuration.
Use the same requested/observed region, Function and Sandbox CPU/memory, ingress, HTTP/1.1 pooled
async client, browser state, desktop content, and one trajectory borrow.

For each predetermined fixture:

1. Create matched A and candidate targets from the same image.
2. Verify placement and resolved resources.
3. Warm the desktop, encoder, and pooled connection outside the timer.
4. Alternate A/candidate order with at least 30 successful samples per arm; prefer 100 for a stable
   p95.
5. Time only the exact public call:

   ```python
   started = perf_counter_ns()
   screenshot = await computer.screenshots.full()
   elapsed_ns = perf_counter_ns() - started
   ```

6. Validate the returned `Screenshot` and independently decode PNG pixels for the parity harness.
7. Keep screenshot bytes, URLs, and tokens out of retained logs; store hashes, dimensions, sizes,
   timings, and sanitized attribution only.

Use paired/interleaved comparisons and publish all successful samples, warmups, failures, retries,
and fallback events. Do not replace failed samples silently. Do not use `full_bytes()`, a direct
daemon route, an action-plus-screenshot call, observation WebSocket, or local encoder timer as the
promotion result.

### Promotion and rollback

Promote only if the candidate simultaneously satisfies every fixed gate for the aggregate and has
no content-class cliff that violates p95, payload, or parity. The initial rollout remains explicitly
selectable with process-lifetime attribution and the Python/MSS arm available.

Current fixed-Up status:

| Gate | Result | Status |
| --- | --- | --- |
| Complete public-SDK p50 | 24.896→10.425 ms, −58.13% | Pass on deterministic fixture |
| Complete public-SDK p95 | 26.364→12.252 ms, −53.53% | Pass on deterministic fixture |
| Median payload | 46,061→47,988 B, +4.18% | Pass |
| Daemon absolute saving | Controller header 15.365→2.141 ms median, 13.225 ms saving | Pass |
| Exact pixel/metadata semantics | Exact RGB, dimensions, coordinates, cursor, hash, backend | Pass |
| Readiness/fallback/cleanup | All arms ready; zero fallback; one clean teardown | Partial pass |
| Memory, FD/SHM soak, cancellation, concurrency, restart | Not yet run in the SDK slice | Blocking |
| Real Chromium content | Not yet run | Blocking |

The candidate is therefore eligible for operational validation, not default cutover yet.

Rollback triggers include any pixel/metadata mismatch, payload guard failure, readiness failure,
fallback spike, SHM/FD/RSS growth, cleanup survivor, concurrency regression, or either latency gate
failure. Rollback changes only native selection; it does not change routes, SDK schemas, stored
artifacts, or Modal orchestration.

If the viable D arm fails a remaining gate, retain the research and tests and recommend
**no cutover**. Do not broaden the scope to an observation protocol to manufacture a win for
`screenshots.full()`.

## Appendix A: observation and XDamage work is secondary

XDamage can signal dirty rectangles after an action; WebRTC uses persistent buffers and damage
regions, Guacamole uses copy/fill/cache instructions, and Xpra/RustDesk use persistent capture and
encode queues. Those are useful designs for a future streaming protocol. The reusable slot owner
from `NativeCaptureSession` may later support `apply_damage()`.

They do not reduce the mandatory work in the first one-shot `screenshots.full()` call: there is no
trusted previous frame, and the response must contain a complete lossless PNG. Therefore:

- no XDamage, tile, patch, CDP, DOM/accessibility, or video metric appears in the headline table;
- action-to-changed-frame and patch-arrival latency are not promotion evidence;
- `apply_damage()` is not required to implement or ship the general engine;
- a patch or video codec cannot silently replace `image/png`; and
- failure or success of a later observation protocol does not decide whether the general engine ships.

Reusable full-frame techniques remain relevant: FFmpeg's refcounted XShm pool, WebRTC's persistent
X11 pixel-buffer ownership, and Chromium's direct BGRA PNG encoder all map to the one-shot native
session. Their damage, streaming, semantic, and video layers do not.

## Primary-source index

- [MSS 10.2 XShm source](https://github.com/BoboTiG/python-mss/blob/v10.2.0/src/mss/linux/xshmgetimage.py)
- [MSS 10.2 pixel-buffer source](https://github.com/BoboTiG/python-mss/blob/v10.2.0/src/mss/screenshot.py)
- [MSS 10.2 PNG writer](https://github.com/BoboTiG/python-mss/blob/v10.2.0/src/mss/tools.py)
- [MSS 10.2 release notes](https://python-mss.readthedocs.io/latest/release-history/v10.2.0.html)
- [MSS 11 draft zero-copy notes](https://python-mss.readthedocs.io/latest/release-history/v11.0.0.html)
- [MIT-SHM protocol](https://xorg.freedesktop.org/archive/X11R7.7/doc/xextproto/shm.html)
- [XShm manual](https://www.x.org/archive/X11R7.5/doc/man/man3/XShm.3.html)
- [Rust `xcb::shm`](https://docs.rs/xcb/latest/xcb/shm/)
- [FFmpeg XCB grabber](https://www.ffmpeg.org/doxygen/8.0/xcbgrab_8c_source.html)
- [WebRTC X11 capturer](https://chromium.googlesource.com/external/webrtc/+/98903d2f5ef420adb343358824b94731b4a00b38/webrtc/modules/desktop_capture/screen_capturer_x11.cc)
- [Chromium PNG codec](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/ui/gfx/codec/png_codec.cc)
- [Rust `png::StreamWriter`](https://docs.rs/png/latest/png/struct.StreamWriter.html)
- [libdeflate](https://github.com/ebiggers/libdeflate)
- [libspng encoding](https://libspng.org/docs/encode/)
- [PyO3 `PyBuffer`](https://pyo3.rs/main/doc/pyo3/buffer/struct.pybuffer)
- [PyO3 `PyBytes`](https://docs.rs/pyo3/latest/pyo3/types/struct.PyBytes.html)
- [HTTPX clients and pooling](https://www.python-httpx.org/advanced/clients/)
- [Starlette 1.5.1 response source](https://github.com/Kludex/starlette/blob/1.5.1/starlette/responses.py)
