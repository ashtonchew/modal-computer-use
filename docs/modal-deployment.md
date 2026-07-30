# Modal Deployment

`ComputerSandbox.create()` lazily imports Modal, builds or accepts a Modal `Image`, and starts the daemon inside the sandbox:

```bash
python -m modal_computer_use.daemon
```

## Image and readiness

The daemon listens on port `8080`. Modal waits for the port to accept connections via `modal.Probe.with_tcp(8080)`. SDK clients should poll `/readyz` rather than relying on the TCP probe, because `/healthz` only confirms the daemon process is alive, not that the desktop is up.

The image launch command stays `python -m modal_computer_use.daemon`. Local repo work uses `uv run computer-use-daemon`. They differ because Modal installs the package into the image runtime; `uv run` is for the editable repo checkout.

Pass `SessionStartupTiming` to `ComputerSandbox.create(timing=...)` to record the observable cold
path. The SDK records request receipt, create start and return, TCP readiness, Connect token
creation, Connect `/readyz`, tunnel attestation, and final tunnel `/readyz`. Modal V1 does not expose
a supported scheduling timestamp. The SDK reports scheduling and daemon process start as
`unsupported` instead of inventing values. Call `ensure_browser_ready(..., timing=...)`, start the
observation stream with `timing=...`, and call `first_valid_frame(..., timing=...)` to extend the
same timeline through browser readiness, baseline consumption, and a decoded frame with the
configured geometry.

## Sandbox configuration

Configure the Sandbox through `ComputerConfig` and the creation arguments:

