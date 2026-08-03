# Release Checklist

Use this checklist for a release candidate or a production-readiness pull request. Run it from a
clean checkout of the exact commit that will be tagged.

## Core verification

Run the same dependency, schema, lint, type, test, and benchmark-smoke commands as the default CI
job:

```bash
uv sync --extra dev --extra modal --frozen
uv run python scripts/export_openapi.py --check
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run computer-use benchmark report --mock-local --iterations 5 --output benchmark-results/benchmark-report.json
```

The mock benchmark is a deterministic CLI and artifact smoke check. For live prerequisites,
sampling rules, costs, cleanup, and publication procedures, follow
[Benchmarking](benchmarking.md). Do not redefine benchmark methodology in a release checklist.

When the benchmark CLI changes, run its focused command tests before the full suite:

```bash
uv run pytest tests/benchmarks/test_report_cli.py tests/benchmarks/test_action_batch_cli.py -q
```

## Distribution verification

Build and inspect both distributions:

```bash
test ! -e dist/release
mkdir -p dist/release
uv build --out-dir dist/release
uvx --from 'twine>=6.2.0' twine check dist/release/*
uv run python scripts/check_distribution_metadata.py dist/release/*
(cd dist/release && shasum -a 256 *.whl *.tar.gz > SHA256SUMS)
(cd dist/release && shasum -a 256 -c SHA256SUMS)
```

The metadata checker verifies the wheel and source distribution, including the MIT license
expression, included license file, project URLs, and package metadata. `twine check` additionally
validates the rendered long description.

Install both artifacts in clean Python 3.12 environments:

```bash
uv venv /tmp/mcu-wheel-smoke --python 3.12
uv pip install --python /tmp/mcu-wheel-smoke/bin/python dist/release/*.whl
/tmp/mcu-wheel-smoke/bin/computer-use --help
/tmp/mcu-wheel-smoke/bin/computer-use trace --help
/tmp/mcu-wheel-smoke/bin/computer-use benchmark action-batch --mock-local --iterations 1

uv venv /tmp/mcu-sdist-smoke --python 3.12
uv pip install --python /tmp/mcu-sdist-smoke/bin/python dist/release/*.tar.gz
/tmp/mcu-sdist-smoke/bin/python -c "import modal_computer_use; assert modal_computer_use.__version__"
```

The release workflow also imports the installed wheel outside the checkout, instantiates the mock
daemon, verifies both console-script entry points, starts the installed daemon, and probes
`/healthz`, `/readyz`, `/v1/version`, and `/v1/capabilities`.

Normal pull requests and main builds validate the mock report, wheel, and source distribution
without uploading them. GitHub keeps new Actions logs for 14 days. Published distributions live on
PyPI and the immutable GitHub Release. Historical private-era logs and artifacts are deleted
separately because retention changes are not retroactive.

## Architecture and security scans

Run the same fail-on-match boundary scans as CI:

```bash
! rg "(^|[^A-Za-z0-9_])(import|from) +(openai|anthropic)" src
! rg "NetworkFileSystem" src
! rg -n "print\([^\n]*(vnc_url|debug\.vnc_url|\.uri|artifact_uri|token|data_base64|raw_path|stdout|stderr)" examples docs README.md
uv run python scripts/check_repository_hygiene.py
uv export --frozen --all-extras --no-hashes --no-emit-project \
  --output-file /tmp/modal-computer-use-audit-requirements.txt
uvx --python 3.12 --from pip-audit pip-audit \
  --requirement /tmp/modal-computer-use-audit-requirements.txt \
  --no-deps --disable-pip
uvx --from bandit bandit -q -lll -r src
uvx semgrep scan --config p/security-audit --error src
```

Run the focused security regressions before the full suite:

```bash
uv run pytest \
  tests/test_auth_security.py \
  tests/test_daemon_validation.py \
  tests/test_artifacts.py \
  tests/test_recordings.py \
  tests/test_trace_and_budgets.py \
  tests/test_modal_sdk_boundary.py -q
```

Confirm that:

- Core imports without Modal, OpenAI, Anthropic, or provider credentials.
- Modal-specific calls remain isolated to `sandbox.py`, `image.py`, `manager.py`, and
  `registry.py`.
- Artifact traversal, encoded traversal, absolute paths, and symlink escapes remain covered by
  tests.
- noVNC is off by default, and logs and examples do not expose tokens, noVNC URLs, typed or
  clipboard text, screenshots, recordings, artifact bytes, stdout, or stderr.
- Immediately after the repository becomes public and before publishing `v1.1.0`, GitHub private
  vulnerability reporting is enabled. Verify the API and signed-out form without submitting a
  fake report.

Security-sensitive hot-path changes also require local comparison against the merge base. For
isolated validation, collect at least 60 warmed values across multiple worker processes for 1, 50,
and 500 flat actions and nested depths 1 and 32. A regression requires statistical significance,
more than 5%, and more than 0.05 ms. For daemon end-to-end latency, collect at least 30 interleaved
baseline/candidate pairs and retain raw samples. A regression requires a bootstrap 95% lower bound,
more than 5%, and more than 0.25 ms. Validation failures or changed rejection semantics fail the
gate regardless of timing.

These are local release gates. A hosted check that does not run because of billing or service
availability does not replace or invalidate the local evidence.

## Protected Modal verification

Run live tests only from a trusted developer machine or protected environment with Modal
credentials. The protected v1 run is:

```bash
MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 MODAL_COMPUTER_USE_RUN_V1_SMOKE=1 \
  uv run pytest -m modal tests/test_modal_integration.py -q
```

To run only the noVNC smoke:

```bash
MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 uv run pytest -m modal tests/test_modal_integration.py -q
```

The GitHub Actions workflow exposes the same run through `workflow_dispatch` with
`run_modal_smoke=true`. The protected `modal-smoke` environment must provide `MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET`; the job fails if either is missing. Use a restricted Modal service user, not
a personal token.

To include the named Image canary, publish the standard, Firefox, and Chromium Images from the
clean release commit:

```bash
uv run python scripts/publish_modal_images.py --environment prod
```

Set `MODAL_COMPUTER_USE_NAMED_IMAGE_REVISION` to the full Git revision before the protected test
run. The named Image canary is skipped when the variable is absent.

## Publication prerequisites

Before creating a tag or uploading artifacts, confirm that:

- `pyproject.toml`, `src/modal_computer_use/_version.py`, and `docs/openapi.json` contain the same
  version.
- `CHANGELOG.md` has a dated entry for that version and no release change remains only under
  `Unreleased`.
- The release tag points to the exact verified source commit and uses the repository's version-tag
  convention. `v1.1.0` uses an annotated unsigned tag.
- The wheel and source distribution were built from that clean tagged commit. `SHA256SUMS` verifies
  both files.
- The checked-in OpenAPI schema has no unexplained regeneration diff.
- The README, documentation, changelog, issue tracker, and security-policy project URLs resolve to
  their intended public pages.
- Protected Modal verification passed for changes that affect Modal creation, ingress, images,
  Volumes, snapshots, noVNC, attach/reuse, or cleanup.
- PyPI Trusted Publishing is configured for this repository and release environment. Publish the
  verified wheel and source distribution from the tagged commit, then confirm both files and the
  expected version are available on PyPI.
- Immutable GitHub Releases are enabled. Create the release as a draft, attach the wheel, source
  distribution, and `SHA256SUMS`, then publish them together and verify the tag, assets, immutable
  state, and release attestation.

Do not call a version published until PyPI exposes the wheel and source distribution and the
immutable GitHub Release contains the matching tag, both distributions, and `SHA256SUMS`.
