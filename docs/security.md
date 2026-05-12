# Security

**The daemon has full desktop control.** It can click, type, read clipboard contents, launch applications, and read or write artifacts. Never expose it as an unauthenticated public service.

## Authentication

In Modal, authenticate the daemon with Sandbox Connect Tokens against port `8080`. Modal forwards verified user metadata in the `X-Verified-User-Data` header, which the daemon checks when `COMPUTER_USE_REQUIRE_CONNECT_USER` is on (the default).

For local development, set `COMPUTER_USE_LOCAL_TOKEN` and have clients send `Authorization: Bearer <token>`. This is for local testing only. Do not set a weak token in production or expose `127.0.0.1:8080` to untrusted networks.

Tokens passed in URL query strings are rejected by default, because URLs leak into logs and browser history. If you have a specific reason to allow them, set `COMPUTER_USE_REJECT_QUERY_TOKENS=false` and accept the leakage risk.

See [configuration.md](configuration.md) for the full list of auth-related variables.

## noVNC

`expose_vnc` accepts three values:

- `off` (default): no VNC server runs.
- `view_only`: a noVNC tunnel is created that lets viewers watch the desktop but not click or type.
- `control`: a noVNC tunnel is created with full remote input.

When noVNC is enabled, the URL grants live desktop access. Treat it as a [secret](glossary.md#novnc); never paste it into chat, tickets, or shared logs.

## Logs

Structured logs redact typed text, clipboard text, screenshot bytes, tokens, provider keys, noVNC URLs, and artifact bytes. They retain lengths, hashes, dimensions, action types, elapsed time, and `call_id` so traces remain useful for debugging.

## Artifacts

Artifact paths are relative. Absolute paths, `..` segments, encoded traversal, symlink escapes, and control characters are rejected server-side. The artifact root defaults to `/home/desktop/artifacts`; see [artifacts.md](artifacts.md).

## Provider credentials

Core does not require OpenAI or Anthropic credentials. Provider SDK calls belong in user applications and examples, never in the daemon image.
