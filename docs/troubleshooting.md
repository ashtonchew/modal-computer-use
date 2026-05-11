# Troubleshooting

## `/healthz` passes but `/readyz` fails

`/healthz` only means the daemon process is alive. `/readyz` checks desktop readiness. Verify Xvfb, the window manager, `xdotool`, `wmctrl`, `maim` or screenshot fallback tools, and noVNC when enabled.

## Artifacts are not visible outside Modal

Ephemeral sandbox artifacts live inside the sandbox. If you mount a Volume, visibility depends on Modal Volume sync/commit/reload semantics. Call `computer.artifacts.sync()` where configured and consult your Modal Volume setup.

## Provider actions click the wrong location

Make sure the screenshot coordinates sent to the model match the desktop coordinates, or pass the screenshot `CoordinateSpace` into the adapter.
