# Troubleshooting

Group covers daemon readiness, X11 desktop, input, screenshots, recordings, artifacts, deployment, and security.

## Daemon readiness

### `/healthz` passes but `/readyz` fails

`/healthz` only confirms the daemon process is alive. `/readyz` checks Xvfb, native X11 input, the
window manager, the screenshot pipeline, and noVNC when enabled. `xdotool` remains required while
the explicit compatibility typing method and automatic pre-emission fallback are supported;
`wmctrl` is required only when the native EWMH adapter cannot verify a live, self-referencing
`_NET_SUPPORTING_WM_CHECK` owner plus listing, activation, and close support. Hit `/readyz`
directly and read the per-check failure list.

### Sandbox fails readiness on Modal

Modal's `Probe.with_tcp(8080)` only waits for TCP accept. The daemon may accept connections before the desktop is fully up. SDK clients should poll `/readyz`, not just rely on Modal's readiness probe.

## X11 desktop

### Xvfb not responding

Xvfb may have failed to start or crashed. Check the supervised process logs (`processes.logs("xvfb")`) and confirm `DISPLAY` matches what Xvfb is bound to (default `:99`). A second Xvfb instance binding the same display will silently lose to the first.

### Screenshots come back black or empty

The window manager often hasn't drawn yet when the daemon starts. Wait for `/readyz`, then confirm at least one window is mapped. If you launch an app and screenshot immediately, give the WM a frame to draw, or use `windows.wait_for(...)`.

### Keyboard input not appearing

The target window probably is not focused. Activate it first with `windows.activate(...)`, then send
keys. If multiple Xvfb sessions are present, verify `DISPLAY`. For diagnosis, force
`COMPUTER_USE_INPUT_BACKEND=xtest` to fail closed when the native adapter cannot open the intended
display, or force `xdotool` to isolate compatibility-path behavior.

### Unicode typing issues

XTest injects keycodes rather than Unicode text. `method="keystrokes"` therefore requires every
character to be representable by the active XKB layout. Use `method="auto"` for normal SDK calls:
layout-mapped text uses native keystrokes, while long or unmapped Unicode text uses clipboard paste
and restores the previous clipboard. `method="clipboard"` forces that behavior, and legacy
`method="xdotool"` forces the compatibility path.

## Adapters

### Provider actions click the wrong location

The screenshot you sent the model and the desktop the daemon clicks on are not in the same coordinate space. Pass a [`CoordinateSpace`](glossary.md#coordinatespace) into the adapter that describes the screenshot dimensions, and the adapter will translate model coordinates back to desktop coordinates.

## Recordings

### Recording file is corrupted or empty

ffmpeg was killed hard before it could flush its trailer. Always stop a recording with `recordings.stop(...)` rather than terminating the sandbox mid-record. The daemon now surfaces ffmpeg's stderr tail in `Recording.error` to help confirm this.

## Artifacts

### Artifacts are not visible outside Modal

Ephemeral sandbox artifacts live inside the sandbox and disappear when it exits. If you mount a
Modal Volume, visibility depends on Modal commit and reload semantics. For Modal Volume v2 mounts,
configure `StorageConfig(persist_artifacts=True)` and call `computer.artifacts.sync()`; the daemon
runs `sync <artifacts_dir>` inside the sandbox and reports failure if the mountpoint commit fails.
Modal Volume v1 is not a supported immediate-sync target for this package; use run-scoped paths,
avoid concurrent writes to the same file, and reload already-mounted reader containers before
checking for committed changes.

## Deployment

### Modal sandbox keeps timing out idle

Increase the Modal idle timeout on the Sandbox config, or send periodic no-op actions from the agent loop. The daemon itself does not enforce idle timeouts; Modal does.

### Stale sandboxes are hard to identify

Use `ComputerSandboxManager.list()` to inspect safe Modal metadata, including run ID, owner,
creation time, config hash, and artifact directory when those tags are present. Use
`cleanup_expired(..., dry_run=True)` first to see candidates. Missing or invalid
`computer-use.created_at` tags are skipped rather than terminated automatically.

### Browser prewarm did not reduce first-page latency

Confirm the sandbox is using a browser profile and that `/v1/capabilities` reports
`image_profile` as `browser` or `browser-gpu`. Prewarm only helps browser startup cost; it does not
make slow pages, network calls, or login flows faster.

If you are testing GPU rendering, also check `/v1/browser/status`. `gpu_mode` shows which launch
mode the daemon resolved and `prewarm_result.output` shows the actual command args used. A Modal
GPU allocation alone is not proof that X11 browser windows are hardware-rendered.

### Warm-pool sandbox expired before claim

Keep an expiration timestamp with each sandbox ID and discard entries without enough TTL for the
expected task. Always call `/readyz` after attach because a listed sandbox may still be shutting
down or recovering.

### Filesystem snapshot does not restore expected GUI state

Use filesystem snapshots for files and installed application state. Do not rely on them as a
guarantee for live window placement, browser memory, authenticated sessions, or in-flight GUI
process state. Prefer the documented directory flow: `snapshot_directory(path)` on the source
sandbox, create a fresh normal computer-use sandbox, then `mount_image(path, snapshot_image)`.
Creating the desktop sandbox directly from a directory snapshot image is not a supported restore
pattern for this package. Re-open applications and verify readiness after restoring.

## Security incidents

### A noVNC URL was shared accidentally

Treat the URL as compromised. Tear down the sandbox immediately. The next sandbox you create with `expose_vnc` will receive a fresh tunnel and (if you did not pin one) a freshly generated VNC password.
