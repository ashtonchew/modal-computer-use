# Modal optimized create-to-validated-screenshot research

Research date: 2026-07-26. The installed and locked Modal Python SDK is 1.5.2
([`pyproject.toml`](../pyproject.toml), [`uv.lock`](../uv.lock)). This non-normative note uses current
Modal documentation, the installed SDK, and this repository. The executable benchmark validator and
the accepted dated report remain authoritative.

## Decision

**Recommendation:** Run the complete benchmark in one ephemeral, normal Modal Function invocation.
Create one fresh V1 target Sandbox per lifecycle from that Function. Request the same Modal region
selector for the Function and target. Start one monotonic timer immediately before the product's public
`ComputerSandbox.create(..., wait=True)` call. Stop it only after the caller receives the raw PNG
through Sandbox Connect and Pillow fully decodes and validates the frame.

Do not use a Sandbox as the lifecycle runner. Modal states that Sandboxes are not authorized to
access other workspace resources. Normal Functions have Modal resource access unless
`restrict_modal_access=True` is set ([Modal security model](https://modal.com/docs/guide/sandbox-networking#security-model),
[Restricted Functions](https://modal.com/docs/guide/restricted-access)). The installed SDK also
selects container credentials inside a remote Function and ignores `MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET` there
(verified by inspecting the installed Modal SDK's `client.py`).

## Verified facts

### Creation and placement

- `modal.Sandbox.create(...)` is public. It accepts `app`, `image`, `region`, `cpu`, `memory`,
  `timeout`, `idle_timeout`, `tags`, and `readiness_probe`. It returns after Modal registers the
  Sandbox. Container creation continues asynchronously
  ([Modal lifecycle](https://modal.com/docs/guide/sandboxes#events); also verified against the
  installed Modal SDK's `sandbox.py`).
- An App is required only when creation occurs outside a Modal container. A normal Modal Function
  can therefore create a target Sandbox in its container context
  ([Modal Sandboxes](https://modal.com/docs/guide/sandboxes#what-are-sandboxes-and-why-should-i-use-them)).
- Functions and Sandboxes accept the same `region=` selector. The public selectors are broad or
  narrow geographic groups, such as `us` and `us-west`. Modal says that more granular definitions
  require sales access. A public narrow selector does not prove the same cloud region, zone, host,
  or availability zone ([Modal region selection](https://modal.com/docs/guide/region-selection#container-region-options)).
- Modal exposes the observed provider and location as `MODAL_CLOUD_PROVIDER` and `MODAL_REGION` in
  every container ([Modal environment variables](https://modal.com/docs/guide/environment_variables#container-runtime-environment-variables)).
  This repository reads those variables inside a target with `Sandbox.exec(...)`
  ([`sandbox.py`](../src/modal_computer_use/sandbox.py)).
- Modal routes Function calls through `us-east` by default. `routing_region=` can change that route.
  Most Sandbox operations go directly to the container, with minor exceptions. The proposed timer
  starts inside the Function, so Function invocation and runner cold start stay outside the measured
  target lifecycle ([Modal regional routing](https://modal.com/docs/guide/region-selection#regional-routing)).

**Consequence:** Requesting the same Modal region does not prove identical placement. Record whether
runner and target report the same `(MODAL_CLOUD_PROVIDER, MODAL_REGION)` pair, but label the
topology `same requested Modal region`. Do not claim exact co-region placement. If the
workspace has a documented granular-region capability, request that same granular value on both
resources and still verify the observed pair.

### Public lifecycle and readiness

The public V1 sequence is:

1. `modal.Sandbox.create(..., readiness_probe=modal.Probe.with_tcp(8080))`.
2. `sandbox.wait_until_ready(timeout=...)`.
3. `sandbox.create_connect_token(user_metadata=..., port=8080)`.
4. Send daemon requests to `credentials.url` with `Authorization: Bearer <credentials.token>`.
5. `sandbox.terminate(wait=True)`.
6. `sandbox.detach()`.

Modal defines the states as Created, Scheduled, Started, optional Ready, and Finished. Started means
that the entrypoint runs and tunnels and mounts are active. Ready means that the configured probe
succeeds. `wait_until_ready()` requires a configured probe and waits for that probe
([Modal lifecycle](https://modal.com/docs/guide/sandboxes#events),
[Modal readiness probes](https://modal.com/docs/guide/sandboxes#readiness-probes)). The installed SDK
inspection confirmed these semantics at `Sandbox.wait_until_ready()`.

The repository configures a TCP probe on port 8080, creates a Connect token, and then polls the
daemon `/readyz` route ([`sandbox.py`](../src/modal_computer_use/sandbox.py)). The two readiness
checks are not equal:

- Modal TCP readiness proves that port 8080 accepts connections.
- Daemon `/readyz` proves that the daemon and desktop substrate can accept work. It can become true
  after TCP readiness ([`glossary.md`](../docs/glossary.md#readiness)).
- A decoded screenshot proves the measured terminal condition. It is stronger than either probe for
  this benchmark.

Sandbox Connect Tokens are the public authenticated HTTP and WebSocket ingress. The token defaults
to port 8080. Modal adds the verified user metadata to an unspoofable header
([Modal Connect Tokens](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets)).
In SDK 1.5.2, inspection of `create_connect_token()` showed that it explicitly rejects V2 Sandbox
handles. Therefore, this benchmark must use the public V1 `modal.Sandbox.create`, not
`_experimental_create`.

### Exact measurement boundary

Use `time.perf_counter()` in the runner Function. Do not combine clocks from the external
coordinator, runner, target, or Modal control plane.

```text
t0 = immediately before ComputerSandbox.create(..., wait=True)
app/image lookup -> modal.Sandbox.create -> wait_until_ready -> create_connect_token -> Connect /readyz
   -> POST /v1/screenshots/full/raw -> receive all bytes
   -> Pillow Image.open -> image.load -> validate PNG and 1024x768
t1 = immediately after validation succeeds
elapsed_ms = (t1 - t0) * 1000
```

The raw screenshot call is `screenshots.full_bytes(format="png", processing="daemon")`. It sends a
`POST /v1/screenshots/full/raw` request and returns response bytes
([`screenshots.py`](../src/modal_computer_use/namespaces/screenshots.py)). The validator rejects an
empty payload, forces a full Pillow decode with `image.load()`, and checks format and exact geometry
([`latency.py`](../src/modal_computer_use/latency.py)). This is the required parsed and validated
terminal boundary. The historical benchmark contract starts before `ComputerSandbox.create` and
ends after a protected image decode. That contract remains
available in the [July 19 evidence artifact](../benchmark-data/modal-optimization-results-2026-07-19.json)
and its [commit-pinned benchmark source](https://github.com/ashtonchew/modal-computer-use/blob/8c21cf1338fd747dca57bca6941c307270069712/src/modal_computer_use/benchmarks/modal_optimization.py).
Current measurements use the maintained
[`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py)
workflow.

Record sub-stage marks, but do not replace the primary metric with them: create returned, Modal TCP
ready, Connect token returned, daemon `/readyz` true, response headers received, response body
complete, decode complete, and validation complete. A sample is valid only when all stages succeed.
Do not retry a lifecycle and do not replace a failed sample.

Read runner placement before `t0`. Read target placement after `t1`, because the target placement
probe uses `Sandbox.exec(...)` and must not inflate the create-to-screenshot metric. Mark a sample
ineligible if the required placement relation fails. Do not silently discard it.

### Credentials and secret handling

- Do not inject account tokens into the Function. The remote Function uses its Modal container
  identity. Inspection of SDK 1.5.2 confirmed that it ignores account-token environment variables
  in that context.
- Do not set `restrict_modal_access=True` on the runner. That option prevents Modal resource access
  ([Modal Restricted Functions](https://modal.com/docs/guide/restricted-access)).
- Keep the Connect URL and token only in local variables in the Function. Send the token only in the
  `Authorization` header. Do not use a query parameter. The repository removes query tokens from a
  returned URL and sends bearer authorization ([`sandbox.py`](../src/modal_computer_use/sandbox.py),
  [`http.py`](../src/modal_computer_use/transports/http.py)).
- Do not return or log tokens, Connect URLs, request headers, screenshot bytes, or exception text
  that can contain them. Return only stage names, timings, status codes, byte counts, image
  dimensions, image format, non-secret resource IDs, placement labels, and cleanup status. This
  matches the repository security rules ([`security.md`](../docs/security.md#logs)).
- Close the daemon client after the last request. Token lifetime is not a cleanup substitute.

### Cleanup guarantees

`Sandbox.terminate(wait=True)` is public, is a no-op for an already finished Sandbox, waits for
termination, and returns the exit code. `Sandbox.detach()` closes the client-side connection. In
Python, termination does not detach automatically
([Modal return codes](https://modal.com/docs/guide/sandboxes#return-codes),
[Modal client cleanup](https://modal.com/docs/guide/sandboxes#cleaning-up-client-side-connections)).

Use four cleanup layers:

1. Put `terminate(wait=True)` and `detach()` in an inner `finally` block for every acquired target
   handle.
2. Give each target a unique sample ID and exact run tags before creation.
3. Set short target `timeout` and `idle_timeout` values that exceed the per-sample readiness limit.
   These values bound leakage if the runner is killed before `finally` runs. Modal permits a maximum
   Sandbox lifetime of 24 hours ([Modal Sandbox timeouts](https://modal.com/docs/guide/sandboxes#timeouts)).
4. After all Function calls finish, run the external, exact-run-tag cleanup sweep. Fail the benchmark
   unless the sweep terminates all matches and a second inventory reports zero. The repository has
   this exact tagged sweep ([`sandbox.py`](../src/modal_computer_use/sandbox.py)) and treats
   incomplete target, detach, runner, or sweep cleanup as terminal
   ([`modal_optimized_frontier_execution.py`](../src/modal_computer_use/benchmarks/modal_optimized_frontier_execution.py)).

No in-process `finally` block is an absolute guarantee. A Function crash or timeout can bypass it.
The target lifetime bound and the out-of-band inventory are required for a defensible cleanup
claim. Configure the Function with no user retry policy. Modal propagates failures by default, but
container crashes can be rescheduled ([Modal retries](https://modal.com/docs/guide/retries)). The
unique sample ID must make duplicate execution visible.

### Thirty-sample practicality

**Verified:** This repository already completed 30 independent cold Modal lifecycles with 30 valid,
zero failed, and zero timed-out samples. The measured p50 was 11.195 seconds. The recorded target
resource-time proxy was USD 0.12697 for 343.79 target-seconds. It excluded runner compute, control
plane, and billing adjustments
([`modal-optimization-native-x11-2026-07-24.json`](../benchmark-data/modal-optimization-native-x11-2026-07-24.json)).
Modal bills Sandboxes by the second on the greater of requested and actual resource use. It has no
minimum usage-time increment ([Modal Sandbox pricing](https://modal.com/docs/guide/sandbox-resources#pay-for-what-you-use),
[Modal billing](https://modal.com/docs/guide/billing)). A specified narrow region has a 1.75x
multiplier ([Modal region pricing](https://modal.com/docs/guide/region-selection#pricing)).

**Assessment:** Thirty sequential samples are technically practical. The historical target proxy is
well below USD 1. A Function runner adds compute cost, and current actual usage can exceed requests.
Therefore, preregister a conservative spend ceiling and report Modal billing as unreconciled until
the usage export is available. Do not claim the historical USD 0.12697 as the new run cost.

Run samples sequentially for the primary distribution. Parallel creation tests capacity and
scheduler contention instead of one independent lifecycle. Use 30 attempted samples, no
replacement, and report p50 and p95 only when at least 20 samples are valid. The Function execution
timeout must exceed the per-target readiness timeout plus cleanup time. Modal Function timeouts can
be 1 second to 24 hours ([Modal Function timeouts](https://modal.com/docs/guide/timeouts)).

## Recommended protocol

1. Invoke one ephemeral, normal Modal Function with the pinned repository image and SDK 1.5.2. Set
   the runner container region and do not override routing. Invoke it once for the complete batch so
   that one runner is the measurement instrument. Keep runner startup outside every `t0`. Define the
   Function from the importable module baked into the Python 3.12 image; do not serialize the local
   function definition or mount caller source. Either would make the clean, revision-addressed image
   non-authoritative, and serialization would also unnecessarily require compatible caller and image
   Python versions.
2. Prebuild and pin the named target image. Do not let image construction occur inside the measured
   `Sandbox.create` call. Modal recommends named images to avoid lazy build work in creation
   ([Modal named Sandbox images](https://modal.com/docs/guide/sandboxes#separating-image-builds-from-sandbox-creation)).
   Selecting the named Chromium image installs the browser runtime; `browser.prewarm=False`
   intentionally avoids launching a browser process for this desktop-screenshot boundary.
3. Read runner placement once. In the Function, run 30 sequential target lifecycles with distinct
   sample IDs. Start a new `t0` for each target. Catch and record each sample failure. Do not retry it.
4. Call public `ComputerSandbox.create(..., wait=True)`, which uses V1 `modal.Sandbox.create`, with
   the daemon entrypoint, TCP readiness probe on 8080, pinned image, explicit resources, target
   region selector, short lifetime limits, and exact run and sample tags.
5. Call `wait_until_ready`. Create one port-8080 Connect token with bounded, non-secret metadata.
   Poll Connect `/readyz` until `ready=true`.
6. Request one raw full PNG over Connect. Receive all bytes. Force a Pillow decode. Validate nonempty
   payload, PNG format, and 1024x768 geometry. Stop `t1` only after all checks pass.
7. After `t1`, read target placement. Return only sanitized measurements and verification flags.
   Preserve placement mismatches and failures as attempted samples.
8. In `finally`, close the HTTP client, terminate the target with `wait=True`, and detach it. Treat
   each cleanup failure as terminal.
9. Create one separate warm target in the same Function, validate its initial raw frame, run only the
   six report operations, and clean it up. The lifecycle attempts never become warm-operation targets.
10. After all samples, run the exact-run-tag sweep from the external coordinator. Require a zero-item
   post-sweep inventory. Reconcile billed cost later.
11. Publish the primary metric as runner-local `t0` to `t1`. State whether placement was exact
    observed placement match or only the same requested Modal region. Do not compare this optimized
    topology with an external-caller create metric without labeling the topology difference.
