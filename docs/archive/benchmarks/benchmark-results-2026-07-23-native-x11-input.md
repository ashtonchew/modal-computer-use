# Native X11 input benchmark, 2026-07-23

> **Archive category:** Diagnostic  
> **Date or revision:** 2026-07-23 to 2026-07-24  
> **Question:** Did native XTest input reduce daemon execution time relative to the retained
> `xdotool` adapter?  
> **Disposition:** The report supported the native-input implementation and retains its exact A/B
> context, including the dirty-worktree three-sample comparison. It is not the current
> cross-provider reference.

This benchmark validates the native X11 input implementation against the retained
`xdotool` compatibility adapter. It was run from the same local checkout, caller, Modal profile,
environment, image type, ingress, and benchmark contract for both arms.

## Environment

- Modal profile: `auto-alphafold3`
- Modal environment: `main`
- Image: inline standard image
- Ingress: attested tunnel over HTTP/1.1
- Resource placement: Modal provider default
- Browser/GPU: none
- Measured iterations: 3 after one warmup
- Input rate limit: disabled
- Typing: `method="keystrokes"`, `delay_ms=0`
- Source: dirty implementation worktree; both final arms used the same source state

Commands:

```bash
uv run computer-use benchmark sdk --create-modal-sandbox --surfaces daemon-http \
  --input-backend xtest --iterations 3 --output /private/tmp/native-x11-input-xtest.json

uv run computer-use benchmark sdk --create-modal-sandbox --surfaces daemon-http \
  --input-backend xdotool --iterations 3 --output /private/tmp/native-x11-input-xdotool.json
```

Both commands created one sandbox, waited for daemon readiness, ran the benchmark and verification
suite, and terminated the sandbox. Both reports returned `ok=true`. Cursor-position and controlled
`xev` typing readbacks returned `ok` in both arms. Every measured mouse and typing case reported the
forced adapter.

## Results

Daemon time isolates action execution from Modal ingress overhead. End-to-end time includes the
attested-tunnel request path.

| Case | XTest daemon mean | xdotool daemon mean | Native speedup | XTest end-to-end mean | xdotool end-to-end mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| Move + click | 1.15 ms | 146.33 ms | 127.7x | 90.92 ms | 229.77 ms |
| Four move/click pairs | 4.80 ms | 443.99 ms | 92.5x | 77.56 ms | 522.94 ms |
| Type 100 characters | 20.61 ms | 119.77 ms | 5.8x | 92.46 ms | 198.16 ms |
| Type 1,000 characters | 201.34 ms | 607.35 ms | 3.0x | 273.37 ms | 686.35 ms |

The native implementation is the canonical fast path. `xdotool` remains useful as an explicit
compatibility adapter and as an automatic fallback when native preflight fails before any event is
emitted.

## Fully optimized Modal operation harness, 2026-07-24

The native-X11 branch was stacked on the canonical colocated-region harness and run at clean,
reviewed revision `fe788eaf74104ded52ce000ae3041b0660e10e03`, rebased onto main at
`7fd5e8dfae7ecf0cd38d8729cd257aab42fb1059`. The run used a 4 CPU / 8192 MiB Chromium target
and a separate 1 CPU / 1024 MiB Connect runner, both requested in `us-west-2`. It forced XTest,
disabled input throttling, used one warmup, and measured 30 iterations of every selected public
daemon operation.

The run returned `ok=true` with zero failures. Every selected operation completed 30/30 samples,
all input cases reported `xtest`, external and colocated cursor/typing readbacks passed, runner
preflight passed, and the post-run Modal container inventory was empty.

| Public operation, p50 | Modal optimized XTest | Daytona default | E2B default | Modal vs Daytona | Modal vs E2B |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot | **38.3 ms** | 214.2 ms | 177.0 ms | **5.59x** | **4.62x** |
| Move + click | **3.7 ms** | 347.0 ms | 279.5 ms | **93.77x** | **75.54x** |
| Four move/click pairs | **8.5 ms** | 1,429.5 ms | 1,122.1 ms | **167.21x** | **131.25x** |
| Type 100 characters | **10.0 ms** | 634.6 ms | 4,100.7 ms | **63.39x** | **409.65x** |
| Type 1,000 characters | **52.1 ms** | 5,371.1 ms | 41,520.4 ms | **103.01x** | **796.31x** |
| Command echo | 86.1 ms | 120.9 ms | **62.9 ms** | **1.40x** | 0.73x |

The Daytona and E2B columns reuse the distinct fresh provider-default run at `cebdaa3`, which used
three measured product lifecycles per provider. The operation boundaries match, but the samples are
not contemporaneous or paired, so these are descriptive best-system ratios rather than isolated
provider causal effects.

The same-run external Connect control used the same target and operation boundaries. Moving the
caller into the selected region reduced screenshot p50 from 74.4 ms to 38.3 ms, move/click from
38.6 ms to 3.7 ms, four move/click pairs from 43.8 ms to 8.5 ms, 100-character typing from
45.7 ms to 10.0 ms, 1,000-character typing from 87.3 ms to 52.1 ms, and command echo from 115.0 ms
to 86.1 ms.

