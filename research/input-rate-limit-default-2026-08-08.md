# Default input limiter for the optimized SDK

Date: 2026-08-08

## Live outcome

The first preregistered same-runtime gate rejected the `500/1000` candidate. On the minimum
1 CPU/2,048 MiB AWS `us-west-2` runtime, 80 ordered mixed XTest batches sustained 507.802
normalized tokens per second. All 80 batches completed in order, cleanup succeeded, and RSS grew
by about 3 MiB. The run did not meet the 1,000-token promotion threshold.

Follow-up runs showed material capacity variance. The same fixed workload sustained between
246.495 and 579.158 normalized tokens per second. Use `100/400`: five times the former sustained
limit, about 4.4 times the reciprocal 44.29 ms Step median, and below half of the slowest observed
mixed-workload throughput. Require repeated clean-commit runs to pass the 200-token gate before
release. Do not promote `500/1000` or `200/400` from this report.

## Pre-measurement candidate

Use this as the default promotion candidate:

```text
Refill rate: 500 normalized input-work tokens per second
Bucket capacity: 1,000 tokens
Admission: reserve the complete recursive batch before mutation
Scope: one daemon-local input bucket shared by all input mutation routes
```

Do not promote this number only because it is higher than the current value. Run a same-runtime capacity test first. The minimum supported runtime must sustain at least 1,000 representative tokens per second without lost input, X11 errors, cleanup failures, unhealthy CPU or memory growth, or a material increase in tail latency. Use `200/400` if that test fails.

Do not use `2,000/4,000` as the default without stronger evidence. The repository's available native-input measurements put simple-action throughput near 1,300 to 1,400 actions per second. A 2,000-action-per-second ceiling could sit above the backend's useful capacity and would not protect it from sustained saturation. Weighted tokens could change that comparison, so the capacity test must use the final weight policy.

No external specification defines the correct numeric limit for this SDK. Primary sources support a token bucket, complete request admission, and capacity-based tuning. They do not support `20`, `50`, `500`, or another universal rate.

## Why the current limit is wrong

