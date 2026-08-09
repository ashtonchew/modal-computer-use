# Safe fused computer steps

**Date:** 2026-08-08

**Scope:** Whether `modal-computer-use` can make an action batch followed by a screenshot one canonical SDK operation

**Evidence:** Repository implementation and tests, IETF standards, official Modal documentation, X11 specifications, and first-party OpenAI and Anthropic documentation
**Constraint:** This research did not run live or billable Modal resources.

## Decision

A fused action-to-screenshot operation can become the canonical warm-path SDK operation, but the existing raw fused route is not yet a safe canonical interface.

Fusion can guarantee these properties:

- the daemon validates the complete request before desktop mutation;
- one lease-fenced mutation sequence executes once;
- ordered actions execute under one input lock;
- the daemon captures a frame after the X server has processed the injected input requests;
- a capture failure never causes the action batch to be replayed;
- the SDK strictly verifies the returned action result, screenshot metadata, and image bytes.

Fusion cannot guarantee that the frame shows the application's response, that the application is ready, or that the action was semantically successful. Those are different claims. The generic daemon has no universal readiness signal.

The recommended public method is:

```python
step = await computer.step(
    actions,
    continue_on_error=False,
    screenshot_options=ScreenshotOptions(show_cursor=False, format="png"),
    max_action_timeout_ms=5_000,
)

step.actions       # ActionBatchResult; known failures remain data
step.screenshot    # Screenshot, including verified bytes and metadata
step.timing        # action, capture, transfer, and parse attribution
```

The return type should be `ComputerStepResult`. `step()` follows the established environment
meaning: apply an action or ordered action batch and return the next observation. It does not mean
a provider turn and does not adopt Gymnasium's reward or episode tuple. Do not add `fused=True` or
an optimization profile. Keep first-visual-change observation separate and experimental.

The stable method should define its observation as an **immediate post-dispatch frame**. It should become the documented default only after the protocol, receipt boundary, failure behavior, provider examples, and freshness benchmark described below pass. Applications that need readiness must use an application-specific wait predicate before a later read-only screenshot.

## Why the current route is not sufficient

The repository already has most of the mutation-safety foundation:

- [`run_batch`](../src/modal_computer_use/daemon/actions/batch.py) preflights the full action tree, bounds, lease, operation budget, and mutation receipt before dispatch. It executes the ordered batch under one input lock and stops on the first failure unless continuation is requested.
- The native XTest backend calls `XSync` before it reports success and distinguishes failures before event emission from failures after possible emission in [`xtest.py`](../src/modal_computer_use/daemon/desktop/xtest.py).
- The lease journal writes `IN_PROGRESS` before dispatch and records completed, not-applied, or indeterminate mutation outcomes in [`receipts.py`](../src/modal_computer_use/daemon/receipts.py).
- The borrowed SDK path sends a lease fence and a gap-free operation sequence without automatically retrying a mutation in [`session_lease.py`](../src/modal_computer_use/session_lease.py).
- Tests already cover action ordering, stop and continuation behavior, XDamage verification, polling fallback, cancellation receipt resolution, and no replay in [`test_action_batch.py`](../tests/test_action_batch.py) and [`test_session_lease.py`](../tests/test_session_lease.py).

The existing fused surface nevertheless has six material gaps.

### 1. The SDK accepts weaker screenshot evidence

[`ActionsNamespace.run_and_screenshot_bytes`](../src/modal_computer_use/namespaces/actions.py) returns bytes plus loosely parsed headers. It accepts a missing action-result header as an empty object and does not apply the strict screenshot parser used by [`ScreenshotsNamespace`](../src/modal_computer_use/namespaces/screenshots.py). The normal parser verifies content type, positive dimensions, declared size, SHA-256, timestamp, coordinate space, cursor state, cursor bounds, and timing metadata.

A canonical operation must return the normal semantic `Screenshot`, not a second, weaker bytes result. Raw binary transport should remain an implementation detail of the deep public method.

### 2. The action result is placed in a response header

