# Benchmarking

This page defines the repository's general benchmark procedure and reporting policy. Dated reports
contain measured results. Experiment-specific methodology pages add any stricter gates for their
experiment.

## Preserve the historical article-parity promotion gate

This section documents the existing screenshot-path experiment. Do not use it as evidence for
Computer Step. Do not promote the screenshot default from unit tests or the historical
cross-provider table. Use a
preregistered, interleaved comparison between the prior public path and the candidate default. Hold
the caller topology, target, exact region, resources, image, ingress, HTTP version, input backend,
screenshot format, action payload, warmup, and connection reuse constant.

The historical article-parity gate accepts only one application-owned Modal Function, attested-tunnel ingress,
HTTP/1.1, native XTest input, and one pooled async client. It rejects external callers, Connect-only
ingress, HTTP/2, xdotool, per-request clients, WebSocket control, and fused action-plus-screenshot
measurements. Test those alternatives in separate experiments.

Use enough alternating samples to estimate p50 and p95 without replacing failures. Retain sanitized
raw observations. Record every failure and the terminal cleanup result. Reject the run when
requested and observed placement or any fixed configuration differs between arms.

Report these phases separately:

- cold allocation and desktop startup;
- application-owned Function dispatch;
- `borrow_async()` entry, protocol preflight, authentication, and lease acquisition;
- repeated warm operation time;
- lease release and owner cleanup.

The existing optimized-default screenshot experiment must call the public `screenshots.full()` method and confirm that it uses
the raw binary response over the reused pooled client. The action case must preserve the model's
ordered array as one batch. Do not substitute a fused action-plus-screenshot route, WebSocket,
HTTP/2, a positive warm-capacity setting, or a different release image.

Call `measure_interleaved_promotion()` inside the explicitly placed application Function. Pass a
callback that creates `handle.borrow_async(...)`; the runner enters it once and follows the exact
preregistered arm order. The candidate Adapter calls public
`screenshots.full(storage="inline")` and requires a byte-backed `Screenshot`. The prior Adapter
uses the retained inline JSON/base64 route through the same pooled async client. Both Adapters send
the ordered action array through one `actions.run(...)` call. The runner stops after the first
operation failure and never replaces or replays a sample.

The runner does not create Modal resources and cannot infer allocation or Function-dispatch time.
The owner and placed Function must measure `cold_start_ms`, `startup_ms`, and `dispatch_ms`, then
pass those values with the fixed, observed configuration. The returned two artifacts still need
the offline gate below. Running the helper does not authorize or perform publication.

The article's 37.25 ms screenshot result used the raw endpoint, persistent capture, and its recorded
same-region topology. Its opening 47.10 ms screenshot-plus-click value is arithmetic over separate
warm medians. It is not a measured fused turn and is not a latency promise for `computer.step()`.
Publish a new dated step report only after a same-topology step candidate passes its promotion gate.
Do not edit historical reports or artifacts.

The offline gate reads two sanitized JSON artifacts. It validates the evidence before it compares
latency, and it never starts a Modal Function or Sandbox:

```bash
uv run computer-use benchmark promotion-gate \
  --prior-public benchmark-results/candidates/prior-public.json \
  --candidate benchmark-results/candidates/candidate-default.json \
  --output benchmark-results/candidates/promotion-decision.json
```

Each artifact records one arm of `optimized-default-promotion`. The `configuration` object records
the caller topology, target identity, requested and observed exact placement, resources, image,
ingress, HTTP version, input backend, screenshot request, action-payload digest, warm-up count,
and connection reuse. The `observations` array retains sanitized raw rows with separate
`cold_start_ms`, `startup_ms`, `dispatch_ms`, `borrow_ms`, and `warm_operation_ms` timings, frame
validation, attribution, and cleanup. Every timing is present, non-null, and nonnegative. Every
measured trajectory records `borrow_count: 1`; observed input and screenshot transport attribution
must match the explicit configuration. `warm_capacity` is required and its
`function_min_containers` and `sandbox_pool_capacity` values are both zero for article parity. A
run needs at least 30 complete measured samples per arm.
The schedule is deterministic and interleaves both arms for each pair. Replacement samples,
retries, configuration drift, placement mismatch, invalid frames, missing attribution, and failed
cleanup reject promotion. The result reports paired bootstrap 95% confidence intervals. The
candidate fails the warm-operation gate only when the lower confidence bound exceeds both 5% and
0.25 ms. The July 30 artifacts remain historical evidence and are not rewritten by this command.

