# Benchmarking

This page defines the repository's general benchmark procedure and reporting policy. Dated reports
contain measured results. Experiment-specific methodology pages add any stricter gates for their
experiment.

## Choose a command

Run a credential-free release report against the in-process mock daemon:

```bash
uv run computer-use benchmark report --mock-local --iterations 5
```

Use `action-batch` to compare one five-action batch with five separate calls:

```bash
uv run computer-use benchmark action-batch --mock-local --iterations 5
```

Use `sdk` for daemon and adapter surfaces. The adapter cases do not call model APIs:

```bash
uv run computer-use benchmark sdk --mock-local --iterations 5
```

Against an existing daemon, replace `--mock-local` with `--base-url` and, when required, `--token`:

```bash
uv run computer-use benchmark report \
  --base-url http://127.0.0.1:8080 \
  --token dev \
  --iterations 5 \
  --output benchmark-results/report.json
```

`benchmark report` and `benchmark action-batch` do not create Modal resources. The optional
`--include-sandbox-exec --sandbox-id <id>` report mode attaches to an existing Sandbox.

## Run a Modal SDK benchmark

Install the Modal extra and authenticate before a live run:

```bash
uv sync --extra modal
uv run modal token new
```

The following command creates a billable Sandbox, waits for it, runs the selected surface, and
attempts termination and detachment:

```bash
uv run computer-use benchmark sdk \
  --create-modal-sandbox \
  --surfaces daemon-http \
  --browser chromium \
  --resource-profile browser \
  --iterations 30 \
  --output benchmark-results/modal-sdk.json
```

Record the caller location with `--caller-region-label` on commands that support it. Use
`--modal-region` only when the experiment requires a fixed placement policy. Do not infer an exact
physical availability zone from a requested broad region.

Use `modal-region-ab` for a controlled placement comparison and `modal-region-summary` to render its
artifact:

```bash
uv run computer-use benchmark modal-region-ab \
  --iterations 30 \
  --modal-region default \
  --modal-region us-west \
  --modal-region us-east \
  --caller-region-label dev-laptop-us-west \
  --output benchmark-results/modal-region-ab.json

uv run computer-use benchmark modal-region-summary \
  benchmark-results/modal-region-ab.json
```

Use `modal-colocated-client` to measure a runner and target with the same requested Modal region.
`--runner-only` omits the unrelated external-caller diagnostic and its comparison fields:

```bash
uv run computer-use benchmark modal-colocated-client \
  --runner-only \
  --modal-region us-west-2 \
  --modal-ingress connect \
  --daemon-http-version 1.1 \
  --runner-path connect \
  --surface daemon-http \
  --browser chromium \
  --resource-profile browser \
  --input-rate-limit-per-sec 0 \
  --input-backend xtest \
  --subprocess-backend isolated-asyncio \
  --iterations 30 \
  --output benchmark-results/modal-runner.json
```

The combined provider report uses `modal-optimized-provider` for the optimized lifecycle and warm
operation rows. From the clean evidence-harness commit, publish its revision-addressed Images into
the active Modal environment used by the run, as described in
[Modal deployment](modal-deployment.md), then run:

```bash
evidence_harness_sha="$(git rev-parse HEAD)"

uv run computer-use benchmark modal-optimized-provider \
  --modal-region us-west-2 \
  --image-revision "$evidence_harness_sha" \
  --modal-cpu 4 \
  --modal-memory-mib 8192 \
  --browser chromium \
  --iterations 30 \
  --warmup-iterations 1 \
  --output benchmark-results/modal-optimized-provider-2026-07-26.json
```

This command runs one warmup and 30 fresh create-to-validated-screenshot samples, then uses a
separate warm target for the six operation rows. It fails unless every required sample, placement
check, and cleanup gate passes. The resources are billable.

First-visual-change measurements are experimental. They confirm a changed frame by its hash under
the documented boundary. They do not measure application settle or semantic readiness. Read the
[Alpha observation guide](experimental-visual-change-observation.md) before using that surface.

## Run the provider-default comparison

Install the pinned provider benchmark dependencies:

```bash
uv sync --extra modal --extra bench-providers
```

Set credentials in the process environment or pass an ignored dotenv file with `--env-file`.
Existing environment values take precedence. The live providers use:

- `DAYTONA_API_KEY`; optional `DAYTONA_API_URL`, `DAYTONA_TARGET`, and `DAYTONA_SNAPSHOT` change the
  default path and must be disclosed.
- `E2B_API_KEY`; optional `E2B_TEMPLATE` changes the default path and must be disclosed.
- `TZAFON_API_KEY`; optional `LIGHTCONE_BASE_URL` changes the default endpoint and must be
  disclosed.