The public configuration allows 50 actions in a batch but defaults to 20 actions per second ([`ActionConfig`](../src/modal_computer_use/config.py#L173-L189)). The current rolling-window limiter admits each action separately ([`BudgetPolicy`](../src/modal_computer_use/daemon/budget_policy.py#L110-L143)). The batch executor therefore can reject action 21 after actions 1 through 20 have changed the desktop ([batch executor](../src/modal_computer_use/daemon/actions/batch.py#L333-L380)).

This is both a performance problem and an atomic-admission problem. A request can pass structural validation and then fail because of the limiter after partial mutation.

Git history does not provide a capacity reason for `20`. Commit `15e5130bc4caca0035ec73202c1e102f22c72136` added the constant as part of broader recording and budget work. It added no benchmark or provider constraint for that number.

## Repository capacity evidence

The new fused Step benchmark measured a 44.29 ms median for one click and its immediate screenshot ([dated report](../docs/benchmark-results-2026-08-08-computer-step.md)). Its reciprocal is about 22.6 serialized one-action steps per second:

```text
1,000 ms / 44.29 ms = 22.58 steps per second
```

This is a service-time calculation, not a saturation test. The benchmark paced operations at 125 ms and did not measure maximum steady-state throughput ([candidate artifact](../benchmark-data/computer-step-candidate-2026-08-08.json)). Still, it shows that `20/sec` can intersect the normal warm path. A `500/sec` refill gives 22.1 times the reciprocal median:

```text
500 / 22.58 = 22.14
```

The native X11 replication gives a second bound. With the limiter disabled, the XTest backend averaged 1.56 ms for a two-action move-and-click batch and 5.72 ms for an eight-action move-and-click sequence ([replication artifact](../benchmark-data/modal-native-x11-backend-ab-replication-2026-08-02.json), [benchmark actions](../src/modal_computer_use/benchmarks/constants.py#L47-L60)). These means imply about 1,282 and 1,399 simple actions per second if the measured batches were repeated with no other work:

```text
2 actions / 0.0015598 s = 1,282 actions per second
8 actions / 0.0057199 s = 1,399 actions per second
```

These values are not a safe operating capacity. They use arithmetic means, short samples, one workload, and no sustained-load health gate. They do show why `2,000` flat action tokens per second is not yet a defensible protection boundary. It may exceed useful native-action capacity.

The daemon also serializes each action batch and its Step screenshot under one input lock ([batch executor](../src/modal_computer_use/daemon/actions/batch.py#L298-L305)). One active trajectory lease owns the daemon at a time ([lease coordinator](../src/modal_computer_use/daemon/leases.py#L74-L145)). These constraints prevent interleaving. They do not bound a runaway trajectory over time.

## Native work is not a flat action count

The XTest implementation queues native events and then calls `XSync` before it returns success ([native input session](../src/modal_computer_use/daemon/desktop/xtest.py#L522-L557)). The action shapes expand differently:

- A move produces one motion event.
- A coordinate click produces one motion event plus press and release events. Modifiers add more key events ([mouse input](../src/modal_computer_use/daemon/desktop/mouse.py#L530-L562)).
- A triple click produces three press-release pairs, plus optional motion and modifier events.
- A scroll with `amount=N` produces `2N` button events, plus optional motion. The public schema permits an amount up to 10,000 ([mouse input](../src/modal_computer_use/daemon/desktop/mouse.py#L570-L586), [`ScrollAction`](../src/modal_computer_use/models.py#L457-L469)).
- A drag can include up to 1,024 path points under the daemon setting. It emits pointer movement as well as button and modifier state changes ([daemon settings](../src/modal_computer_use/daemon/settings.py#L170-L185)).
- Native typing builds key press and release sequences. Explicit keystroke input is chunked at 4,096 characters, while `auto` uses clipboard paste for text longer than 80 characters ([keyboard input](../src/modal_computer_use/daemon/desktop/keyboard.py#L630-L702)).

XTEST does not impose a human-speed ceiling. `CurrentTime` means no artificial delay for fake input events ([XTEST library specification](https://www.x.org/releases/X11R7.7/doc/libXtst/xtestlib.html)). `XSync` flushes requests and waits for the X server to process them; it is a completion boundary, not a pacing rule ([XSync manual](https://xorg.freedesktop.org/archive/X11R7.5/doc/man/man3/XSync.3.html)). Playwright also defaults keyboard delays to zero ([Playwright Keyboard](https://playwright.dev/docs/api/class-keyboard)).

For these reasons, call the units **normalized input-work tokens**, not events and not actions. The weights are an admission policy. They are not a claim about the exact number of native X11 events.

## Weight policy

Keep the first version small and inspectable:

| Action | Normalized cost |
| --- | ---: |
| move, mouse down, mouse up, keypress, release all | 1 |
| click, double click, triple click | click count |
| hotkey | `max(1, ceil(number of keys / 4))` |
| type | `1 + ceil(number of characters / 32)` |
| scroll | `1 + ceil(amount / 32)` |
| coordinate drag without a path | 1 |
| drag with a path | `1 + ceil(number of path points / 32)` |
| hold key with nested actions | `1 + sum(nested action costs)` |
| wait, screenshot, zoom, cursor query | 0 input tokens |

These weights distinguish the largest known expansion factors without copying backend implementation into the policy. They also bound a single request. With a 1,000-token bucket:

- A maximum 50-action simple batch costs 50. A full bucket admits 20 such batches.
- A maximum 50-action triple-click batch costs 150. A full bucket admits six complete batches.
- One explicit type action can contain about 31,968 characters before its normalized cost exceeds the bucket.
- One maximum 10,000-unit scroll costs 314 and can be admitted as one complete request.
- One maximum 1,024-point drag costs 33.

The weight function must be versioned and tested as a product policy. Record the resolved refill rate, capacity, and policy version in capabilities and sanitized benchmark artifacts.

Do not branch weights on whether the backend later chooses XTest, xdotool, or clipboard. Admission must finish before dispatch. Backend-dependent admission would make fallback behavior unpredictable.

## Policy comparison

| Policy | Performance effect | Protection effect | Judgment |
| --- | --- | --- | --- |
| No limiter | Cannot throttle normal work. | Leaves sustained runaway input unbounded. Batch size, timeout, and serialization do not replace a trajectory-rate bound. | Reject as the general default. |
| `50/100` | Only 2.2 times the reciprocal Step median. A 50-action triple-click batch costs 150 and can never enter the bucket. | Provides a strong bound but conflicts with valid public batches. | Too low. |
| `200/400` | 8.9 times the reciprocal Step median. It admits eight maximum simple batches or two maximum triple-click batches from a full bucket. | Meaningful protection with limited capacity evidence. | Safe fallback. |
| `500/1000` | 22.1 times the reciprocal Step median. It admits 20 maximum simple batches or six maximum triple-click batches. | Likely below simple native saturation while well above normal provider-loop demand. | Recommended promotion candidate. |
| `2000/4000` | Effectively invisible to normal provider loops. | May be above the measured simple-action capacity, so it may not stop sustained saturation. | Do not make default without a new capacity result. |
| Adaptive rate | Could track runtime capacity. | Adds feedback, state, and failure modes before the fixed policy is characterized. | Defer. Use fixed atomic admission first. |

OpenAI requires applications to run every action in the returned `actions[]` array in order and then capture the updated screen. It supports batched actions and does not prescribe a local input rate ([OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use), [OpenAI Agents SDK](https://openai.github.io/openai-agents-js/guides/tools/)). This supports complete batch admission. It does not support a particular number.

## Admission and error contract

Use strict token-bucket conformance:

1. Parse and validate the complete recursive action tree.
2. Compute its complete normalized cost.
3. Reject a request whose total cost is greater than bucket capacity. Use a non-retryable request/configuration error because waiting cannot make it fit.
4. Refill from a monotonic clock.
5. Reserve the entire cost atomically while holding the same feature-local admission lock.
6. Start mutation only after admission succeeds.
7. Do not consume more tokens during execution.
8. Do not refund tokens after dispatch. The work was attempted and its outcome may be ambiguous.

RFC 3290 describes token buckets with a refill rate and maximum burst size. Strict conformance requires enough tokens for the complete unit before admission ([RFC 3290 section 5.1.3 and appendix A.4](https://www.rfc-editor.org/rfc/rfc3290.html)). This maps cleanly to whole-batch reservation.

When the batch can fit but the bucket does not have enough current credit, return:

- HTTP `429`;
- stable code `rate_limited`;
- `required_tokens`, `available_tokens`, `refill_tokens_per_second`, and `bucket_capacity`;
- precise `retry_after_ms` in the JSON body;
- `Retry-After: ceil(retry_after_ms / 1000)` as integer seconds;
- cache prevention headers.

RFC 6585 says a `429` response should explain the condition and may include `Retry-After` ([RFC 6585 section 4](https://www.rfc-editor.org/rfc/rfc6585.html#section-4)). HTTP permits an HTTP date or an integer number of seconds in that header, so keep millisecond precision in the response body ([RFC 9110 section 10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)).

Never retry a mutation automatically. HTTP permits automatic retry of a non-idempotent request only when the client knows that the request semantics are idempotent or can prove that the original request was not applied ([RFC 9110 section 9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)).

## Scope and lifecycle

Use one daemon-local bucket for all routes that can emit input. This matches the protected resource: one desktop, one X11 server, one persistent input backend, and one input lock. Do not reset the bucket to full on every lease acquisition. A caller could churn leases to bypass the sustained limit.

The active lease still gives the application one trajectory owner. The rate-limit state should remain daemon-local across lease handoffs. Direct compatibility routes must use the same admission policy so they cannot bypass the limit.

Keep these separate controls:

- maximum recursive batch actions;
- maximum batch and action duration;
- total trajectory action budget;
- JSON and string length limits;
- drag path and key collection limits;
- one input lock and one active lease;
- command concurrency, process, and timeout controls;
- approval and policy checks for high-impact actions.

OWASP recommends rate limits together with execution-time, memory, payload-size, and per-request operation limits. It also says limits must be tuned for the business need ([OWASP API4:2023](https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/)). Rate limiting protects capacity. It is not semantic authorization.

## Promotion test

AWS describes token-bucket capacity as burst allowance and refill as sustained rate. Its reliability guidance says a service's known capacity should be established through load testing ([AWS ECS throttling](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/request-throttling.html), [AWS Well-Architected REL05-BP02](https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_mitigate_interaction_failure_throttle_requests.html)). Apply that rule here.

Run the promotion test on the minimum supported Modal resource configuration and the default persistent XTest backend. Use the same image and X11 setup as the optimized SDK. Test at least:

- single move and click steps;
- 50-action simple batches;
- mixed click, keypress, type, scroll, and drag batches;
- the largest valid weighted action near bucket capacity;
- sustained offered loads below, at, and above 500 tokens per second;
- a 1,000-token initial burst;
- leased Step requests and direct compatibility routes.

Pre-register pass conditions. Require:

- no missing, reordered, or duplicate observed input;
- no XTest/Xlib errors;
- no pressed-key or pressed-button cleanup failures;
- no partial execution after an admission rejection;
- exact token accounting and retry time;
- stable daemon health, CPU, and resident memory;
- no material p95 or p99 latency regression below the configured rate;
- a clean recovery after deliberate overload;
- sanitized raw observations and failure records.

The runner must measure actual offered and completed weighted rates. Do not infer capacity from the 44.29 ms Step median or the short native-input benchmark alone.

## Final judgment

`500/1000` was the research candidate, but the live gate did not establish the required two-times
capacity margin. It must not become the default from this evidence.

`2000/4000` is not yet a better default. It has more application headroom, but current evidence does not show that the minimum runtime can process that sustained rate safely. A limiter above backend capacity only records overload after the protected resource is already saturated.

The first fallback did not preserve a two-times margin across repeated runs. Use `100/400`. If
later evidence shows materially higher sustained normalized throughput with a suitable safety
margin, raise the default in a new evidence-backed change.
