# Architecture

The central invariant is: orchestration is Modal-native, primitive execution is daemon-native.

User code calls `ComputerSandbox` or `DaemonClient`. The SDK sends typed HTTP requests to `computer-use-daemon` on port `8080`. In Modal, that daemon is reached through Sandbox Connect Tokens. Inside the sandbox, the daemon supervises Xvfb, a window manager, x11vnc/noVNC when enabled, and desktop tools such as `xdotool`, `wmctrl`, `maim`, `ffmpeg`, `xclip`, and `xsel`.

Core modules do not own provider calls or model loops. OpenAI and Anthropic adapters only translate provider action JSON into the provider-neutral action union and call `computer.actions.run(...)`.

Local tests use a deterministic mock backend when X11 tools are unavailable. The same daemon routes and SDK namespaces are exercised.