The repository provides one application-owned live runner for this complete sequence. Run it from
the exact clean commit that you want to promote:

```bash
source_sha="$(git rev-parse HEAD)"
MODAL_COMPUTER_USE_PROMOTION_ENVIRONMENT=main \
MODAL_COMPUTER_USE_PROMOTION_CLOUD=aws \
MODAL_COMPUTER_USE_PROMOTION_REGION=us-west-2 \
uv run modal run --env main scripts/run_optimized_default_promotion.py \
  --source-sha "$source_sha" \
  --sample-count 30 \
  --warmup-iterations 1 \
  --output-dir benchmark-results/candidates/optimized-default-live
```

The runner creates one async owner, observes the Sandbox placement, measures one placed Function
dispatch, enters one borrow for the interleaved trajectory, writes both sanitized artifacts, runs
the offline gate, and cleans up the owner and Function App. Its Function and Sandbox each request
1 CPU and 2048 MiB, use zero warm capacity, and permit no retries. Change these explicit source
constants only in a new preregistered experiment.

## Compare inline and managed Image lifecycles

Use the Image Lifecycle Benchmark when you need to decide whether a Managed Image Release should
replace the inline image recipe. This is a credential-gated Benchmark Surface. It is not part of
`benchmark sdk`.

The two arms are `inline-recipe` and `managed-exact-id`. They represent the current product paths.
They do not isolate name lookup. The inline browser recipe installs both supported browsers and
mounts SDK source at startup. A managed browser recipe installs one browser and bakes SDK source
into the Image. The result therefore measures the complete Image policy that users would receive.

The timer starts before `ComputerSandbox.create()`. It ends at the `first_valid_frame` startup mark.
Image identity and placement checks run after that mark. Their time is outside lifecycle latency but
inside the measured lifecycle wall time and cost estimate. One long-lived external SDK process runs
the complete schedule. Record that caller with `--caller-label`. The label identifies the harness;
it does not claim that the caller is in the target region. Every target must use the requested exact
region, and both arms must report one observed cloud and region. This topology preserves the inline
recipe's caller-local source mount. A Modal Function cannot recreate that local mount. The run does
not measure Image build time. Modal cache state cannot be reset reliably for a paired Sandbox
experiment, so record release build duration as separate deployment evidence.

Both arms request and cap the target at 1 physical CPU core and 2048 MiB for the commands below.
The cost estimate applies Modal's narrow-region multiplier. The fixed limits prevent resource bursts
from exceeding the preregistered target allocation. They apply only to this Benchmark Surface. At the
rates recorded on 8 August 2026, the pilot target ceiling is $0.10 and the primary target ceiling is
$1.04. Image build, canary, the external caller, control-plane, and billing adjustments remain outside
those ceilings. The commands stop before launch if the target ceiling exceeds the $20 budget.

Publish one verified standard Managed Image Release from the clean benchmark commit:

Confirm that `uv --version` reports `0.12.3` first. The publisher verifies the same executable
through `UV_EXECUTABLE` before it contacts Modal. Set that variable to the exact `uv 0.12.3`
binary when the first `uv` on `PATH` is a different version.

```bash
source_sha="$(git rev-parse HEAD)"
result_root="benchmark-results/image-lifecycle/$source_sha"

UV_EXECUTABLE="$(command -v uv)" uv run python scripts/publish_modal_image_release.py \
  --logical-release 2.0.0 \
  --variant standard \
  --environment main \
  --image-builder-version 2025.06 \
  --manifest "$result_root/standard-image-release.json"
```

Replace the Environment and Image Builder Version with the exact values approved for the run. The
publication command builds the Image, runs its protected canary, verifies the revision tag, and
records the exact Modal object ID. Stop if any step fails.

Run the pilot first. It uses one warmup pair and two measured samples per arm:

```bash
uv run python scripts/run_modal_image_lifecycle_benchmark.py pilot \
  --source-sha "$source_sha" \
  --manifest "$result_root/standard-image-release.json" \
  --region us-west-2 \
  --cpu 1 \
  --memory-mib 2048 \
  --sandbox-timeout-seconds 180 \
  --max-estimated-cost-usd 20 \
  --caller-label codex-desktop-local-process \
  --output "$result_root/pilot.json"
```

