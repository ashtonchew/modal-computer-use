# Repository Conventions

This repository implements daemon-first computer-use primitives for Modal Sandboxes.

## Architecture Rules

- Keep orchestration Modal-native and primitive execution daemon-native.
- Prefer locality of behavior: put behavior near the route, backend, namespace, or SDK surface that owns it.
- Preserve modularity by behavior: keep each change focused on one behavioral theme and avoid broad cross-cutting abstractions unless they remove real duplication.
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

## Implementation Discipline

- Define success in verifiable terms before changing code; never assume behavior from inspection alone.
- Add or update tests with every behavior change, including validation and failure-path tests when relevant.
- Verify implementation with targeted tests while iterating, then run `uv run ruff check .`, `uv run mypy src`, and `uv run pytest` before handoff.
- Keep tests close to the behavior they protect and prefer focused regression coverage over broad, brittle assertions.

## Release Criteria

- Core imports without Modal, OpenAI, Anthropic, or provider credentials.
- Local daemon app instantiates and exposes `/healthz`, `/readyz`, `/v1/version`, and `/v1/capabilities`.
- SDK local client can call daemon routes.
- Artifact paths reject traversal and symlink escapes.
- Action batches stop on first error by default and support `continue_on_error`.
- Modal smoke tests are marked and skipped unless credentials are available.
