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
  "normalized_action": {"type": "click", "x": 300, "y": 240, "button": "left"},
  "result": {"ok": true, "elapsed_ms": 47},
  "elapsed_ms": 47,
  "screenshot_before_uri": "artifact://screenshots/before_call_abc.png",
  "screenshot_after_uri": "artifact://screenshots/after_call_abc.png",
  "coordinate_space": {"desktop_width": 1440, "desktop_height": 900, "image_width": 1440, "image_height": 900},
  "redactions": ["text"],
  "error": null
}
```

## Reading traces from Python

```python
from modal_computer_use.tracing import ComputerTrace, TraceWriter, load_trace

entries = load_trace("/home/desktop/artifacts/traces/actions.ndjson")
for entry in entries:
    print(entry.call_id, entry.normalized_action, entry.elapsed_ms)

trace = ComputerTrace.load("/home/desktop/artifacts/traces/actions.ndjson")
validation = trace.validate()
plan = trace.replay(dry_run=True)
```

`TraceWriter.append(entry)` is what the daemon uses internally; user code rarely needs it.

## Redactions

By default, typed text and clipboard text are redacted from traces. The action trace writer
uses `redactions=["text"]` for typed text and stores `normalized_action.text` as
`{"redacted": true, "length": <characters>}`. The validator accepts older
`typed_text` redaction names with a warning, but new traces should use `text`.
When actions came through a provider adapter, `provider_action` is populated from the adapter's
redacted provenance metadata. Provider typed text is reported as `provider_action.text` in
`redactions`.
Tokens and noVNC URLs are always redacted. Full plaintext capture is opt-in and intended only
for local debugging.

## Replay CLI

```bash
computer-use trace validate /home/desktop/artifacts/traces/actions.ndjson
computer-use trace replay /home/desktop/artifacts/traces/actions.ndjson --dry-run
computer-use trace replay /home/desktop/artifacts/traces/actions.ndjson --base-url http://127.0.0.1:8080 --token dev
computer-use trace replay /home/desktop/artifacts/traces/actions.ndjson --target-run-id run-456
```

All commands emit JSON and return nonzero when validation or execution fails. Dry-run replay never
touches a daemon, Modal Sandbox, provider credentials, screenshots, or artifact contents. It only
produces an ordered plan: executable normalized actions are marked `execute`; pseudo-actions such
as `screenshot_after` and redacted typed text are marked `skip` with a reason.

Real replay requires an explicit target through `--base-url`, `--sandbox-id`, or `--target-run-id`.
Replay validates the trace before contacting the target, executes supported normalized actions
through `computer.actions`, skips redacted typed text, stops on the first failed action by default,
and emits per-step status. Screenshot bytes and base64 payloads in replay results are redacted; safe
metadata such as dimensions, hashes, and `artifact://` references remains available for debugging.