Run the primary experiment only when the pilot status is `complete`. The primary run uses one
warmup pair and 30 measured samples per arm. It creates 62 Sandboxes in a deterministic paired and
interleaved order. It allows no retry or replacement sample.

```bash
uv run python scripts/run_modal_image_lifecycle_benchmark.py primary \
  --source-sha "$source_sha" \
  --manifest "$result_root/standard-image-release.json" \
  --region us-west-2 \
  --cpu 1 \
  --memory-mib 2048 \
  --sandbox-timeout-seconds 180 \
  --max-estimated-cost-usd 20 \
  --caller-label codex-desktop-local-process \
  --pilot-result "$result_root/pilot.json" \
  --output "$result_root/primary.json"
```

The Benchmark Surface stops on the first create, identity, placement, frame, or cleanup failure.
It records raw paired samples, p50, p95, mean, paired deltas, bootstrap 95% confidence intervals,
lifecycle wall time, and a public-rate target cost estimate. The estimate excludes Image build,
canary, the external caller, control-plane, and billing adjustments. Reconcile delayed Modal billing
separately before you publish a cost claim.

## Promote Computer Step

Use a separate gate for the canonical action-to-observation interface. Run
`scripts/run_step_promotion.py` only after an operator authorizes the live, billable run. The
runner compares these arms inside the same placed Function and the same borrowed trajectory:

- the prior public arm calls `actions.run(...)` followed by `screenshots.full(...)`;
- the candidate arm calls `computer.step(...)` once;
- both arms send the same ordered action array and the same explicit screenshot options: PNG,
  quality 90, scale 1, cursor hidden, daemon processing, and inline storage;
- each preparation resets the pointer and captures one untimed baseline through the same daemon;
  both arms pay this preparation equally, and the measured click uses one preregistered coordinate;
- each returned screenshot must report that coordinate and a daemon capture timestamp after its
  baseline. This is the deterministic causality check for the immediate frame without comparing
  clocks across the Function and Sandbox.
- the dated runner fixed the then-current 20-actions-per-second setting and waited 125 ms after
  each arm. This untimed pacing kept the preparation move and measured click below that historical
  rolling limit without disabling it, retrying, or replacing samples. The result remains immutable
  evidence for that configuration; it does not describe the later weighted token-bucket default.

The runner uses one async owner, one versioned handle, one exact requested region, one
`borrow_async()` context, and one pooled async HTTP client. It makes two calls to the same placed
Function definition: a mutation-free placement probe and one measurement invocation. The recorded
`dispatch_ms` is the probe round trip; it is a lifecycle diagnostic, not a promotion metric. The
measurement invocation owns the whole interleaved trajectory and enters the borrow once. The runner
keeps caller topology, target, requested and observed placement, resources, image, ingress,
HTTP/1.1, XTest, screenshot format, action payload, warm-up, connection reuse, and zero warm
capacity fixed. It also records the input rate limit and pacing interval. It stops after the first
failure. It does not retry, replay, or replace a sample.

The preregistration requires at least 100 complete paired samples per arm. Each sanitized raw
observation records cold start, startup, dispatch, borrow, action-to-frame, action phase,
screenshot phase, daemon total, and transport-and-decode timings. Candidate phase timings must
come from the daemon; missing values reject the run. The gate requires the paired bootstrap 95%
confidence interval for the candidate-minus-prior median to remain below zero. The candidate p95
must not exceed the prior p95. Any configuration mismatch, missing freshness proof, failed
cleanup, unknown artifact field, or non-allowlisted failure category rejects promotion.

Only `action_to_frame_ms` is a promotion metric. The prior arm's action and screenshot phase values
are caller-observed request durations. The candidate's phase values are daemon-reported parts of
one Step request. They are arm-specific diagnostics and must not be compared as equivalent phases.
The prior arm records daemon total and transport-and-decode as null because its semantic screenshot
does not expose a comparable daemon capture duration. Candidate values must be present and finite.

## Weighted input capacity gate

