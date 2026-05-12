# Trace and replay

Traces record every action the daemon executes so you can debug failures, audit agent behavior, and replay runs against a fresh sandbox. Each entry is one line of NDJSON.

## Enable trace capture

Set `COMPUTER_USE_TRACE_ACTIONS=true` on the daemon. Traces are written to `COMPUTER_USE_TRACE_DIR`, which defaults to `/home/desktop/artifacts/traces/actions.ndjson`.

## Entry format

Each line is a `TraceEntry` (defined in `modal_computer_use.models`):

```json
{
  "ts": "2026-05-11T14:00:00Z",
  "run_id": "run-123",
  "call_id": "call_abc",
  "sequence": 1,
  "source": "openai-adapter",
  "provider_action": {"type": "click", "x": 300, "y": 240},
  "normalized_action": {"type": "left_click", "coordinate": [300, 240]},
  "result": {"ok": true, "elapsed_ms": 47},
  "elapsed_ms": 47,
  "screenshot_before_uri": "artifact://screenshots/before_call_abc.png",
  "screenshot_after_uri": "artifact://screenshots/after_call_abc.png",
  "coordinate_space": {"desktop_width": 1440, "desktop_height": 900, "image_width": 1440, "image_height": 900},
  "redactions": ["typed_text"],
  "error": null
}
```

## Reading traces from Python

```python
from modal_computer_use.tracing import TraceWriter, load_trace

entries = load_trace("/home/desktop/artifacts/traces/actions.ndjson")
for entry in entries:
    print(entry.call_id, entry.normalized_action, entry.elapsed_ms)
```

`TraceWriter.append(entry)` is what the daemon uses internally; user code rarely needs it.

## Redactions

By default, typed text and clipboard text are redacted from traces (length and SHA-256 retained where useful). Tokens and noVNC URLs are always redacted. Full plaintext capture is opt-in and intended only for local debugging.

## Replay CLI

A `computer-use trace validate / replay` CLI is planned. Validation lands in v0.2; controlled replay against a fresh sandbox lands in v1.0. See [section 14.2 of the v6 spec](spec/modal_computer_use_spec_v6.md) for the planned interface.