- Modal credentials from its normal local configuration or `MODAL_TOKEN_ID` and
  `MODAL_TOKEN_SECRET`.

Do not commit the dotenv file. Do not print its contents.

```bash
uv run computer-use benchmark compare \
  --create-modal-sandbox \
  --providers modal-daemon,daytona,e2b,tzafon \
  --iterations 3 \
  --env-file .env \
  --output benchmark-results/candidates/provider-compare-coordinate-command-2026-07-26.json
```

Provider-default means the documented public SDK path and its default provider configuration.
For Modal that means the `ComputerConfig` defaults: standard resources, no browser profile, the
20-actions-per-second input limit, `auto` typing with a 10 ms character delay, and default placement.
For the 100- and 1000-character cases, `auto` resolves to clipboard, so the requested delay is not
applied per character. When Modal retains the default input limit, its runner records and applies
1.05 seconds of Modal-only pacing before every warmup and measured action invocation, including
each action-batch subcase. The pacing is outside the timer and prevents earlier samples from
crowding the next operation; the limit counts actions, not typed characters. The command workload
requests `sh -c "printf '42\\n'"`, requires exit code 0 and stdout exactly `"42\n"`, and never
strips whitespace. Record any override and do not label that provider arm as default. Only the
separate Modal-optimized arm receives repository optimizations, including explicit `keystrokes`
typing with zero delay. Keep provider-default and Modal-optimized columns separate because their
caller topology, configuration, and sample count differ.

## Retain and publish artifacts

`benchmark-results/` contains ignored raw output, candidates, preregistrations, rejected runs, and
replay inputs. `benchmark-data/` contains tracked, sanitized evidence. Do not write benchmark
output at the repository root.

Before publishing an artifact:

1. Freeze and record the exact source revision and harness revision.
2. Use a clean tracked worktree. If a diagnostic permits a dirty tree, label it as a candidate and
   bind its diff digest; do not present it as revision-only evidence.
3. Record the command, workload, timer boundaries, units, warmup policy, sample count, requested and
   observed configuration, caller topology, failures, cleanup, and artifact digest.
4. Retain raw observations when policy and secret handling permit it. Never reconstruct missing raw
   samples from summaries.
5. Run the artifact's repository validator or sanitizer. Inspect the result for endpoints, resource
   identifiers, credentials, URLs with user information or query strings, typed or clipboard text,
   screenshots, command output, and raw failure content.
6. Regenerate with the validator's check mode when available so review detects drift.

The combined provider report has stricter gates. Run all three measurements from the same clean,
committed evidence-harness revision. The `modal-optimized-provider` command above produces its
optimized input.
Produce the single-case observation input with:

```bash
uv run computer-use benchmark modal-colocated-client \
  --runner-only \
  --modal-region us-west-2 \
  --modal-ingress connect \
  --daemon-http-version 1.1 \
  --runner-path connect \
  --surface daemon-observation-stream \
  --observation-case observation_action_click_observe_change_http_raw \
  --browser chromium \
  --resource-profile browser \
  --input-rate-limit-per-sec 0 \
  --input-backend xtest \
  --subprocess-backend isolated-asyncio \
  --iterations 30 \
  --output benchmark-results/modal-observation-2026-07-26.json
```

Sanitize the raw provider-default artifact before combining it. `current_reference` requires the
declared harness commit to equal `HEAD` and the tracked worktree to be clean:

```bash
evidence_harness_sha="$(git rev-parse HEAD)"

uv run python scripts/sanitize_provider_benchmark.py \
  benchmark-results/candidates/provider-compare-coordinate-command-2026-07-26.json \
  benchmark-data/provider-compare-coordinate-command-2026-07-26.json \
  --raw-artifact-path benchmark-results/candidates/provider-compare-coordinate-command-2026-07-26.json \
  --harness-commit "$evidence_harness_sha" \
  --status current_reference \
  --scope "provider-default SDK paths, one warmup and three measured iterations"
```

Convert the two raw Modal artifacts into strictly allowlisted tracked inputs. This preserves the
numeric samples and required attestations while excluding endpoints, resource identifiers, tokens,
screenshots, command output, and raw failure content:

```bash
uv run python scripts/sanitize_modal_provider_inputs.py \
  benchmark-results/modal-optimized-provider-2026-07-26.json \
  benchmark-results/modal-observation-2026-07-26.json \
  benchmark-data/modal-optimized-provider-2026-07-26.json \
  benchmark-data/modal-observation-2026-07-26.json \
  --evidence-harness-sha "$evidence_harness_sha"
```