Run the capacity gate before promoting the 100-token-per-second refill and 400-token burst. The
gate uses the minimum supported 1 CPU and 2,048 MiB Sandbox, exact placement, native XTest, one
borrow, and one pooled client. It sends ordered mixed batches and verifies every result, XTest
attribution, pointer sentinels, daemon health, cleanup, throughput, tail stability, CPU use, and RSS
growth. It does not retry or replace failed work. A passing run uses no more than 0.02 aggregate
cgroup CPU-seconds per normalized token and adds no more than 128 MiB of RSS during the measured
workload. The
application-owned Function samples only processes with the target Sandbox's cgroup membership; it
does not expose privileged command execution through the borrowed-computer interface.

The measurement configures a 2,000-token refill and 4,000-token burst to keep the limiter outside
the capacity result. The release gate requires at least 200 representative normalized input-work
tokens per second. The product continues to use the lower 100-token default.

```bash
modal run --env main scripts/run_input_capacity_gate.py \
  --source-sha "$(git rev-parse HEAD)" \
  --authorize \
  --output benchmark-results/candidates/input-capacity-live.json
```

The runner rejects implicit authorization, missing Modal credentials, a dirty source tree, broad or
mismatched placement, non-XTest attribution, missing or inefficient resource observations,
incomplete cleanup, and unsafe artifact fields. Keep failed artifacts immutable and publish only a
new sanitized passing artifact with its exact source revision and resolved configuration.

Do not wait for a browser paint or another visual change in this promotion gate. That would change
the stable immediate-observation contract and favor the prior arm's extra network round trip. Run
click-to-first-visual-change as a separate, non-promoting experiment. It remains experimental and
does not establish application readiness.

The current `Screenshot` result does not expose the capture backend. This gate therefore does not
claim per-sample MSS/XShm attribution. Daemon backend contract tests must establish persistent
capture selection separately. Add capture-backend attribution to this gate only after the Step
envelope exposes it consistently for both arms.

Run the exact clean commit that you want to promote:

```bash
source_sha="$(git rev-parse HEAD)"
MODAL_COMPUTER_USE_STEP_PROMOTION_ENVIRONMENT=main \
MODAL_COMPUTER_USE_STEP_PROMOTION_CLOUD=aws \
MODAL_COMPUTER_USE_STEP_PROMOTION_REGION=us-west-2 \
uv run modal run --env main scripts/run_step_promotion.py \
  --source-sha "$source_sha" \
  --sample-count 100 \
  --warmup-iterations 2 \
  --output-dir benchmark-results/candidates/computer-step-live
```

Keep the generated artifacts private until they pass secret review and the offline gate. Publish a
new dated report; never rewrite a historical report or artifact. The article's 47.10 ms value is
arithmetic over separate warm screenshot and click medians. It is not a measured fused turn, does
not establish Computer Step latency, and is not a release promise. Treat 47.10 ms only as a
non-gating engineering goal and distance metric for a newly measured Step result.

## Choose a command

Run a credential-free release report against the in-process mock daemon:

```bash
uv run computer-use benchmark report --mock-local --iterations 5
```

Use `action-batch` to compare one five-action batch with five separate calls:

```bash
uv run computer-use benchmark action-batch --mock-local --iterations 5
```

Add `--four-click-only` to compare the same four coordinate clicks as one ordered request and as
four sequential requests. Each iteration times the complete arm at the caller, and failed attempts
are retained without retries or replacement:

```bash
uv run computer-use benchmark action-batch \
  --mock-local \
  --four-click-only \
  --warmup-iterations 1 \
  --iterations 5
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
uv run modal setup
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

The tracked subprocess-runner A/B in
[`benchmark-data/modal-subprocess-runner-ab-2026-07-30.json`](../benchmark-data/modal-subprocess-runner-ab-2026-07-30.json)
came from three runs of that command into an isolated app, one per daemon subprocess backend, on the
shipping attested-tunnel path:

```bash
for backend in asyncio threaded isolated-asyncio; do
  uv run computer-use benchmark modal-colocated-client \
    --app-name modal-computer-use-subproc-ab \
    --runner-only \
    --modal-region us-west-2 \
    --modal-ingress attested-tunnel \
    --daemon-http-version 1.1 \
    --runner-path inherited \
    --surface daemon-http \
    --browser chromium \
    --resource-profile browser \
    --modal-cpu 4 \
    --modal-memory-mib 8192 \
    --runner-cpu 4 \
    --runner-memory-mib 8192 \
    --input-rate-limit-per-sec 0 \
    --input-backend xtest \
    --subprocess-backend "$backend" \
    --iterations 30 \
    --output "benchmark-results/subprocess-runner-ab-2026-07-30/$backend.json"
