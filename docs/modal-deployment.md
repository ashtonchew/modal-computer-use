# Modal Deployment

`ComputerSandbox.create()` lazily imports Modal, builds or accepts a Modal `Image`, and starts the daemon inside the sandbox:

```bash
python -m modal_computer_use.daemon
```

## Readiness

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

Per current Modal docs, configure the Sandbox with:

- **Connect Tokens** authenticate HTTP and WebSocket requests to the daemon on port `8080`. See [security.md](security.md).
- **Network restrictions** use `block_network`, `outbound_cidr_allowlist`,
  `outbound_domain_allowlist`, and `inbound_cidr_allowlist`. All allowlists default to `None`, so
  general browser egress remains unrestricted unless a caller opts into a policy.
- **Daemon ingress** defaults to an attested encrypted tunnel: Modal Connect authenticates the
  bootstrap request, then the daemon mints a short-lived bearer token for low-latency tunnel calls
  on port `8080`. Set `ComputerConfig(ingress="connect")` to keep all daemon traffic on Modal
  Connect, or `ingress="tunnel"` for a static daemon bearer token in trusted benchmark harnesses.
- **Region placement** is controlled by `ComputerConfig(runtime={"modal_region": "..."})` for new
  sandboxes. Leave it unset to let Modal choose placement. Pin it when latency matters and a
  benchmark from the caller/model-loop environment shows a clear winner; live 2026-05-26
  transport-floor runs from the current development environment measured `attested-tunnel` 0B
  WebSocket p50 at `29.5ms` in `us-west` versus roughly `71ms` for default/`us-east`. Region is
  part of `ComputerConfig`, so attach-or-create reuse with config-hash checks will not silently
  reuse a sandbox created for a different region.
- **noVNC** is exposed only with explicit `encrypted_ports=[6080]`. Do not expose it on the public internet; use it only when you need manual debugging through an access-controlled tunnel.
- **Tags** are passed to `Sandbox.create(tags=...)` and used for `Sandbox.list(tags=...)` attach and
  recovery flows.

The SDK passes the complete reserved and caller tag set during Sandbox creation. Built-in tags are
string-only and limited to safe
operational metadata such as `computer-use.run_id`, `computer-use.owner`,
`computer-use.created_at`, `computer-use.config_hash`, and `computer-use.artifacts_dir`.
The resolved set is validated against Modal's 10-tag Sandbox limit before allocation. The desktop
window manager remains part of the configuration hash instead of consuming a separate tag.

The inline Image builder is the default rollback path. `ImageConfig(source="named",
revision="<full-git-sha>")` selects a revision-tagged standard, Firefox, or Chromium Image through
`Image.from_name()`. Run `scripts/publish_modal_images.py` from a clean commit to build and publish
all three variants. Modal Image tags are mutable, so repository policy treats a full Git SHA tag as
write-once. The publisher lists existing named Images, keeps existing target tags untouched, and
publishes only missing variants. This makes retries safe after partial publication. Run one
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
`BrowserConfig(kind="firefox" | "chromium", prewarm=True)` when browser startup dominates the
measured workload. Use `profile="browser-gpu"` only with an explicit `gpu` value. The SDK passes
`COMPUTER_USE_IMAGE_PROFILE`, `COMPUTER_USE_BROWSER`, and `COMPUTER_USE_BROWSER_PREWARM` into the
daemon environment so `/v1/capabilities` and `/v1/computer/status` can report the selected profile.

`ComputerSandbox.snapshot_directory(path)` delegates to Modal's documented
`Sandbox.snapshot_directory(path)` API for a Modal-backed sandbox. Restore by creating a fresh
normal computer-use sandbox and calling `computer.mount_image(path, snapshot_image)`, matching
Modal's `mount_image` pattern. Do not use a directory snapshot as the whole desktop base image:
live smoke on May 12, 2026 found that path did not reach desktop readiness. Both snapshot helpers
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

Attached metadata is limited to safe operational fields such as sandbox ID, app name, sandbox
name, run ID, owner, creation time, config hash, tags, and artifact directory. Connect tokens are
never stored there.

Region placement only applies when a sandbox is created. `attach()` and the reuse branch of
`attach_or_create()` cannot move an existing sandbox to a different Modal region. If a latency
profile requires a specific region, create a new sandbox with
`ComputerConfig(runtime={"modal_region": "us-west"})` or use the default config mismatch behavior to
reject an incompatible reused sandbox.

## Co-located runners and brokers

When the caller or model loop is the latency bottleneck, the lowest-risk production pattern is a
short-lived co-located runner Sandbox. The external SDK process creates the target desktop sandbox,
then starts a second runner Sandbox in the same Modal region. The runner receives only ephemeral
daemon connection details through its environment, talks directly to the target daemon, and is
terminated after the workload. See `examples/modal_colocated_runner.py`.

Use `run_modal_daemon_command_with_fallback()` for the production path. It creates a fresh Connect
Token and runs the workload in the target's requested Modal region. For a target created by
`ComputerSandbox.create()`, or reused through `attach_or_create()` with a matching config hash, the
helper inherits `runtime.modal_region`; callers should specify the placement once in
`ComputerConfig`. A conflicting explicit runner region raises `ConfigConflictError` rather than
silently breaking co-location. A target attached by ID, name, URL, or a deliberately mismatched
config has unknown creation policy, so its runner still requires an explicit `modal_region`.

