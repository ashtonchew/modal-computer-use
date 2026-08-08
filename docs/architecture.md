# Architecture

The design splits orchestration from execution. Modal-native orchestration creates Sandboxes,
selects images, applies tags, and attaches to existing runs. The daemon executes clicks,
keystrokes, screenshots, recordings, and artifact reads. Provider model loops live outside the core
package in application code. Adapters only translate provider action and screenshot shapes.

The primary composition is async owner → versioned session handle → application-owned Modal
Function → one trajectory borrow → pooled daemon client → owner cleanup. The Function and Sandbox
use the same exact requested region. Missing or unverifiable placement fails before mutation.

## Layers

### SDK layer

`ComputerSandbox`, `AsyncComputerSandbox`, `DaemonClient`, and `AsyncDaemonClient` live in the
`modal_computer_use` package. They expose typed Python namespaces (`computer.mouse`,
`computer.screenshots`, etc.) and send typed HTTP requests to the daemon. Sync and async Modal
provisioning share one security-sensitive creation plan and validation policy; only provider I/O
changes at the adapter boundary. `ComputerSessionHandle.borrow_async()` is a separate leased
trajectory surface for a desktop provisioned earlier. The SDK does not import `openai` or
`anthropic`.

### Daemon layer

`computer-use-daemon` is an HTTP server that runs inside the sandbox on port `8080`. It supervises desktop processes, validates incoming actions, executes desktop-affecting primitives under an input lock, writes artifacts, and appends trace entries. In Modal mode, the daemon is reached through Sandbox Connect Tokens. See [security.md](security.md) for auth details.

### Desktop stack

The daemon supervises these processes:

- `Xvfb` provides the X11 display.
- A window manager (XFCE by default) handles window placement and focus.
- `x11vnc` plus `noVNC` provide optional remote view, off by default.

It drives native X11 adapters and compatibility CLI tools through feature-local controllers. The daemon-facing
backend seam remains `DesktopBackend`/`X11DesktopBackend` in `daemon/desktop/x11.py`, while the
behavior lives next to the feature that owns it:

- `daemon/desktop/xtest.py` owns the persistent Xlib/XTest session and distinguishes failure before
  input emission from failure after emission may have started.
- `daemon/desktop/mouse.py` owns native XTest pointer/button input, the `xdotool` compatibility
  adapter, and held-button state.
- `daemon/desktop/keyboard.py` owns XKB key mapping, native XTest input, the `xdotool`
  compatibility adapter, clipboard-paste typing fallback, and held-key state.
- `daemon/desktop/screenshots.py` owns cursor-hidden MSS capture, the file-capture ladder, scaling,
  encoding, coordinate-space metadata, screenshot readiness, and screenshot artifact writes.
- `daemon/desktop/clipboard.py` owns clipboard read/write/clear through `xclip`.
- `daemon/desktop/windows.py` owns native EWMH/Xlib window operations and the `wmctrl`
  compatibility adapter.
- `daemon/desktop/apps.py` and `daemon/desktop/browser.py` own application spawn and browser URL
  opening/wait behavior.
- `daemon/desktop/display.py` owns display metadata exposed by the backend.

Native input uses one persistent display connection rather than spawning a process per event.
With `COMPUTER_USE_INPUT_BACKEND=auto`, compatibility fallback is allowed only when the native
adapter reports that emission did not start. A possibly partial native operation is returned as a
non-replayable error.

The compatibility and capture controllers use these CLI tools:

- `xdotool` for compatibility mouse and keyboard input.
- `wmctrl` when the native EWMH adapter is unavailable.
- `scrot` as the first file-capture fallback when in-process MSS capture is unavailable.
- `maim` as the final file-capture fallback and the cursor-visible capture adapter.
- `ffmpeg` for screen recording.
- `xclip` for clipboard read and write.

Fallback decisions stay with the feature that can prove whether retrying through another
implementation is safe:

| Behavior | Preferred path | Compatibility path | Fallback boundary |
| --- | --- | --- | --- |
| Mouse and keyboard input | Persistent XTest/XKB | `xdotool` | Only before native emission starts. If native input might be partial, the request is terminal. |
| Window operations | Native EWMH/Xlib | `wmctrl` | When the window manager does not advertise or complete the requested EWMH operation. |
| Cursor-hidden capture | MSS XShm-preferred capture | `scrot`, then `maim` | After one MSS reset and retry cannot produce a valid frame. MSS can use XGetImage when XShm is unavailable. |
| Cursor-visible capture | `maim` | None | Invalid or unavailable cursor composition is terminal. |
| Change notification | XDamage hint | Source-hash polling | XDamage selects when to capture. Pixels and hashes confirm the change. |
| Same-region runner preparation | Modal Connect runner | Explicit external runner | Only when endpoint preparation is unavailable before dispatch. Authentication, permission, validation, version, and quota errors are terminal. |
| Warm allocation | Locked, live-verified slot | Cold creation | Only after an owned candidate rejection. Stale or unverifiable state cannot authorize termination. |

These paths are not one global retry chain. Each controller reports the backend or reason for its
own operation. Orchestration does not replay work after dispatch might have started.

The boundaries follow the upstream contracts. XTest is an input-synthesis protocol. EWMH
window-manager requests are advisory. MSS 10.2 selects XShm and can use XGetImage when required.
See the
[XTEST protocol](https://www.x.org/releases/X11R7.6/doc/xextproto/xtest.html),
[EWMH specification](https://specifications.freedesktop.org/wm/latest-single/), and
[MSS 10.2 release notes](https://python-mss.readthedocs.io/latest/release-history/v10.2.0.html).
Modal availability handling uses documented specialized exception types. It does not use the
catch-all `modal.exception.Error` base. See the
[Modal exception reference](https://modal.com/docs/sdk/py/latest/modal.exception).

The observation transport and action execution remain stable primitives. The Alpha
post-action visual-change feature composes them without creating another runtime layer:

```text
stable action primitive + stable observation transport
                    ↓
experimental first-visual-change composition
                    ↓
caller-owned application/model policy
```

The composition reports a correlated first visual change only. Semantic application readiness,
settle policy, and provider model loops stay outside core. See the
[Alpha guide](experimental-visual-change-observation.md).

## Local mock backend

When `COMPUTER_USE_BACKEND=mock` is set, the daemon answers every action with a deterministic stub response. This lets tests and CI exercise the same routes and SDK surface without an X server. See [local-development.md](local-development.md).

## Reference projects

The design borrows operational patterns from Daytona's computer-use primitives, E2B's compact SDK, the Modal Sandbox examples, and the `anthropic-computer-use-modal` reference. It deliberately does not copy provider-first server APIs or hardcoded model loops; this package stays closer to a primitives layer than a provider-specific agent server.

## Formal spec

See the [product specification](spec/product-spec.md) for the canonical
architecture, maturity boundaries, route ownership, and rationale.