done
```

Each arm runs one warmup and 30 measured samples. The arms compare to each other only. The tracked
artifact records the configuration differences that stop them from replacing the 2026-07-24
subprocess A/B values.

The same three arms were rerun on 2026-07-31 at the canonical 1 core and 2048 MiB shape that the
rest of the current measurements use. The tracked artifact is
[`benchmark-data/modal-subprocess-runner-ab-1cpu-2026-07-31.json`](../benchmark-data/modal-subprocess-runner-ab-1cpu-2026-07-31.json)
and it came from three runs of this command:

```bash
for backend in asyncio threaded isolated-asyncio; do
  uv run computer-use benchmark modal-colocated-client \
    --app-name modal-computer-use-subprocess-ab-1cpu \
    --runner-only \
    --modal-region us-west-2 \
    --modal-ingress attested-tunnel \
    --daemon-http-version 1.1 \
    --runner-path inherited \
    --surface daemon-http \
    --browser chromium \
    --resource-profile browser \
    --modal-cpu 1 \
    --modal-memory-mib 2048 \
    --runner-cpu 1 \
    --runner-memory-mib 2048 \
    --input-rate-limit-per-sec 0 \
    --input-backend xtest \
    --subprocess-backend "$backend" \
    --iterations 30 \
    --output "benchmark-results/subprocess-ab-1cpu-2026-07-31/$backend.json"
done
```

Run the arms back to back and keep the default branch quiet for the length of the run, then check
that all three arms report one `git_revision` before promoting them. The rerun changes the p50
ordering between the two candidate fixes, so read the two artifacts together rather than
substituting one for the other. The rerun records the 2026-07-30 figures under
`comparison_baseline` and binds them by SHA-256. That pair varies date and requested shape at once,
so it does not isolate the effect of shape.

Dropping `--runner-only` keeps the external-caller arm, which turns the same command into a
caller-placement comparison. The tracked artifact in
[`benchmark-data/modal-caller-placement-us-west-2-2026-07-31.json`](../benchmark-data/modal-caller-placement-us-west-2-2026-07-31.json)
came from two draws of this command:

```bash
uv run computer-use benchmark modal-colocated-client \
  --app-name modal-computer-use-caller-placement \
  --modal-region us-west-2 \
  --caller-region-label dev-laptop-us-west \
  --modal-ingress attested-tunnel \
  --daemon-http-version 1.1 \
  --runner-path inherited \
  --surface daemon-http \
  --browser chromium \
  --resource-profile browser \
  --modal-cpu 1 \
  --modal-memory-mib 2048 \
  --runner-cpu 1 \
  --runner-memory-mib 2048 \
  --input-rate-limit-per-sec 0 \
  --input-backend xtest \
  --subprocess-backend isolated-asyncio \
  --iterations 30 \
  --output benchmark-results/caller-placement-2026-07-31/attested-tunnel-1cpu-draw1.json
```

One run measures both arms against one target desktop, so the external caller and the co-located
runner drive the same daemon. Because `--runner-path inherited` reuses the target base URL, both arms
also share the attested-tunnel ingress, which is why the tracked artifact records the ingress once
under `configuration.observed` rather than naming it in any measurement key.

The second draw used the same command with a `draw2` output path. The tracked artifact pins draw 1
and records draw 2 as replication. Draw 1 is pinned because unrelated pull requests landed on the
default branch while draw 2 was in flight, so draw 2's co-located runner launched at a newer revision
than its own external arm. Keep the default branch quiet for the length of a run, and check that both
arms of a draw report one `git_revision` before promoting it.

Use `modal-action-batching-ab` for the publishable four-click A/B alone. It launches one Modal
Function, creates one Connect target with the same requested region, checks their observed placement,
performs one warmup plus 30 measured iterations per arm, and runs terminal cleanup. The command will
only write beneath ignored `benchmark-results/`. Counts other than 30 plus one require `--pilot` and
produce an ineligible artifact.

```bash
evidence_harness_sha="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"

uv run python scripts/publish_modal_images.py --revision "$evidence_harness_sha"

