# X11 shared-memory screenshot implementation review

Date: 2026-08-08

This note preserves the bounded prototype measurements and records how the production slice differs
from them. It does not make a default-cutover claim. The only promotion boundary is the retained,
validated matched Modal result from the exact public call:

```python
await computer.screenshots.full()
```

## Canonical terminology

The product feature is **X11 shared-memory screenshot capture** and its source token is `x11-shm`.
The public selector is `screenshot_capture_source = auto | mss | x11-shm`. `auto` is a policy:
use `x11-shm` only after the extension and live display pass readiness, otherwise select MSS once
for the current X-server generation. Display restart clears the quarantine and re-probes. Explicit
`x11-shm` fails closed. The production default is `mss`; `auto` remains an opt-in evaluation mode.

Rust, XCB, MIT-SHM AttachFd, fixed-Up filtering, and DEFLATE level 1 describe the current private
implementation. They are not stable product or configuration names. Response attribution continues
to report the source that actually returned the screenshot.

This naming follows Python's usage-oriented naming guidance and the Rust API guideline that names
should communicate their role rather than their representation:

- [PEP 8 naming conventions](https://peps.python.org/pep-0008/#naming-conventions)
- [Rust API Guidelines: naming](https://rust-lang.github.io/api-guidelines/naming.html)

## Preserved bounded evidence

These results motivated the production experiment but are not promotable evidence.

| Experiment | Boundary | Result | Limitation |
|---|---|---:|---|
| Candidate B | Local encoder-only, 1,000 calls | ordinary flat/text frames saved about 3.7–4.0 ms p50; image-heavy saved about 6.8 ms | Retained the MSS snapshot and a full RGB staging allocation; ordinary frames missed the 5 ms daemon gate |
| Candidate C2 | Local repo-browser fixture, 1,000 interleaved calls | MSS 7.620/13.134 ms p50/p95; fused fixed-Up 3.492/6.121 ms; payload +6.90% | Encoder-only; no X server, daemon route, Modal transport, or SDK receipt |
| MSS draft zero-copy control | Matched Modal Xvfb browser-like fixture, 100 alternating calls | MSS 10.2 total 12.591 ms p50; upstream draft total 11.557 ms; about 1.03 ms saved | Component path only; well below the 5 ms daemon gate |
| Candidate D component | Modal Xvfb browser fixture, 100 samples | MSS 7.918/9.616 ms p50/p95; X11 shared-memory fixed-Up 1.974/2.852 ms; payload +4.18%; decoded parity exact | Sequential component benchmark, not the public SDK call |
| Candidate D public prototype | Modal deterministic core-X11 layout, 30 calls/arm | MSS 24.896/26.488 ms p50/p95; fixed-Up 10.425/12.253 ms; daemon median 15.365→2.141 ms; payload +4.18%; decoded parity exact | Only 30 samples, non-Chromium fixture, incomplete provenance and operational gates |

The raw Candidate B/C and 30-call D artifacts were removed from publishable `benchmark-data`
because their schemas and fixtures cannot support a production decision. Their measurements and
limitations remain here and in the dated consolidated research notes.

## What the implementation optimizes

```text
MSS control
Xvfb -> persistent XShm buffer -> Python bytearray -> Python RGB -> PNG scanlines -> zlib -> bytes

x11-shm candidate
Xvfb -> one persistent AttachFd XShm slot -> reusable Rust RGB scratch
     -> fixed-Up PNG + level-1 DEFLATE -> Python bytes
```

The candidate removes MSS's snapshot and Python pixel/scanline work. It reuses one shared-memory
slot because the daemon's authoritative input lock already serializes screenshot work. It still
reads every 1024x768 pixel, materializes a reusable full RGB buffer, compresses a complete PNG,
copies the final Rust `Vec` into Python `bytes`, hashes the PNG, and sends it through ASGI, Modal,
HTTPX, and SDK validation. It is a coarse native kernel, not end-to-end zero-copy and not a 1–5 ms
complete-request claim.

Primary implementation references:

- [MIT-SHM protocol specification](https://www.x.org/releases/X11R7.7/doc/xextproto/shm.html)
- [Rust `xcb` MIT-SHM bindings](https://docs.rs/xcb/latest/xcb/shm/index.html)
- [MSS 10.2 XShm source](https://raw.githubusercontent.com/BoboTiG/python-mss/refs/tags/v10.2.0/src/mss/linux/xshmgetimage.py)
- [Chromium PNG codec source](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/ui/gfx/codec/png_codec.cc)

## Adversarial review fixes

Two pre-implementation reviews and the first post-implementation two-axis review found and fixed:

1. source/session creation before Xvfb startup;
2. readiness that imported the extension without executing `GetImage`;
3. runtime PNGs whose IHDR dimensions were not checked per request;
4. automatic failures that retried the native constructor every screenshot;
5. display stop/restart leaving capture state or source quarantine attached to the old X server;
6. shutdown stopping after the first component cleanup failure;
7. Image publication that imported the module without a real Xvfb/MIT-SHM capture canary;
8. an RSS gate based on `ru_maxrss` rather than the daemon's current `VmRSS`;
9. a 10,000-capture soak that bypassed the daemon/controller;
10. an artifact without an evaluated decision, confidence interval, cursor-position semantics, or
    clean local Git provenance; and
11. concurrency and failure booleans that did not retain enough detail to audit source attribution;
12. cached readiness and automatic fallback state surviving an X-server generation change; and
13. unbounded XCB reply waits and explicit failures retaining a broken native session.

The current runner uses a real managed Chromium fixture, at least 100 paired randomized public SDK
samples per arm, a 10,000-request daemon-local full/region soak, concurrency levels 1/2/4/8, a
controller failure matrix, warm readiness and matched concurrency comparisons, a bounded paused-X
failure probe, and an attributed X-server restart probe. Runtime XCB reply waits are bounded at
500 ms. The retained artifact must bind the actual Modal Image object and native module digest,
validate, and carry its exact computed promotion decision. Failed or partial runs remain untracked.

Automatic mode still selects MSS after ordinary native construction or capture failures. It does
not call MSS after an X-server reply deadline: MSS and the file tools share that unresponsive
display, so the safe behavior is a bounded failure until display restart clears the quarantine.

## Decision rule

No threshold changes after observing results:

- at least 20% lower complete public-SDK p50;
- no more than 5% p95 regression;
- no more than 10% median payload growth;
- at least 5 ms daemon-side absolute saving including the PNG hash;
- exact decoded pixels and metadata semantics;
- no fallback, retry, replacement, readiness, concurrency, cleanup, FD, mapping, or RSS regression.

If any gate fails, retain the evidence, keep MSS as the selected production source, and do not
publish faster-default documentation.

## Promotion outcome

The exact-resource matched Modal campaign reached the canonical public SDK path with one CPU,
2 GiB memory, a deterministic Chromium fixture, a pooled client, and the fixed 1024x768 lossless
PNG contract. It did not authorize a default cutover:

- the candidate exceeded the allowed readiness-latency regression; and
- the X-server restart recovery gate failed.

Both are terminal under the preregistered rules. No threshold was changed and no failed sample was
replaced. The evidence validator rejected the operationally failed run before producing a
publishable promotion artifact, so this note retains the decision without private Modal URLs or
an invented successful artifact. MSS remains the production default. The implemented `x11-shm`
source is explicit opt-in evidence for a future iteration, not a claim that every session should
use it.
