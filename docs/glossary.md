# Glossary

Short definitions for terms used across the docs. Listed alphabetically.

## Action schema

The provider-neutral dict format every adapter converts into. A typical entry has a `type` field (e.g. `mouse_move`, `left_click`, `type`) and a small set of typed parameters. The daemon validates the schema before executing any action.

## Adapter

Translator from a provider's action JSON into the action schema. Adapters do not call the provider API; they only normalize. See `docs/anthropic-adapter.md` and `docs/openai-adapter.md`.

## Application readiness

A caller-defined semantic condition that must hold before a particular next step. The Alpha
visual-change observation feature does not infer it.

## Artifact, artifact root

Files the daemon produces or accepts: screenshots, recordings, downloads, logs, traces. They live under the artifact root, which defaults to `/home/desktop/artifacts`. Public artifact APIs accept relative paths only.

## CoordinateSpace

The mapping from the screenshot pixel grid the model sees to the desktop pixel grid the daemon clicks on. If you downscale a screenshot before sending it to a model, pass a matching `CoordinateSpace` so the adapter can convert click coordinates back to the desktop grid.

## Causal frame

A frame correlated to a particular action request through matching request/action identifiers and
causal metadata.

## Computer Step

One borrowed action-to-frame request through `computer.step()`. It validates and executes an
ordered action array and returns `ComputerStepResult.actions`, `.screenshot`, and `.timing`.

## Immediate post-action frame

The screenshot returned by a Computer Step directly after its action phase. It is not proof of
application readiness or visual stability.

## Daemon

`computer-use-daemon`. The HTTP server that runs inside the sandbox on port `8080` and owns the desktop. The SDK is a thin client over its routes.

## First visual change

The first detected pixel or frame difference after the pre-action baseline. It is an observable,
not proof that the application is settled or semantically ready.

## Modal Sandbox

Modal's per-run container primitive. This package supervises one daemon plus a desktop stack inside each Sandbox.

## noVNC

A browser-based VNC frontend. It is off by default. When enabled, the URL grants live desktop access and must be treated as a secret.

## Primitive

A single typed operation the SDK exposes (`mouse.click`, `keyboard.type`, `screenshots.full`). The package calls itself "primitive-first" because it ships these and stops short of model loops.

## Sandbox Connect Token

Modal's auth mechanism for HTTP and WebSocket access into a Sandbox. The daemon expects connect-token requests on port `8080` in production; tokens passed in URL query strings are rejected.

## Service readiness

The `/readyz` lifecycle contract: the daemon and desktop substrate can accept work. It is distinct
from first visual change and application readiness.

## Settle policy

The caller or model's choice of an explicit wait, workload predicate, quiet window, or visual-change
heuristic before a dependent next action.

## Trace

NDJSON record of every action the daemon executes. Each entry includes the provider action, the normalized action, the result, timing, screenshot references, coordinate space, and any redactions. Stored at `artifacts/traces/actions.ndjson` by default.