uv run computer-use benchmark modal-action-batching-ab \
  --modal-region us-west-2 \
  --image-revision "$evidence_harness_sha" \
  --modal-cpu 4 \
  --modal-memory-mib 8192 \
  --warmup-iterations 1 \
  --iterations 30 \
  --output benchmark-results/action-batching-ab/final.json
```

Use `modal-optimized-ingress-ab` to compare Connect with the repository's attested-tunnel path from
one Function to one warm target. The harness requests the same explicit region, image, and resources
for the Function and target, verifies observed cloud and region placement, completes Connect
authorization before tunnel warmup, and reuses one HTTP/1.1 client per arm. It alternates arm order
for the zero-byte floor, full 1024x768 PNG, one move-and-click, and one ordered four-click batch.
Counts other than two warmups and 30 measured samples per arm require `--pilot`.

```bash
evidence_harness_sha="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"

uv run python scripts/publish_modal_images.py --revision "$evidence_harness_sha"

uv run computer-use benchmark modal-optimized-ingress-ab \
  --modal-region us-west-2 \
  --image-revision "$evidence_harness_sha" \
  --modal-cpu 4 \
  --modal-memory-mib 8192 \
  --warmup-iterations 2 \
  --iterations 30 \
  --output benchmark-results/modal-optimized-ingress-ab/final.json
```

The selection gate uses the geometric p50 score across the screenshot and two action cases. A winner
must improve that score by at least 10%, win at least two cases, and stay within 5% on any losing
case. The zero-byte floor does not select ingress. Keep authorization latency separate from recurring
samples, and run one bounded confirmation if the first result does not identify a winner.

Use `modal-optimized-provider` for the optimized provider evidence. From the clean evidence-harness
commit, publish its revision-addressed Images into
the active Modal environment used by the run, as described in
[Modal deployment](modal-deployment.md), then run:

```bash
evidence_harness_sha="$(git rev-parse HEAD)"

uv run computer-use benchmark modal-optimized-provider \
  --modal-region us-west-2 \
  --image-revision "$evidence_harness_sha" \
  --modal-cpu 1 \
  --modal-memory-mib 2048 \
  --runner-cpu 1 \
  --runner-memory-mib 2048 \
  --browser chromium \
  --iterations 30 \
  --warmup-iterations 1 \
  --output benchmark-results/modal-optimized-provider.json
```

This command runs one warmup and 30 fresh create-to-validated-screenshot samples, then uses a
separate warm target for the six operation rows. It fails unless every required sample, placement
check, and cleanup gate passes. Both target phases use the SDK's default attested-tunnel ingress:
Modal Connect authorizes the daemon before recurring requests move to the encrypted tunnel. The
resources are billable.

The run creates two kinds of machine, and each takes its own resource request. `--modal-cpu` and
`--modal-memory-mib` size every target Sandbox, which runs a full desktop. `--runner-cpu` and
`--runner-memory-mib` size the Modal Function that creates those targets and issues their daemon
requests. An omitted runner flag inherits the matching target value, so a command written before
these flags existed still requests one shape for both machines and still reports `runner_cpu`,
`runner_memory_mib`, `target_cpu`, and `target_memory_mib` as equal. The runner holds the HTTP
client inside the warm-operation timer, so a different runner shape is a different configuration:
rerun both phases and report the shapes rather than comparing across them.

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
  --iterations 30 \
  --env-file .env \
  --output benchmark-results/candidates/provider-compare-coordinate-command.json
```

Provider-default means the documented public SDK path and its default provider configuration.
For Modal that means the `ComputerConfig` defaults: standard resources, no browser profile, a
100-normalized-token-per-second input refill with a 400-token burst, `auto` typing with a 10 ms
character delay, and default placement.
For the 100- and 1000-character cases, `auto` resolves to clipboard, so the requested delay is not
applied per character. A benchmark must record both token-bucket values and any untimed pacing.
The command workload
requests `sh -c "printf '42\\n'"`, requires exit code 0 and stdout exactly `"42\n"`, and never
strips whitespace. Record any override and do not label that provider arm as default. Only the
separate Modal-optimized arm receives repository optimizations, including explicit `keystrokes`
typing with zero delay. Keep provider-default and Modal-optimized columns separate because their
caller topology, configuration, and sample count differ.

