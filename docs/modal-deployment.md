# Modal Deployment

`ComputerSandbox.create()` lazily imports Modal, builds or accepts a Modal `Image`, and starts the daemon inside the sandbox:

```bash
python -m modal_computer_use.daemon
```

## Readiness

The daemon listens on port `8080`. Modal waits for the port to accept connections via `modal.Probe.with_tcp(8080)`. SDK clients should poll `/readyz` rather than relying on the TCP probe, because `/healthz` only confirms the daemon process is alive, not that the desktop is up.

The image launch command stays `python -m modal_computer_use.daemon`. Local repo work uses `uv run computer-use-daemon`. They differ because Modal installs the package into the image runtime; `uv run` is for the editable repo checkout.

## Sandbox configuration

Per current Modal docs, configure the Sandbox with:

- **Connect Tokens** authenticate HTTP and WebSocket requests to the daemon on port `8080`. See [security.md](security.md).
- **Network restrictions** use `block_network` and `cidr_allowlist` to limit outbound traffic.
- **Daemon ingress** defaults to an attested encrypted tunnel: Modal Connect authenticates the
  bootstrap request, then the daemon mints a short-lived bearer token for low-latency tunnel calls
  on port `8080`. Set `ComputerConfig(ingress="connect")` to keep all daemon traffic on Modal
  Connect, or `ingress="tunnel"` for a static daemon bearer token in trusted benchmark harnesses.
- **noVNC** is exposed only with explicit `encrypted_ports=[6080]`. Do not expose it on the public internet; use it only when you need manual debugging through an access-controlled tunnel.
- **Tags** are applied after creation with `Sandbox.set_tags()` and used for `Sandbox.list(tags=...)` attach and recovery flows.

The SDK does not pass tags into `Sandbox.create()`. It applies tags after creation so it works
with the current Modal Sandbox contract. Built-in tags are string-only and limited to safe
operational metadata such as `computer-use.run_id`, `computer-use.owner`,
`computer-use.created_at`, `computer-use.config_hash`, `computer-use.window_manager`, and
`computer-use.artifacts_dir`.

`modal.NetworkFileSystem` is intentionally unused. Persistent artifacts should use Modal Volumes
in user configuration or examples. For immediate visibility before sandbox termination, use a
Modal Volume v2 mount and set `StorageConfig(persist_artifacts=True)`;
`computer.artifacts.sync()` then runs Modal's documented `sync <artifacts_dir>` mountpoint commit
inside the sandbox. Modal Volume v1 is not a supported immediate-sync target for this package.
Readers already running with the same Volume still need `Volume.reload()` or
`Sandbox.reload_volumes()` before they can observe committed changes.

Browser profiles are explicit. Use `ResourceConfig(profile="browser")` plus
`BrowserConfig(kind="firefox" | "chromium", prewarm=True)` when browser startup dominates the
measured workload. Use `profile="browser-gpu"` only with an explicit `gpu` value. The SDK passes
`COMPUTER_USE_IMAGE_PROFILE`, `COMPUTER_USE_BROWSER`, and `COMPUTER_USE_BROWSER_PREWARM` into the
daemon environment so `/v1/capabilities` and `/v1/computer/status` can report the selected profile.

`ComputerSandbox.snapshot_directory(path)` delegates to Modal's documented
`Sandbox.snapshot_directory(path)` API for a Modal-backed sandbox. Restore by creating a fresh
normal computer-use sandbox and calling `computer.mount_image(path, snapshot_image)`, matching
Modal's `mount_image` pattern. Do not use a directory snapshot as the whole desktop base image:
live smoke on May 12, 2026 found that path did not reach desktop readiness. Directory snapshots
are documented by Modal as 30-day images after last creation or use; store durable artifacts in a
Volume or external system instead. `snapshot_filesystem()` remains a compatibility helper for
SDKs exposing that older method.

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
