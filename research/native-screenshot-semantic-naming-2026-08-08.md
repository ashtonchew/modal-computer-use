# Semantic naming review for the general screenshot capture arm

Research date: 2026-08-08
Scope: the benchmark-only Candidate-D SDK slice at commit
`7b11efd0e6b0287dd0e3799ab14f5789f92925bc` in
`/private/tmp/modal-computer-use-candidate-d-sdk`. This note does not edit product
code or change the screenshot contract.

Pass 2 at the end supersedes the initial capability-only recommendation after
rechecking source attribution and the PNG-only boundary.

## Pass 1 decision (superseded)

Use **`lossless-frame`** as the capability name. Define it narrowly as:

> Capture a validated desktop rectangle as exact RGB pixels and return an allowed
> lossless representation. The capability makes no claim about X11 transport,
> shared-memory ownership, programming language, PNG encoder, compression level,
> filter policy, latency, application readiness, or whether pixels changed.

The recommended surface names are:

| Surface | Candidate-D name | Recommended semantic name |
| --- | --- | --- |
| Python module | `daemon/desktop/native_capture.py` | `daemon/desktop/frame_capture.py` |
| Python session/interface | `NativeCaptureSession` | `FrameCaptureSession` with `capture_frame(...)` |
| PyO3 class | `NativeCaptureSession` | `FrameCaptureSession` |
| Private extension | `_native_capture` | `_frame_capture` (still private) |
| Config selector | `screenshot_capture_backend` | `screenshot_capture_capability` |
| Config capability value | `native-xcb-adaptive`, `native-xcb-fixed-up` | `lossless-frame` |
| Response attribution | `native-xcb-adaptive`, `native-xcb-fixed-up` | `lossless-frame` |

The capability is eligible only for the measured contract (lossless output, scale
`1.0`, and hidden cursor); JPEG/WebP, scaling, cursor composition, file storage, and
mock paths keep their existing owners. The existing portable default remains the
default. For an explicit A/B, a benchmark arm requests `lossless-frame` and records
the selected implementation separately; `auto` may resolve whichever implementation
can prove that capability, but it is not evidence that a native arm was selected.

The exact benchmark arm must remain observable, but it is a separate diagnostic
dimension, not the capability name. Record (in benchmark-only metadata) values such
as `capture_implementation: xcb-shm` and `encoding_policy: level1-up` or
`encoding_policy: level1-adaptive`. Do not put those values in the stable config
selector or the `capture_backend` header. If exact A/B selection is required, use a
benchmark-only `baseline`/`candidate` arm label and retain implementation and codec
details in the artifact.