- **Connect Tokens** authenticate HTTP and WebSocket requests to the daemon on port `8080`. See [security.md](security.md).
- **Network restrictions** use `network.block_all`, `network.outbound_cidr_allowlist`,
  `network.outbound_domain_allowlist`, and `network.inbound_cidr_allowlist`. The SDK passes
  `network.block_all` to Modal as `block_network`; it cannot be combined with any allowlist.
  Outbound allowlists restrict egress. The inbound CIDR allowlist restricts incoming tunnel and
  Connect traffic, not egress. All allowlists default to `None`; see
  [Configuration](configuration.md#network-and-ingress) for field definitions.
- **Daemon ingress** defaults to an attested encrypted tunnel: Modal Connect authenticates the
  bootstrap request, then the daemon mints a short-lived bearer token for low-latency tunnel calls
  on port `8080`. Set `ComputerConfig(ingress="connect")` to keep all daemon traffic on Modal
  Connect, or `ingress="tunnel"` for a static daemon bearer token in trusted benchmark harnesses.
- **Region placement** is controlled by `ComputerConfig(runtime={"modal_region": "..."})` for new
  sandboxes. Leave it unset to let Modal choose placement. Pin it only after measuring from the
  actual caller or model-loop environment. Region is part of `ComputerConfig`, so config-hash
  checks prevent silent reuse of a sandbox created for a different requested region.
- **Resources** use `ResourceConfig` for profile, CPU, memory, and GPU requests. Browser profiles
  additionally use `BrowserConfig` for browser kind, prewarm, profile directory, launch arguments,
  startup URL, and GPU mode.
- **noVNC** is enabled with `ComputerConfig(expose_vnc="view_only")`. Use `"control"` only when the
  viewer must send input. Do not expose noVNC to the public internet; use it only through an
  access-controlled tunnel.
- **Tags** are passed to `Sandbox.create(tags=...)` and used for `Sandbox.list(tags=...)` attach and
  recovery flows.

The SDK passes the resolved reserved and caller tag set during Sandbox creation. Built-in tags are
string-only and limited to operational metadata such as `computer-use.run_id`, `computer-use.owner`,
`computer-use.created_at`, `computer-use.config_hash`, and `computer-use.artifacts_dir`.
The resolved set is validated against Modal's 10-tag Sandbox limit before allocation. The desktop
window manager remains part of the configuration hash instead of consuming a separate tag.

The inline Image builder is the default rollback path. `ImageConfig(source="named",
revision="<full-git-sha>")` selects a revision-tagged standard, Firefox, or Chromium Image through
`Image.from_name()`. Run [`scripts/publish_modal_images.py`](../scripts/publish_modal_images.py)
from a clean commit to build and publish all three variants. Modal Image tags are mutable, so
repository policy treats a full Git SHA tag as
write-once. The publisher lists existing named Images, keeps existing target tags untouched, and
publishes only missing variants. A retry therefore publishes only variants still missing. Run one
publisher at a time per Environment because the list and publish operations are not atomic.

`modal.NetworkFileSystem` is intentionally unused. Persistent artifacts should use Modal Volumes
in user configuration or examples. For immediate visibility before sandbox termination, use a
Modal Volume v2 mount and set `StorageConfig(persist_artifacts=True)`;
`computer.artifacts.sync()` then runs Modal's documented `sync <artifacts_dir>` mountpoint commit
inside the sandbox. Modal Volume v1 is not a supported immediate-sync target for this package.
Readers already running with the same Volume still need `Volume.reload()` or
`Sandbox.reload_volumes()` before they can observe committed changes. The SDK exposes
`computer.reload_volumes(timeout=55)`, which uses Modal 1.5.2 blocking reload behavior.

Browser profiles are explicit. Use `ResourceConfig(profile="browser")` plus
`BrowserConfig(kind="firefox", prewarm=True)` when Firefox startup dominates the measured workload;
use `kind="chromium"` for Chromium. Use `profile="browser-gpu"` only with an explicit `gpu` value.
The SDK passes `COMPUTER_USE_IMAGE_PROFILE`, `COMPUTER_USE_BROWSER`, and
`COMPUTER_USE_BROWSER_PREWARM` into the daemon environment so `/v1/capabilities` and
`/v1/computer/status` can report the selected profile.

`ComputerSandbox.snapshot_directory(path)` delegates to Modal's documented
`Sandbox.snapshot_directory(path)` API for a Modal-backed sandbox. Restore by creating a fresh
normal computer-use sandbox and calling `computer.mount_image(path, snapshot_image)`, matching
Modal's `mount_image` pattern. Do not use a directory snapshot as the whole desktop base image;
the supported restore contract mounts it into a normal computer-use image. Both snapshot helpers
pass an explicit 30-day TTL and a 55-second timeout. Callers can override either value or pass
`ttl=None` for indefinite retention. Store durable artifacts in a Volume or external system.

## Attach and recovery

Use `ComputerSandbox.attach()` for known handles:

- `sandbox_id` attaches directly with `modal.Sandbox.from_id`.
- `name` attaches with `modal.Sandbox.from_name` inside the selected app.
- `run_id` lists sandboxes tagged with `computer-use.run_id`.

Run ID matches must be exact. If more than one running sandbox has the same run ID, the SDK raises
`SandboxAmbiguousError` and the caller should attach by sandbox ID or name. Missing run ID matches
raise `SandboxUnavailableError`.

`ComputerSandbox.attach_or_create()` accepts `reuse="by_run_id"`, `reuse="by_name"`, or
`reuse="never"`. The old boolean form is still accepted: `True` means `"by_run_id"` and `False`
means `"never"`. Reuse policy is intentionally not part of `ComputerConfig`.

Existing sandboxes are checked against the requested config when their
`computer-use.config_hash` tag is available. A mismatch raises `ConfigConflictError` by default
so incompatible desktop/runtime settings are not silently reused. Use
`on_config_mismatch="reuse"` only for an intentional attach to the existing configuration.

Attached metadata is limited to operational fields such as sandbox ID, app name, sandbox
name, run ID, owner, creation time, config hash, tags, and artifact directory. Connect tokens are
never stored there.

Region placement only applies when a sandbox is created. `attach()` and the reuse branch of
`attach_or_create()` cannot move an existing sandbox to a different Modal region. If a latency
profile requires a specific region, create a new sandbox with
`ComputerConfig(runtime={"modal_region": "us-west"})` or use the default config mismatch behavior to
reject an incompatible reused sandbox.

### Hand a desktop to a Modal Function

For a stateful model loop, create the desktop in an owner process, call `session_handle()`, and pass
that value through normal Modal Function arguments. The complete executable pattern is
[`examples/modal_function_session_handoff.py`](../examples/modal_function_session_handoff.py). It
keeps the native user-owned `@app.function` surface. Deploy that App once, then look up the deployed
Function with `modal.Function.from_name(...)` and choose either `.remote(handle, task, run_id)` or
`.spawn(handle, task, run_id)`, including `.cancel()` for a spawned call. The SDK never calls `app.run()` or
creates an App for the handoff. One Function invocation enters one borrow context and holds that
same lease and daemon connection across its repeated screenshot, application-owned model, and
action steps. Native `borrow_async()` is canonical for async Modal Functions; `borrow()` remains
available for synchronous Functions. The borrow `run_id` is an application-generated unique ID for
one trajectory: it is the stable execution identity and cannot be reused after that durable run is
sealed. Modal FunctionCall, input, and task identifiers are private orchestration metadata, not
retry fences. If infrastructure dispatches overlapping attempts, the first attempt that acquires
the target lease wins and the others receive `SessionBusyError`. After any ambiguity following
acquisition, automatic takeover with the same run ID is rejected even after release or heartbeat
expiry. The application must reconcile the prior run and use a fresh run ID to continue.

Each async borrow renews its lease from one private worker thread and a dedicated blocking HTTP
transport, so ordinary blocking Python I/O on the Function event loop does not starve heartbeats.
Screenshots, actions, model calls, other async inputs, and cancellation remain native async work on
that event loop. Async Function code must not block the event loop. Prefer native async model and
application clients; use `asyncio.to_thread()` only for unavoidable blocking I/O, and move
CPU-bound work to a separate process or Function.

Modal Function `region=...` is a container placement request. It must exactly match the handle's
`requested_modal_region`. Modal's `routing_region=...` controls routing of Function inputs and
outputs; it is a different option and does not satisfy the placement check. The handoff makes no
claim about host identity, availability zone identity, loopback reachability, or private
networking.

Pass one region constant to both the Function decorator and `handle.borrow(function_region=...)`.
The borrow context checks that declaration against the handle; it cannot introspect the deployed
Function decorator or turn observed placement into a scheduling selector.

The deployed Function image must include `modal-computer-use[modal]`, and its Modal identity must
be allowed to access the target Sandbox's environment. Do not set `restrict_modal_access=True` on
this Function: borrowing resolves the Sandbox and mints fresh access from inside the invocation.
Borrow entry also requires Modal's official deployed-Function runtime marker
`MODAL_IS_REMOTE=1`; setting environment and region strings in a local process is not sufficient.
The compact live session tag contains a random suffix and a digest binding app name, Modal
environment, requested region, ingress, daemon HTTP version, VNC policy, and config hash. This
rejects any policy-field tampering before credentials are minted without reducing caller tag
capacity.

The creator owns the desktop lifetime. It must keep the target running until every `.remote()` call
has returned and every spawned call has completed or reached its cancellation outcome, then
terminate it. A borrower only detaches and closes its daemon client. Function cancellation requests
cancellation of that invocation; it is not a rollback of possibly partial input and never means
that the target desktop was terminated. On ordinary return or Python exception, the borrow context
detaches deterministically. The owner remains responsible for eventual target cleanup even when an
invocation is cancelled or lost.

Use `retries=0` to avoid configured Function retries for a stateful trajectory. Modal can still
reschedule an input after a container crash, so this does not create exactly-once execution. The
SDK does not retry, switch ingress, replay the callback, or replay the action. One borrow acquires
an exclusive trajectory lease, heartbeats it independently from long target operations, and
injects one gap-free sequence per mutation across HTTP and WebSocket primitives. A batch is one
sequenced operation. Lost responses are resolved against the exact durable receipt and never
replayed. A completed response-loss raises `OperationResultUnavailableError` with only the safe
sequence and an allowlisted stable operation-kind label. That state permits repeated
`observe_after_result_loss()` calls, each fixed to one daemon-processed full inline PNG with no
artifact write or operation sequence. It does not retain or reconstruct the original result, and a
successful observation never permits another mutation in the same borrow. The Function example
reobserves once, returns only safe status metadata, and exits the borrow; an application that elects
to continue must start a new borrow with a fresh run ID.

Reobservation is evidence of one later visible frame, not proof of semantic success. It misses
intermediate frames and animation and cannot prove readiness or expose invisible command,
clipboard, download, filesystem, network, or other effects outside the screen. Indeterminate state
still quarantines the target until the original owner inspects `recovery_status()` and acknowledges
the exact incident. The lease serializes trajectories for one desktop; it is not a multi-tenant
isolation boundary.

Function capacity is independent from desktop capacity. `min_containers=0` allows the Function to
scale to zero, adding a cold start after idle periods. A positive warm minimum reduces that startup
cost but accrues warm-container cost. Bound `max_containers` and application concurrency so one
desktop cannot receive overlapping trajectories. The target Sandbox continues to follow its own
timeout, idle-timeout, and billing lifecycle while Function capacity scales separately.

Async cancellation is reconciled and cleaned up under shielding before `CancelledError` propagates.
Borrowed cleanup stops its heartbeat worker, closes tracked WebSockets, joins the worker, releases
the lease when valid, closes both HTTP transports, and calls Modal detach; it never terminates the
owner target. If the bounded worker join deadline is missed, cleanup clears the worker's credentials,
attempts release to fence stale heartbeat headers, detaches, and reports a redacted lease-loss
failure. An existing user exception remains primary. Sync cleanup provides the same ownership rule.

The heartbeat worker does not make a blocked event loop responsive: screenshots, actions, other
async inputs, and cancellation delivery still wait for the loop. Process death stops the worker,
the daemon lease TTL remains the final ownership release, and CPU-bound or GIL-holding work can
still delay all Python threads.

The receipt journal is target-local SQLite using WAL. The pre-dispatch `IN_PROGRESS` transaction
uses an effective `synchronous=FULL` boundary; mutation dispatch does not begin until that commit
returns. Terminal completion, abandonment, status, and recovery bookkeeping use
`synchronous=NORMAL`. If a terminal commit is lost, the durable `IN_PROGRESS` receipt keeps the
target quarantined and fails closed.

This guarantee is **surviving target filesystem** durability: it covers daemon restart and an
operating-system or power loss when the target's local filesystem survives. It does not recover a
destroyed Sandbox, host filesystem, or GUI state. Target or Sandbox loss requires a new target and
a new run; the receipt alone cannot reconstruct the desktop. After an interrupted in-progress
receipt, owner acknowledgment is required before a new lease.

## Co-located runners and brokers

When caller-to-Sandbox latency is material, one deployment option is a short-lived co-located
runner Sandbox. The external SDK process creates the target desktop sandbox, then starts a second
runner Sandbox in the same Modal region. The runner receives only ephemeral
daemon connection details through its environment, talks directly to the target daemon, and is
terminated after the workload. See the
[co-located runner example](../examples/modal_colocated_runner.py).

Use `run_modal_daemon_command_with_fallback()` for this topology when the application has an
external fallback runner. The helper creates a fresh Connect Token and runs the workload in the
target's requested Modal region. For a target created by
`ComputerSandbox.create()`, or reused through `attach_or_create()` with a matching config hash, the
helper inherits `runtime.modal_region`; callers should specify the placement once in
`ComputerConfig`. A conflicting explicit runner region raises `ConfigConflictError` and prevents
the request. A target attached by ID, name, URL, or a deliberately mismatched
config has unknown creation policy, so its runner still requires an explicit `modal_region`.

The helper never guesses from the external caller's location or from the target's observed concrete
runtime region. Modal placement selectors such as `us-west` are scheduling policy, while an observed
region such as `us-west-2` is runtime evidence and is not automatically reusable as that policy.
Broad selectors preserve scheduling flexibility and therefore do not guarantee that two Sandboxes
land in one concrete provider region. If the workload needs that stronger co-location guarantee,
select a supported narrow region explicitly in `ComputerConfig` after measuring the real topology
and accepting the narrower region's availability and pricing tradeoffs.

Fallback to the external attested endpoint requires an explicit `external_runner`. Connect endpoint
preparation must fail before dispatch. Connection, service, timeout, documented
retriable-internal, missing-target, and terminated-Sandbox errors can use this fallback.

Validation, placement, reserved-environment, programming, authentication, permission,
invalid-request, version, and quota errors are terminal. Without `external_runner`, preparation
errors propagate. After dispatch starts, every failure is terminal. The helper does not repeat the
command.

Fallback attribution uses a stable reason and a sanitized exception type. It does not return raw
exception text. The command owns the persistent hot session and observation stream. A broker does
not proxy action or frame bytes.

Use `run_modal_daemon_command()` for explicit diagnostics. It supports three paths:

| Path | Execution location | Daemon endpoint | Use when |
| --- | --- | --- | --- |
| `inherited` | Separate runner Sandbox | The target `ComputerSandbox` client's current URL/token | The target already exposes the desired attested tunnel or tunnel endpoint. |
| `connect` | Separate runner Sandbox | A fresh Modal Connect Token URL/token | You want Modal's authenticated HTTP/WebSocket ingress for the runner. |
| `target-loopback` | Target desktop Sandbox via `Sandbox.exec` | `http://127.0.0.1:8080` plus the daemon bearer token | You need the same-container lower-bound diagnostic or trusted in-sandbox command execution. |

The helper injects `COMPUTER_USE_DAEMON_BASE_URL`, `COMPUTER_USE_DAEMON_RUNNER_PATH`,
`COMPUTER_USE_DAEMON_TOKEN` when present, and `COMPUTER_USE_TARGET_SANDBOX_ID` when available. User
environment values cannot override those reserved keys. `target-loopback` is intentionally not a
same-region runner: `127.0.0.1` only reaches the target daemon from inside the target sandbox.
Separate diagnostic runner paths also inherit a known target request when `modal_region` is omitted;
an explicit diagnostic region remains available for intentional cross-region measurements.

## Warm capacity

`ComputerSandboxManager.fill_warm_pool()` maintains a bounded set of fixed named slots. It enqueues
a slot only after Modal TCP readiness, daemon readiness, browser prewarm, and a decoded first frame.
Before it counts capacity, it removes incompatible, invalid, near-expiry, abandoned, and
out-of-capacity slots. Claimed slots remain owned by their consumers.
[Modal requires Sandbox names to be unique within an App](https://modal.com/docs/sdk/py/latest/modal.Sandbox),
so each fixed name is also the provider-side provisioning reservation. If concurrent fillers race,
the loser accepts the winner only after a registry read confirms the same compatible reserved slot.
`claim_warm_pool()` uses a non-blocking Modal Queue dequeue as the atomic claim point. A claim rejects
an incompatible, invalid, finished, unready, or near-expiry Sandbox. It scans a bounded number of
entries. An expected candidate rejection can continue to the normal cold fallback.

A claim retires a running candidate only after it holds the slot lock and verifies the live tags.
A failed live-tag read is terminal because the claim cannot verify ownership. The client detaches
and does not terminate the unverified target. A busy or mismatched target also detaches without
termination.

Configuration errors, programming errors, ambiguous claim transitions, and incomplete retirement
are terminal. The manager does not relabel them as pool misses. Browser rejection uses
`BrowserReadinessError`. First-frame rejection uses `FrameValidationError`. Generic `RuntimeError`
and `ValueError` remain terminal. Claimed capacity is one-shot. It must be closed and is never
requeued.

Each Modal App and pool pair receives a distinct Queue. Each fixed slot uses its own partition.
`fill_warm_pool()` rebuilds that partition from the Sandbox's durable lifecycle tags, because
[Modal Queue partitions](https://modal.com/docs/sdk/py/latest/modal.Queue)
are cleared after 24 hours without a put and when their App stops. A fresh queue identity is written to the Sandbox before enqueue.
Claims compare that identity twice, so stale or concurrent duplicate entries cannot claim a slot.
Refill, stale-entry discard, candidate validation, claim transition, and reconciliation use a
non-blocking file lock inside a running target Sandbox. Each path refreshes the lifecycle tags while
it holds the lock. Busy, claimed, mismatched, or unverifiable targets are detached or skipped. They
are not terminated.

A finished target does not need this lock because it cannot return to the running state. These
rules prevent stale queue or registry snapshots from terminating or restoring capacity after
another consumer claims it.

Warm configs cannot set `idle_timeout_seconds` because Modal does not expose the remaining idle
lifetime. They also cannot set an explicit `vnc_password`, because that credential is fixed when
the Sandbox starts and must not cross claims. Expiry is conservative: it starts before the create
request and subtracts a configured skew. Pool tags record stable configuration identity, fixed slot, requested and actual region,
ready time, expiry, CPU, and memory. `reconcile_warm_pool()` removes near-expiry, incompatible, and
abandoned provisioning slots only after the same locked live-tag verification. Queue entries can
outlive a terminated slot; the claim path rejects those stale entries and preserves the cold
fallback.

Claim metrics report pool hit or miss, every rejection reason, claim latency, total
request-to-first-frame latency, remaining lifetime, configured pool size, idle resource-seconds,
CPU core-seconds, memory GiB-seconds, public-rate estimated cost, and pending billed-cost status.
Missing resource values keep both slot and aggregate estimates partial.
Use the Workspace or Environment billing report after its data becomes available for reconciled
cost. Compare warm capacity only with the same configuration created on demand.

This is a data-plane optimization, not a new daemon primitive. Keep user/model code in the runner
or application layer; core SDK modules should only provide generic Sandbox orchestration helpers.
Use Connect Tokens or the attested tunnel default for daemon access, and treat returned daemon
tokens as secrets.

A Modal ASGI broker is a separate privileged, single-trust-domain administrative control-plane
pattern. The broker may create, list, inspect, and terminate sessions, but its caller-selected
owner labels and raw Sandbox IDs are not tenant authorization evidence. Do not expose that example
as a multi-tenant service. It should return direct daemon or runner connection metadata and leave
screenshots and input actions on the direct path. Proxying the hot path through the broker adds
another network hop and hides the latency source. See the
[session broker example](../examples/modal_session_broker.py) for a testable
control-plane example based on Modal's ASGI, lifecycle, concurrency, and proxy-auth primitives.

### Application-owned hosted runs

For a non-Python product surface, adapt the
[application-owned run gateway example](../examples/modal_run_gateway.py). Modal proxy
authentication remains an outer Workspace or Environment guard. Every request also goes through
the application's injected principal resolver, and every desktop, task, run read, poll, and
cancellation is authorized against that stable tenant identity. The client supplies only opaque
desktop/task keys plus a required idempotency key; it cannot supply a Sandbox ID, session handle,
Function name, FunctionCall ID, owner label, endpoint, or token.

The executable is a compatibility entry point over the behavior-local
[`examples/run_gateway`](../examples/run_gateway) package: domain invariants, application ports,
service orchestration, HTTP boundaries, and the Modal adapter remain independently testable.

The application must provide an atomic durable `RunStore`. Creation reserves
`(tenant_id, idempotency_key)` before calling `spawn.aio(handle, task, run_id)`, and duplicates
return the original application run only when a durable SHA-256 admission fingerprint matches the
originally authorized opaque desktop/task key pair. A mismatch returns a sanitized conflict and
does not reclaim even a stale reservation; the raw keys, handle, and task text are not stored. A
stale matching `reserved` record can win one CAS claim; a stale `dispatching` record becomes
terminal `indeterminate` because the first spawn may already have crossed Modal's boundary. Once
the private FunctionCall identity is stored, GET first reads the Function call graph. A successful
root is fetched once with `get.aio(timeout=0)` and must contain exactly the strict trajectory
envelope `{"status": "succeeded" | "failed" | "indeterminate"}`. No task or result content is
part of that contract. Pending and transiently unavailable polls preserve durable state. Empty,
multiple, malformed, or incomplete call-graph data falls back to the same nonblocking result poll;
expired, missing, or lossy results become `indeterminate`. A terminated Function becomes
`cancelled` only when cancellation intent was already durable, and otherwise becomes `failed`.
Cancellation durably enters
`cancellation_requested` before `cancel.aio(terminate_containers=False)` and never claims that the
desktop was rolled back or terminated.

The deployed application trajectory receives the same stable application `run_id` that it passes
to `handle.borrow_async(run_id=run_id, ...)`. The daemon lease and durable operation receipts are
the final mutation fence if infrastructure ambiguity dispatches duplicate Functions; private
FunctionCall, input, and task identifiers are only orchestration metadata. The first attempt to
acquire the target lease wins. The application must not use the same run ID for automatic takeover
after acquired-run ambiguity: it reconciles the application record and continues under a fresh run
ID only after the target reaches a safe closed or recovered state. These fences do not close the
cross-system persistence gap or recover a lost provider call handle. The deployer still owns
identity, authorization, quotas, billing, retention, auditing, durable task/result storage, and
reconciliation. Keep screenshot/action bytes on the direct daemon/runner path rather than turning
the gateway into a latency-sensitive primitive proxy.

## Cleanup

`ComputerSandboxManager.cleanup_expired(ttl_seconds=..., owner=None, dry_run=True)` inspects
Modal sandboxes through tags and returns a structured cleanup plan. Dry-run is the default.
Passing `dry_run=False` calls `terminate()` only on sandboxes that have a valid
`computer-use.created_at` tag older than the TTL cutoff. Sandboxes with missing or invalid
creation metadata are skipped with a reason, because the SDK cannot prove they are safe to clean
up.

Cleanup is orchestration, not daemon primitive execution. It never attaches a model loop, never
calls provider APIs, and does not rely on local daemon state to prove cloud lifecycle behavior.

## Authentication

For local Modal smoke tests, prefer Modal's native local auth:

```bash
uv sync --extra modal
uv run modal token new
uv run pytest -m modal
```

The Modal SDK reads credentials from `~/.modal.toml`, or from `MODAL_CONFIG_PATH` if set. In CI, use a Modal service user and expose `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` as CI secrets. The repository does not auto-load `.env` for Modal SDK auth; `.env` files are better used through `modal.Secret.from_dotenv()` when creating remote runtime secrets.

The noVNC view-only smoke test is opt-in because it creates a tunnel:

```bash
MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 uv run pytest tests/test_modal_integration.py -q
```

The test checks daemon readiness and process state without printing noVNC URLs or tokens.

The protected v1 smoke tests exercise live manager attach/reuse/cleanup, Volume v2 sync behavior,
and directory snapshot restore:

```bash
MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 MODAL_COMPUTER_USE_RUN_V1_SMOKE=1 \
  uv run pytest -m modal tests/test_modal_integration.py -q
```

The protected tests verify that Volume v2 sync commits with `sync <artifacts_dir>` and becomes
visible through `Volume.read_file`. They restore snapshots with `snapshot_directory` plus
`mount_image`, rather than treating a directory snapshot as the whole desktop image.

### Protected deployed-Function handoff smoke

The dedicated `Modal Function Handoff Smoke` workflow runs only through `workflow_dispatch` after
approval for the GitHub `modal-smoke` environment. It does not run the broader paid Modal
integration suite first. The Modal workspace must have a dedicated non-production Environment
named `modal-computer-use-smoke`; the protected service user must be able to deploy Functions and
create Sandboxes there. Do not point the job at a production Environment.

For each approved run, the workflow creates private random App and owner labels, deploys the
test-only App from the checked-out package source, and resolves its Function with
`modal.Function.from_name(...)`. The owner creates one noVNC-disabled target Sandbox in the same
non-production Modal Environment and requested region, JSON-round-trips its
`ComputerSessionHandle` for the attested-tunnel policy, and invokes the deployed async Function
exactly once. The Function enters one `borrow_async()` context, authorizes the tunnel, waits for
daemon readiness as part of borrow entry, captures one full inline screenshot, and applies one
sequenced wait action that does not type content.

The smoke proves compatibility with the repository-pinned Modal SDK for deployed Function argument
serialization, the official remote runtime marker, cross-object authorization, Connect-token
issuance, attested handle policy, lease acquisition and release, async daemon access, and borrower
cleanup. It also verifies that the borrower leaves the target running and that the owner explicitly
terminates it.

The result is deliberately limited to success booleans, screenshot width and height, and the
documented `MODAL_CLOUD_PROVIDER` and `MODAL_REGION` observations for the Function. The owner
records the same two placement labels from `runtime_placement()`. Both observations must be
present, but the test does not require them to match and does not infer availability zone, host,
loopback, private-network, or co-location properties. It does not return or log screenshot bytes
or digests, provider identifiers, endpoints, credentials, call identifiers, prompts, or typed
content.

Function retries are disabled, warm and maximum Function capacity are bounded, and both the
Function and target have finite timeouts. An `if: always()` step terminates only targets with the
exact private owner tag and then stops the run-scoped App, while job concurrency prevents duplicate
paid runs. GitHub force-cancellation can prevent cleanup steps from executing, so target timeout and
idle timeout remain the final cost backstop. Run this smoke intentionally for Modal SDK upgrades or
handoff protocol changes; it is compatibility validation, not a recurring benchmark or an
exactly-once guarantee.

## Performance and benchmark evidence

Use [Performance](performance.md) for placement, ingress, image, browser, and warm-capacity decision
guidance. Use [Benchmarking](benchmarking.md) for reproducible commands, credentials, costs,
cleanup, and reporting rules. The [current provider comparison](benchmark-results-2026-07-26-provider-results.md)
records dated evidence and its measurement boundaries; do not treat those results as deployment
defaults for a different caller or workload.
