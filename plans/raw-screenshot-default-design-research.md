# Raw screenshot default: design research and recommendation

## Executive decision

Make the normal `screenshots.full()` Interface return a normal `Screenshot` while its
inline Implementation uses `/v1/screenshots/full/raw`. Reconstruct the `Screenshot` from
the binary response and validated response metadata. Keep `full_bytes()` as an explicit
low-level convenience. Keep the JSON/base64 route for artifact storage, direct REST use,
and compatibility.

This is the strongest design because it makes the article-backed path the default without
making transport encoding part of the primary SDK Interface. The `Screenshot` Module is
already deep enough to hold either `bytes`, `data_base64`, or `artifact_uri`, and its
conversion helpers already hide those representations. OpenAI and Anthropic Adapters
already accept `Screenshot` and call `to_base64()` only at the provider boundary. A single
binary-to-model Adapter therefore has high Leverage across sync, async, borrowed, and
provider workflows.

Do not make `full_bytes()` the primary tutorial Interface. It is a useful primitive, but it
loses the semantic metadata that callers and provider Adapters already use. Do not add
`raw=True`, `optimized=True`, a performance profile, or an environment switch. Do not
silently retry through the JSON route when a binary response is missing or malformed.

The hard cutover must not claim the historical 37.25 ms median until the candidate passes a
same-topology promotion benchmark. In particular, exact cursor-position parity adds work
that the historical raw benchmark did not request. Preserve the public contract first,
measure the candidate, and treat any proposal to make cursor position opt-in as a separate
semantic change.

## What the article actually measured

The article result includes both a daemon optimization and a response-transport choice.