The capability is deliberately not called an “adapter.” The repository glossary
reserves **Adapter** for translating provider action JSON into the provider-neutral
action schema ([`docs/glossary.md`](../docs/glossary.md#adapter)); a local X11/PyO3
session is a source, session, or implementation. The existing architecture also puts
capture behavior in the feature-local screenshot controller
([`docs/architecture.md`](../docs/architecture.md#desktop-stack)).

## What Candidate-D gets wrong semantically

The candidate's Python module defines
`CaptureSelection = Literal["auto", "mss", "native-xcb-adaptive", "native-xcb-fixed-up"]`,
passes a string filter to `NativeCaptureSession.capture_png`, and exposes the selected
value as `capture_backend` (candidate file `src/modal_computer_use/daemon/desktop/native_capture.py`,
lines 16-18, 82-114). The Rust class repeats that coupling: it is a
`NativeCaptureSession`, its public method is `capture_png`, and the method accepts only
`"adaptive"` or `"fixed-up"` (candidate file `native/native_capture/src/lib.rs`, lines
121-176).

`native-xcb-fixed-up` has three unrelated axes in one token:

* `native` says that the implementation is a compiled/native arm, not what the caller
  can rely on;
* `xcb` names one X11 client transport; the arm could later use Xlib, Wayland, a
  different XCB path, or an upstream MSS-owned slot;
* `fixed-up` names a PNG filter policy, and therefore a codec experiment rather than
  a capture capability.

`native-xcb-adaptive` has the same transport leak and substitutes a different codec
  policy. `screenshot_capture_backend` then conflates a selection policy with backend
  attribution. The Rust module attribute `xcb-mit-shm-attach-fd` is useful sanitized
  diagnostic data, but is not a stable caller-facing backend name.

The candidate also leaves `prefer_native_png` as an internal call argument. That is a
second-order leak: callers should request a lossless frame, while the controller may
choose a native encoder. A future cleanup should rename this internal preference to a
capability-level term (or remove it behind the session interface), without changing the
public route.

## Primary-source naming constraints

### Rust API naming

The Rust API Guidelines require modules, functions, and methods in `snake_case`, and
types and traits in `UpperCamelCase`; acronyms in a type count as one word (`Xcb`, not
`XCB`) ([Rust API Guidelines, C-CASE](https://rust-lang.github.io/api-guidelines/naming.html#casing-conforms-to-rfc-430-c-case)).
That supports `frame_capture`, `FrameCaptureSession`, and `capture_frame`, but it does
not justify surfacing `Xcb` in the interface. The same guide says `as_`/`to_`/`into_`
names describe representation conversions and ownership ([C-CONV](https://rust-lang.github.io/api-guidelines/naming.html)),
which is another reason not to encode a BGRA-to-PNG conversion policy in a session
name.

### Python module and interface visibility

PEP 8 calls for short lowercase module names and specifically recommends a leading
underscore for a low-level C/C++ extension paired with a higher-level Python module
([package/module names](https://peps.python.org/pep-0008/#package-and-module-names)).
Keep `_frame_capture` private and give the Python owner a capability-level name.
PEP 8 also distinguishes documented public interfaces from undocumented/internal
ones and recommends a leading underscore for internal names
([public/internal interfaces](https://peps.python.org/pep-0008/#public-and-internal-interfaces)).
The benchmark-only environment setting is documented in Candidate-D, so its values
should not pretend that an implementation detail is a durable product capability.

### XCB and MIT-SHM are transport terms

The canonical XCB Shm API calls are explicitly named
`xcb_shm_attach_fd` and `xcb_shm_get_image` ([XCB Shm API function list](https://xcb.freedesktop.org/manual/group__XCB__Shm__API.html#function-documentation)).
The MIT-SHM specification similarly names `XShmQueryExtension`, `XShmAttach`, and
`XShmGetImage`, and says a client must check whether the extension is available before
falling back to conventional Xlib calls ([MIT-SHM specification, setup and query](https://xorg.freedesktop.org/archive/X11R7.6/doc/xextproto/shm.html#chapter-3-how-to-use-the-shared-memory-extension)).
Those names correctly belong in private implementation diagnostics and fault tests.
They describe how the pixels are acquired, not the semantic result delivered to the
route. `xcb-mit-shm-attach-fd`, `x11-shm`, and `xshm` are therefore rejected as stable
config or response names.

### PNG filters are encoder policy

The PNG specification defines filtering as a transformation intended to improve
compressibility and leaves filter selection to the encoder; it explicitly permits an
encoder to switch filter types between scanlines ([PNG §7.3](https://www.w3.org/TR/png/#filtering)).
For filter method 0, `Up` is filter type 2 and is defined as a byte difference from
the previous scanline ([PNG §9.2](https://www.w3.org/TR/png/#filter-types-for-filter-method-0)).
Thus `Up`, `fixed-up`, `adaptive`, `level1`, `fdeflate`, `zlib-rs`, and `rust-png`
describe implementation or benchmark policy. They must not define a capture
capability. The existing research already treats the encoder as a C/D subvariant, not
as a new protocol ([candidate expansion, codec ranking](native-general-screenshot-candidate-expansion-2026-08-08.md#codec-subvariant-ranking)).

## Repository conventions and history

The current architecture keeps the screenshot behavior in
`daemon/desktop/screenshots.py`, reports a backend at the feature boundary, and keeps
fallback ownership with the controller that can prove retry safety
([architecture, desktop stack and fallback table](../docs/architecture.md#desktop-stack)).
That is a good locality seam for a `FrameCaptureSession`; a new global provider or
transport registry would be a wider abstraction than this change needs.

The history makes the intended distinction visible:

* `81cc626339bfdab0946497cfbe09f078e7570939` added `capture_backend` attribution for
  the actual screenshot source ([commit](https://github.com/ashtonchew/modal-computer-use/commit/81cc626339bfdab0946497cfbe09f078e7570939)).
* `6d3d22d24ead251e8574d19815c3a751594fe621` kept the XShm preference explicit inside
  the screenshot implementation ([commit](https://github.com/ashtonchew/modal-computer-use/commit/6d3d22d24ead251e8574d19815c3a751594fe621)).
* `aaa2f80b1c1ec1b47e2b1c66a668c86683e6eca6` introduced an isolated Rust PNG encoder
  experiment with names such as `screenshot_encoder`, `rust-png`, and
  `level1-adaptive` ([commit](https://github.com/ashtonchew/modal-computer-use/commit/aaa2f80b1c1ec1b47e2b1c66a668c86683e6eca6)).
  Those names are appropriate for a codec benchmark, which is evidence for keeping
  codec attribution separate from a capture-source capability.

## Three interface/name designs

Scores are 1 (poor) to 5 (strong): **depth** means how much implementation detail the
interface hides; **locality** means how narrowly behavior stays with the screenshot
owner; **semantic stability** means how likely the name survives a transport, codec, or
ownership change.

### 1. Minimal capability seam

Use one semantic token and one coarse session method:

```text
daemon/desktop/frame_capture.py
FrameCaptureSession.capture_frame(region) -> bytes
COMPUTER_USE_SCREENSHOT_CAPTURE_CAPABILITY=lossless-frame
x-computer-use-capture-backend: lossless-frame
```

The bytes are lossless PNG for the currently eligible route, but `capture_frame` does
not bake PNG into the method name. Python continues to own cursor metadata,
coordinate space, hashing, receipts, route validation, and fallback. Benchmark
implementation/codec details stay in an artifact. This is the smallest deep module and
matches the existing controller seam.

| Depth | Locality | Semantic stability |
| ---: | ---: | ---: |
| 4 | 5 | 4 |

### 2. Extensible capability registry

Model capabilities explicitly and let a resolver choose an implementation:

```text
CaptureCapability.LOSSLESS_FRAME = "lossless-frame"
CaptureRequest(region, cursor_policy, scale, representation)
FrameCaptureSource.capabilities -> frozenset[CaptureCapability]
FrameCaptureSource.capture(request) -> CapturedFrame
```

The config requests `screenshot_capture_capabilities=["lossless-frame"]`; the stable
backend attribution remains `lossless-frame`, while an internal resolution record
stores `capture_implementation` and `encoding_policy`. A later source can add region,
raw-pixel, cursor-composition, or changed-frame capabilities without changing the
session name.

| Depth | Locality | Semantic stability |
| ---: | ---: | ---: |
| 5 | 3 | 5 |

The adversarial concern is abstraction tax. A capability registry would cross the
configuration, screenshot controller, readiness, and benchmark layers for one current
lossless-PNG arm. Do not add it until a second genuinely different source needs
negotiation.

### 3. Common caller/source

Make every eligible consumer call one source protocol and remove route-specific
preferences such as `prefer_native_png`:

```text
ScreenshotSource.capture(request) -> CapturedScreenshot
```

`screenshots.full`, regional raw, action-plus-screenshot, and hot-session capture all
use the same `ScreenshotSource`. The selector is
`screenshot_capture_capability=lossless-frame`; source attribution is the same stable
token. The source returns the existing `CapturedScreenshot`, so the route and SDK
contracts stay unchanged.

| Depth | Locality | Semantic stability |
| ---: | ---: | ---: |
| 3 | 4 | 4 |

This design makes the common caller explicit, but it risks pulling route metadata and
cursor policy into a low-level source. The repository already has a shared
`X11ScreenshotController.capture_bytes` seam, so introducing a second common-caller
protocol could be duplicate indirection rather than a deeper module.

## Recommendation and migration boundary

Choose **Design 1** and reserve the `lossless-frame` token for the semantic capability.
It gives the current route a deep, local seam without prematurely adding a registry or
changing all screenshot consumers. If a second source later needs capability
negotiation, Design 1 can grow into Design 2 without renaming the token.

For a future implementation change, the mechanical mapping is:

```text
native_capture.py              -> frame_capture.py
NativeCaptureSession           -> FrameCaptureSession
capture_png(...)               -> capture_frame(...)
screenshot_capture_backend    -> screenshot_capture_capability
native-xcb-{adaptive,fixed-up} -> lossless-frame
```

Keep `xcb`, `mit-shm`, `attach-fd`, `mss`, `rust`, `png`, `adaptive`, `fixed-up`, and
`Up` only in private source comments, sanitized diagnostics, or benchmark artifacts.
Do not rename the public `Screenshot` contract, claim readiness from a frame, or merge
the capture capability with a codec selector. The current promotion gates, exact pixel
parity, fallback behavior, and independent SDK hash remain unchanged.

## Rejected names

| Name | Rejection reason |
| --- | --- |
| `native-fixed-up`, `native-xcb-fixed-up` | Language/runtime + transport + PNG filter in one token. |
| `native-xcb-adaptive`, `xcb-mit-shm-attach-fd`, `xshm` | Protocol/transport details; not the result capability. |
| `rust-png`, `png-encoder`, `level1-adaptive`, `fdeflate`, `zlib-rs` | Codec/library/compression policy; belongs in benchmark attribution. |
| `mss`, `scrot`, `maim` | Tool names; valid diagnostic source values, not a semantic selector. |
| `fast`, `accelerated`, `zero-copy`, `direct` | Performance or ownership claims with no stable contract. |
| `ready-frame`, `settled-frame`, `first-change` | Contradicts repository language: a screenshot does not prove readiness or change. |
| `NativeCaptureAdapter` | “Adapter” conflicts with this repository's provider-action adapter term. |

The one stable statement is the one the caller can verify: **lossless frame capture**.

## Pass 2 — challenge to the capability-only recommendation

The first pass over-generalized. It treated `capture_backend` as if it were a
capability label, but this repository intentionally uses that field as operational
attribution: the existing values `mss`, `scrot`, `maim`, and `mss-fallback` tell an
operator which source actually produced the bytes. Replacing those with
`lossless-frame` would erase the most useful fallback evidence. The first pass also
treated the PNG payload as an incidental representation. It is not incidental for
this seam: Candidate-D's native operation returns a complete PNG, while other
screenshot routes support JPEG and WebP. A generic `capture_frame() -> bytes` hides a
format invariant and invites callers to assume that all screenshot paths share it.

PEP 8's overriding principle is the correct test: public names should reflect **usage
rather than implementation** ([PEP 8, overriding principle](https://peps.python.org/pep-0008/#overriding-principle)).
That principle applies to a Python class or method a caller uses; it does not require
operational metadata to hide the implementation that actually ran. The deep-module
boundary should therefore separate three dimensions:

1. the Python caller-facing operation (`ScreenshotCaptureSession.capture_png`);
2. the selected source recorded for operations and benchmarks (`x11-shm`, `mss`,
   `scrot`, `maim`); and
3. the codec experiment (`level1-up`, `level1-adaptive`) recorded only in benchmark
   detail.

### Three names compared directly

| Design | Module / class / method | Config selector | `capture_backend` attribution | Depth | Locality | Semantic stability | Attribution fidelity |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| X11 transport in the interface | `x11_shm.py` / `X11SharedMemoryScreenshotSession.capture_png()` | `screenshot_capture_backend=x11-shm` | `x11-shm` | 2 | 3 | 2 | 5 |
| Usage-level session, source attribution | `screenshot_capture.py` / `ScreenshotCaptureSession.capture_png()` | benchmark-only `screenshot_capture_source=x11-shm` | actual `x11-shm`, `mss`, `scrot`, `maim`, or fallback | 5 | 5 | 4 | 5 |
| Capability-only frame abstraction | `frame_capture.py` / `FrameCaptureSession.capture_frame()` | `screenshot_capture_capability=lossless-frame` | `lossless-frame` | 4 | 4 | 3 | 1 |

Scores are 1 (poor) to 5 (strong). The first design is honest but shallow: a public
class name commits the caller to X11 and shared memory, and the config cannot survive a
Wayland or upstream MSS implementation. It is acceptable only for a private native
bridge. The third design hides too much: the header loses actual source truth and the
method's `bytes` result does not say whether it is PNG, raw RGB, JPEG, or WebP. It is a
reasonable future registry capability, not the current PNG session contract.

The second design has the useful split. `ScreenshotCaptureSession` describes what the
caller is doing and `capture_png` states the format that this bounded native seam
actually guarantees. The source attribution remains truthful and operationally useful
without making the class or method transport-specific.

### Revised recommendation

For this candidate, supersede the capability-only recommendation with:

```text
Python owner module:       daemon/desktop/screenshot_capture.py
Python session:             ScreenshotCaptureSession
Python method:              capture_png(region) -> bytes
Private extension module:   _x11_shm
Private native class:       X11SharedMemoryScreenshotSession
Benchmark source selector:  screenshot_capture_source
Benchmark source values:    auto | mss | x11-shm
Response attribution:       capture_backend = x11-shm | mss | scrot | maim | ...
Benchmark codec detail:     encoding_policy = level1-up | level1-adaptive
```

The high-level module can remain folded into the existing
`daemon/desktop/screenshots.py` if a new file would add no locality; the important
boundary is the `ScreenshotCaptureSession` interface. `_x11_shm` and
`X11SharedMemoryScreenshotSession` are implementation names and must stay private to
the Python owner. The private native class is allowed to name X11/MIT-SHM because its
only job is to make that transport diagnosable; PEP 8's usage rule does not turn an
internal implementation object into a public API.

Use `screenshot_capture_source`, not `screenshot_capture_backend`, for a benchmark
selector because it chooses the actual source and should line up with the response's
`capture_backend` attribution. Keep it benchmark-only or explicitly provisional; it
is not a promise that every future source is a stable SDK option. Preserve a
compatibility alias for the Candidate-D spelling if this slice is ever rebased.

Do not expose `adaptive` or `fixed-up` in the source selector. Both variants use the
same `x11-shm` source. Put the filter/compression choice in a benchmark artifact or a
separate internal encoder setting, and keep it out of the response's source header.
This preserves the operational fact that a frame came from X11 shared memory while
still allowing the A/B to prove which codec policy ran.

The revised method deliberately says `capture_png`, not `capture_frame`: the native
session's stable output is a PNG byte stream, and Python already owns the surrounding
`CapturedScreenshot` metadata. This is not a promise that all screenshot routes are
PNG; the controller calls this session only for the eligible lossless-PNG path and
continues to use its existing JPEG/WebP/file owners elsewhere. A future generic source
can add a separate `capture_pixels` or `capture_image` interface after it has a real
common caller; do not make the current method vague in anticipation of that change.

### Revised migration map

```text
native_capture.py                    -> screenshot_capture.py (or existing screenshots.py)
NativeCaptureSession (Python)        -> ScreenshotCaptureSession
NativeCaptureSession (private Rust)  -> X11SharedMemoryScreenshotSession
_native_capture                      -> _x11_shm
capture_png(...)                     -> capture_png(...)      # retain format truth
screenshot_capture_backend           -> screenshot_capture_source
native-xcb-adaptive/fixed-up         -> x11-shm + codec detail in benchmark artifact
capture_backend=lossless-frame       -> capture_backend=x11-shm (actual source)
```

This revision keeps the deep interface at the usage level, preserves honest fallback
attribution, and avoids claiming that a particular PNG filter is a product capability.