Against the parent optimized Modal artifact, the native-X11 path was 72.0x faster for 100-character
typing and 125.3x faster for 1,000-character typing. Screenshot was 1.02x faster, move/click 1.24x,
the four-click sequence 1.08x, and command echo 1.09x faster. Those historical
comparisons are unpaired and should be read as directional; the controlled native-vs-compatibility
A/B above isolates adapter cost.

The secret-safe compact evidence is
`benchmark-data/modal-optimized-native-x11-us-west-2-2026-07-24.json`. The credential-bearing raw
report remains ignored at
`benchmark-results/native-x11-colocated-us-west-2-2026-07-24-rebased.json`, bound by SHA-256 in the
compact artifact.

## Optimized action-to-frame and availability harness, 2026-07-24

The 34.6 / 51.0 ms action-observation result in this section is historical,
pre-hash-verification evidence. It remains recorded with its original label and is not eligible for
the current Modal-only experimental result.

The provider-default comparison below measures every provider through its public SDK from the local
macOS caller. It is intentionally neutral, but it is not Modal's optimized production shape. The
repository's dedicated optimization harness was therefore run separately against the exact
revisioned Chromium image for commit `4ea0deb8d2cb37668cab3310a5394487e9140869`.

The harness preregistered and executed 30 independent cold starts, 30 persistent-session action
observations, and 30 warm-pool claims, without replacement samples. A region probe selected
`us-west`; the action path used a separate same-region Modal runner connected to a persistent hot
target and consumed the binary causal-observation envelope.

| Optimized Modal metric | p50 | p95 | Valid | Failures |
| --- | ---: | ---: | ---: | ---: |
| Persistent action to first causally changed frame | **34.6 ms** | **51.0 ms** | 30/30 | 0 |
| Warm-pool claim to first authenticated frame | 1,597.4 ms | 1,765.4 ms | 30/30 | 0 |
| Cold request to first authenticated frame | 11,195.0 ms | 12,712.9 ms | 30/30 | 0 |

The 34.6 ms result is a separate optimized screenshot-sensitive agent-loop result: timing starts
immediately before correlated input dispatch and stops only after the matching causal observation
contains a changed, reconstructable frame. It should not be confused with the 211.6 ms
provider-default `screenshot_full` RPC below, which crosses the external attested HTTP/1.1 tunnel
from the local caller and returns a full PNG, or with the 38.3 ms fully optimized standalone
screenshot operation above.

For context, the immediately preceding neutral provider run at commit
`cebdaa3ea91c8360b3f634d373d7aeb8a6579267` reported the following public-SDK action latencies. The
optimized image revision adds only benchmark documentation on top of that native-X11 revision, but
the evidence is still a distinct run and source SHA. Modal's optimized action-to-frame path is
descriptively 10.0x lower latency than Daytona's action-only p50 and 8.1x lower than E2B's
action-only p50. These are not controlled speedup claims because the vendor timers stop when the
action call completes, while the optimized Modal timer additionally waits for a causally matching
changed frame.

| Context metric | Modal optimized | Daytona default | E2B default |
| --- | ---: | ---: | ---: |
| Warm interaction p50 | **34.6 ms action → changed frame** | 347.0 ms action call | 279.5 ms action call |
| Warm interaction p95 | **51.0 ms action → changed frame** | 367.6 ms action call | 306.1 ms action call |
| Cold request → first frame p50 | 11,195.0 ms | 10,638.5 ms | **1,156.7 ms** |
| Cold request → first frame p95 | 12,712.9 ms | 10,795.4 ms | **1,378.9 ms** |

Warm availability hit 30/30 claims with no pool misses or cold fallbacks. The partial target-only
cost estimate was `$0.12697` for the on-demand attempts and `$0.50711` for 1,373.1 warm-pool idle
resource-seconds; runner compute, control-plane charges, claimed-session compute, and billing
adjustments are excluded as recorded in the artifact.

The secret-safe normalized evidence is
`benchmark-data/modal-optimization-native-x11-2026-07-24.json`. It preserves the complete attempt
records, command manifest, measurement contract, region attestation, cleanup outcomes, raw artifact
digest, and partial-cost assumptions. The credential-bearing raw report remains untracked.

## Same-run provider comparison, 2026-07-24

The PR commit was also run through the repository's neutral provider-default comparison against
Daytona and E2B. The run used the established credential symlink, Chromium at 1024x768, the browser
resource profile, provider-default placement, one warmup, and three measured product lifecycles per
provider. Modal used the attested HTTP/1.1 tunnel and forced XTest.

```bash
uv run computer-use benchmark compare \
  --create-modal-sandbox \
  --provider modal-daemon \
  --provider daytona \
  --provider e2b \
  --modal-ingress attested-tunnel \
  --resource-profile browser \
  --browser chromium \
  --input-backend xtest \
  --iterations 3 \
  --env-file .env \
  --output benchmark-results/candidates/provider-compare-native-x11-all-20260724.json \
  --json
```