[`/v1/actions/run/raw-screenshot`](../src/modal_computer_use/daemon/routes/actions.py) puts base64-encoded action JSON in `x-computer-use-action-result` while the image occupies the body. This is unsuitable as the canonical compound representation. HTTP does not set one universal field-size limit; recipients and intermediaries impose practical limits, so large custom fields reduce interoperability. The IETF also warns that unbounded fields increase request-smuggling and memory risks ([RFC 9110, Section 5.4](https://www.rfc-editor.org/rfc/rfc9110.html#section-5.4)). A maximum-size action array can therefore fail at an ingress or client even when the image body is valid.

Put both results in a bounded, versioned response body. Use headers only for ordinary HTTP representation metadata and small protocol identifiers.

### 3. Direct idempotency is misleading

The raw fused execution path deliberately bypasses the batch result cache because the cached result does not include the exact screenshot bytes. The route still accepts an idempotency key. A direct caller can therefore resend the same apparent idempotent request and dispatch the actions again.

The borrowed, receipt-backed path prevents this replay. The unrestricted REST path must either cache the exact compound response under a request fingerprint, with explicit size and retention limits, or reject `Idempotency-Key` before mutation. Accepting and ignoring it is not safe.

This follows HTTP retry semantics. A client should not automatically retry a non-idempotent request unless it knows that the request semantics are idempotent or can prove that the original request was never applied ([RFC 9110, Section 9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)). `POST` itself does not make the operation retry-safe; its meaning comes from the target resource ([RFC 9110, Section 9.3.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.3.3)).

### 4. Action completion and observation completion share one receipt boundary

Today, raw capture happens before normal receipt finalization. Cancellation, timeout, or daemon loss during capture can therefore turn a known-complete action batch into an indeterminate mutation. Conversely, a handled capture failure produces `raw_screenshot_after_not_captured` after the action result exists, but it does not give the public SDK a precise partial-success contract.

The daemon needs a durable phase boundary:

1. prepare the receipt;
2. execute the action batch;
3. durably record the action phase as not applied, indeterminate, or completed;
4. only then capture and encode the observation;
5. publish the compound response.

Recording action completion before capture is the key safety change. It lets receipt resolution say “the mutation completed but its result or observation is unavailable” instead of guessing that the desktop may have been mutated.

### 5. Immediate capture can be visually stale

`XTestFakeInput` synthesizes input events. `XSync` flushes the client and waits until the X server has processed its requests ([XTEST extension specification](https://www.x.org/releases/X11R7.6/doc/xextproto/xtest.html), [XSync manual](https://www.x.org/archive/X11R7.5/doc/man/man3/XSync.3.html)). This is necessary before returning input success. It does not prove that a separate application client has handled the event, updated its own state, rendered a new frame, or reached readiness. Core X also does not provide universal ordering guarantees across different clients; explicit synchronization primitives are needed where an application supports them ([SYNC extension specification](https://www.x.org/releases/X11R7.7/doc/xextproto/sync.html)).

Removing the network gap between a separate action request and screenshot request can expose this race more often. A latency benchmark without a frame-freshness assertion can make a fused implementation appear faster by returning the old pixels.

### 6. The critical failure boundaries lack focused tests

The existing tests prove successful fused raw capture and extensive individual batch and changed-frame behavior. They do not yet prove the complete compound contract across action completion, receipt finalization, capture failure, response loss, strict metadata parsing, and direct idempotency.

## Three observation meanings

The API and documentation must use these terms consistently.

### Immediate post-dispatch frame

The frame is captured after the ordered action batch and native input synchronization, while the daemon still holds the operation's input lock. It provides deterministic daemon ordering. The pixels may be unchanged or may show an intermediate application state.

This is the only stable semantic proposed for `step(...)`.

### First visual change

The daemon starts with a baseline, uses XDamage as a wake-up hint, and verifies a change with a full-resolution pixel comparison. This proves that pixels differ from the baseline. It does not prove that the requested action caused the difference: a cursor, animation, clock, notification, or unrelated client can change pixels. XDamage reports damaged drawing regions, not business-level completion ([DAMAGE extension protocol](https://www.x.org/releases/X11R7.7/doc/damageproto/damageproto.html)).

Keep this behavior in `run_and_observe_first_change(...)`, labeled experimental, with polling fallback and a bounded timeout. Do not silently use it as the stable fused default.

### Application readiness

Readiness requires a target-specific predicate: a DOM condition, accessibility-tree state, API response, window property, known pixel condition, or application-specific completion signal. No generic screenshot timing rule proves it. X11's RECORD specification likewise notes that useful synchronization information is application-specific and cannot be predicted universally ([X RECORD extension protocol](https://www.x.org/releases/current/doc/recordproto/record.pdf)).

Applications should own these predicates. “First visual change” must never be documented as “ready.”

## Recommended wire contract

### Use one bounded, versioned binary step envelope

Use a vendor response media type and a fixed, length-prefixed body:

```text
magic | version | flags | manifest_length | segment_count
bounded UTF-8 JSON manifest
bounded binary image segments
```

The JSON manifest should contain:

- protocol version and observation kind (`immediate`);
- the complete `ActionBatchResult`;
- screenshot format, dimensions, coordinate space, cursor metadata, capture timestamp, backend, and timings;
- ordered segment roles, byte lengths, media types, dimensions, and SHA-256 digests;
- non-secret lease epoch and operation-sequence acknowledgement;
- an explicit observation success or failure record.

The response should also send `Cache-Control: no-store`. A standard `Content-Digest` may protect
the complete transmitted content, while each manifest digest binds an image segment to its
semantic metadata. `Content-Digest` covers the actual message content, not a selected metadata
subset ([RFC 9530, Section 2](https://www.rfc-editor.org/rfc/rfc9530.html#section-2)). Digests detect
corruption or parser mismatch; TLS and attested-tunnel authentication remain responsible for peer
and transport security.

The client must enforce limits before allocation and reject:

- a bad magic value, unsupported version, or contradictory flags;
- an oversized manifest, segment count, image, or total body;
- negative, overlapping, out-of-range, duplicated, or trailing segment data;
- an unknown protocol version or observation kind;
- a content type that disagrees with the requested format;
- invalid action indexes, counts, or result schema;
- non-positive or contradictory dimensions;
- a byte length or SHA-256 mismatch;
- invalid cursor coordinates, timestamps, lease epoch, or operation sequence;
- trailing bytes after the declared envelope.

Keep the encoder and decoder in the step feature Module. The SDK owns both ends, already uses a
binary-envelope pattern, and needs strict support for action-produced screenshot and zoom segments
plus one distinguished final screenshot. This narrower grammar is easier to bound than a general
MIME response parser. `multipart/related` remains the relevant standard alternative
([RFC 2387](https://www.rfc-editor.org/rfc/rfc2387.html)), but the public `ComputerStepResult`
must remain unchanged if transport evidence later justifies another envelope version.

Do not add streaming or zero-copy parsing in the first slice. Buffer within a strict total-response
limit, prove correctness, and optimize only if the same-topology profile shows parsing material.

## Mutation and observation state machine

The composite operation is one SDK call but it is not one indivisible transaction. Input has irreversible side effects; screenshot capture is a later read.

```text
VALIDATE
  -> RECEIPT_PREPARED
  -> ACTION_NOT_STARTED | ACTION_INDETERMINATE | ACTION_COMPLETED
  -> OBSERVATION_FAILED | OBSERVATION_COMPLETED
  -> RESPONSE_PUBLISHED
```

Required classifications:

| Failure point | Durable mutation outcome | SDK behavior | Automatic action replay |
|---|---|---|---|
| Validation or placement failure | Not started; no receipt | Raise validation/placement error | Never |
| Before durable receipt | Proven not applied | Raise `OperationNotApplied` | Only a caller's new explicit operation |
| After receipt, before any event, with backend proof | Not applied | Raise `OperationNotApplied` | Never inside this call |
| After possible event emission | Indeterminate | Require recovery; fence later mutations | Never |
| After definite action completion, before/during capture | Completed; observation unavailable | Raise typed observation error containing any safely received action result | Never |
| After capture, during response loss | Completed; result unavailable | Resolve receipt, keep mutation blocked, allow attributed read-only re-observation | Never |

The server must shield the small durable action-finalization step from cancellation once action execution reaches a terminal state. It must not shield an unbounded capture. Cancellation during action dispatch retains the existing pre-emission versus possible-emission classification. Cancellation after durable action completion cancels capture and cleanup but does not change the mutation outcome to indeterminate.

If the server can return a response after capture failure, the wire outcome should still describe a completed action phase plus failed observation phase. The SDK should raise a specific `StepObservationUnavailableError` carrying the verified `ActionBatchResult` when available. A generic non-2xx “request failed” response invites unsafe retry middleware. The client may offer an explicit **read-only** `capture_after_result_loss()` recovery operation; it must label the new capture as recovery and must not imply that it is the original immediate frame.

If the whole response is lost, the receipt remains authoritative. Because the current receipt journal intentionally does not store full results, `COMPLETED` means the action must not be replayed even when the action result and frame are unavailable.

## Lease, placement, and Modal replay

The canonical guarantee applies to the documented owner-to-borrow topology:

1. an async owner creates one desktop;
2. it creates a versioned session handle and an application-generated run identifier;
3. an explicitly placed Modal Function receives both;
4. the Function enters `borrow_async()` exactly once for the trajectory;
5. each fused operation consumes one gap-free operation sequence under the active lease fence;
6. the Function releases the lease and the owner performs cleanup.

Missing, mismatching, or unverifiable placement must fail before mutation. Lease tokens, daemon URLs, screenshot bytes, typed text, clipboard values, and artifacts must not appear in handles, errors, logs, receipts, or benchmark output.

Modal Function retries can run an input again after a failure, and a preempted Function can restart the same input. Modal tells applications to make operations idempotent under retry and preemption ([Modal retries](https://modal.com/docs/guide/retries), [Modal preemption](https://modal.com/docs/guide/preemption)). Set application Function retries to zero for this path, but do not treat that as an exactly-once guarantee. Generate the durable run identifier in the owner and pass it as Function input. A restarted Function must present the same run identifier; the daemon receipt then prevents a second dispatch.

Modal Function timeouts are attempt-level and can exceed the configured value by several seconds, so the SDK and daemon need their own precise phase deadlines and cleanup ([Modal timeouts](https://modal.com/docs/guide/timeouts)). The desktop remains a separately owned Sandbox. Borrow cancellation must release its lease without terminating an attached desktop, while owner cancellation must eventually terminate the owned Sandbox. Modal's Sandbox readiness probe only shows that a service accepts traffic, not that a desktop application is ready ([Modal Sandboxes](https://modal.com/docs/guide/sandboxes)).

The attested tunnel remains part of the path. Modal tunnels expose a direct TLS-enabled L4 connection and do not add HTTP translation or headers ([Modal tunnels](https://modal.com/docs/guide/tunnels)). That does not remove all client, proxy, or library limits, and it does not justify large action metadata in HTTP headers.

## Provider integration

Provider loops remain application-owned. Core must not import OpenAI or Anthropic.

OpenAI's computer-use guide requires applications to execute every item in an `actions[]` array in order and then capture the updated screen for the next `computer_call_output` ([OpenAI computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)). The adapter example should pass that entire ordered array to one `step(...)` call. It must preserve the provider call identifier and use the returned semantic screenshot in the next output.

Anthropic's computer-use contract has the application execute a tool request and return a correlated `tool_result`; image results and explicit error results are supported ([Anthropic computer use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool), [Anthropic tool-result handling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)). A single mutating tool action, or an application-defined ordered batch, may use the same core method. The adapter must preserve `tool_use_id`, preserve screenshot and zoom output ordering, and surface `StepObservationUnavailableError` without resending the action.

Neither provider contract turns immediate pixels into application readiness. High-impact action confirmation, domain allowlists, untrusted-content handling, and human review remain application policy. Fusion changes transport and ordering, not the safety policy.

## Implementation invariants

The implementation should use locality of behavior and modularity by feature:

- keep the semantic method and response parser beside the actions namespace;
- reuse the existing screenshot metadata validator instead of copying it;
- keep compound framing in one actions-observation protocol module;
- keep receipt phase transitions in the receipt journal and action executor;
- keep provider translation in provider examples or adapter packages;
- use semantic names such as `ComputerStepResult`, `StepObservationUnavailableError`, `immediate`, and `first_visual_change`;
- do not add a general transport framework for one response type.

The following invariants are release-blocking:

1. The full action array and screenshot options validate before receipt preparation or mutation.
2. One fused call uses one lease fence and one operation sequence.
3. One input lock covers ordered action execution, native synchronization, and immediate capture.
4. Native fallback occurs only before any input event is emitted.
5. Definite action completion is durably recorded before capture begins.
6. Capture failure, cancellation, timeout, or response loss never replays the action batch.
7. A returned `Screenshot` has the same integrity checks as `screenshots.full()`.
8. The wire response is versioned, bounded, unambiguous, and secret-free.
9. The direct REST surface never accepts an idempotency key that it cannot honor.
10. First visual change remains experimental and distinct from immediate capture and readiness.

## Required tests

### Behavior and fault injection

Test every transition, not only route success:

- invalid action, bounds, continuation flag, screenshot option, placement, lease, and budget fail before mutation;
- ordered execution, stop-on-first-failure, explicit continuation, drag, key, pointer, and batch state remain under one lock;
- native success synchronizes before capture;
- pre-emission fallback occurs once, while possible-emission failures never fall back or replay;
- cancellation and timeout before receipt, after receipt, before first event, after possible event, after action completion, during capture, after capture, and during response write produce the expected receipt state;
- daemon restart at the same boundaries preserves fencing and no-replay behavior;
- capture backend failure, encode failure, empty bytes, and deadline expiry after action completion return the typed partial outcome;
- response loss after completed mutation resolves to result unavailable and permits only explicit attributed read-only re-observation;
- owner, attached, borrowed, and cancelled trajectories release exactly the resources they own;
- held keys and buttons are released during partial failure cleanup.

### Protocol and metadata

Use built-distribution client/server tests for:

- valid multipart response and stable version negotiation;
- missing, duplicated, reordered, truncated, unknown, or oversized parts;
- hostile boundaries and conflicting part metadata;
- wrong content type, size, digest, dimensions, coordinate space, cursor state, timestamp, action index, lease epoch, or sequence;
- clean rejection of unsupported daemon/client protocol combinations;
- exact cached response for a supported direct idempotency key, or pre-dispatch rejection when unsupported;
- absence of URLs, bearer tokens, text, clipboard content, screenshot bytes, and artifacts in errors and logs.

### Provider examples

Run executable fake-daemon OpenAI and Anthropic examples from the built wheel and sdist. Assert that:

- an OpenAI `actions[]` array remains one ordered daemon batch;
- provider call identifiers survive the round trip;
- one compound result becomes one provider screenshot result;
- capture failure after action completion does not cause a provider action replay;
- application-specific readiness can opt into a later read-only wait and screenshot.

Run a real Modal smoke test only with explicit authorization.

## Benchmark and promotion gate

Do not derive a fused-turn latency by adding separate operation medians. The historical “47 ms” figure is arithmetic over separate raw screenshot and click medians, not a measured action-to-frame distribution. Percentiles and medians are not additive, and the measurements do not prove that a screenshot contains the result of the preceding action.

The promotion experiment must compare:

- the current canonical borrowed path: action batch, then raw semantic screenshot over the reused HTTP client;
- the candidate borrowed path: one `step(...)` call.

Hold caller topology, target, requested and observed region, Function and Sandbox resources, image, ingress, HTTP version, input backend, screenshot backend and format, cursor choice, action payload, warmup, connection reuse, and timeouts constant. Interleave a preregistered sufficient sample count, with at least 100 valid warm pairs per condition. Retain sanitized raw observations and report exclusions, failures, cleanup, cold allocation, startup, dispatch, borrow, action, capture, encoding, transfer, parse, and total latency separately.

Latency is not enough. Use a deterministic test UI with a monotonic visible state or known pixel generation. For every sample, verify that the returned frame corresponds to the dispatched action. Report:

- median and p95 action-to-verified-frame latency;
- stale-frame rate;
- invalid-metadata and corrupt-body rate;
- action, observation, timeout, cancellation, and cleanup failures;
- no-replay evidence under injected connection loss.

First-visual-change latency is a separate experimental benchmark. Application readiness needs a target-specific benchmark. Do not credit `step()` with the historical 47.10 ms arithmetic. Claim a measured 47 ms computer step only if the same-topology timer measures the action-to-verified-frame distribution at that threshold and all correctness gates pass.

## Delivery sequence

1. Add receipt phase transitions and fault-injection tests without changing the public default.
2. Add the bounded versioned compound route and strict parser behind an explicit versioned endpoint, while retaining JSON/base64 and current raw routes.
3. Add `ComputerStepResult` and `AsyncBorrowedComputer.step(...)`; keep the existing byte-returning method low-level and compatible.
4. Convert fake-daemon provider examples and executable documentation. Keep model loops application-owned.
5. Run protocol compatibility, clean wheel/sdist, daemon capability, lifecycle, and protected Modal smoke gates.
6. Run the preregistered fused benchmark and semantic freshness evaluation.
7. Promote the method in a semver-major SDK cutover only after the gates pass. Publish runtime artifacts first, package second, and hosted documentation last.

Rollback should restore the prior documented SDK method without removing the new daemon route or silently degrading a fused operation into two external-caller requests. A fused request must either satisfy its declared contract or fail before mutation.

## Final recommendation

Build fused action and screenshot as a semantic deep interface, not as a transport flag. Make it canonical only after action completion has its own durable receipt boundary, the response body has a bounded versioned compound representation, the SDK reconstructs and verifies the normal `Screenshot`, and a same-topology benchmark proves both latency and frame freshness.

The existing action executor, XTest synchronization, lease fencing, and no-replay coordinator are a strong base. The central implementation task is to separate **mutation certainty** from **observation availability**. Once that distinction is enforced, fusion is safe against duplicate desktop mutation. It remains, by design, an immediate observation rather than a promise of first visual change or application readiness.

## Locked public contract after design review

```python
step = await computer.step(
    actions,
    screenshot_options=ScreenshotOptions(
        format="png",
        show_cursor=False,
    ),
    continue_on_error=False,
    max_action_timeout_ms=5_000,
)
```

```python
class ComputerStepResult:
    actions: ActionBatchResult
    screenshot: Screenshot
    timing: ComputerStepTiming
```

The Interface exists with the same semantic contract on active `BorrowedComputer` and
`AsyncBorrowedComputer` instances. The async form is the primary documented Modal Function path and
the article-backed Benchmark Surface. The synchronous form preserves the SDK's existing public
parity and uses the same daemon route, envelope, receipt states, errors, and validation. A normal
return may contain `actions.ok is False` only when every dispatched action has a known terminal
outcome, input cleanup is proven, and the returned screenshot is valid. Possibly partial input
raises `StepOutcomeUnknownError`. Completed mutation with no valid observation raises
`StepObservationUnavailableError` and moves the borrow into terminal read-only recovery.

`continue_on_error=True` remains an explicit semantic option. It never overrides the rule that an
indeterminate action stops the batch, quarantines the trajectory, and prevents later mutation.

The complete `ComputerAction` grammar remains valid. Mixed batches may include screenshot and zoom
actions, whose image results are carried as bounded envelope segments in their original order. One
distinguished final screenshot remains the step observation. Provider Adapters should use the
read-only screenshot or cursor-position primitives for purely observational provider calls.

## Prioritized risk plan

### P0: semantic decoding occurs outside the mutation coordinator

The current coordinator treats receipt of an HTTP response as success before namespace code parses
the response. `step()` must move bounded envelope decoding, action-result validation, screenshot
hydration, and frame-integrity validation inside the coordinator callback. Only a fully constructed
`ComputerStepResult` leaves the borrow mutable. Any post-dispatch parse or integrity failure resolves
the receipt and makes the borrow read-only.

Minimum tests: malformed magic, unknown version, truncation, contradictory lengths, wrong digest,
wrong dimensions, cancellation during decode, completed receipt, no second mutation, and explicit
read-only recovery.

Avoid: a second client journal or a separate validation transaction.

### P0: action completion and observation availability share one state

Add private receipt phase evidence for admission, possible input dispatch, known terminal action,
observation, and terminal commit. The durable record does not need the full action result or image.
It needs enough bounded, redacted evidence to distinguish not applied, indeterminate, and completed
mutation even when the observation or response is lost.

Minimum tests: cancellation and timeout at every phase, daemon restart, receipt reconciliation,
cleanup, exact action count, and zero replay.

Avoid: durable screenshot storage, a general event-sourcing system, or distributed transactions.

### P0: known action failure can be confused with possible partial dispatch

Each failed action needs a truthful internal emission classification. Known terminal failure may
return `ComputerStepResult(actions.ok=False, ...)`; possible partial dispatch never may. Capture the
known post-failure state under the same input lock. If held-input cleanup is not proven, upgrade the
outcome to recovery-required.

Minimum tests: failure before emission, known failure after a successful prefix, partial native
input, timeout before and after possible emission, continuation, nested action failure, and failed
`release_all()`.

Avoid: rollback or compensating desktop actions.

### P0: protocol bounds and secret exposure

The step envelope must enforce a fixed header size, manifest limit, segment-count limit, per-segment
limit, and total-response limit before large allocation. Reject integer overflow, impossible sums,
overlap, duplicate roles, undeclared bytes, trailing bytes, unknown required roles, and mismatched
hashes. Headers contain only a vendor content type, envelope version, cache policy, and ordinary
HTTP integrity metadata. They never contain action results or images.

Use the existing screenshot pixel budget to derive image limits, and apply one explicit total step
budget. Sanitize parser errors to stable error codes; do not include manifest values, bytes, URLs,
tokens, typed text, clipboard data, or artifact identifiers.

Avoid: a generic serialization framework, arbitrary extension fields in version 1, content sniffing,
or lenient best-effort parsing.

### P0: capability and mixed-version behavior

Advertise one exact capability such as `computer-step-envelope-v1`. The primary v2 borrowed path
must prove this capability before any trajectory mutation and must never fall back to the two-call
composition. Old clients retain JSON, REST, raw screenshot, and action routes against new daemons.
Compatibility is determined through behavior tests, not package semver.

Minimum matrix: new/new succeeds; new/old rejects before mutation; old/new low-level routes still
work; malformed or unverifiable capabilities reject.

Avoid: `optimized=True`, hidden environment switches, or runtime content sniffing.

### P1: multi-image action results

Screenshot and zoom actions can appear inside ordered provider batches, including nested compound
actions. The manifest must reference ordered binary segments rather than embed base64. The SDK
hydrates those references into the existing `Screenshot` model. The final step screenshot remains
separate even if it duplicates a final screenshot action; safe deduplication is a later measured
optimization.

Minimum tests: screenshot, zoom, nested screenshot, maximum legal image count, total budget
rejection before mutation, result order, coordinate space, and provider output parity.

Avoid: special provider frame models or first-release frame deduplication.

### P1: provider-loop semantic drift

OpenAI must keep response-wide normalization, policy checks, and trajectory budgets before the first
step. Every returned `actions[]` array remains one step and one daemon mutation request. Output order
and `call_id` remain unchanged.

Anthropic must preserve `tool_use_id`, native screenshot and zoom ordering, error status, and its
current duplicate-final-image suppression. Pure screenshot and cursor-position requests remain
read-only. Provider imports stay outside core.

Avoid: per-action step calls, core provider dependencies, or provider-level mutation retries.

### P1: freshness and misleading latency

The benchmark must compare prior action then `screenshots.full()` against candidate `step()`.
Every pair uses a deterministic visual target and verifies the expected post-action pixels. This is
a target-specific freshness assertion, not universal application readiness.

Use at least 100 interleaved pairs. Hold placement, resources, image, ingress, HTTP/1.1, pooled
client, XTest, MSS/XShm, screenshot options, action payload, retries, capacity, and warmup constant.
Report cold allocation, startup, dispatch, borrow, action, capture, encoding, transfer, parse, total,
stale-frame rate, failures, and cleanup separately.

Avoid: adding medians, benchmarking screenshot before action, using first visual change in the
stable-step arm, or using 47.10 ms as an acceptance threshold.

### P1: release and documentation skew

Ship runtime artifacts first, package second, GitHub release third, and hosted docs last. Run the
protected step smoke from the published wheel, not the checkout. Historical reports and artifacts
remain immutable; publish a new dated step result tied to the measured source commit.

Update README, executable examples, OpenAI and Anthropic tutorials, API/reference, lifecycle and
handoff, configuration, performance, security, troubleshooting, migration, release, benchmark, and
the separate Mintlify repository. Use the exact statement: 47.10 ms is arithmetic over separate
historical medians, not a measured step.

Avoid: a README-only cutover, publishing docs before the package, rebuilding between package
stages, or rollback through a semantic downgrade flag.

## Minimal vertical-slice sequence

1. **Freeze the Computer Step domain contract.** Add the term to the domain vocabulary, define the
   result/errors and capability, and write public-seam failing tests. Do not add transport code yet.
2. **Make receipts phase-aware.** Extend the existing receipt/coordinator Modules just enough to
   preserve mutation certainty across observation, response, decode, timeout, and cancellation.
3. **Add the bounded step envelope.** Implement one feature-local codec with strict limits and
   hostile-input tests. Reuse the screenshot semantic validator.
4. **Add the daemon step execution seam.** Reuse the existing batch executor, input lock, XTest
   synchronization, screenshot controller, and cleanup. Do not duplicate action execution.
5. **Add synchronous and asynchronous `step()` parity.** Keep `BorrowedComputer.step()` and
   `AsyncBorrowedComputer.step()` as thin Adapters over one semantic Step Module. That Module owns
   capability admission, one sequence, decoding, typed outcomes, and terminal read-only recovery.
   The async form remains the primary documented and measured Modal Function path.
6. **Migrate provider examples and local documentation.** Preserve response-wide preflight,
   provider correlation, ordered images, and no-replay behavior in executable built-package tests.
7. **Complete compatibility and distribution evidence.** Test mixed daemon/client versions,
   wheel/sdist installation, import boundaries, daemon capabilities, lifecycle, and rollback.
8. **Run the preregistered same-topology benchmark.** Promote only after correctness, freshness,
   latency, placement, and cleanup gates pass. No live Modal run occurs without authorization.
9. **Publish in dependency order.** Runtime, package, release, hosted documentation, and then
   post-release verification. A failed step capability never silently downgrades.

## Explicit non-goals for this slice

- WebSocket control or HTTP/2.
- First-visual-change as the stable default.
- Application-readiness callbacks or a generic wait-policy framework.
- Automatic mutation retry, generic idempotency controls, or a durable full-result cache.
- Streaming, zero-copy, compression negotiation, or frame deltas without measured need.
- Warm-pool creation, positive minimum-container defaults, or universal resource defaults.
- Removing JSON, REST, raw screenshot, or action-only compatibility routes.
- A provider model loop inside core.
