# Security

## Secure the primary trajectory

Treat the versioned session handle as sensitive routing identity even though it is not a bearer
credential. Do not log, publish, or return it through an unauthenticated application endpoint. The
application-owned Modal Function resolves fresh access when the borrow starts.

With attested-tunnel ingress, `borrow_async()` exchanges authentication once and reuses that client
state for the trajectory. Every daemon request still crosses authenticated Modal ingress. Client
reuse does not remove ingress, authorization, or the need to protect the Function entry point.

Placement, handle protocol, live policy, readiness, and capability checks fail before the default
trajectory can mutate the desktop. The SDK does not silently fall back to an external caller or a
different transport. After possible dispatch, it does not replay a mutation automatically.

`computer.step()` returns action data and screenshot bytes in one versioned envelope. Treat the
whole response as secret-bearing. Do not log the envelope, decoded screenshot, typed text,
clipboard text, daemon URL, bearer token, or receipt data. If response decoding fails after possible
dispatch, use receipt recovery and do not replay the step.

Keep daemon URLs, bearer tokens, noVNC URLs, session handles, typed text, clipboard text, screenshot
bytes, and artifact bytes out of logs and error text. Do not include them in resolved configuration
reports or benchmark artifacts. A byte-backed `Screenshot` is still secret-bearing screen content.

Owner and borrower roles are separate. The owner retains lifecycle authority and terminates the
desktop only after remote work finishes. The borrower holds one exclusive trajectory lease,
releases it on exit, and never terminates the owner's Sandbox.

**The daemon has full desktop control.** It can click, type, read clipboard contents, launch applications, and read or write artifacts. Never expose its control routes as an unauthenticated public service. `/healthz` and `/readyz` are unauthenticated probe endpoints only; query-string tokens are still rejected there.

## Authentication

Pure Connect ingress uses a Sandbox Connect Token scoped to port `8080`. Modal forwards verified
user metadata in `X-Verified-User-Data`. The SDK enables private-proxy trust only for this ingress.
Raw and attested tunnel modes never trust that header because a tunnel client can supply it.
Attested tunnel mode uses the SDK-generated daemon bootstrap bearer to mint a short-lived session
token, including on attach and function handoff.

For local development, set `COMPUTER_USE_LOCAL_TOKEN` and have clients send `Authorization: Bearer <token>`. This is for local testing only. Do not set a weak token in production or expose `127.0.0.1:8080` to untrusted networks.

The daemon fails closed when no authenticator is configured. Local unauthenticated use requires
`COMPUTER_USE_ALLOW_UNAUTHENTICATED_LOOPBACK=true`; the entry point then refuses any non-loopback
bind. This flag is not a deployment authentication mode.

Verified Connect access and the static bootstrap tunnel bearer may mint short-lived tunnel session
tokens. Process execution routes require bootstrap, Connect, or local authentication, and desktop
child processes do not inherit daemon-owned token or password variables. A minted token cannot
directly call those routes or mint another token. The default lifetime is one hour and the default
active-session count is unlimited. Operators may set a positive lifetime and an optional active
session cap. Expired tokens are pruned on access; a full configured store rejects minting instead
of evicting an active client.

A minted token still grants full computer use. A client can open a terminal or otherwise execute
code as the desktop user, so the token lifetime limits ordinary bearer reuse; it is not a sandbox
inside the Sandbox. Do not give a minted token to a party that should not control that Sandbox.

Tokens passed in URL query strings are rejected by default, because URLs leak into logs and browser history. If you have a specific reason to allow them, set `COMPUTER_USE_REJECT_QUERY_TOKENS=false` and accept the leakage risk.

The hot-session WebSocket at `/v1/session/hot` uses the same bearer-token and Modal verified-user
rules as daemon HTTP routes. It rejects query-string connect tokens by default and returns raw
screenshot bytes only as WebSocket binary frames, never in logs or JSON error payloads.

Daemon HTTP responses use `Cache-Control: no-store`, including health, readiness, metadata, and
errors. HTTP JSON bodies and WebSocket messages default to a 16 MiB ceiling. Artifact uploads are
excluded because they stream. Hot-session and observation-stream connections have separate global
caps. All limits are configurable; see [configuration.md](configuration.md).

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

Structured logs redact typed text, clipboard text, screenshot bytes, tokens, provider keys, noVNC
URLs, and artifact bytes. Sensitive values use `{"redacted": true, "length": N}` and do not retain
content hashes. Logs may retain dimensions, action types, elapsed time, and `call_id`.

Compatibility typing passes text to `xdotool type --file -` through subprocess stdin. Typed text is
not included in subprocess argv, where it could otherwise be exposed by process-list or
`/proc/<pid>/cmdline` inspection. The plaintext still necessarily exists in daemon memory and the
stdin pipe while the action runs; this is exposure reduction, not encryption.

Optional OpenTelemetry is disabled by default. When `COMPUTER_USE_OTEL_ENABLED=true` and
`opentelemetry-api` is installed by the application image, spans are emitted at SDK request,
daemon route, action execution, artifact write/sync, and trace replay boundaries. Span attributes
use route paths and bounded action/artifact metadata; they do not include query strings,
Authorization headers, typed text, clipboard text, screenshot bytes, recording bytes, stdout, or
stderr.

The daemon combines action budgets with a weighted, daemon-local input token bucket. It reserves a
complete recursive action batch before mutation. This prevents partial execution at a rate-limit
boundary. Over-limit requests fail with structured `budget_exceeded`, `rate_limited`, or
`input_cost_exceeds_burst` errors. Errors may report non-secret input-work token counts, but they do
not include bearer tokens, typed text, clipboard text, raw command output, screenshot bytes, or
artifact bytes.
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

Artifact byte quotas are rechecked under the daemon mutation lock before commit. Uploads reuse the
streamed byte count and digest, and a failed commit restores the previous artifact and manifest.
Active recordings are stopped during daemon shutdown before desktop processes exit.

## Modal resource ownership

New Modal Sandboxes carry a `computer-use.app_id` ownership tag. Listing, name lookup, ID attach,
reuse, and cleanup are scoped to the requested Modal app and verify that tag. Broad cleanup never
terminates an untagged legacy Sandbox.

`allow_legacy_unscoped=True` is a migration-only attach option. It accepts only an untagged
Sandbox that Modal already resolved inside the requested app. A conflicting ownership tag always
fails. Remove the option after legacy Sandboxes are drained.

`sandbox_kwargs` cannot replace app, network, ingress, environment, readiness, or ownership-tag
fields managed by the SDK. Ordinary Modal arguments remain available through their named SDK
parameters or non-conflicting keyword arguments.

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