The complete run returned `ok=true` with zero failures. Every case recorded 3/3 measured samples,
all providers passed cursor and controlled typing readback, and Modal's input cases reported
`xtest`. Values below are p50; ratios above `1.00x` mean Modal is faster.

| Case | Modal XTest | Daytona | E2B | Modal vs Daytona | Modal vs E2B |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product create to first screenshot | 6,419.1 ms | 10,638.5 ms | 1,156.7 ms | **1.66x** | 0.18x |
| Full screenshot | 211.6 ms | 214.2 ms | 177.0 ms | **1.01x** | 0.84x |
| Move + click | 156.5 ms | 347.0 ms | 279.5 ms | **2.22x** | **1.79x** |
| Four move/click pairs | 160.8 ms | 1,429.5 ms | 1,122.1 ms | **8.89x** | **6.98x** |
| Type 100 characters | 168.6 ms | 634.6 ms | 4,100.7 ms | **3.76x** | **24.32x** |
| Type 1,000 characters | 277.5 ms | 5,371.1 ms | 41,520.4 ms | **19.35x** | **149.60x** |
| Command echo | 197.9 ms | 120.9 ms | 62.9 ms | 0.61x | 0.32x |

This result supports action-path claims, not a universal provider ranking. E2B remains much faster
for product startup, both competitors remain faster for command execution, and E2B is faster for
the default screenshot call. Modal's advantage is concentrated in native input and daemon batching.

The secret-safe PR evidence is
`benchmark-data/provider-compare-native-x11-2026-07-24-candidate.json`. Its raw artifact SHA-256 is
`7befb822232442cd4dcf46fcd12cf684855a134262885d4c759ce2af643421eb`; the raw report remains
untracked. The artifact name records its status when captured. The implementation was subsequently
merged in PR #124; this dated report remains historical evidence rather than a moving current
reference.

## Diagnostic findings during the run

Cloud validation and independent autoreview caught issues that local happy-path fakes did not:

1. A `UV_NO_EDITABLE=1` invocation reused an older wheel. That run was rejected because typing
   metadata still said `xdotool`. Dirty-tree Modal validation must use the editable workspace or a
   freshly built immutable revision.
2. Xlib expects semantic punctuation keysyms such as `minus`, not literal punctuation on every
   server. The native keyboard resolver now maps ASCII punctuation and control characters to their
   canonical X11 keysym names.
3. An early readiness poll could cache `XOpenDisplay failed` before Xvfb finished starting. Display
   connection failures and missing window-manager properties are now retryable; missing libraries
   and a missing XTest extension remain fail-closed.
4. Xlib's default asynchronous error handler can terminate the daemon when a window disappears
   during enumeration. The shared Xlib runtime now enables thread support and installs a nonfatal
   process-wide handler; feature adapters still validate synchronous operation results.
5. Native keyboard fallback is limited to pre-emission failures. Active-XKB-group misses and
   unmapped click modifiers take the compatibility path, while possibly partial input is never
   replayed. Held shifted keys retain owned modifiers until the matching key-up.
6. Native window readiness verifies a live, self-referencing `_NET_SUPPORTING_WM_CHECK` owner
   before trusting client-list and supported-operation properties.

Typing speed cases now set `delay_ms=0`. Functional tests separately verify requested key duration
and inter-character delay behavior; including an intentional delay in a throughput benchmark would
measure pacing rather than adapter cost.

After the implementation was rebased onto current `origin/main`, the full local suite passed with
919 tests and 10 credential-gated skips; Ruff, mypy, and the checked-in OpenAPI check also passed.
A fresh one-iteration forced-XTest Modal smoke returned `ok=true`, attributed mouse and typing cases
to `xtest`, and passed both cursor-position and controlled typing readback verification.

## Final reviewed revision, 2026-07-24

The complete fallback-safety and XKB-resolution tranche was committed as
`474331688b4e448638467f60ace59efad59aeb9c` and rerun through the same fully optimized
30-sample colocated harness. The worktree was clean, the active profile remained
`auto-alphafold3`, every input sample reported `xtest`, and cursor and typing readbacks passed.

| Public operation, p50 | Final reviewed run | Prior optimized run | Change |
| --- | ---: | ---: | ---: |
| Full screenshot | **33.65 ms** | 38.32 ms | **12.2% faster** |
| Move + click | 4.57 ms | **3.70 ms** | 23.6% slower |
| Four move/click pairs | 11.13 ms | **8.55 ms** | 30.2% slower |
| Type 100 characters | **9.67 ms** | 10.01 ms | **3.4% faster** |
| Type 1,000 characters | **32.78 ms** | 52.14 ms | **37.1% faster** |

The input change is therefore a decisive long-text win, a small 100-character win, and neutral
to noisy for already-sub-12-ms mouse operations; no further mouse optimization is accepted from
this run without a paired ablation. The final compact evidence is
`benchmark-data/modal-native-x11-final-us-west-2-2026-07-24.json`; raw samples remain ignored.