- The benchmark marks `screenshot_full` as `raw=True` in
  [`daemon_surface.py`](../src/modal_computer_use/benchmarks/daemon_surface.py#L55-L62).
- The benchmark posts to `/v1/screenshots/full/raw` and reads the image bytes and headers
  directly in
  [`operations.py`](../src/modal_computer_use/benchmarks/operations.py#L330-L348).
- The benchmark framework records this as `transport_encoding="binary"`,
  `screenshot_api="raw_bytes"`, and `comparison_role="canonical_fast_path"` in
  [`hot_paths.py`](../src/modal_computer_use/benchmarks/hot_paths.py#L63-L71).
- The stored artifact contains 30 samples and a 37.25311 ms median in
  [`modal-optimized-provider-2026-07-30.json`](../benchmark-data/modal-optimized-provider-2026-07-30.json#L196-L239).
- The dated report publishes 37.25 ms in
  [`benchmark-results-2026-07-30-warm-paths.md`](../docs/benchmark-results-2026-07-30-warm-paths.md#L19-L31).
- The article explains the persistent MSS/XShm session, in-memory encoding, authenticated
  ingress, and bounded file-capture fallback in
  [`modal-optimized-low-latency.md`](../docs/drafts/modal-optimized-low-latency.md#L43-L65).

The measured path was one raw screenshot request. The article's opening screenshot-plus-click
figure is arithmetic over separate warm medians. It is not a measured fused turn. Fused
action-plus-screenshot, WebSocket control, and HTTP/2 must remain outside this cutover.

## Current Module and Interface audit

### Public screenshot shape

The public `Screenshot` model already supports three payload representations:
`bytes`, `data_base64`, and `artifact_uri`. It also owns format, dimensions, size, digest,
capture time, coordinate space, cursor visibility, and optional cursor position. Its
`as_bytes()` and `to_base64()` methods provide a representation-independent Interface
([`models.py`](../src/modal_computer_use/models.py#L122-L156)). This is the right deep
Module boundary: users ask for a screenshot, not for an HTTP encoding.

The present sync and async `full()` methods post to the structured JSON endpoint and return
`Screenshot`. The separate `full_bytes()` methods post to the raw endpoint and return only
`bytes` ([sync Interface](../src/modal_computer_use/namespaces/screenshots.py#L49-L90),
[async Interface](../src/modal_computer_use/namespaces/screenshots.py#L211-L253)). The
documented default therefore does not use the response shape that produced the article's
measurement.

### Daemon response contracts

The structured route calls the screenshot backend and returns the full `Screenshot` model.
Inline structured responses contain base64 data. Artifact and `auto` storage can write an
artifact and are treated as mutation-capable
([`screenshots.py`](../src/modal_computer_use/daemon/routes/screenshots.py#L70-L89)).

The raw route requires inline storage, calls `screenshot_bytes(...,
prefer_native_png=True)`, and returns the encoded image as the HTTP body
([`screenshots.py`](../src/modal_computer_use/daemon/routes/screenshots.py#L92-L113)). Its
headers currently carry width, height, byte count, SHA-256, capture time, coordinate space,
daemon timing, and capture backend. They do not carry cursor visibility or cursor position
([`screenshots.py`](../src/modal_computer_use/daemon/routes/screenshots.py#L46-L56)).

The structured backend always asks for cursor position. The byte capture API defaults to
not asking for it
([`desktop/screenshots.py`](../src/modal_computer_use/daemon/desktop/screenshots.py#L105-L115),
[`desktop/screenshots.py`](../src/modal_computer_use/daemon/desktop/screenshots.py#L147-L205)).
This is a real semantic difference. `cursor_position` is optional in the model, but callers
of the current structured route normally receive it. A transparent replacement must either
preserve this behavior or make a documented major-version semantic change.

### Provider Adapter leverage

The shared output Adapter derives media type and metadata from `Screenshot` and calls
`to_base64()` only when a provider needs a data URL
([`adapters/output.py`](../src/modal_computer_use/adapters/output.py#L14-L38)). The OpenAI
Adapter consumes the same model
([`adapters/openai.py`](../src/modal_computer_use/adapters/openai.py#L201-L220)), and the
Anthropic Adapter does likewise
([`adapters/anthropic/computer.py`](../src/modal_computer_use/adapters/anthropic/computer.py#L227-L260)).
Because `to_base64()` already accepts a bytes-backed model, reconstructing `Screenshot`
preserves the provider Interface and delays base64 encoding until it is actually required.

The handoff example also already uses the semantic Interface inside one `borrow_async()`
context ([`modal_function_session_handoff.py`](../examples/modal_function_session_handoff.py#L46-L69)).
Changing the Implementation below `full()` lets the example keep semantic naming while it
gains the article-backed response transport.

## External standards and first-party SDK conventions

These sources guide the design. They support binary transfer and semantic SDK objects, but
they do not dictate this repository's compatibility policy.

1. HTTP representation metadata belongs with the representation. `Content-Type` states the
   media type of the response content, and `Accept` can express a client's media-type
   preference ([RFC 9110, Sections 8.3 and 12.5.1](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.3)).
   The current raw endpoint already returns `image/png`, `image/jpeg`, or `image/webp`, so
   the SDK should validate `Content-Type` instead of inferring format silently.
2. HTTPX exposes binary response content as `response.content` and response headers through
   a case-insensitive mapping
   ([HTTPX QuickStart](https://www.python-httpx.org/quickstart/#binary-response-content)).
   The repository's pooled client already exposes exactly the `(bytes, headers)` Seam needed
   for reconstruction; a new generic transport abstraction is unnecessary.
3. Base64 maps each three input octets to four encoded characters
   ([RFC 4648, Section 4](https://www.rfc-editor.org/rfc/rfc4648.html#section-4)). This creates
   roughly one-third representation expansion before JSON quoting and also requires encode
   and decode work. Binary transfer avoids that expansion between daemon and Function.
4. The official OpenAI Python SDK returns parsed semantic objects by default and exposes raw
   response access through a separate `.with_raw_response` prefix
   ([openai-python, "Accessing raw response data"](https://github.com/openai/openai-python#accessing-raw-response-data-eg-headers)).
   This is useful precedent: transport details are an advanced escape hatch, not the normal
   resource Interface.
5. The Google Cloud Storage Python client uses the semantically named
   `download_as_bytes()` for callers who specifically want only bytes
   ([Google Cloud `Blob.download_as_bytes`](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.blob.Blob#google_cloud_storage_blob_Blob_download_as_bytes)).
   This supports retaining `full_bytes()` as an explicit convenience alongside the richer
   semantic result.
6. AWS's first-party Boto3 object response carries a binary body together with content type,
   length, digest/checksum fields, and other metadata
   ([Boto3 `get_object`](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html#response-structure)).
   A binary body does not require discarding metadata.
7. `Content-Digest` is the current standard HTTP field for a digest over message content
   ([RFC 9530, Section 2](https://www.rfc-editor.org/rfc/rfc9530.html#section-2)). The current
   protocol uses `x-computer-use-sha256`; dual-emitting and later adopting `Content-Digest`
   is a sensible protocol follow-up, but it should not expand the article-parity cutover.
8. New protocol parameters should not use an `X-` prefix
   ([RFC 6648, Section 3](https://www.rfc-editor.org/rfc/rfc6648.html#section-3)). The existing
   response contract already uses an `x-computer-use-*` family. The cutover should extend
   that family minimally for compatibility, then consider a separately versioned header
   cleanup instead of mixing a protocol rename into the performance change.
9. SemVer reserves a major release for incompatible public API changes
   ([Semantic Versioning 2.0.0](https://semver.org/#summary)). A change in which payload field
   is populated can be observable even when `full()` keeps returning `Screenshot`; the v2
   migration table must describe it.

## Design alternatives

### Alternative A: document `full_bytes()` as the optimized default

The primary tutorial would call `await computer.screenshots.full_bytes(...)` and hand bytes
to provider-specific code.

**Strengths**

- It exactly selects the existing route used by the historical benchmark.
- It needs no daemon metadata change for a bytes-only caller.
- It is easy to verify at the request Seam.

**Weaknesses**

- It makes a transport representation part of the primary public Interface.
- It discards dimensions, digest, coordinate space, capture time, and cursor state.
- It forces provider tutorials to recover format and metadata or add provider-specific
  branches.
- It bypasses the existing `Screenshot` conversion and metadata helpers.
- It creates two competing concepts of observation: a semantic screenshot for ordinary
  SDK use and untyped bytes for the recommended path.
- It has low Depth: callers must understand the raw route's omitted information and rebuild
  behavior that belongs in the screenshot feature.

**Judgment:** keep this as a low-level Interface, not the default.

### Alternative B: reconstruct `Screenshot` from the raw response

For `storage="inline"`, `full()` requests the raw route, validates the response, and returns
`Screenshot(bytes=...)`. Artifact and `auto` storage continue to use the structured route.

**Strengths**

- The primary Interface remains semantic and provider-neutral.
- Current `as_bytes()`, `to_base64()`, `save()`, `to_pil()`, and provider Adapters keep
  working.
- Binary transfer becomes the default without a performance flag.
- Route selection remains a private Implementation decision based on storage semantics.
- One parser has high Leverage across sync, async, borrowed, OpenAI, and Anthropic paths.
- JSON and raw daemon routes remain available for direct and compatibility use.

**Weaknesses**

- The raw response contract must gain cursor metadata and strict validation.
- A bytes-backed `Screenshot` is observably different for callers that read
  `data_base64` directly or serialize the model instead of using its conversion methods.
- Exact cursor-position parity may add latency relative to the historical raw benchmark.
- Older daemons that lack the complete metadata contract cannot support transparent
  reconstruction.

**Judgment:** choose this alternative and address the weaknesses explicitly through a
major-version migration, protocol capability checks, and promotion evidence.

### Alternative C: add content negotiation to the structured route

The SDK would post to one URI and choose JSON or image bytes through `Accept`.

**Strengths**

- It follows standard HTTP representation negotiation.
- It could reduce the number of public daemon URIs over time.

**Weaknesses**

- It changes a stable route whose current response is a JSON `Screenshot`.
- It complicates OpenAPI generation, compatibility, and debugging without improving the
  already measured raw route.
- It would require a new negotiation and fallback contract.
- It combines protocol redesign with the hard cutover and lowers Locality of the change.

**Judgment:** do not do this in the article-parity cutover.

## Recommended public contract

### Primary Interface

The primary documented call remains:

```python
screenshot = await computer.screenshots.full(
    format="png",
    processing="daemon",
    storage="inline",
)
```

Its contract is semantic:

- Return `Screenshot`.
- Put inline image data in `Screenshot.bytes` on the v2 optimized path.
- Keep `as_bytes()`, `to_base64()`, `save()`, and `to_pil()` stable.
- Preserve format, dimensions, size, digest, captured time, coordinate space, cursor
  visibility, and cursor-position semantics.
- Use the raw binary daemon response for inline full screenshots.
- Use structured JSON for `storage="artifact"` and `storage="auto"`, because those modes
  can create artifacts and the raw route correctly rejects them.
- Do not expose a selector named `raw`, `fast`, `optimized`, or `performance_profile`.

`full_bytes()` remains public for callers that truly want only the image body. Direct
`DaemonClient.post_json()` remains the low-level compatibility path to the structured
endpoint. The daemon keeps both routes.

### Observable migration

The method name and return type remain stable, but the populated payload field changes for
default inline screenshots:

| Concern | Current structured default | v2 optimized default |
| --- | --- | --- |
| Return type | `Screenshot` | `Screenshot` |
| Inline payload | `data_base64` | `bytes` |
| Recommended access | `as_bytes()` / `to_base64()` | unchanged |
| Direct `data_base64` read | populated | may be `None` |
| Direct model JSON serialization | base64 string is present | callers must use the semantic conversion or an explicit serialization Adapter |
| Artifact/auto storage | structured route | unchanged |
| Explicit bytes method | raw route | unchanged |
| JSON daemon route | available | retained |

Because direct field inspection and model serialization are public observable behavior,
the migration belongs in the semver-major release even though well-behaved callers that use
the semantic methods need no code change.

## Recommended binary response contract

The daemon screenshot route owns serialization. The SDK screenshot Module owns parsing.
The generic HTTP transport must remain unaware of screenshot fields.

### Required response data

The binary response must provide enough information to construct `Screenshot` without
guessing:

- image body bytes;
- `Content-Type`, exactly matching the selected PNG, JPEG, or WebP representation;
- width and height as positive integers;
- size in bytes, equal to the received body length;
- SHA-256 for the received bytes;
- capture timestamp with timezone;
- complete coordinate-space JSON;
- cursor visibility as an explicit boolean;
- cursor position as a complete point or explicit absence.

Daemon timing and capture backend remain diagnostic metadata. They must not become required
public `Screenshot` fields.

For the hard cutover, extend the existing `x-computer-use-*` header family with cursor
visibility and optional cursor position. Do not rename all headers in the same change.
Document the headers in OpenAPI and daemon protocol tests. A future protocol ticket can
dual-emit `Content-Digest` and migrate away from new `X-` names.

### Cursor-position decision

Preserve the current `full()` semantic by requesting cursor position for reconstructed
models, including cursor-hidden images. The raw body must still use persistent MSS/XShm
capture and in-memory encoding when `show_cursor=False`; cursor position is separate
metadata. The promotion benchmark must include this actual candidate request and report the
added metadata cost.

Do not optimize by returning `cursor_position=None` silently. If measurement shows that the
position query materially prevents promotion, propose a separate v2 semantic decision such
as an explicit `include_cursor_position` observation option. Benchmark that option and
document its migration. Do not infer position from the requested action or stale SDK state.

### Parser behavior

Use one semantically named Adapter, such as `screenshot_from_binary_response`, for sync and
async namespaces. Avoid names such as `fast_parser`, `raw_v2`, or `optimized_screenshot`;
they describe an implementation or marketing claim rather than behavior.

The Adapter must:

1. validate `Content-Type` against the requested format;
2. validate required integers, timestamp, coordinate space, boolean, and optional point;
3. compare declared size with the received byte length;
4. compute SHA-256 over the received bytes and compare it with the declared digest;
5. let the `Screenshot` model validate dimensions and coordinate-space consistency;
6. construct `Screenshot(bytes=body, data_base64=None, artifact_uri=None, ...)`;
7. raise one typed, secret-free protocol error for missing, malformed, contradictory, or
   unsupported metadata.

The error must identify the failed field by a fixed semantic code. It must not include the
daemon URL, bearer token, raw response headers, image bytes, clipboard text, typed text, or
artifact values.

## Fallback, retry, and compatibility rules

### No post-dispatch transport fallback

Do not send a JSON screenshot request after any raw request was dispatched. This rule should
apply even though a screenshot is observational rather than mutating:

- A second capture represents a later point in time and can hide an incompatible or corrupt
  first response.
- A parser failure is evidence of protocol mismatch, not evidence that the desktop needs a
  second capture.
- A timeout can occur after the daemon captured the frame. Reissuing changes the observation
  identity and obscures performance failures.

Return the typed error. Let the caller choose whether to observe again.

### Pre-dispatch compatibility only

The primary owner-to-borrow flow should verify a daemon protocol capability for complete
binary screenshot metadata before the trajectory starts. Missing, mismatching, or
unverifiable capability must fail before desktop mutation. It must not downgrade the
trajectory to JSON/base64 or to an external caller.

The low-level SDK remains available for explicit direct-daemon and structured REST use.
Protocol tests, not package-semver assumptions, define which client and daemon combinations
support binary reconstruction.

### Daemon capture fallback stays local

Cursor-visible screenshots and failed persistent display connections must keep their bounded
daemon fallback behavior. That fallback belongs beside the screenshot backend because the
backend knows whether capture emitted any state and which capture mechanism failed. Do not
move it into the namespace or HTTP transport.

## Locality, modularity, and semantic naming

These are development requirements, not review preferences.

### Locality of behavior

- The daemon screenshot route owns binary response metadata.
- The desktop screenshot controller owns MSS/XShm selection and bounded file-capture
  fallback.
- The screenshot namespace owns the binary-to-`Screenshot` Adapter and deterministic route
  choice.
- The generic client owns only pooled request execution and `(body, headers)` delivery.
- Provider Adapters own provider-required base64 conversion.
- Borrow and lifecycle Modules own client reuse and cleanup; screenshot code must not create
  or close a client.

### Modularity by feature

Keep this slice within the screenshot feature. Do not build a cross-cutting generic
"optimized response" framework. Do not combine fused action-plus-screenshot normalization,
WebSocket frames, HTTP/2, region defaults, warm pools, managed images, or rate-limit changes
with this cutover.

If region and zoom later adopt the same binary model Adapter, add them as separate
behavior-complete slices after their metadata and compatibility contracts pass. Reuse the
parser; do not widen the first ticket before full screenshots work end to end.

### Semantic naming

Names must state the domain meaning:

- prefer `screenshot_from_binary_response`, `ScreenshotResponseError`,
  `cursor_position`, and `coordinate_space`;
- avoid `optimized`, `fast`, `magic`, `legacy2`, `raw_mode`, and transport-version suffixes
  in the primary Interface;
- reserve `raw` and `bytes` for the explicit low-level route and convenience method where
  they accurately describe the return representation.

This produces a deep Module: a small `Screenshot` Interface hides validation, wire format,
payload representation, and provider conversion. That Depth gives the SDK freedom to evolve
the Implementation without teaching every caller about daemon transport.

## Testing decisions

Test behavior at the highest available Seams.

### Public SDK trajectory Seam

Prove owner creation, versioned handle, explicitly placed Function, exactly one
`borrow_async()` context, one reused async client, repeated `screenshots.full()` calls, one
ordered action batch per model array, lease release, and owner cleanup.

For each inline screenshot, assert:

- exactly one request reaches `/v1/screenshots/full/raw`;
- no structured screenshot request occurs;
- the returned object is `Screenshot` with `bytes` populated;
- all semantic fields equal the daemon response metadata;
- the same async transport/client identity serves the trajectory;
- timeout, cancellation, and malformed metadata do not trigger a second request.

### Daemon behavior Seam

Compare structured and binary capture results for PNG, JPEG, and WebP; cursor hidden and
visible; scale changes; and the mock and X11 backends. Compare semantic fields, not payload
encoding. Account for capture-time tolerance only where two separate captures are necessary.

Prove:

- all required metadata is present and bounded;
- content type, length, digest, coordinate space, cursor visibility, and cursor position are
  correct;
- cursor-hidden capture selects persistent MSS/XShm when available;
- cursor-visible capture and failed display connection follow the documented bounded
  fallback;
- ordinary capture failure fails one request and leaves the daemon alive;
- image bytes, tokens, text, URLs, and artifacts do not enter logs or error details.

### Parser contract Seam

Use one shared contract suite for sync and async. Cover missing, duplicate, malformed,
out-of-range, contradictory, and unsupported metadata. Include wrong media type, byte-count
mismatch, digest mismatch, invalid timestamp, invalid coordinate JSON, invalid cursor
boolean, half-present cursor point, and impossible dimensions.

Each case must fail with a typed sanitized error and one request. Do not assert private helper
names or parsing steps.

### Provider Adapter Seam

Build equivalent base64-backed and bytes-backed `Screenshot` fixtures. Assert that OpenAI
and Anthropic Adapters emit the same provider image and metadata payload for both. This
proves transport representation does not leak into provider integration behavior.

### Compatibility and distribution Seam

Test a matrix of daemon capabilities against a clean installed wheel and sdist:

- complete binary metadata: optimized `full()` succeeds;
- missing or old metadata capability: the primary trajectory fails before mutation;
- direct structured REST: remains available;
- artifact and auto storage: retain structured behavior;
- explicit `full_bytes()`: retains its current byte result;
- imports remain free of Modal, OpenAI, Anthropic, and credentials in core.

Executable README, handoff, OpenAI, and Anthropic examples must call the semantic `full()`
Interface and verify that the built distribution selects raw binary transport.

### Performance Seam

The promotion benchmark must compare the prior public JSON/base64 path with the candidate
`Screenshot` reconstruction path. Interleave enough samples and hold caller topology,
target, requested and observed region, resources, image, ingress, HTTP version, screenshot
format and options, warmup, and connection reuse constant. Record raw sanitized observations
and separate cold allocation, daemon readiness, Function dispatch, borrow, capture/encode,
response transport, SDK reconstruction, provider encoding, and repeated warm operation
timings.

The report must state that the historical 37.25 ms case omitted transparent model
reconstruction and its cursor-position work. It must not promise 37.25 ms or the arithmetic
47 ms figure. It must not credit fused transport. No live or billable Modal run may occur
without explicit authorization.

## Ticket and specification corrections

Correct the parent specification and ticket split before publication:

1. Add raw binary full-screenshot transport over the reused pooled HTTP client to article
   parity.
2. Make semantic `Screenshot` reconstruction the selected SDK design.
3. Add a narrow prerequisite slice for binary metadata parity and fail-closed parsing.
4. Add a vertical slice that changes inline `full()` for sync, async, borrowed, provider,
   executable docs, and compatibility behavior.
5. Keep JSON/base64 routes and `full_bytes()`.
6. Remove fused action-plus-screenshot work from the article-parity cutover and ticket it
   separately.
7. Remove any claim that cursor position should only be queried when `show_cursor=True`.
   That does not preserve today's structured `full()` semantics. Either preserve it and
   measure the cost, or make a separate explicit semantic change.
8. Ensure every ticket requires locality of behavior, modularity by feature, semantic
   naming, secret redaction, and no post-dispatch fallback or replay.

The existing binary transport plan currently combines transparent screenshot reconstruction
with fused action-plus-screenshot selection. It also proposes omitting cursor position from
cursor-hidden captures. Both points conflict with the corrected article boundary and
current semantic parity. Replace that plan rather than implementing it as written.

## Out of scope and separately ticketable follow-ups

- fused action-plus-screenshot requests;
- hot-session WebSocket control;
- HTTP/2;
- automatic warm pools or positive minimum-container settings;
- managed or heavier release images;
- globally disabled input rate limits;
- universal region, CPU, or memory defaults;
- automatic retry after a dispatched request;
- replacing all custom response headers or adopting content negotiation;
- region and zoom default cutovers before independent parity tests;
- changing cursor-position semantics without a separate migration and benchmark;
- removing JSON/base64 daemon routes.

Potential follow-ups should use the same `Screenshot` model Adapter where their correctness
contracts match. Reuse should follow verified semantics, not merely similar response shapes.

## Final judgment

The best-practice design is not "make users call the fastest-looking method." It is "make
the semantic method use the fastest proven representation while preserving its contract."

Use binary HTTP as an internal Adapter beneath `screenshots.full()`. Complete and validate
the metadata contract first. Keep the low-level bytes and JSON paths. Fail closed on protocol
mismatch. Preserve cursor semantics until a separate measured decision changes them. Let
provider Adapters encode base64 only at their boundary. This gives the SDK a deep, stable
Interface and keeps the article-backed optimization local to the feature that owns it.
