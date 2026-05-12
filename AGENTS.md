# Repository Conventions

This repository implements daemon-first computer-use primitives for Modal Sandboxes.

## Architecture Rules

- Keep orchestration Modal-native and primitive execution daemon-native.
- Do not add provider-owned model loops to core.
- Do not import `openai` or `anthropic` from core modules.
- Keep Modal-specific calls isolated to `sandbox.py`, `image.py`, `manager.py`, and `registry.py`.
- Do not use `modal.NetworkFileSystem`; use Sandbox filesystem APIs and optional Volumes.
- Treat noVNC URLs, bearer tokens, clipboard text, typed text, screenshot bytes, and artifact bytes as secrets in logs.

## Development

- Install: `uv sync --extra dev`
- Lint: `uv run ruff check .`
- Tests: `uv run pytest`
- Types: `uv run mypy src`

## Release Criteria

- Core imports without Modal, OpenAI, Anthropic, or provider credentials.
- Local daemon app instantiates and exposes `/healthz`, `/readyz`, `/v1/version`, and `/v1/capabilities`.
- SDK local client can call daemon routes.
- Artifact paths reject traversal and symlink escapes.
- Action batches stop on first error by default and support `continue_on_error`.
- Modal smoke tests are marked and skipped unless credentials are available.
