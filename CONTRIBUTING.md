# Contributing

Thank you for helping improve `modal-computer-use`.

This project accepts bug reports, documentation updates, tests, and focused code changes. Keep each
change limited to one behavior or one documentation theme.

By submitting a contribution, you agree to license it under the repository's MIT License.

## Before you start

Search the issue tracker for related work.

Open an issue before you make a large change. Describe the problem and the proposed behavior. Wait
for agreement before you invest in a broad design.

Do not open a public issue for a vulnerability. Follow the [security policy](SECURITY.md).

Follow the [code of conduct](CODE_OF_CONDUCT.md) in all project spaces.

## Set up the project

Use Python 3.12 or later. Install `uv` 0.12.3 before you continue. The project pins this version so
local and CI dependency operations use the same resolver.

Clone your fork. Create a branch from the latest `main` branch.

Install the development environment:

```bash
uv sync --extra dev
```

Add the Modal extra only when your change needs Modal integration:

```bash
uv sync --extra dev --extra modal
```

Do not put credentials in source files, test data, logs, screenshots, or benchmark output.

## Make the change

Keep orchestration Modal-native. Keep primitive execution daemon-native.

Put behavior near the route, backend, namespace, or SDK surface that owns it.

Do not add a provider-owned model loop to the core package. Do not import `openai` or `anthropic`
from a core module.

Keep Modal calls in `sandbox.py`, `image.py`, `manager.py`, or `registry.py`.

Use Sandbox filesystem APIs and optional Volumes. Do not use `modal.NetworkFileSystem`.

Add a focused test for each behavior change. Add validation and failure-path tests when they apply.

Update the generated OpenAPI schema when an API shape changes:

```bash
uv run python scripts/export_openapi.py
```

## Protect sensitive data

Treat noVNC URLs and bearer tokens as secrets. Treat clipboard text, typed text, screenshots,
recordings, and artifact bytes as secrets.

Do not commit raw benchmark output. Follow the [benchmark data policy](benchmark-data/README.md)
before you add benchmark evidence.

## Check the change

Run focused tests while you work. Run all local checks before you submit the change:

```bash
uv run python scripts/export_openapi.py --check
uv run ruff check .
uv run mypy src
uv run pytest
```

This default pytest invocation skips credentialed `@pytest.mark.modal` smoke tests. Run those only
from a protected environment with `MODAL_COMPUTER_USE_RUN_LIVE_TESTS=1` and the per-surface
authorization variables from the release checklist; credentials alone never enable billable tests.

Run Modal tests only when the change needs them and you have suitable credentials. Modal tests can
create billable resources.

## Submit the change

Open a pull request against `main`.

Use a clear title. Explain the problem, the solution, and the checks that you ran. Link each related
issue.

Keep generated files and documentation in sync with the code. Respond to review comments with a
new commit or a concise explanation.

Do not force-push after review starts unless a maintainer asks you to do so.