The E2B benchmark target uses a one-hour session lifetime so the 30-sample warm matrix can finish.
This changes how long the benchmark desktop remains available, not the public screenshot, input,
or command methods inside the timer. The SDK's five-minute default expires during repeated
1,000-character typing calls and leaves later command and verification rows on a dead session.

## Reproduce the historical July 19 Modal optimization evidence

[`modal-optimization-results-2026-07-19.json`](../benchmark-data/modal-optimization-results-2026-07-19.json)
is retained as immutable historical evidence. Its legacy optimization harness is not a current workflow.
Its recorded command manifests are provenance, not instructions to run against the current tree.

Reproduce the execution only from a separate worktree at source revision
`8c21cf1338fd747dca57bca6941c307270069712`, following the artifact's `command_manifest` and
`v2_command_manifest`. The tracked artifact records execution date `2026-07-19`, sanitizer and
normalizer revision `6f860de38df716c7cfdc0a23b186049751f34cd8`, and an open, unmerged dependency
revision `37f977f80de93800c005caeec7ead5222b00b040`. Re-normalization additionally requires the
private raw artifact named by its provenance and a checkout of the recorded normalizer revision.
The current checkout is not a valid reproduction environment for those legacy commands.

Use these maintained workflows for new evidence:

| Measurement goal | Current workflow or evidence |
| --- | --- |
| Optimized lifecycle and warm operations | `computer-use benchmark modal-optimized-provider`; [`modal-optimized-provider-2026-07-30.json`](../benchmark-data/modal-optimized-provider-2026-07-30.json) |
| Provider-default comparison | `computer-use benchmark compare`, followed by the provider sanitizer; [`provider-compare-coordinate-command-2026-07-30.json`](../benchmark-data/provider-compare-coordinate-command-2026-07-30.json) |
| Current provider presentation | [Warm-operation results, 2026-07-30](benchmark-results-2026-07-30-warm-paths.md) |
| Optimized SDK default promotion | [`run_optimized_default_promotion.py`](../scripts/run_optimized_default_promotion.py); [eligible 2026-08-08 result](benchmark-results-2026-08-08-optimized-default.md) |
| Inline versus Managed Image Release lifecycle | [`run_modal_image_lifecycle_benchmark.py`](../scripts/run_modal_image_lifecycle_benchmark.py); [eligible standard-variant result, 2026-08-08](benchmark-results-2026-08-08-image-lifecycle.md) |
| Action-to-frame observation | `computer-use benchmark modal-colocated-client --surface daemon-observation-stream`; [`modal-observation-2026-07-30.json`](../benchmark-data/modal-observation-2026-07-30.json) |
| Placement comparison | `computer-use benchmark modal-region-ab`, then `modal-region-summary` |
| Modal V2 candidate or optimized-frontier experiments | Use [`run_modal_v2_candidate_benchmark.py`](../scripts/run_modal_v2_candidate_benchmark.py) or [`run_modal_optimized_frontier_benchmark.py`](../scripts/run_modal_optimized_frontier_benchmark.py) with the archived gated methodology linked below |

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

## Verify the archived July 26 combined report

The archived combined report required all three measurements to share one clean, committed
evidence-harness revision. Its dated commands remain below as provenance, not as the current
provider workflow. The tracked artifacts are verified by
`tests/benchmarks/test_provider_results_artifact.py`.

The single-case observation input was produced with:

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

The raw provider-default artifact was sanitized before combining it. `current_reference` requires the
declared harness commit to equal `HEAD` and the tracked worktree to be clean:

```bash
evidence_harness_sha="$(git rev-parse HEAD)"

uv run python scripts/sanitize_provider_benchmark.py \
  benchmark-results/candidates/provider-compare-coordinate-command-2026-07-26.json \
  benchmark-data/provider-compare-coordinate-command-2026-07-26.json \
  --raw-artifact-path benchmark-results/candidates/provider-compare-coordinate-command-2026-07-26.json \
  --harness-commit "$evidence_harness_sha" \
  --status current_reference \
  --scope "provider-default SDK paths, one warmup and 30 measured iterations"
```

The two raw Modal artifacts were converted into strictly allowlisted tracked inputs. This preserves the
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

The combined artifact generator verifies the exact provider list,
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
  --format markdown
