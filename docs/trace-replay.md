# Trace And Replay

Trace entries use NDJSON and include run/call metadata, provider action, normalized action, result, elapsed time, screenshot references, coordinate space, redactions, and errors.

The core package includes `TraceWriter` and `load_trace`. A replay CLI is planned for a later release; the schema is already stable enough for tests and user tooling.
