# Architecture

The design splits orchestration from execution. Orchestration (creating sandboxes, picking images, applying tags, attaching to existing runs) is Modal-native. Primitive execution (clicks, keystrokes, screenshots, recordings, artifact reads) is daemon-native. Provider model loops live outside the core package, in adapters and user code.

## Layers

### SDK layer

`ComputerSandbox` and `DaemonClient` live in the `modal_computer_use` package. They expose typed Python namespaces (`computer.mouse`, `computer.screenshots`, etc.) and send typed HTTP requests to the daemon. The SDK does not import `openai` or `anthropic`.

### Daemon layer

`computer-use-daemon` is an HTTP server that runs inside the sandbox on port `8080`. It supervises desktop processes, validates incoming actions, executes desktop-affecting primitives under an input lock, writes artifacts, and appends trace entries. In Modal mode, the daemon is reached through Sandbox Connect Tokens. See [security.md](security.md) for auth details.

### Desktop stack

The daemon supervises these processes:

- `Xvfb` provides the X11 display.
- A window manager (XFCE by default) handles window placement and focus.
- `x11vnc` plus `noVNC` provide optional remote view, off by default.

It also drives these CLI tools per request through feature-local X11 controllers. The daemon-facing
backend seam remains `DesktopBackend`/`X11DesktopBackend` in `daemon/desktop/x11.py`, while the
behavior lives next to the feature that owns it:

- `daemon/desktop/mouse.py` owns `xdotool` pointer, click, drag, scroll, and button state.
- `daemon/desktop/keyboard.py` owns key normalization, xdotool key commands, clipboard-paste
  typing fallback, and held-key state.
- `daemon/desktop/screenshots.py` owns `maim` capture, scaling, encoding, coordinate-space
  metadata, and screenshot artifact writes.
- `daemon/desktop/clipboard.py` owns clipboard read/write/clear through `xclip`.
- `daemon/desktop/windows.py` owns `wmctrl` parsing and window activation/close commands.
- `daemon/desktop/apps.py` and `daemon/desktop/browser.py` own application spawn and browser URL
  opening/wait behavior.
- `daemon/desktop/display.py` owns display metadata exposed by the backend.

Those controllers use these CLI tools:

- `xdotool` for mouse and keyboard input.
- `wmctrl` for window listing, activation, and close.
- `maim` for screenshots, with fallbacks when unavailable.
- `ffmpeg` for screen recording.
- `xclip` and `xsel` for clipboard read and write.

## Local mock backend

When `COMPUTER_USE_BACKEND=mock` is set, the daemon answers every action with a deterministic stub response. This lets tests and CI exercise the same routes and SDK surface without an X server. See [local-development.md](local-development.md).

## Reference projects

The design borrows operational patterns from Daytona's computer-use primitives, E2B's compact SDK, the Modal Sandbox examples, and the `anthropic-computer-use-modal` reference. It deliberately does not copy provider-first server APIs or hardcoded model loops; this package stays closer to a primitives layer than a provider-specific agent server.

## Formal spec

See [spec/modal_computer_use_spec_v7.md](spec/modal_computer_use_spec_v7.md) for the full design, route schemas, and rationale.
