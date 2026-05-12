# Troubleshooting

Group covers daemon readiness, X11 desktop, input, screenshots, recordings, artifacts, deployment, and security.

## Daemon readiness

### `/healthz` passes but `/readyz` fails

`/healthz` only confirms the daemon process is alive. `/readyz` checks Xvfb, the window manager, the screenshot pipeline, required CLI tools (`xdotool`, `wmctrl`, `maim` or its fallback), and noVNC when enabled. Hit `/readyz` directly and read the per-check failure list.

### Sandbox fails readiness on Modal

Modal's `Probe.with_tcp(8080)` only waits for TCP accept. The daemon may accept connections before the desktop is fully up. SDK clients should poll `/readyz`, not just rely on Modal's readiness probe.

## X11 desktop

### Xvfb not responding

Xvfb may have failed to start or crashed. Check the supervised process logs (`processes.logs("xvfb")`) and confirm `DISPLAY` matches what Xvfb is bound to (default `:99`). A second Xvfb instance binding the same display will silently lose to the first.

### Screenshots come back black or empty

The window manager often hasn't drawn yet when the daemon starts. Wait for `/readyz`, then confirm at least one window is mapped. If you launch an app and screenshot immediately, give the WM a frame to draw, or use `windows.wait_for(...)`.

### Keyboard input not appearing

The target window probably is not focused. Activate it first with `windows.activate(...)`, then send keys. If multiple Xvfb sessions are present, `xdotool` may be typing into the wrong one; verify `DISPLAY`.

### Unicode typing issues

`xdotool type` falls back to keysym lookup for non-ASCII characters and may drop unsupported codepoints. For long Unicode strings, paste through the clipboard: `clipboard.set_text(text)` then `keyboard.hotkey("ctrl", "v")`.

## Adapters

### Provider actions click the wrong location

The screenshot you sent the model and the desktop the daemon clicks on are not in the same coordinate space. Pass a [`CoordinateSpace`](glossary.md#coordinatespace) into the adapter that describes the screenshot dimensions, and the adapter will translate model coordinates back to desktop coordinates.

## Recordings

### Recording file is corrupted or empty

ffmpeg was killed hard before it could flush its trailer. Always stop a recording with `recordings.stop(...)` rather than terminating the sandbox mid-record. The daemon now surfaces ffmpeg's stderr tail in `Recording.error` to help confirm this.

## Artifacts

### Artifacts are not visible outside Modal

Ephemeral sandbox artifacts live inside the sandbox and disappear when it exits. If you mount a Modal Volume, visibility depends on Volume sync, commit, and reload semantics. Call `computer.artifacts.sync()` where configured, then check the Volume from outside.

## Deployment

### Modal sandbox keeps timing out idle

Increase the Modal idle timeout on the Sandbox config, or send periodic no-op actions from the agent loop. The daemon itself does not enforce idle timeouts; Modal does.

### Stale sandboxes are hard to identify

Use `ComputerSandboxManager.list()` to inspect safe Modal metadata, including run ID, owner,
creation time, config hash, and artifact directory when those tags are present. Use
`cleanup_expired(..., dry_run=True)` first to see candidates. Missing or invalid
`computer-use.created_at` tags are skipped rather than terminated automatically.

## Security incidents

### A noVNC URL was shared accidentally

Treat the URL as compromised. Tear down the sandbox immediately. The next sandbox you create with `expose_vnc` will receive a fresh tunnel and (if you did not pin one) a freshly generated VNC password.
