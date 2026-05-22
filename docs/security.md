# Security

**The daemon has full desktop control.** It can click, type, read clipboard contents, launch applications, and read or write artifacts. Never expose its control routes as an unauthenticated public service. `/healthz` and `/readyz` are unauthenticated probe endpoints only; query-string tokens are still rejected there.

## Authentication

In Modal, authenticate the daemon with Sandbox Connect Tokens against port `8080`. Modal forwards verified user metadata in the `X-Verified-User-Data` header, which the daemon checks when `COMPUTER_USE_REQUIRE_CONNECT_USER` is on (the default).
Modal-created sandboxes set `COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY=true` so that verified
metadata forwarded by Modal's private connect proxy is accepted. The daemon default remains
fail-closed for arbitrary deployments; enable private proxy trust only when Modal Connect is the
intended ingress path to the daemon.

For local development, set `COMPUTER_USE_LOCAL_TOKEN` and have clients send `Authorization: Bearer <token>`. This is for local testing only. Do not set a weak token in production or expose `127.0.0.1:8080` to untrusted networks.

Tokens passed in URL query strings are rejected by default, because URLs leak into logs and browser history. If you have a specific reason to allow them, set `COMPUTER_USE_REJECT_QUERY_TOKENS=false` and accept the leakage risk.

The hot-session WebSocket at `/v1/session/hot` uses the same bearer-token and Modal verified-user
rules as daemon HTTP routes. It rejects query-string connect tokens by default and returns raw
screenshot bytes only as WebSocket binary frames, never in logs or JSON error payloads.

See [configuration.md](configuration.md) for the full list of auth-related variables.

## noVNC

`expose_vnc` accepts three values:

- `off` (default): no VNC server runs.
- `view_only`: a noVNC tunnel is created that lets viewers watch the desktop but not click or type.
- `control`: a noVNC tunnel is created with full remote input.

When noVNC is enabled, the URL grants live desktop access. Treat it as a [secret](glossary.md#novnc); never paste it into chat, tickets, or shared logs.
The daemon starts `x11vnc` with a password file in both `view_only` and `control` modes. The SDK
generates a per-sandbox VNC password when creating Modal sandboxes and does not include it in
metadata, tags, URLs, or logs. If you need operator-controlled noVNC access, provide
`COMPUTER_USE_VNC_PASSWORD` through your own secret channel and still treat the URL and password as
secrets.
See `examples/novnc_view_only.py` for a view-only pattern that reports only whether a URL exists.

## Logs

Structured logs redact typed text, clipboard text, screenshot bytes, tokens, provider keys, noVNC URLs, and artifact bytes. They retain lengths, hashes, dimensions, action types, elapsed time, and `call_id` so traces remain useful for debugging.

Optional OpenTelemetry is disabled by default. When `COMPUTER_USE_OTEL_ENABLED=true` and
`opentelemetry-api` is installed by the application image, spans are emitted at SDK request,
daemon route, action execution, artifact write/sync, and trace replay boundaries. Span attributes
use route paths and bounded action/artifact metadata; they do not include query strings,
Authorization headers, typed text, clipboard text, screenshot bytes, recording bytes, stdout, or
stderr.

The daemon enforces action budgets and a simple per-sandbox rolling action rate limit. Over-limit
requests fail with structured `budget_exceeded` or `rate_limited` errors and do not include typed
text, clipboard text, raw command output, tokens, screenshot bytes, or artifact bytes.
Authenticated debug routes that intentionally return command output or process log tails sanitize
known secret-bearing substrings such as bearer tokens, noVNC URLs, and artifact URIs before sending
the response.

Trace validation treats raw plaintext in `type` actions as unsafe. New action traces use
`redactions=["text"]` and replace typed text with redaction metadata before writing NDJSON.
Replay dry-runs skip redacted typed text because the original plaintext is intentionally absent.
Provider adapter provenance is redacted before it is attached to normalized actions, and daemon
traces promote only that redacted copy to `provider_action`.

## Artifacts

Artifact paths are relative. Absolute paths, `..` segments, encoded traversal, symlink escapes, and control characters are rejected server-side. The artifact root defaults to `/home/desktop/artifacts`; see [artifacts.md](artifacts.md).

Recordings are artifacts. Treat recording paths, artifact URIs, and recording bytes as sensitive
run data unless your application has explicitly sanitized and retained them. See
`examples/recording_lifecycle.py` for a lifecycle example that reports only bounded metadata.

Volume and snapshot examples follow the same rule: print bounded metadata, not raw artifact URIs,
paths, or bytes. A filesystem snapshot Image ID is operational metadata; store it in your own
access-controlled system if you need to restore from it later.
When artifact persistence is enabled, `artifacts.sync()` reports only bounded sync status. It does
not expose raw mount paths, command output, or stderr. Until a daemon-side Modal Volume commit path
is live-verified, the sync result fails honestly instead of claiming external persistence.

## Provider credentials

Core does not require OpenAI or Anthropic credentials. Provider SDK calls belong in user applications and examples, never in the daemon image.

## Adapter policy hooks

Adapters are not policy engines. They normalize provider-returned actions and call
`computer.actions`; user code owns model calls, confirmation, domain allowlists, and takeover
rules.

Use `before_action` to inspect the normalized native action before execution. The hook sees the
coordinates that will be sent to the daemon after any explicit `CoordinateSpace` transform.
Returning `deny`, `ask_user`, or `handoff` stops execution before the action batch reaches the
daemon.

Treat screen content, typed text, clipboard text, provider fixtures, and provider-returned tool
payloads as untrusted input. Fixtures should contain only synthetic actions and no credentials,
tokens, screenshots, clipboard contents, or user data.

See `examples/adapter_policy_hook.py` for a deterministic hook that stops risky native actions
without provider credentials or model calls.
