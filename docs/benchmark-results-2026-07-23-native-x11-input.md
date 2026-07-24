# Native X11 input benchmark, 2026-07-23

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
