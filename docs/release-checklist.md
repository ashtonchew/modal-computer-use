# Release Checklist

Run this checklist before publishing a release or opening a production-readiness PR.

## Verification

- `uv run ruff check .`
- `uv run mypy src`
- `uv run python scripts/export_openapi.py --check`
- `uv run pytest -q`
- `uv run computer-use benchmark report --mock-local --iterations 5 --output benchmark-report.json`
- `uv build`

The `Release Validation` GitHub Actions workflow repeats these checks on pull requests and pushes
to `main` and uploads the benchmark report and distributions as artifacts. It intentionally does
not require Modal credentials in GitHub.

Run live Modal smoke tests from a trusted developer machine or protected release environment with
local Modal auth:

```bash
MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 uv run pytest -m modal tests/test_modal_integration.py -q
```

For v1 release candidates, also run the protected manager/Volume/snapshot smoke:

```bash
MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 MODAL_COMPUTER_USE_RUN_V1_SMOKE=1 \
  uv run pytest -m modal tests/test_modal_integration.py -q
```

Expected state on May 12, 2026: live manager attach/reuse/config-conflict/cleanup passes; Volume
v2 artifact sync commits with `sync <artifacts_dir>` and is visible through `Volume.read_file`;
directory snapshot restore uses Modal's documented `snapshot_directory` plus `mount_image` flow
and should pass.

Do not store personal Modal tokens in repository or CI configuration. If a future workspace needs
fully automated Modal CI, use a Modal service user scoped to a restricted Modal Environment rather
than a human token.

## Targeted Checks

Run these targeted checks before the full suite when changing release-critical surfaces:

```bash
uv run pytest tests/test_benchmark_cli.py -q
uv run pytest tests/test_modal_sdk_boundary.py tests/test_imports.py -q
uv run pytest tests/test_adapters.py tests/test_trace_replay.py -q
uv run python -m py_compile \
  examples/04_warm_pool.py \
  examples/browser_profile.py \
  examples/snapshot_filesystem.py \
  examples/volume_artifacts.py \
  examples/novnc_view_only.py \
  examples/recording_lifecycle.py \
  examples/adapter_policy_hook.py \
  examples/anthropic_message_server.py
```

## Architecture Boundaries

- Core imports without Modal, OpenAI, Anthropic, or provider credentials.
- `src/` has no OpenAI or Anthropic SDK imports.
- `src/` has no `modal.NetworkFileSystem` usage.
- Modal-specific SDK calls remain isolated to `sandbox.py`, `image.py`, `manager.py`, and `registry.py`.
- Provider adapters translate provider-returned actions only; they do not call provider APIs or own prompts, policies, credentials, or model loops.

## Security

- noVNC is off by default and any enabled noVNC URL is treated as a secret.
- Examples and docs do not print bearer tokens, noVNC URLs, artifact URIs, raw artifact paths, recording bytes, screenshot bytes, typed text, clipboard text, raw command strings, stdout, or stderr.
- Trace validation and replay dry-runs handle redacted typed text and provider provenance.
- Artifact traversal, encoded traversal, absolute paths, and symlink escapes are covered by tests.
- Recording examples report bounded metadata only.

Run the boundary and secret-output scans:

```bash
rg "(^|[^A-Za-z0-9_])(import|from) +(openai|anthropic)" src
rg "NetworkFileSystem" src
rg -n "print\([^\n]*(vnc_url|debug\.vnc_url|\.uri|artifact_uri|token|data_base64|raw_path|stdout|stderr)" examples docs README.md
rg -n "noVNC|artifact_uri|data_base64|raw_path|token|secret|clipboard|typed text" docs examples tests src
```

The first three scans should return no matches. The broad sensitivity scan is expected to match
models, docs, tests, and security examples; inspect matches manually and fix any example or doc
that prints secrets.

## Compatibility

- OpenAI and Anthropic fixture tests pass and unknown provider actions fail closed by default.
- Modal boundary tests cover `Sandbox.create`, connect tokens, readiness probes, encrypted noVNC ports, tags/listing, attach/reuse, cleanup, and directory snapshot/mount delegation.
- Modal smoke tests remain marked and skipped unless credentials are explicitly available.
- Protected v1 Modal smoke covers manager lifecycle live, honest Volume sync behavior, and
  directory snapshot restore through `mount_image`.

## Performance

- Benchmark output distinguishes `ok`, `failed`, `not_measured`, `unsupported`, and
  `unavailable` cases.
- Benchmark output does not include URL query strings, URL userinfo, bearer tokens, noVNC URLs,
  raw command strings, stdout, stderr, typed text, clipboard text, screenshot bytes, recording
  bytes, raw paths, or artifact URIs.
- Browser prewarm and GPU guidance is documented as optional and measured-workload dependent.
- Warm-pool and snapshot examples remain example-level; core lifecycle does not auto-create pools, snapshots, public tunnels, or Volumes.