```

The original workflow reran all three sanitizer commands with `--check` to verify that the tracked
artifacts match their inputs. Keep the two raw Modal runner artifacts ignored. The tracked
allowlisted inputs bind their SHA-256 digests, and the combined artifact binds the exact bytes of
all three tracked inputs. The archived document adds its disposition notice and archive-relative
links around this rendered body.

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
   latency, as the maximum duration. Where a command sizes its runner and its targets separately,
   price the two shapes separately.
3. Calculate an explicit ceiling: resource rate multiplied by maximum billable duration and instance
   count, plus fixed operation, storage, snapshot, data-transfer, and minimum charges. State any
   omitted or unknown charge.
4. Record the ceiling with the run plan. Configure a provider budget or quota when available, and do
   not start when the ceiling exceeds the approved amount.

Modal charges CPU and memory on whichever is higher, the request or the actual usage, and enforces a
floor of 0.125 physical cores per container, where one physical core is two vCPU
([Resources](https://modal.com/docs/guide/resources) and [Pricing](https://modal.com/pricing), both
accessed 2026-07-29). A request above real usage is billed in full, so size each machine from its own
measured usage rather than copying the other's shape.

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

## Read the latency evidence

The following artifacts form a dated evidence set. Each artifact has a specific scope. A later run
does not replace an earlier run.

| Question | Tracked evidence | Boundary |
| --- | --- | --- |
| Does one request reduce four-click overhead? | [`modal-action-batching-ab-replication-2026-08-02.json`](../benchmark-data/modal-action-batching-ab-replication-2026-08-02.json) | The replication retains 30 samples per arm. It does not restore the missing July 29 arrays. |
| Does subprocess runner ownership affect latency? | [`modal-subprocess-runner-ab-samples-2026-07-30.json`](../benchmark-data/modal-subprocess-runner-ab-samples-2026-07-30.json) | The artifact retains 30 samples for each of three dated arms. |
| How was the six-cent figure calculated? | [`modal-optimized-provider-cost-estimate-2026-07-30.json`](../benchmark-data/modal-optimized-provider-cost-estimate-2026-07-30.json) | The value uses recorded wall time and July 29 list rates. It is not an invoice. |
| What produced the historical native-X11 result? | [`modal-native-x11-historical-source-2026-07-23.json`](../benchmark-data/modal-native-x11-historical-source-2026-07-23.json) | The source and aggregates are retained. The original three samples per arm are not retained. |
| Does a clean run reproduce the native-X11 direction? | [`modal-native-x11-backend-ab-replication-2026-08-02.json`](../benchmark-data/modal-native-x11-backend-ab-replication-2026-08-02.json) | The replication retains 30 samples per arm. It is not a reconstruction of the July 23 arrays. |
| What explains the large historical `xdotool` result? | [`modal-native-x11-runner-matrix-2026-08-02.json`](../benchmark-data/modal-native-x11-runner-matrix-2026-08-02.json) | The matrix tests 12 cells in three blocks. It supports a runner effect without superseding either dated result. |
| Did the matrix Sandboxes terminate? | [`modal-native-x11-sandbox-termination-reconciliation-2026-08-03.json`](../benchmark-data/modal-native-x11-sandbox-termination-reconciliation-2026-08-03.json) | Modal result metadata reports all 12 Sandboxes finished with code 137. Audit and billing records are not reconciled. |

The runner matrix is the primary entry point for the runner-effect claim. Its dependency digests
bind the historical source, clean replication, subprocess control, and bounded interpretation. The
termination record is a separate provider reconciliation. It does not validate the latency samples.

## Find methodology and evidence

- [Performance](performance.md) explains stable latency mechanisms.
- [Current provider results](benchmark-results-2026-07-30-warm-paths.md) state their evidence
  status, measurement boundaries, and provenance.
- [Optimized-default promotion results](benchmark-results-2026-08-08-optimized-default.md) report
  the same-topology SDK cutover gate and distinguish the measured operation from the historical
  47 ms arithmetic figure.
- The archive retains the [Modal V2 candidate methodology](archive/benchmarks/modal-v2-candidate-benchmark.md)
  and [optimized-frontier methodology](archive/benchmarks/modal-optimized-frontier-benchmark.md)
  with their gated experiment results.
- [Benchmark data policy](../benchmark-data/README.md) defines tracked artifact eligibility.
- [Archive policy](archive/README.md) explains why evidence leaves the current set.