The helper never guesses from the external caller's location or from the target's observed concrete
runtime region. Modal placement selectors such as `us-west` are scheduling policy, while an observed
region such as `us-west-2` is runtime evidence and is not automatically reusable as that policy.
Broad selectors preserve scheduling flexibility and therefore do not guarantee that two Sandboxes
land in one concrete provider region. If the workload needs that stronger co-location guarantee,
select a supported narrow region explicitly in `ComputerConfig` after measuring the real topology
and accepting the narrower region's availability and pricing tradeoffs.

Fallback to the current external attested endpoint happens only when the caller supplies an explicit
`external_runner` and typed Connect endpoint preparation is unavailable before dispatch. Validation,
placement, reserved-environment, programming, and Modal authentication, permission, invalid-request,
version, and quota errors remain terminal. The availability set is intentionally narrow:
connection, service, timeout, documented retriable-internal, missing-target, and terminated-Sandbox
errors. Without the explicit callable, preparation errors propagate. Once runner dispatch starts,
every failure is terminal and the helper never repeats the command. Fallback attribution uses a
stable semantic reason plus a sanitized exception type; it never returns raw exception text. The
command owns the persistent hot session and observation stream. A broker does not proxy action or
frame bytes.

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
an incompatible, invalid, finished, unready, or near-expiry Sandbox, retires an owned live candidate
with `wait=True`, and scans a bounded number of entries. Expected candidate rejection can continue
to the normal cold fallback. A failed live-tag read is terminal because ownership cannot be verified
after the queue entry has been consumed; the client detaches without terminating an unverified
target. Configuration/programming failures, ambiguous claim transitions, and incomplete retirement
are likewise terminal rather than being relabeled as pool misses. Claimed capacity is one-shot and
must be closed; it is never requeued. Browser and first-frame rejection use the feature-owned
`BrowserReadinessError` and `FrameValidationError` types; generic `RuntimeError` and `ValueError`
remain terminal. A candidate may be terminated only after the slot lock is held and its live tags
still match the dequeued entry; detach failures while declining a busy or mismatched slot propagate
without terminating capacity that another claimant may own.

Each Modal App and pool pair receives a distinct Queue. Each fixed slot uses its own partition.
`fill_warm_pool()` rebuilds that partition from the Sandbox's durable lifecycle tags, because
[Modal Queue partitions](https://modal.com/docs/sdk/py/latest/modal.Queue)
are cleared after 24 hours without a put and when their App stops. A fresh queue identity is written to the Sandbox before enqueue.
Claims compare that identity twice, so stale or concurrent duplicate entries cannot claim a slot.
Refill and the final claim transition also take a non-blocking file lock inside the target Sandbox.
The lock prevents a stale registry snapshot from restoring a slot after another consumer claims it.

Warm configs cannot set `idle_timeout_seconds` because Modal does not expose the remaining idle
lifetime. They also cannot set an explicit `vnc_password`, because that credential is fixed when
the Sandbox starts and must not cross claims. Expiry is conservative: it starts before the create
request and subtracts a configured skew. Pool tags record stable configuration identity, fixed slot, requested and actual region,
ready time, expiry, CPU, and memory. `reconcile_warm_pool()` removes near-expiry, incompatible, and
abandoned provisioning slots. Queue entries can outlive a terminated slot; the claim path rejects
those stale entries and preserves the cold fallback.

Claim metrics report pool hit or miss, every rejection reason, claim latency, total
request-to-first-frame latency, remaining lifetime, configured pool size, idle resource-seconds,
CPU core-seconds, memory GiB-seconds, public-rate estimated cost, and pending billed-cost status.
Public-rate estimates apply Modal's documented 1.5x broad-region or 1.75x narrow-region multiplier
when a container region is requested. Missing resource values keep both slot and aggregate estimates
partial.
Use the Workspace or Environment billing report after its data becomes available for reconciled
cost. Compare Modal warm capacity only with the same Modal config created on demand. Keep normal
provider-default warm actions for Daytona and E2B. Do not compare Modal warm claims with competitor
cold creation.

This is a data-plane optimization, not a new daemon primitive. Keep user/model code in the runner
or application layer; core SDK modules should only provide generic Sandbox orchestration helpers.
Use Connect Tokens or the attested tunnel default for daemon access, and treat returned daemon
tokens as secrets.

A Modal ASGI broker is a separate control-plane pattern. The broker may create, list, inspect, and
terminate sessions, but it should return direct daemon or runner connection metadata instead of
proxying screenshots and input actions. Proxying the hot path through the broker adds another
network hop and hides the latency source. See `examples/modal_session_broker.py` for a testable
control-plane example based on Modal's ASGI, lifecycle, concurrency, and proxy-auth primitives.

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

The protected v1 smoke tests exercise live manager attach/reuse/cleanup, honest Volume sync
semantics, and directory snapshot restore:

```bash
MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 MODAL_COMPUTER_USE_RUN_V1_SMOKE=1 \
  uv run pytest -m modal tests/test_modal_integration.py -q
```

As of May 12, 2026, manager lifecycle passes live. Volume v2 sync commits with
`sync <artifacts_dir>` and is visible through `Volume.read_file`. Snapshot restore uses Modal's
documented `snapshot_directory` plus `mount_image` flow rather than treating a directory snapshot
as the whole desktop image.
