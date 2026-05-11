# Security

The daemon can click, type, read screenshots, read clipboard text, launch applications, and read/write artifacts. It must not be exposed as an unauthenticated public service.

## Authentication

Modal deployments should use Sandbox Connect Tokens for port `8080`. Modal forwards verified user metadata in `X-Verified-User-Data`. Local development can set `COMPUTER_USE_LOCAL_TOKEN` and clients must send `Authorization: Bearer <token>`.

Query-string tokens are rejected by default because URLs leak into logs and browser history.

## noVNC

`expose_vnc` supports `off`, `view_only`, and `control`. noVNC is off by default. Treat noVNC URLs as secrets.

## Logs

Structured logs redact typed text, clipboard text, screenshot base64, tokens, provider keys, noVNC URLs, and artifact bytes. Logs should record lengths, hashes, dimensions, action types, elapsed time, and `call_id`.

## Artifacts

Artifact paths are relative. Absolute paths, `..`, encoded traversal, symlink escapes, and control paths are rejected server-side.

## Provider Credentials

Core does not require OpenAI or Anthropic credentials. Provider SDK calls belong in user applications and examples.