Generate the combined artifact. The generator verifies the exact provider list,
sample counts, runner-only topology, selected observation case, configuration, recorded failure
outcomes, evidence-harness revision, report-source revision, and absence of external comparison
fields. Generation requires the report-source revision to equal `HEAD`; later clean descendants can
use `--check` against that immutable revision. The sanitizer also removes fields that repository
policy treats as secrets.

```bash
report_source_sha="$(git rev-parse HEAD)"

uv run python scripts/sanitize_provider_results.py \
  benchmark-data/provider-compare-coordinate-command-2026-07-26.json \
  benchmark-data/modal-optimized-provider-2026-07-26.json \
  benchmark-data/modal-observation-2026-07-26.json \
  benchmark-data/provider-results-2026-07-26.json \
  --report-source-sha "$report_source_sha" \
  --evidence-harness-sha "$evidence_harness_sha"

uv run computer-use benchmark provider-results \
  benchmark-data/provider-results-2026-07-26.json \
  --format markdown \
  --output docs/benchmark-results-2026-07-26-provider-results.md
```

After generation, rerun all three sanitizer commands with `--check` to verify that the tracked
artifacts match their inputs. Keep the two raw Modal runner artifacts ignored. The tracked
allowlisted inputs bind their SHA-256 digests, and the combined artifact binds the exact bytes of
all three tracked inputs.

## Report statistics

Apply this repository policy to human-facing tables:

- With fewer than 20 successful observations, report the median and observed minimum-maximum range.
  Do not use p95 as headline evidence.
- With 20 or more successful observations, report p50 and p95 as sample statistics. State the
  sample count and quantile method.
- Report failures and attempted samples. Do not replace failed samples unless a preregistered
  protocol explicitly permits replacement and reports it.
- Keep exact raw observations in the linked artifact when they can be retained safely.

The threshold of 20 is an editorial rule for this repository. Twenty samples do not guarantee a
stable p95. For small samples, percentile interpolation methods can produce materially different
answers. The current benchmark implementation uses a zero-based fractional rank of
`(percentile / 100) * (n - 1)` and linear interpolation between adjacent ordered observations. A
dated artifact may retain that deterministic percentile for machine compatibility while the human
report follows the policy above.

Do not rank unlike configurations. Do not calculate speedup ratios across different timer
boundaries. Identify each boundary, including whether creation ends at readiness, first bytes, or a
decoded and validated screenshot. Separate action acknowledgement, immediate screenshot,
hash-confirmed first visual change, visual settle, and application readiness.

## Account for cost and cleanup

Live provider and Modal commands can create billable resources. Before a run:

1. Open each provider's official pricing page and record its URL, access date, currency, billing
   unit, minimum charge, and the rates that apply to the requested resources.
2. Count the maximum target and runner instances, including warmups, measured attempts, permitted
   replacements, retries, and concurrent arms. Use configured lifecycle timeouts, not expected
   latency, as the maximum duration.
3. Calculate an explicit ceiling: resource rate multiplied by maximum billable duration and instance
   count, plus fixed operation, storage, snapshot, data-transfer, and minimum charges. State any
   omitted or unknown charge.
4. Record the ceiling with the run plan. Configure a provider budget or quota when available, and do
   not start when the ceiling exceeds the approved amount.

The commands on this page do not enforce a maximum-cost gate. They can continue creating billable
resources until the benchmark finishes or a lifecycle limit stops it. Use an isolated app or project
when practical, monitor the run, and inspect every provider console afterward.

Treat cleanup failures as benchmark failures. Record them and check for leaked resources; do not
hide cleanup time inside a lifecycle boundary unless the protocol explicitly measures it.

Public-rate `cost_estimate` values provide approximate context and do not replace billing data.
Keep delayed Modal `billing_reconciliation` separate from the estimate. Billing rows can lag,
cover full reporting intervals, omit unused tag keys, and include account adjustments outside the
artifact. When several surfaces share one Sandbox, report one shared resource estimate unless a
fair allocation is known.

## Find methodology and evidence

- [Performance](performance.md) explains stable latency mechanisms.
- [Current provider results](benchmark-results-2026-07-26-provider-results.md) state their evidence
  status, measurement boundaries, and provenance.
- The archive retains the [Modal V2 candidate methodology](archive/benchmarks/modal-v2-candidate-benchmark.md)
  and [optimized-frontier methodology](archive/benchmarks/modal-optimized-frontier-benchmark.md)
  with their gated experiment results.
- [Benchmark data policy](../benchmark-data/README.md) defines tracked artifact eligibility.
- [Archive policy](archive/README.md) explains why evidence leaves the current set.
