# `modal-computer-use`: Daytona-style computer-use primitives on Modal

> **Archive category:** Superseded  
> **Date or revision:** 2026-05-11, v5  
> **Question:** How should the daemon-first Modal computer-use architecture incorporate the v4
> implementation, API, and security review?  
> **Disposition:** [Specification v6](modal_computer_use_spec_v6.md) superseded this design with
> UV-first tooling. Preserve the historical body; add corrections only as explicit notes.

**Status:** implementation plan and technical specification  
**Prepared:** 2026-05-11  
**Revision:** v5, best-practice architecture and implementation patch after competitor/API/security review  
**Recommended repository name:** `modal-computer-use`  
**Recommended Python import name:** `modal_computer_use`  
**Alternative brand name:** `modal-compute-use`, though `modal-computer-use` better matches the accepted term used by Daytona, E2B, OpenAI, and Anthropic-style agent harnesses.

**v5 delta:** this revision preserves the v4 daemon-first, primitive-first, Modal-native design, but applies the critique as concrete spec changes. v5 adds explicit liveness/readiness/version/capability endpoints; a local daemon/test mode; renamed and split public config models; a public `ComputerSandboxManager` instead of generic `SandboxManager`; stricter Connect Token and noVNC rules; a first-class coordinate-space model; provider-versioned OpenAI and Anthropic adapters; clipboard, browser, apps, windows, and stuck-input recovery primitives; artifact manifests with content hashes; an actionable trace/replay schema; budget controls; Volume sync semantics; compatibility fixtures; and a sharply scoped v0.1/v0.2/v1.0 roadmap.

**Positioning:** this project is not intended to beat managed desktop-sandbox providers for every user. It is intended to be the best open-source, Modal-native computer-use primitive layer for users who want to own the image, daemon protocol, adapters, artifacts, traces, replay, and Modal scaling/persistence strategy.

---

## 0. Best-practice fixes applied in v5

This section is new in v5 so implementers can see exactly how the critique was converted into concrete spec changes. The rest of the document folds these fixes into the normal v4 format.

| Area | What changed | Why | How to implement |
|---|---|---|---|
| Daemon lifecycle | Split `/health` into `/healthz`, `/readyz`, `/v1/version`, and `/v1/capabilities`. | A live HTTP server is not the same as a usable desktop. SDK/daemon compatibility must be explicit. | `/healthz` returns daemon liveness only. `/readyz` checks Xvfb, window manager, screenshot capture, required tools, and noVNC when enabled. `/v1/version` returns daemon/API compatibility. `/v1/capabilities` returns supported primitives, formats, adapter versions, and image profile. |
| Transport security | Keep Modal Connect Tokens as the daemon control-plane auth, disable query-param token use by default, and add local bearer-token mode. | Connect Tokens are the right Modal-native auth surface, but local tests cannot depend on Modal. Query tokens leak through URLs/logs. | SDK sends `Authorization: Bearer <token>`. Daemon can optionally reject `_modal_connect_token` query auth. Local mode accepts `COMPUTER_USE_LOCAL_TOKEN` on localhost only. |
| noVNC | Replace boolean-only `expose_vnc` semantics with `off`, `view_only`, and `control` modes. | Viewing and controlling a live desktop are different security states. | `expose_vnc: Literal[False, True, "off", "view_only", "control"]`; normalize `True` to `control` for compatibility. Generate passwords, redact URLs, and expose manual takeover state. |
| Public manager name | Rename public `SandboxManager` to `ComputerSandboxManager`. | The old name is generic and collides conceptually with Modal’s sandbox API. | Keep `SandboxManager` only as a deprecated alias or example-local name. Public docs use `ComputerSandboxManager`. |
| Config model | Split flat `ComputerConfig` into nested desktop/runtime/resources/network/storage/browser/action/budget configs. | v4 mixed runtime desktop config, lifecycle policy, adapter defaults, storage, and cost controls in one class. | `ComputerConfig(desktop=..., resources=..., storage=..., browser=..., actions=..., budgets=...)`. Move reuse policy to `attach_or_create`, not config. |
| Naming cleanup | Rename `blocked` to `block_all`, `allowlist` to `cidr_allowlist`, `memory_mb` to `memory_mib`, `recording_dir` to `recordings_dir`, `action_trace` to `trace_actions`. | The old names were ambiguous or inconsistent. | Accept old names as Pydantic aliases with deprecation warnings through v0.x only. |
| Local development | Add `DaemonClient` and `ComputerSandbox.local()`. | A daemon-first repo needs fast tests without Modal credentials or cloud cold starts. | Local Docker/Xvfb runner starts daemon on `127.0.0.1:8080`; SDK uses the same namespaces over HTTP. |
| Actions | Make `wait`, path-based drag, modifier-assisted mouse actions, per-action timeouts, idempotency, and max batch limits first-class. | OpenAI can return multiple ordered actions and modifier/path shapes; batching is the daemon’s clearest performance advantage. | `/v1/actions/run` uses discriminated action unions, validates whole batch before execution, returns per-action results, and optionally captures final screenshot. |
| Input recovery | Add `POST /v1/input/release-all`. | Down/up/hold failures can leave the virtual desktop unusable. | Track held keys/buttons in daemon. Release all in try/finally paths and expose manual recovery route/SDK method. |
| Coordinate transforms | Add `CoordinateSpace` to every screenshot and adapter. | Wrong coordinate scaling is one of the highest-risk model-loop failure modes. | Screenshot responses include desktop/image dimensions, scale, source region, and transform helpers. Adapters never silently scale without metadata. |
| Screenshot API | Make zoom region-first and add artifact-backed screenshot storage. | Region-first zoom maps better to provider schemas and replay. Inline base64 is bad for large screenshots. | `screenshots.zoom(region=Region, scale=2.0)` plus `zoom_around(...)` convenience. Request `storage='inline'|'artifact'|'auto'`. |
| Clipboard | Add explicit clipboard get/set/clear primitives. | Unicode typing and paste-heavy workflows need reliable clipboard control; logs must avoid leaking text. | `/v1/clipboard/text` GET/PUT/DELETE; SDK `computer.clipboard.*`; logs record length/hash only. |
| Browser/app ergonomics | Add `browser.open_url`, `browser.status`, `apps.launch`, `apps.open_artifact`, and window focus APIs. | E2B-style desktop users expect launch/open/wait helpers; v4 only had image profile knobs. | Implement as daemon routes using `xdg-open`, configured browser commands, `wmctrl`, and readiness checks. Keep Playwright/DOM out of core. |
| Artifacts | Add artifact manifest, `artifact://` URIs, SHA-256, MIME/content type, retention, and call linkage. | Trace/replay/debugging need stable references, not just paths. | Every artifact write returns `ArtifactInfo`; daemon appends `artifacts/manifest.ndjson` when tracing or persistence is enabled. |
| Trace/replay | Define NDJSON schema and CLI plan. | This is a realistic differentiator over provider SDKs and the Anthropic Modal reference. | `actions.ndjson` stores provider action, normalized action, result, screenshot refs, coordinate space, redactions, and errors. `computer-use trace validate/replay` lands in v0.2/v1.0. |
| Provider adapters | Version OpenAI/Anthropic adapters and fail closed on unknown actions. | Provider schemas evolve and should not silently drift. | Fixture matrix: OpenAI actions, Anthropic `computer_20241022`, `computer_20250124`, and `computer_20251124`. Unknown actions raise `UnsupportedActionError` by default. |
| Persistence | Add explicit `artifacts.sync()` semantics for Modal Volumes. | Volume changes are not always visible immediately; v4 did not specify user-facing behavior. | If a Volume is mounted, daemon/SDK can run supported sync/commit operation or document no-op. Return `persistent=true/false`. |
| Cost controls | Add budgets for actions, screenshots, bytes, recording duration, batches, and idle time. | Agents can loop indefinitely and generate cost. | `BudgetConfig`; daemon enforces per-run counters and returns `budget_exceeded`. |
| Observability | Add structured logs and optional OpenTelemetry boundary. | Production users need traces across SDK, daemon, xdotool/ffmpeg, and artifacts. | JSON logs by default; optional OTel spans for SDK request, daemon route, subprocess action, screenshot processing, and artifact writes. |
| Roadmap | Replace broad milestones with v0.1/v0.2/v1.0 scope. | v4 was too broad for a first implementation. | v0.1 = daemon-backed desktop MVP. v0.2 = Daytona-core parity plus provider action compatibility. v1.0 = production Modal-native harness. |

---

## 1. Executive summary

`modal-computer-use` should be a thin, high-rigor open-source wrapper that turns a Modal Sandbox into a remotely controllable Linux desktop with a stable SDK and API surface modeled after Daytona's Computer Use primitives.

The repository should not try to become a full agent framework. It should provide the substrate that agents need: a desktop, a screenshot loop, mouse and keyboard controls, recording, display/window metadata, lifecycle/process management, safe networking, traceable actions, reproducible artifacts, and strongly typed client APIs. The LLM loop should live in examples and optional adapters, not in the core package.

After reviewing `yasyf/anthropic-computer-use-modal`, the plan should become more Modal-native in orchestration but remain daemon-backed for primitives. That repository validates Modal as a computer-use substrate and contributes several strong patterns: run-scoped sandbox reuse, debugging tunnels, artifact persistence, browser prewarm, optional GPU acceleration, and screenshot post-processing outside the sandbox hot path. The critical refinement is to borrow those operational patterns without inheriting its core shape: the new repo should not be Anthropic-loop-first, and it should not use repeated Modal API command execution as the normal transport for every GUI action.

The best implementation is:

1. **A Modal-managed Linux Sandbox** launched with a custom Modal `Image` that contains X11 desktop dependencies: Xvfb, XFCE or a minimal window manager, x11vnc, noVNC/websockify, xdotool, wmctrl, maim, ffmpeg, xclip/xsel, and the package's daemon.
2. **An in-sandbox daemon** (`computer-use-daemon`) listening on port `8080`, exposed through Modal Sandbox Connect Tokens. The daemon owns process supervision, input serialization, screenshots, recordings, display/window inspection, and logs.
3. **A Python client SDK** that creates or attaches to Modal Sandboxes, obtains connect tokens, calls the daemon over HTTP, optionally exposes a noVNC tunnel for manual viewing, and presents a Daytona-like API:
   - `computer.start()`, `computer.stop()`, `computer.status()`
   - `computer.mouse.click/move/drag/scroll/position/down/up()`
   - `computer.keyboard.type/press/hotkey/hold/supported_keys()`
   - `computer.clipboard.get_text/set_text/clear()`
   - `computer.screenshots.full/region/zoom()` with `format`, `quality`, `scale`, `show_cursor`, `processing`, and `storage` parameters
   - `computer.recordings.start/stop/list/get/delete/download()`
   - `computer.display.info()` and `computer.windows.list/active/activate/close/wait_for()`
   - `computer.processes.status/restart/logs/stderr()`
   - `computer.actions.run/validate()`
   - `computer.artifacts.list/read/write/download/upload/delete/sync()`
   - `computer.browser.open_url/status()` and `computer.apps.launch/open_artifact()`
   - `computer.input.release_all()`
4. **A session and artifact layer** inspired by `anthropic-computer-use-modal`, but generalized around one canonical `run_id`, sandbox lookup by ID/name/tags, safe artifact roots, optional Volume persistence, and explicit cleanup semantics. A legacy `request_id` alias may exist only at compatibility boundaries.
5. **Optional adapters** for OpenAI Computer Use actions, Anthropic-style action schemas, and generic tool-calling actions. These adapters translate model-returned actions into core SDK calls but do not own model calls, prompts, browser policy, or business logic.
6. **Performance profiles** for browser-heavy agents: browser prewarm during image build/startup, optional GPU, raw-screenshot fast paths, client/control-plane screenshot post-processing, and warm-pool examples.
7. **Production defaults:** secure-by-default API access via Modal Connect Tokens, no exposed raw control API by default, optional encrypted noVNC tunnel with generated password, strict coordinate/key validation, serialized input events, call IDs, structured logs, artifact path restrictions, and recording retention controls.

This design matches Daytona's primitive surface while using Modal's native strengths: secure Sandboxes, `Sandbox.create`, custom Images, runtime command execution, Connect Tokens, tunnels, filesystem access, snapshots, Volumes, tags, readiness probes, and warm-pool patterns. The public Modal/Anthropic repo is best treated as proof that these patterns matter in production, not as the core architecture to copy.

---

## 2. Source-grounded current state

### 2.1 Daytona Computer Use primitives

Daytona Computer Use exposes programmatic desktop control inside sandboxes. The documented surface includes:

- Lifecycle: start, stop, status.
- Process management: process status, restart, logs, errors.
- Mouse operations: click, move, drag, scroll, get position.
- Keyboard operations: type text, press keys with modifiers, hotkeys, supported-key discovery.
- Screenshot operations: full screen, region, compressed full screen, compressed region.
- Screen recording: configurable directory, start, stop, list, get, delete, download, dashboard.
- Display operations: display info and window listing.

The Daytona implementation starts a desktop stack consisting of Xvfb, xfce4, x11vnc, and noVNC. Linux Computer Use is generally documented; Windows and macOS support are described as private alpha. Daytona's docs frame VNC as the human visual interface and Computer Use as the programmatic API for AI agents.

### 2.2 E2B Computer Use primitives

E2B's Computer Use docs describe AI agents that operate virtual Linux desktops through screenshots, clicks, typing, scrolling, and VNC streaming. Their documented example creates an Ubuntu 22.04 XFCE desktop sandbox, starts VNC streaming, takes screenshots, sends them to an LLM, executes returned actions, and repeats until completion.

The E2B Desktop SDK has a compact direct-action API: `leftClick`, `rightClick`, `doubleClick`, `middleClick`, `moveMouse`, `drag`, `write`, `press`, `scroll`, `screenshot`, and `commands.run`. The open-source E2B Desktop repo also demonstrates application launch and authenticated streaming of the whole desktop or a window. E2B is a useful reference for an agent loop and a simple SDK shape, while Daytona is a better reference for the full primitive surface because Daytona adds process observability, richer screenshots, recordings, display/window metadata, and process lifecycle APIs.

### 2.3 Modal primitives relevant to this project

Modal is not a desktop automation platform by default. It is a serverless cloud container platform with Sandboxes, Functions, Images, Secrets, Volumes, Tunnels, web endpoints, queues, and snapshots.

The Modal pieces this repository should use are:

- **`modal.Sandbox`**: runtime-created secure containers that can execute untrusted or agent code.
- **`Sandbox.create`**: create a sandbox from a custom image and command, with resources, timeout, volumes, environment, tags, readiness probes, and tunnels.
- **`Sandbox.exec`**: run commands inside an existing sandbox and access stdout, stderr, and stdin.
- **Sandbox Connect Tokens**: authenticated HTTP/WebSocket requests to a server running inside the sandbox on port `8080`. Modal forwards verified user metadata to the sandbox and requires compact JSON-serializable metadata.
- **Tunnels**: expose live TCP ports for noVNC/manual desktop viewing.
- **`modal.Image`**: define the desktop image as code with apt packages, pip packages, local package source, environment variables, and entrypoint.
- **Filesystem API**: stream files into and out of sandboxes, including recordings and test artifacts.
- **Snapshots**: reduce startup time or preserve a prepared desktop state.
- **Volumes**: optionally persist recordings, downloads, datasets, browser profiles, or shared test assets. Volume visibility requires explicit sync/commit/reload semantics.
- **Tags and names**: identify and list running computer-use sandboxes.
- **Warm-pool pattern**: optional production helper to pre-create ready desktops when cold start latency matters.

### 2.4 Modal examples already close to computer use

Modal's official examples include a computer-use demo that runs Anthropic's computer-use image inside a Modal Sandbox, exposing a Streamlit UI on port `8501` and noVNC on port `6080`. That validates the substrate: Modal can run a GUI desktop/container stack and expose the interactive desktop stream. However, that example runs an existing demo image; it does not provide a reusable Daytona-like API. `modal-computer-use` should fill that gap.

---


### 2.5 Reference implementation: `yasyf/anthropic-computer-use-modal`

The repository `yasyf/anthropic-computer-use-modal` is the closest known public Modal-native computer-use implementation. It is useful as a reference, but it is not the same product shape as `modal-computer-use`.

Observed properties:

- It deploys a Modal app and exposes a `ComputerUseServer` that owns an Anthropic message loop.
- It creates or reuses Modal Sandboxes keyed by `request_id`.
- It exposes noVNC/debug URLs, including port `6080` for the desktop view.
- It uses an Anthropic quickstart computer-use image as the sandbox base image.
- It translates Anthropic tool calls into Modal-side operations.
- It includes Bash, Edit, and Computer tool reimplementations.
- It uses per-sandbox filesystem persistence for files/artifacts.
- It includes performance workarounds: browser prewarm, optional T4 GPU, screenshot post-processing outside the sandbox, conservative fuzzy matching for edit operations, and faster package installation conventions.

Source-level notes that should influence the implementation:

- The reference repo's package metadata pins a 2024-era Modal SDK range (`modal>=0.64.211`) and publishes as `computer-use-modal` with an Anthropic dependency. The new repo should target the current Modal SDK surface and isolate provider dependencies behind extras.
- Its app image and sandbox image are split. That split is useful: keep a lightweight manager/control image for deployed examples, and a heavier desktop sandbox image for X11/noVNC/browser/daemon.
- Its `SandboxManager` creates or reuses a sandbox from a `request_id`, sets resources (`cpu=8`, `memory=8 GiB`, `gpu="T4"`), exposes ports `8501` and `6080`, stores files through a per-request filesystem, and implements screenshots by capturing in the sandbox then resizing elsewhere. This strongly supports adding registry, artifacts, optional GPU, and outside-sandbox screenshot processing to this plan.
- Its `ComputerUseServer` owns the Anthropic beta message loop. That is useful as an example server but should not become the primary API.
- Its `ComputerTool` maps Anthropic actions to `xdotool`, including `middle_click`. The adapter should support the exact same action set, including coordinate-less current-cursor click behavior.
- Its blog explicitly identifies network round trips and serialization/deserialization at each tool call as a speed bottleneck. That makes a daemon API plus batch-action endpoint a higher-quality primitive design than repeated command execution.

What this validates:

1. **Modal has enough primitives for computer use.** Sandboxes, Images, tunnels, resource configuration, filesystem mounts, and Modal classes can host and coordinate a remote desktop agent stack.
2. **A run-scoped sandbox model is practical.** A stable provider request identifier can be mapped to the SDK's canonical `run_id` to find or recreate the client-side handle to a still-running sandbox.
3. **Debug VNC is valuable.** Users need to watch and manually inspect a run, especially during early agent development.
4. **Performance requires deliberate choices.** Browser initialization, screenshot processing, and remote call count have an outsized impact on UX.

What should not be copied as the core architecture:

1. **Do not make Anthropic the native abstraction.** The project should expose Daytona-style primitives first, then optional Anthropic/OpenAI adapters.
2. **Do not make the model loop the core API.** A `messages_create`-style method is appropriate for an example package or adapter demo, not for the primitive library.
3. **Do not use repeated Modal command execution as the primary GUI action transport.** It works for a demo or compatibility layer, but the lower-latency and more extensible v1 should route mouse, keyboard, screenshot, recording, and process status through the in-sandbox daemon.
4. **Do not rely on Modal `NetworkFileSystem` for the new design.** That repo used it effectively, but current Modal docs mark `NetworkFileSystem` as deprecated. The updated plan should use the Sandbox filesystem APIs for ephemeral files and `modal.Volume`/Volumes v2 for persistent artifacts.
5. **Do not use the Anthropic quickstart image as the default product image.** It can remain useful for compatibility examples, but the package should own its image recipe so it can provide stable APIs, process supervision, versioning, and security defaults.

Implications for this spec:

- Add a Modal orchestration layer inspired by the reference `SandboxManager`, but keep it model-agnostic.
- Add a first-class `run_id` state model, with deprecated `request_id` normalization only at compatibility boundaries.
- Add optional VNC debug URLs as a helper, not as the control plane.
- Prefer `modal.Volume` and Sandbox filesystem APIs over `NetworkFileSystem`.
- Add explicit Anthropic action compatibility in the adapter section.
- Add performance knobs for browser prewarm, optional GPU, screenshot post-processing location, and warm pools.
- Add tests that replay Anthropic-style action JSON against the generic executor.
- Add batch action tests for ordering, stop-on-error, continue-on-error, and final screenshot behavior.
- Add artifact path-safety tests for traversal, symlinks, encoded paths, and large streaming downloads.

---

## 3. Design goals and non-goals

### 3.1 Goals

1. **Expose high-quality computer-use primitives on Modal.** Users should not manually wire Xvfb, VNC, screenshots, ffmpeg, or xdotool.
2. **Mirror Daytona's practical primitive surface.** The API should feel familiar to users who have seen Daytona Computer Use.
3. **Stay agent-model agnostic.** The core should work with OpenAI Computer Use, Anthropic, custom vision models, browser agents, QA agents, and deterministic scripts.
4. **Preserve Modal-native advantages.** Use Modal Sandboxes, Images, Connect Tokens, Tunnels, Filesystem API, Snapshots, Volumes, tags, and readiness probes instead of recreating infrastructure.
5. **Be safe by default.** Expose the control daemon through Connect Tokens, not a public unauthenticated URL. Make noVNC optional, password-protected, and clearly labeled as sensitive.
6. **Be deterministic and observable.** Validate inputs, serialize GUI events, return structured results, expose process logs/errors, and make screenshots/recordings easy to inspect.
7. **Support both direct SDK mode and deployed manager mode.** Direct mode should be the default quickstart; manager mode should serve production users who want run-scoped state, cross-app invocation, warm pools, and centralized cleanup.
8. **Support provider compatibility without provider lock-in.** Anthropic/OpenAI actions should replay through a provider-neutral action executor, including coordinate-less clicks, enhanced low-level mouse/key variants, and explicit coordinate transforms.
9. **Prefer current Modal storage primitives.** Use Sandbox filesystem APIs and `modal.Volume` for artifacts instead of new `NetworkFileSystem` usage.
10. **Make performance visible.** Return timing metadata, expose action batching, and document browser prewarm/GPU/screenshot-processing tradeoffs instead of hiding latency.
11. **Make replay/debugging excellent.** Store redaction-aware traces, artifact manifests, hashes, coordinate metadata, and enough information to replay or validate action sequences without model calls.
12. **Ship as a small library with strong tests.** The first version should be installable, documented, and production-quality without trying to solve every agent problem.

### 3.2 Non-goals

1. **No full autonomous agent framework in core.** The package should not own prompts, reasoning loops, provider selection, or task policies.
2. **No Windows/macOS support in v1.** Modal Sandboxes here are Linux containers. Linux desktop support is the implementation target.
3. **No DOM automation as a core primitive.** Browser DOM control can be implemented by users or optional examples, but the core primitive is visual/desktop control.
4. **No hidden credential management.** Users should explicitly decide which credentials and Secrets are mounted into a sandbox.
5. **No generic remote desktop product.** noVNC is for observability and manual takeover, not for building a general remote-desktop service.
6. **No overbroad network permissions.** Network egress/inbound behavior should be configurable and documented; default posture should be least privilege compatible with the user's app.
7. **No provider-first server API in core.** `messages_create`-style Anthropic/OpenAI server examples are allowed, but the package API must remain primitive-first.
8. **No new `NetworkFileSystem` dependency.** Legacy references can explain why older Modal examples use NFS, but v1 defaults should use Volumes and Sandbox filesystem APIs.
9. **No forced GPU/browser bundle.** Browser and GPU variants should be opt-in image/config choices, not required for simple desktop automation.
10. **No mandatory deployed service.** The manager is optional; local direct SDK creation and local daemon testing must remain supported.
11. **No silent coordinate scaling.** If screenshots are downscaled before model use, coordinate transforms must be explicit and traceable.
12. **No implicit public debug channels.** `debug.urls()` must never create a tunnel or expose noVNC by surprise.

---

## 4. Best-practice implementation plan

### 4.1 Core architectural choice: in-sandbox daemon over repeated `Sandbox.exec`

The most important design decision is to run a daemon inside the sandbox and call it through Modal Connect Tokens.

**Why this is the best practice:**

- **Latency:** a screenshot-click-screenshot loop performs many small operations. Calling `Sandbox.exec` for every click or screenshot adds overhead and process startup variance. A daemon keeps tools and state warm.
- **State ownership:** the daemon can supervise Xvfb, XFCE, VNC, noVNC, recordings, input locks, and display metadata in one place.
- **Typed API:** a daemon can expose a stable OpenAPI-compatible HTTP surface independent of Modal SDK changes.
- **Security:** Modal Connect Tokens provide authenticated HTTP/WebSocket access to port `8080`. The SDK can create short-lived tokens and avoid public raw control endpoints.
- **Observability:** call IDs, structured logs, per-process logs, last errors, and action timings are easier to centralize inside a daemon.
- **Agent portability:** OpenAI/Anthropic/custom adapters can map actions to the daemon without assuming Modal internals.

`Sandbox.exec` should remain available as a bootstrap/debug transport and for optional terminal commands. It should not be the normal transport for mouse, keyboard, screenshots, or recording.

### 4.2 Desktop stack

Use a standard X11 stack first:

- `Xvfb :99 -screen 0 {WIDTH}x{HEIGHT}x24 -nolisten tcp`
- XFCE desktop or a lighter window manager:
  - v1 default: XFCE because it is familiar and matches Daytona/E2B patterns.
  - optional minimal mode: Openbox/Fluxbox for lower memory and faster startup.
- `x11vnc` bound to localhost, pointed at `DISPLAY=:99`.
- noVNC/websockify on port `6080`, forwarding to x11vnc.
- `computer-use-daemon` on port `8080`.

Recommended default resolution: `1440x900`, with `1280x720` and `1600x900` supported. OpenAI's computer-use guidance notes strong performance with 1440x900 and 1600x900 when downscaling becomes necessary; the SDK should allow explicit resolution to match the model/harness coordinate system.

### 4.3 Transport model

There are two channels:

1. **Control channel:** HTTP to `computer-use-daemon` on port `8080`, accessed with a Modal Sandbox Connect Token. This channel handles all programmatic primitives.
2. **View channel:** optional noVNC tunnel on port `6080`, exposed through Modal `encrypted_ports`. This channel is for manual view/takeover and debugging.

Do not expose the daemon via `encrypted_ports` by default. Use Connect Tokens for the daemon because they add authentication and verified user metadata to requests.

### 4.4 API shape

The Python SDK should be familiar and explicit:

```python
from modal_computer_use import ComputerSandbox, ComputerConfig

computer = ComputerSandbox.create(
    name="qa-run-001",
    config=ComputerConfig(
        desktop={"resolution": (1440, 900)},
        runtime={"timeout_seconds": 3600},
    ),
    expose_vnc="view_only",
)

computer.wait_until_ready()
computer.mouse.click(320, 240)
computer.keyboard.type("hello")
shot = computer.screenshots.full(format="png")

print({"width": shot.width, "height": shot.height, "remote_view_enabled": computer.debug.urls().vnc is not None})

computer.terminate()
```

Internally:

- `ComputerSandbox.create()` calls `modal.Sandbox.create(...)` with the correct image, command, env vars, ports, tags, timeout, and readiness probe.
- `wait_until_ready()` waits for Modal readiness, daemon `/healthz`, and desktop `/readyz`.
- SDK namespaces (`mouse`, `keyboard`, `screenshots`, `recordings`, `display`, `processes`, `actions`, and `artifacts`) are thin wrappers around HTTP routes.
- The SDK stores the sandbox ID, app name, name/tags, daemon base URL, connect token, VNC tunnel URL, and Modal `Sandbox` object.

### 4.5 Package boundaries

Core package:

- Modal sandbox creation/attachment.
- Daemon HTTP client.
- Daytona-like primitives.
- Image builder helpers.
- Basic examples.

Optional extras:

- `modal-computer-use[openai]`: action adapter for OpenAI Computer Use actions.
- `modal-computer-use[anthropic]`: action adapter for Anthropic-style actions.
- `modal-computer-use[dev]`: tests, local daemon runner, lint, type checking.

Do not put model credentials, prompt templates, or task policies into the core package.



### 4.6 Refinements from the Modal/Anthropic reference repo

The reference repo suggests a useful split between **orchestration** and **primitive execution**:

```text
User app / model loop
  -> modal_computer_use SDK
    -> optional Modal orchestration manager
      -> Modal Sandbox
        -> computer-use-daemon on port 8080
          -> Xvfb / window_manager / x11vnc / noVNC / ffmpeg / xdotool / wmctrl
```

The orchestration manager is responsible for creating, finding, tagging, reusing, and terminating sandboxes. The daemon is responsible for desktop primitives. This avoids the two extremes:

- Too local: only a local SDK that cannot be easily reused by deployed Modal apps.
- Too model-specific: a deployed Anthropic server that hides the primitives behind one provider loop.

Adopt these patterns:

| Pattern from the reference repo | Updated implementation in `modal-computer-use` |
|---|---|
| Run-scoped sandbox lookup | Use canonical `run_id` tags and optional sandbox names; normalize legacy `request_id` at adapter/server boundaries only. |
| VNC debug tunnel | Keep optional noVNC tunnel on `6080`; never use it as the daemon auth model. |
| Per-sandbox artifacts | Use Sandbox filesystem APIs for ephemeral artifacts; use `modal.Volume` for persistence. |
| Browser prewarm | Add image build and/or startup hook options for browser profile initialization. |
| Optional GPU | Add `gpu` config for browser-heavy workloads, default `None`. |
| Screenshot post-processing outside sandbox | Support both daemon-side and client/control-plane post-processing. |
| Fuzzy edit matching | Keep out of core computer primitives; add only to optional text-editor adapter. |
| Scale-to-zero/resumable coordination | Support attach by `sandbox_id`, `name`, and tags; document lifecycle semantics. |

Avoid these as defaults:

- A hard Anthropic dependency in core.
- `messages_create` as the primary API.
- Streamlit or demo UI as part of the primitive package.
- Modal `NetworkFileSystem` as the storage primitive.
- Public unauthenticated control endpoints.

This refinement means the repo should include two example tracks:

1. **Primitive quickstart:** create a desktop, click/type/screenshot/record.
2. **Provider compatibility examples:** Anthropic-style and OpenAI-style loops that translate model actions into primitives.

### 4.7 Batch actions as a first-class primitive

Add batch action execution to v1 rather than treating it as adapter-only behavior. Modern computer-use loops often return multiple actions in one turn, and the reference repo's latency analysis shows that many remote round trips can become the bottleneck. The daemon should expose a single route that validates and executes an ordered action list under one input lock:

```python
result = computer.actions.run(
    [
        {"type": "move", "x": 300, "y": 240},
        {"type": "click", "button": "left"},
        {"type": "type", "text": "hello"},
    ],
    screenshot_after=True,
)
```

Batch execution rules:

- Use the same validation and key/coordinate normalization as single-action routes.
- Execute under one serialized input lock to preserve event order.
- Return per-action status, duration, errors, and optional final screenshot.
- Stop at first error by default; allow `continue_on_error=True` only when the caller explicitly asks for it.
- Keep batch semantics provider-neutral; OpenAI and Anthropic adapters should convert provider-specific actions into this schema.


### 4.8 Local development mode

A daemon-first architecture needs a local test path. v5 requires:

```python
from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(
    base_url="http://127.0.0.1:8080",
    token="dev-token",
)
computer.mouse.click(100, 100)
```

Local mode requirements:

- `DaemonClient(base_url, token=None)` uses the same namespaces as a Modal-backed `ComputerSandbox`.
- `ComputerSandbox.local(...)` returns a sandbox-like handle without Modal creation methods.
- `scripts/run_local_daemon.sh` or Docker Compose starts Xvfb, the daemon, and optional noVNC.
- Local auth uses `COMPUTER_USE_LOCAL_TOKEN` and refuses non-local bind by default.
- CI local integration tests should cover most daemon primitives without Modal credentials.

### 4.9 Best-practice v1 scope after the refinement

The refined v1 should include exactly these primitive categories:

1. Lifecycle/process supervision.
2. Mouse, keyboard, clipboard, screenshots, recordings, display/window metadata.
3. Browser/app launch helpers.
4. Registry/attach/reuse by sandbox ID, name, and canonical `run_id`.
5. Artifact storage/download helpers and explicit persistence sync.
6. Batch action executor.
7. Trace/replay schema and validator.
8. Optional command/session helper.
9. Optional OpenAI, Anthropic, and generic adapters.
10. Security hooks, budgets, and noVNC view-only/control modes.

It should **not** include a mandatory chat UI, provider-owned message loop, persistent browser profile service, DOM automation layer, or a full policy engine. Those can be examples or future modules.

### 4.10 Public naming and semantic rules adopted in v5

The public API should be boring, guessable, and semantically precise. Apply these naming rules across code, docs, routes, examples, and tests:

| Concept | Canonical name | Notes |
|---|---|---|
| Repo / distribution | `modal-computer-use` | Matches the domain term. |
| Import package | `modal_computer_use` | Standard Python package spelling. |
| Daemon binary | `computer-use-daemon` | Optional console script; examples prefer `python -m modal_computer_use.daemon`. |
| Daemon module | `modal_computer_use.daemon` | No acronym-based top-level daemon module. |
| OS user | `desktop` | Describes the runtime user. |
| OS home | `/home/desktop` | Keeps recordings/artifacts under the desktop user's home. |
| Stable sandbox/run identity | `run_id` | One sandbox-lifetime identifier. |
| One HTTP/API invocation | `call_id` | Distinct from `run_id`. |
| Legacy provider request identifier | `request_id` | May be accepted only at adapter/server compatibility boundaries, then normalized to `run_id`. |
| Modal tags | `computer-use.*` | Example: `computer-use.run_id`, `computer-use.version`. |
| HTTP headers | `Computer-Use-Run-Id`, `Computer-Use-Call-Id`, `Idempotency-Key` | No `X-` prefix. Use standard `Idempotency-Key` for retries. |
| Window-manager process | `window_manager` | Avoid overloading `desktop`. |
| Config field | `desktop.window_manager` | Selects `xfce` or `openbox`. |
| Browser absence | `browser=None` | Avoid sentinel strings such as `"none"`. |
| Network config | `network: NetworkConfig` | Groups `block_all` and `cidr_allowlist`. |
| Resource profile | `browser-gpu` | Kebab-case string literal for config values. |
| Manager class | `ComputerSandboxManager` | Avoid generic public `SandboxManager`. |
| Command namespace | `computer.commands.run(...)` | Avoid public `computer.exec(...)` in quickstarts. |
| Generic adapter | `ActionExecutor` | Keep `GenericAdapter` only as deprecated alias if needed. |

SDK namespace rules:

- Use plural namespaces for collections: `processes`, `recordings`, `screenshots`, `actions`, `artifacts`, `windows`, `apps`, and `commands`.
- Use singular namespaces for one device/concept: `mouse`, `keyboard`, `clipboard`, `display`, `browser`, `input`, `session`, and `debug`.
- Collapse safe filesystem helpers into `artifacts`; do not expose a parallel `files` namespace in v1.

Screenshot API rules:

```python
computer.screenshots.full(format="png", quality=90, scale=1.0)
computer.screenshots.region(0, 0, 800, 600, format="webp", quality=80)
computer.screenshots.zoom(region=Region(x=500, y=300, width=400, height=300), scale=2.0)
computer.screenshots.zoom_around(center=(700, 450), width=400, height=300, scale=2.0)
```

Compression, scaling, cursor display, coordinate metadata, storage mode, and processing location are parameters. Daytona-style names such as `take_full_screen`, `take_compressed`, and `take_zoomed` may exist only as deprecated aliases.

Class naming rules:

- `ComputerSandbox` is the SDK handle.
- `ComputerSandboxManager` is the optional deployed orchestration class.
- `SandboxRegistry` is the attach/list/create helper.
- `OpenAIAdapter`, `AnthropicAdapter`, and `ActionExecutor` are adapter classes.
- `WarmSandboxPool` is the optional pooling helper/example.
- `AnthropicMessageServer` is the optional server example that implements an Anthropic-like messages entrypoint.
- `DebugUrls` uses `Url` capitalization for Pydantic/API model consistency.

Data model refinements:

- Use `DisplayGeometry` for display rectangles to avoid colliding with the `computer.display` namespace.
- Use `X11Window` for window metadata to avoid ambiguity with browser windows.
- Use `CoordinateSpace` for model/desktop/image coordinate mapping.
- Use `Screenshot.bytes` in SDK and `data_base64` in HTTP. Keep `.data` only as a compatibility alias if needed.


## 5. Repository layout

```text
modal-computer-use/
  README.md
  pyproject.toml
  LICENSE
  CHANGELOG.md
  docs/
    architecture.md
    api.md
    security.md
    modal-deployment.md
    local-development.md
    openai-adapter.md
    anthropic-adapter.md
    trace-replay.md
    artifacts.md
    comparison.md
    reference-implementations.md
    performance.md
    troubleshooting.md
  src/modal_computer_use/
    __init__.py
    config.py
    models.py
    errors.py
    sandbox.py                # ComputerSandbox handle and lifecycle.
    image.py                  # default_image(), browser_image(), image helpers.
    client.py                 # HTTP transport facade.
    local.py                  # DaemonClient and ComputerSandbox.local helpers.
    manager.py                # Optional deployed Modal ComputerSandboxManager.
    registry.py               # SandboxRegistry for attach/list/create flows.
    artifacts.py              # Artifact path, download, manifest, and Volume helpers.
    state.py                  # run_id, sandbox tags, config hash, attach metadata.
    coordinates.py            # CoordinateSpace and transform helpers.
    tracing.py                # Trace models, writer, validator, replay helpers.
    performance.py            # Screenshot processing and browser prewarm helpers.
    actions.py                # Provider-neutral action schema and batch executor client.
    policy.py                 # ActionDecision and hook models.
    pool.py                   # Optional WarmSandboxPool helper/example.
    _version.py
    namespaces/
      __init__.py
      lifecycle.py
      processes.py
      mouse.py
      keyboard.py
      clipboard.py
      screenshots.py
      recordings.py
      display.py
      windows.py
      actions.py
      artifacts.py
      browser.py
      apps.py
      input.py
      commands.py
      debug.py
      session.py
    transports/
      __init__.py
      http.py
      local.py
      exec_fallback.py
    adapters/
      __init__.py
      openai.py               # OpenAIAdapter.
      anthropic/
        __init__.py
        computer.py           # AnthropicAdapter.
        versions.py           # Tool/action version registry.
        bash.py               # Optional compatibility module.
        editor.py             # Optional fuzzy text-edit helper; not core.
      generic.py              # ActionExecutor.
    daemon/
      __init__.py
      __main__.py             # python -m modal_computer_use.daemon
      app.py
      settings.py
      errors.py
      schemas.py
      auth.py
      logging.py
      metrics.py
      budgets.py
      supervisor.py
      traces.py
      artifacts.py
      desktop/
        __init__.py
        x11.py
        mouse.py
        keyboard.py
        clipboard.py
        screenshots.py
        recordings.py
        display.py
        windows.py
        apps.py
        browser.py
        processes.py
      routes/
        __init__.py
        health.py
        lifecycle.py
        processes.py
        mouse.py
        keyboard.py
        clipboard.py
        screenshots.py
        recordings.py
        display.py
        windows.py
        actions.py
        artifacts.py
        browser.py
        apps.py
        input.py
      static/
        recording_dashboard/
    modal_app.py
  examples/
    00_create_desktop.py
    01_mouse_keyboard_screenshots.py
    02_recordings.py
    03_openai_computer_loop.py
    04_warm_pool.py
    05_visual_regression_qa.py
    06_local_daemon.py
    07_trace_replay.py
    08_policy_hooks.py
    anthropic_message_server.py
    browser_prewarm.py
    gpu_browser.py
    volume_artifacts.py
    attach_existing_sandbox.py
    domain_allowlist_policy.py
    deterministic_desktop_app.py
  tests/
    unit/
    integration_local/
    integration_modal/
    fixtures/
      openai_actions.json
      anthropic_20241022.json
      anthropic_20250124.json
      anthropic_20251124.json
      deterministic_desktop/
    test_openai_adapter.py
    test_anthropic_adapter.py
    test_artifacts.py
    test_trace_replay.py
    test_manager_state.py
    test_action_batch.py
    test_path_safety.py
    test_coordinate_space.py
    test_local_daemon.py
  benchmarks/
    bench_daemon_vs_exec.py
    bench_screenshots.py
    bench_batch_actions.py
  scripts/
    build_image.py
    run_local_daemon.sh
    docker-compose.local.yml
  .github/workflows/
    ci.yml
```

Naming constraints for the repo layout:

- Use the package namespace `modal_computer_use.daemon` for daemon code rather than a standalone acronym-based daemon module.
- Use plural namespace modules when the namespace manages a collection: `processes`, `recordings`, `screenshots`, `artifacts`, `actions`, `windows`, `apps`, and `commands`.
- Keep provider names out of core class names except in adapters. For example, use `ComputerSandboxManager` for orchestration and `AnthropicAdapter` for provider translation.
- Keep `files` out of the public SDK. The safe file surface is `artifacts`; raw Modal filesystem access remains available through Modal itself or the narrow debug/escape-hatch layer.

### 5.1 Single package with clear daemon boundary

The daemon should live inside the same Python package as `modal_computer_use.daemon`. For v1, avoid publishing a second package unless operational experience proves that it is necessary. The Modal image can copy the repository source or install the same wheel with only daemon/runtime dependencies.

Recommended:

- Root package: `modal-computer-use`, installed by users locally.
- Daemon package path: `modal_computer_use.daemon`, executed with `python -m modal_computer_use.daemon`.
- Daemon binary/console script: `computer-use-daemon` as a convenience alias.
- Optional extras: `modal-computer-use[openai]`, `modal-computer-use[anthropic]`, `modal-computer-use[server-examples]`, `modal-computer-use[otel]`, and `modal-computer-use[dev]`.
- Optional deployed Modal manager: implemented as an example or extra, not as the only way to use the SDK.

This avoids publishing two packages too early while keeping code boundaries clean and prevents the reference repo's provider-specific server shape from becoming the default API.

## 6. Modal image specification

### 6.1 Image builder helper

Expose a helper so users can reuse or extend the default image:

```python
from modal_computer_use import default_image

image = default_image(
    window_manager="xfce",
    browser="firefox",          # None | "firefox" | "chromium"
    browser_prewarm=True,
    extra_apt=["libnss3"],
    extra_pip=["playwright"],
    extra_run_commands=[],
)
```

Under the hood:

```python
import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "xvfb",
        "xfce4",
        "xfce4-terminal",
        "x11vnc",
        "novnc",
        "websockify",
        "xdotool",
        "wmctrl",
        "maim",
        "scrot",  # compatibility fallback; the reference repo used scrot for captures
        "ffmpeg",
        "xclip",
        "xsel",
        "dbus-x11",
        "procps",
        "psmisc",
        "curl",
        "ca-certificates",
        "fonts-dejavu",
        "fonts-noto-color-emoji",
        "fonts-liberation",
        "libgtk-3-0",
        "libdbus-glib-1-2",
    )
    .pip_install(
        "fastapi",
        "uvicorn[standard]",
        "pydantic>=2",
        "pillow",
        "python-multipart",
    )
    # Prefer add_local_python_source where supported by the pinned Modal SDK.
    # Use add_local_dir only as compatibility fallback for older SDKs/static assets.
    .add_local_python_source("modal_computer_use", copy=True)
)
```

Exact Modal image code should follow Modal's current `modal.Image` APIs (`debian_slim`, `apt_install`, `pip_install`, `add_local_python_source`, `add_local_dir`, `env`, `cmd`, etc.).

#### 6.1.1 Image ownership and compatibility images

The default image should be owned by this repository. Do not make `ghcr.io/anthropics/anthropic-quickstarts:computer-use-demo-latest` the production default. That image is useful as a compatibility reference and possibly an example, but using it as the default would couple this package to Anthropic demo internals and make daemon/version guarantees harder.

Recommended image strategy:

1. `default_image()` builds the official project image from Debian/Ubuntu primitives.
2. `browser_image()` extends `default_image()` with Firefox/Chromium and a prewarmed browser profile.
3. `anthropic_quickstart_compat_image()` is optional and documented as an example-only escape hatch.
4. The daemon version is embedded in the image through `COMPUTER_USE_DAEMON_VERSION` or package metadata and returned by `/v1/version`.

#### 6.1.2 Browser prewarm and optional GPU

Browser-heavy workloads are the most likely place to need non-default resources. Add config knobs, but do not force them:

```python
ComputerConfig(
    resources=ResourceConfig(profile="browser", gpu=None),
    browser=BrowserConfig(kind="firefox", prewarm=True),
    actions=ActionConfig(screenshot_processing_location="auto"),
)
```

Implementation notes:

- During image build, run the browser briefly in headless mode to initialize profile/cache directories where practical.
- At sandbox startup, optionally launch and close the browser once after Xvfb is ready if build-time prewarm is insufficient.
- GPU should be opt-in because it increases cost and is not needed for simple desktop or visual regression workflows.
- Screenshot post-processing should default to `auto`: small screenshots can be compressed in the daemon; large resize/format conversions can happen in the client/control-plane process to keep the desktop responsive.

### 6.2 Entrypoint

The sandbox command should start the daemon entrypoint:

```bash
python -m modal_computer_use.daemon
```

The package may also expose a console script named `computer-use-daemon`, but examples should prefer the `python -m` form because it is stable under editable installs and copied source trees. The daemon, not a shell script, should supervise child processes. This makes lifecycle, restart, logs, health, readiness, capabilities, and errors observable through the same API.


### 6.3 Environment variables

v5 changes the v4 unprefixed environment variables to a **prefixed public convention**. Unprefixed aliases may remain inside the project-owned image for one minor series, but public docs should prefer `COMPUTER_USE_*` to avoid collisions with user-provided images.

| Variable | Default | Purpose |
|---|---:|---|
| `COMPUTER_USE_DISPLAY` | `:99` | X11 display. Legacy alias: `DESKTOP_DISPLAY`. |
| `COMPUTER_USE_DESKTOP_WIDTH` | `1440` | Desktop width. Legacy alias: `DESKTOP_WIDTH`. |
| `COMPUTER_USE_DESKTOP_HEIGHT` | `900` | Desktop height. Legacy alias: `DESKTOP_HEIGHT`. |
| `COMPUTER_USE_DESKTOP_DEPTH` | `24` | Xvfb color depth. Legacy alias: `DESKTOP_DEPTH`. |
| `COMPUTER_USE_DESKTOP_DPI` | `96` | Desktop DPI. Legacy alias: `DESKTOP_DPI`. |
| `COMPUTER_USE_WINDOW_MANAGER` | `xfce` | Window-manager choice: `xfce` or `openbox`. |
| `COMPUTER_USE_DAEMON_HOST` | `0.0.0.0` | Daemon bind host. |
| `COMPUTER_USE_DAEMON_PORT` | `8080` | Daemon port for Modal Connect Tokens. |
| `COMPUTER_USE_VNC_PORT` | `5900` | x11vnc internal port. |
| `COMPUTER_USE_NOVNC_PORT` | `6080` | noVNC/websockify port. |
| `COMPUTER_USE_RECORDINGS_DIR` | `/home/desktop/recordings` | Recording output directory. |
| `COMPUTER_USE_ARTIFACTS_DIR` | `/home/desktop/artifacts` | Screenshots, traces, downloads, and non-recording artifacts. |
| `COMPUTER_USE_LOG_DIR` | `/var/log/modal-computer-use` | Managed process logs. |
| `COMPUTER_USE_BROWSER` | empty | Optional browser profile: `firefox`, `chromium`, or unset. |
| `COMPUTER_USE_BROWSER_PREWARM` | `true` | Whether to initialize browser profile at build/startup time. |
| `COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION` | `auto` | Screenshot processing location: `daemon`, `client`, or `auto`. |
| `COMPUTER_USE_POST_ACTION_DELAY_MS` | `100` | Default delay before post-action screenshots in adapters/batches. |
| `COMPUTER_USE_TRACE_ACTIONS` | `false` | Whether to append redaction-aware action traces to artifacts. |
| `COMPUTER_USE_REQUIRE_CONNECT_USER` | `false` | Optional enforcement of Modal verified-user header. |
| `COMPUTER_USE_LOCAL_TOKEN` | empty | Local daemon bearer token for development. Disabled by default. |
| `COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC` | `20` | Basic action rate limit. |
| `COMPUTER_USE_SCREENSHOT_MAX_PIXELS` | `10000000` | Guardrail for generated screenshot size. |
| `COMPUTER_USE_VNC_PASSWORD` | generated | Password for x11vnc/noVNC when exposed. |
| `COMPUTER_USE_VNC_MODE` | `off` | `off`, `view_only`, or `control`. |
| `COMPUTER_USE_RUN_ID` | empty | Stable run identifier for logs, traces, and tags. |
| `COMPUTER_USE_MAX_ACTIONS` | empty | Optional action budget. |
| `COMPUTER_USE_MAX_SCREENSHOTS` | empty | Optional screenshot budget. |
| `COMPUTER_USE_MAX_ARTIFACT_BYTES` | empty | Optional artifact byte budget. |
| `COMPUTER_USE_MAX_RECORDING_SECONDS` | empty | Optional recording budget. |

### 6.4 Desktop user

Default to a non-root user where practical:

- User: `desktop`
- Home: `/home/desktop`
- Recordings: `/home/desktop/recordings`
- Artifacts: `/home/desktop/artifacts`
- Downloads: `/home/desktop/Downloads`

Some Modal image build or process-supervision details may be simpler as root. The spec should aim for non-root runtime for desktop processes, with a documented fallback if required by Modal image constraints.

---

## 7. Sandbox creation specification

### 7.1 Main class

```python
class ComputerSandbox:
    @classmethod
    def create(
        cls,
        *,
        name: str | None = None,
        app_name: str = "modal-computer-use",
        config: ComputerConfig | None = None,
        image: modal.Image | None = None,
        expose_vnc: bool | Literal["off", "view_only", "control"] = "off",
        secrets: list[modal.Secret] | None = None,
        volumes: dict[str, modal.Volume] | None = None,
        tags: dict[str, str] | None = None,
        auto_detach_on_exit: bool = False,
    ) -> "ComputerSandbox": ...

    @classmethod
    def attach(cls, sandbox_id: str, *, app_name: str = "modal-computer-use") -> "ComputerSandbox": ...

    @classmethod
    def from_id(cls, sandbox_id: str, *, app_name: str = "modal-computer-use") -> "ComputerSandbox": ...  # alias

    @classmethod
    def from_name(cls, name: str, *, app_name: str = "modal-computer-use") -> "ComputerSandbox": ...

    @classmethod
    def from_run_id(cls, run_id: str, *, app_name: str = "modal-computer-use") -> "ComputerSandbox": ...

    @classmethod
    def attach_or_create(
        cls,
        *,
        run_id: str,
        config: ComputerConfig | None = None,
        app_name: str = "modal-computer-use",
        image: modal.Image | None = None,
        expose_vnc: bool | Literal["off", "view_only", "control"] = "off",
        replace: bool = False,
        reuse: Literal["by_run_id", "by_name", "never"] = "by_run_id",
    ) -> "ComputerSandbox": ...

    @classmethod
    def local(cls, *, base_url: str = "http://127.0.0.1:8080", token: str | None = None) -> "ComputerSandbox": ...

    def wait_until_ready(self, timeout: float = 120.0) -> None: ...
    def terminate(self) -> None: ...
    def detach(self) -> None: ...
    def snapshot_filesystem(self) -> modal.Image: ...
```

`from_run_id(create_if_missing=True)` is removed from the primary public surface. Creation requires config, image, VNC mode, secrets, volumes, and conflict behavior, so the explicit method is `attach_or_create(...)`.

### 7.2 Configuration model

v5 splits the flat v4 config into nested models. This keeps long-term API evolution manageable and separates lifecycle policy from desktop runtime settings.

```python
from typing import Literal
from pydantic import BaseModel, Field

class DesktopConfig(BaseModel):
    resolution: tuple[int, int] = (1440, 900)
    dpi: int = 96
    window_manager: Literal["xfce", "openbox"] = "xfce"
    display_depth: int = 24

class RuntimeConfig(BaseModel):
    timeout_seconds: int = 3600
    idle_timeout_seconds: int | None = None
    readiness_timeout_seconds: int = 120
    modal_region: str | None = None

class ResourceConfig(BaseModel):
    profile: Literal["standard", "browser", "browser-gpu", "custom"] = "standard"
    cpu: float | None = None
    memory_mib: int | None = None
    gpu: str | None = None

class NetworkConfig(BaseModel):
    block_all: bool = False
    cidr_allowlist: list[str] | None = None

class StorageConfig(BaseModel):
    recordings_dir: str = "/home/desktop/recordings"
    artifacts_dir: str = "/home/desktop/artifacts"
    persist_artifacts: bool = False
    trace_dir: str = "/home/desktop/artifacts/traces"

class BrowserConfig(BaseModel):
    kind: Literal["firefox", "chromium"] | None = None
    prewarm: bool = True
    profile_dir: str | None = None
    launch_args: list[str] = Field(default_factory=list)
    open_url_on_start: str | None = None

class ActionConfig(BaseModel):
    post_action_delay_ms: int = 100
    screenshot_after: bool = False
    trace_actions: bool = False
    screenshot_processing_location: Literal["daemon", "client", "auto"] = "auto"
    max_batch_actions: int = 50
    max_batch_duration_ms: int = 30_000
    default_action_timeout_ms: int = 5_000

class BudgetConfig(BaseModel):
    max_actions: int | None = None
    max_screenshots: int | None = None
    max_artifact_bytes: int | None = None
    max_recording_seconds: int | None = None
    max_idle_seconds: int | None = None

class ComputerConfig(BaseModel):
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    browser: BrowserConfig | None = None
    actions: ActionConfig = Field(default_factory=ActionConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    run_id: str | None = None
    vnc_password: str | None = None
```

Naming rules for config:

- `run_id` is the stable sandbox-lifetime identifier. Do not use `request_id` for this concept in new code.
- The SDK may accept `request_id` as a deprecated keyword alias in compatibility helpers, but it should warn and normalize immediately to `run_id`.
- `window_manager` selects `xfce` or `openbox`; avoid using `desktop` for the process name or config field.
- `browser=None` or `BrowserConfig(kind=None)` means no browser profile. Avoid sentinel strings such as `"none"`.
- Network controls are grouped under `network` to keep security-related settings together.
- Reuse policy is **not** a config field. It is an argument to `attach_or_create(...)`.
- `region` is renamed to `modal_region` if implemented. Otherwise remove it until exact Modal SDK support is verified.
- `memory_mb` is renamed `memory_mib` or mapped exactly to Modal’s supported memory parameter.

### 7.3 Modal call sketch

```python
app = modal.App.lookup(app_name, create_if_missing=True)
image = image or default_image(
    profile=config.resources.profile,
    browser=config.browser.kind if config.browser else None,
    window_manager=config.desktop.window_manager,
    browser_prewarm=config.browser.prewarm if config.browser else False,
)

vnc_mode = normalize_vnc_mode(expose_vnc)
ports = [6080] if vnc_mode != "off" else []
config_hash = compute_config_hash(config)

env = {
    "COMPUTER_USE_DESKTOP_WIDTH": str(config.desktop.resolution[0]),
    "COMPUTER_USE_DESKTOP_HEIGHT": str(config.desktop.resolution[1]),
    "COMPUTER_USE_DESKTOP_DPI": str(config.desktop.dpi),
    "COMPUTER_USE_RECORDINGS_DIR": config.storage.recordings_dir,
    "COMPUTER_USE_ARTIFACTS_DIR": config.storage.artifacts_dir,
    "COMPUTER_USE_WINDOW_MANAGER": config.desktop.window_manager,
    "COMPUTER_USE_BROWSER": (config.browser.kind if config.browser and config.browser.kind else ""),
    "COMPUTER_USE_BROWSER_PREWARM": str(config.browser.prewarm if config.browser else False).lower(),
    "COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION": config.actions.screenshot_processing_location,
    "COMPUTER_USE_POST_ACTION_DELAY_MS": str(config.actions.post_action_delay_ms),
    "COMPUTER_USE_TRACE_ACTIONS": str(config.actions.trace_actions).lower(),
    "COMPUTER_USE_VNC_MODE": vnc_mode,
    "COMPUTER_USE_MAX_ACTIONS": "" if config.budgets.max_actions is None else str(config.budgets.max_actions),
    "COMPUTER_USE_MAX_SCREENSHOTS": "" if config.budgets.max_screenshots is None else str(config.budgets.max_screenshots),
}

if config.run_id:
    env["COMPUTER_USE_RUN_ID"] = config.run_id

base_tags = {
    "computer-use": "true",
    "computer-use.version": __version__,
    "computer-use.window_manager": config.desktop.window_manager,
    "computer-use.config_hash": config_hash,
}
if config.run_id:
    base_tags["computer-use.run_id"] = config.run_id

sandbox = modal.Sandbox.create(
    "python", "-m", "modal_computer_use.daemon",
    app=app,
    image=image,
    cpu=config.resources.cpu,
    memory=config.resources.memory_mib,
    gpu=config.resources.gpu,
    encrypted_ports=ports,
    timeout=config.runtime.timeout_seconds,
    idle_timeout=config.runtime.idle_timeout_seconds,
    secrets=secrets or [],
    volumes=volumes or {},
    environment_variables=env,
    block_network=config.network.block_all,
    cidr_allowlist=config.network.cidr_allowlist,
    name=name,
    tags={**base_tags, **(tags or {})},
    readiness_probe=modal.web_server(port=8080, startup_timeout=config.runtime.readiness_timeout_seconds),
)
```

The exact parameter names should be verified against the Modal SDK version used in the repository. The pattern is the important part: create a sandbox from the computer-use image, run the daemon as the entrypoint, expose noVNC optionally, and use a readiness probe for port `8080` followed by SDK `/readyz` checks.

### 7.4 Connect token lifecycle

The SDK should generate a connect token when it needs to call the daemon:

```python
token_info = sandbox.create_connect_token(
    user_metadata={
        "sdk": "modal-computer-use",
        "sandbox_id": sandbox.object_id,
        "run_id": config.run_id or "",
        "owner": owner or "",
    }
)
```

Expected SDK behavior:

- Store token and URL in the HTTP transport.
- Refresh token on `401`, expiration, or explicit `refresh_token()`.
- Use `Authorization: Bearer <token>` rather than query parameters for API calls.
- Include `Computer-Use-Call-Id` and `Computer-Use-Run-Id` on each request.
- Use title-case hyphenated header names without the deprecated `X-` prefix.
- Use standard `Idempotency-Key` for whole-request idempotency.
- Keep `user_metadata` compact and JSON-serializable; do not put secrets, prompts, screenshots, typed text, provider tokens, or large context in connect-token metadata.

The daemon should optionally inspect Modal's verified user header for access control or diagnostics. Do not depend on that header during local development.

### 7.5 Optional Modal orchestration manager

The SDK should support direct local creation through `modal.Sandbox.create`, but production users often want a deployed Modal entrypoint that can be called from other Modal apps. Add an optional `ComputerSandboxManager` class that borrows the useful shape of the reference repo's `SandboxManager` without taking over the model loop.

Responsibilities:

- Create a sandbox for a `run_id` or attach to an existing one.
- Store/recover minimal state: `run_id`, `sandbox_id`, app name, sandbox name, created time, artifact paths, owner, and config hash.
- Return noVNC debug URLs if `expose_vnc` is not `off`.
- Return short-lived daemon connect-token info.
- Expose artifact helpers: list, read, download, delete, sync.
- Expose safe command execution only as an explicit debug/terminal helper.
- Terminate or detach sandboxes according to caller policy.
- List and cleanup stale/expired sandboxes.

Non-responsibilities:

- It must not call OpenAI, Anthropic, or any model provider.
- It must not own prompts, policies, or agent loops.
- It must not translate every primitive into `Sandbox.exec`; it should point clients at the daemon for primitives.

Recommended tags:

```python
{
    "computer-use": "true",
    "computer-use.run_id": run_id,
    "computer-use.version": __version__,
    "computer-use.window_manager": config.desktop.window_manager,
    "computer-use.owner": owner or "unknown",
    "computer-use.config_hash": config_hash,
    "computer-use.created_at": created_at_iso,
}
```

A deprecated `request_id` input may be accepted only at adapter/server boundaries that mirror existing Anthropic examples. It must be normalized immediately to `run_id`; internal state, tags, headers, and SDK docs should use `run_id`.

### 7.6 Resource profiles

The reference repo's GPU, browser prewarm, and outside-sandbox screenshot processing are useful, but they should be expressed as explicit resource profiles rather than hidden defaults.

Recommended profiles:

| Profile | CPU/memory | GPU | Browser | Screenshot processing | Use case |
|---|---:|---:|---|---|---|
| `standard` | modest default | none | optional | daemon or auto | simple GUI automation, tests, forms |
| `browser` | higher CPU/memory | none | Firefox or Chromium | auto | browser-heavy tasks where GPU is not needed |
| `browser-gpu` | higher CPU/memory | `T4`, `L4`, or caller supplied | Firefox or Chromium | client/auto | interactive browser tasks where page rendering is the bottleneck |
| `custom` | caller supplied | caller supplied | caller supplied | caller supplied | production users who know their workload |

Rules:

- The SDK must expose the resolved resources in `ComputerSandbox.status()` so users understand cost and latency tradeoffs.
- GPU should never be silently enabled by default.
- Browser prewarm should be enabled for browser images but overridable for deterministic tests.
- The quickstart should use `standard`; the browser examples can use `browser` or `browser-gpu`.

### 7.7 Attach-or-create semantics

Make attach/reuse a first-class API because the reference repo's run-scoped sandbox reuse is one of its best ideas.

```python
computer = ComputerSandbox.attach_or_create(
    run_id="support-ticket-123",
    config=ComputerConfig(
        resources={"profile": "browser"},
        browser={"kind": "firefox"},
    ),
    expose_vnc="view_only",
)
```

Behavior:

1. If `run_id` is supplied, normalize it into a stable sandbox name and tags.
2. Try `Sandbox.from_name(...)` first, then fall back to listing by `computer-use.run_id` tag.
3. Verify the daemon is healthy and ready before returning an existing sandbox.
4. Compare requested config hash with `computer-use.config_hash` tag.
5. If the sandbox exists but the config hash differs, return `ConfigConflictError` unless `replace=True` is set.
6. If no matching sandbox exists, create one and wait for readiness.
7. Return `created=False` for reattached sandboxes and `created=True` for new sandboxes.

This keeps resumability without adopting an Anthropic-specific `request_id` server as the core abstraction.


## 8. Daemon architecture

### 8.1 Runtime responsibilities

`computer-use-daemon` owns:

- Starting/stopping Xvfb, window manager, x11vnc, noVNC.
- Liveness, readiness, version, and capability checks.
- Process registry, status, restart, stdout logs, stderr logs, last errors.
- Mouse operations through X11 tools.
- Keyboard operations through X11 tools and clipboard fallback.
- Clipboard get/set/clear.
- Screenshots, region crops, zoom crops, compression, optional cursor overlay, and coordinate metadata.
- Recordings through ffmpeg.
- Display/window metadata.
- Browser/app launch helpers.
- Provider-neutral action batch validation/execution.
- Stuck-input recovery.
- Artifact listing, safe file writes, manifest append, Volume sync, and redaction-aware trace append.
- Safe input validation and error responses.
- Budget enforcement.
- Structured logs and optional OpenTelemetry spans.
- Static recording dashboard, optional.

### 8.2 Process supervisor

Use a Python process supervisor instead of a separate `supervisord` dependency for v1.

Reasons:

- Easier to expose process status/logs/stderr directly through the daemon.
- Fewer moving parts in the Modal image.
- Clean restart semantics for Daytona-style process management.
- Better unit-testability.

Managed process names:

| Name | Command | Restartable | Required |
|---|---|---:|---:|
| `xvfb` | `Xvfb :99 -screen 0 WIDTHxHEIGHTx24 -nolisten tcp` | yes | yes |
| `window_manager` | `startxfce4` or minimal WM | yes | yes |
| `x11vnc` | `x11vnc -display :99 -localhost -forever -shared ...` | yes | optional if noVNC disabled, but default yes |
| `novnc` | `websockify --web=/usr/share/novnc 6080 localhost:5900` | yes | optional |
| `browser_prewarm` | profile initialization command | no | no |
| `recording:<id>` | ffmpeg command | yes/no depending status | no |

The daemon itself should not list as a restartable child process. The client can restart the sandbox to restart the daemon.

### 8.3 Startup sequence

1. Load settings from environment.
2. Create log, recording, artifact, trace, and download directories.
3. Start Xvfb.
4. Poll until display responds (`xdpyinfo -display :99`).
5. Start window manager.
6. Start x11vnc if noVNC/viewing is enabled or if recording/screenshot tooling requires it.
7. Start noVNC if enabled.
8. Run optional browser prewarm after display readiness.
9. Perform one screenshot probe and one mouse-position probe.
10. Start FastAPI/uvicorn server on `0.0.0.0:8080`.
11. `/healthz` returns successful once the HTTP server is alive.
12. `/readyz` returns `ready=true` only after Xvfb responds, the window manager is running, required tools are present, screenshot capture succeeds once, input tooling responds, and noVNC is healthy when enabled.

### 8.4 Input serialization

Mouse, keyboard, and clipboard actions must be serialized through a single lock. Many X11 tools behave badly when called concurrently. The daemon should use an `asyncio.Lock` or thread lock around every input action and provide a per-action timeout.

Pseudo-code:

```python
async with input_lock:
    validate_bounds(x, y)
    await budget.consume_action()
    await run_xdotool(...)
    await trace.append(...)
    return ActionResult(ok=True, elapsed_ms=...)
```

### 8.5 Stuck-input recovery

Low-level `mouse_down`, `mouse_up`, `hold_key`, and failed batches can leave keys or mouse buttons held. v5 requires a recovery route:

```http
POST /v1/input/release-all
```

Behavior:

- Track held keys and mouse buttons in daemon state.
- Send mouse-up for buttons 1/2/3 even if daemon state is incomplete.
- Send key-up for all daemon-tracked held keys.
- Adapters must call this in `finally` blocks after hold/down/up errors.
- SDK exposes `computer.input.release_all()`.

### 8.6 Error response shape

All routes should return consistent errors:

```json
{
  "error": {
    "code": "invalid_coordinate",
    "message": "x must be between 0 and 1439",
    "call_id": "call_...",
    "details": {"x": 1600, "width": 1440}
  }
}
```

Core error codes:

- `daemon_not_ready`
- `process_not_found`
- `process_start_failed`
- `invalid_coordinate`
- `invalid_region`
- `invalid_mouse_button`
- `invalid_scroll_direction`
- `invalid_action_batch`
- `unsupported_action`
- `action_sequence_conflict`
- `invalid_key`
- `unsupported_control_character`
- `screenshot_failed`
- `recording_not_found`
- `recording_already_running`
- `recording_stop_failed`
- `display_unavailable`
- `artifact_not_found`
- `artifact_path_invalid`
- `budget_exceeded`
- `rate_limited`
- `timeout`
- `config_conflict`
- `internal_error`

### 8.7 Screenshot and artifact fast paths

The daemon should support multiple modes so users can optimize for latency or bandwidth:

- **Raw bytes:** capture and return PNG without resizing or recompression.
- **Compressed bytes:** capture, optionally crop, scale, and encode to PNG/JPEG/WebP.
- **File-backed capture:** capture into `/home/desktop/artifacts/screenshots/...` and return metadata/path/URI.
- **Client-side post-processing:** return raw screenshot bytes to the SDK/control-plane helper, which performs scaling/compression outside the desktop sandbox.

All screenshot responses must include enough metadata for adapters and replay:

```json
{
  "format": "png",
  "width": 1440,
  "height": 900,
  "size_bytes": 123456,
  "data_base64": "...",
  "artifact_uri": "artifact://screenshots/2026-05-11T120000Z_call_abc.png",
  "sha256": "...",
  "captured_at": "2026-05-11T12:00:00Z",
  "coordinate_space": {
    "desktop_width": 1440,
    "desktop_height": 900,
    "image_width": 1440,
    "image_height": 900,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "source_region": null
  },
  "cursor_visible": false,
  "cursor_position": {"x": 0, "y": 0}
}
```


## 9. HTTP API specification

The daemon's HTTP API should be versioned under `/v1`. Liveness/readiness endpoints are intentionally unversioned because infrastructure probes often expect simple paths.

### 9.1 Health, version, capabilities, and lifecycle

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Basic daemon process liveness. No heavy display checks. |
| `GET` | `/readyz` | Full desktop readiness check. Used by SDK and readiness probes. |
| `GET` | `/v1/version` | API/daemon version and SDK compatibility. |
| `GET` | `/v1/capabilities` | Supported features, formats, action types, adapter versions, and resource profile. |
| `GET` | `/v1/computer/status` | Daytona-style status for all managed processes and resolved resources. |
| `POST` | `/v1/computer/start` | Start missing desktop processes. Idempotent. |
| `POST` | `/v1/computer/stop` | Stop desktop processes. Idempotent. |
| `POST` | `/v1/computer/restart` | Restart all managed desktop processes. |

Version response:

```json
{
  "api_version": "v1",
  "daemon_version": "0.2.0",
  "sdk_min_version": "0.2.0",
  "sdk_max_version": "0.x",
  "image_profile": "browser",
  "modal_computer_use_package": "0.2.0"
}
```

Status response:

```json
{
  "status": "running",
  "ready": true,
  "display": ":99",
  "width": 1440,
  "height": 900,
  "resources": {"profile": "browser", "cpu": 4, "memory_mib": 8192, "gpu": null},
  "processes": {
    "xvfb": {"status": "running", "pid": 41, "uptime_seconds": 123},
    "window_manager": {"status": "running", "pid": 52, "uptime_seconds": 122},
    "x11vnc": {"status": "running", "pid": 63, "uptime_seconds": 121},
    "novnc": {"status": "running", "pid": 70, "uptime_seconds": 121}
  }
}
```

### 9.2 Processes routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/processes/{name}/status` | Status for a process. |
| `POST` | `/v1/processes/{name}/restart` | Restart a process. |
| `GET` | `/v1/processes/{name}/logs?tail=200` | stdout tail. |
| `GET` | `/v1/processes/{name}/stderr?tail=200` | stderr tail. |
| `GET` | `/v1/processes/{name}/errors?tail=200` | Deprecated alias for stderr tail. |

### 9.3 Mouse routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/mouse/click` | Click at coordinates or current cursor. |
| `POST` | `/v1/mouse/move` | Move cursor. |
| `POST` | `/v1/mouse/drag` | Drag from start to end or through a path. |
| `POST` | `/v1/mouse/scroll` | Scroll up/down/left/right by ticks where supported. |
| `POST` | `/v1/mouse/down` | Press and hold a mouse button at current or supplied coordinate. |
| `POST` | `/v1/mouse/up` | Release a mouse button at current or supplied coordinate. |
| `GET` | `/v1/mouse/position` | Get cursor position. |

Mouse action requests may include modifiers/keys. The daemon normalizes these through the same key alias table as `keyboard.hotkey` and uses try/finally to release modifiers. Drag supports either start/end coordinates or `path: list[Point]`.

### 9.4 Keyboard routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/keyboard/type` | Type text. |
| `POST` | `/v1/keyboard/press` | Press key with optional modifiers. |
| `POST` | `/v1/keyboard/hotkey` | Press key sequence. |
| `POST` | `/v1/keyboard/hold` | Hold a key while executing nested key/mouse actions. |
| `GET` | `/v1/keyboard/keys` | Supported key names and aliases. |

Typing behavior:

- Accept Unicode text.
- Translate `\n`, `\r`, and `\r\n` into Enter key presses.
- Reject literal tab and other control characters. Users should call `press("tab")` for tabs.
- `method="auto"` should use clipboard paste for Unicode or long strings and xdotool type for simple ASCII.
- Logs must record text length and hash, not plaintext, unless explicit debug logging is enabled.
- `hold` uses keydown/keyup try/finally so keys are released even if nested actions fail.

### 9.5 Clipboard routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/clipboard/text` | Read text clipboard. |
| `PUT` | `/v1/clipboard/text` | Set text clipboard. |
| `DELETE` | `/v1/clipboard/text` | Clear text clipboard. |

Clipboard content is sensitive. Logs record length and hash only. Clipboard operations use the same input lock as keyboard/mouse to avoid racing paste actions.

### 9.6 Screenshots routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/screenshots/full` | Full screenshot with options. |
| `POST` | `/v1/screenshots/region` | Region screenshot with options. |
| `POST` | `/v1/screenshots/zoom` | Crop a region and scale up for provider compatibility. |

Full screenshot request:

```json
{
  "format": "png",
  "quality": 90,
  "scale": 1.0,
  "show_cursor": false,
  "encoding": "base64",
  "storage": "inline",
  "processing": "auto"
}
```

Zoom request:

```json
{
  "region": {"x": 500, "y": 300, "width": 400, "height": 300},
  "scale": 2.0,
  "format": "png",
  "show_cursor": true,
  "storage": "inline"
}
```

Screenshot response:

```json
{
  "format": "png",
  "width": 1440,
  "height": 900,
  "size_bytes": 123456,
  "data_base64": "...",
  "artifact_uri": null,
  "sha256": "...",
  "captured_at": "2026-05-11T14:00:00Z",
  "coordinate_space": {
    "desktop_width": 1440,
    "desktop_height": 900,
    "image_width": 1440,
    "image_height": 900,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "source_region": null
  }
}
```

Supported formats are `png`, `jpeg`, and `webp`. `zoom_around(center, width, height, scale)` is an SDK convenience that converts to region form.

### 9.7 Recordings routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/recordings` | Start recording. |
| `POST` | `/v1/recordings/{id}/stop` | Stop recording. |
| `GET` | `/v1/recordings` | List recordings. |
| `GET` | `/v1/recordings/{id}` | Get metadata. |
| `GET` | `/v1/recordings/{id}/download` | Stream video file. |
| `DELETE` | `/v1/recordings/{id}` | Delete recording. |

Stop behavior: send SIGINT to ffmpeg, wait up to five seconds, send SIGTERM if needed, update metadata atomically, and return final size/duration/hash.

### 9.8 Action batch routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/actions/run` | Execute an ordered batch of provider-neutral UI actions. |
| `POST` | `/v1/actions/validate` | Validate an action batch without executing it. |

Supported action types include `move`, `click`, `double_click`, `triple_click`, `drag`, `scroll`, `mouse_down`, `mouse_up`, `type`, `keypress`, `hotkey`, `hold_key`, `wait`, `screenshot`, `zoom`, `cursor_position`, and `release_all`.

Batch request:

```json
{
  "actions": [
    {"type": "move", "x": 300, "y": 240},
    {"type": "click", "button": "left"},
    {"type": "type", "text": "hello"},
    {"type": "wait", "duration_ms": 500}
  ],
  "screenshot_after": true,
  "continue_on_error": false,
  "source": "anthropic-adapter",
  "max_action_timeout_ms": 5000
}
```

### 9.9 Artifact routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/artifacts?prefix=...` | List artifacts under a safe relative prefix. |
| `GET` | `/v1/artifacts/{path:path}` | Download or read an artifact. |
| `PUT` | `/v1/artifacts/{path:path}` | Write/upload an artifact. |
| `DELETE` | `/v1/artifacts/{path:path}` | Delete an artifact. |
| `POST` | `/v1/artifacts/sync` | Flush/persist artifacts where supported. |
| `GET` | `/v1/artifacts/manifest` | Return or stream artifact manifest entries. |

Artifact safety rules:

- All paths are relative to `ARTIFACTS_DIR` or a declared artifact root.
- Reject absolute paths, `..`, URL-encoded traversal, symlinks escaping the root, hidden control paths, and disallowed prefixes.
- Large artifacts stream bytes; do not base64 encode large recordings.
- Every write returns `ArtifactInfo` with SHA-256, content type, size, and `artifact://` URI.

### 9.10 Display, windows, apps, and browser routes

Display routes:

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/display/info` | Display geometry and primary display. |

Window routes:

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/windows` | Window list and active window. |
| `GET` | `/v1/windows/active` | Active window metadata. |
| `POST` | `/v1/windows/{id}/activate` | Focus/raise a window. |
| `POST` | `/v1/windows/{id}/close` | Close a window. |
| `POST` | `/v1/windows/wait-for` | Wait for title regex/class/pid. |

App/browser routes:

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/apps/launch` | Launch an application or command. |
| `POST` | `/v1/apps/open-artifact` | Open an artifact with xdg-open/default app. |
| `POST` | `/v1/browser/open-url` | Open URL in configured browser. |
| `GET` | `/v1/browser/status` | Browser process/profile/window status. |

### 9.11 Input recovery route

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/input/release-all` | Release tracked held keys and mouse buttons. |

### 9.12 Request envelope, sequencing, and idempotency

Every mutating primitive should accept optional metadata:

```json
{
  "call_id": "call_...",
  "sequence": 42,
  "source": "sdk|openai-adapter|anthropic-adapter|test"
}
```

Rules:

- The daemon serializes input events with an `asyncio.Lock` or equivalent.
- If `sequence` is supplied and strict sequencing is enabled, reject out-of-order actions with `409`.
- If the HTTP `Idempotency-Key` header is supplied, duplicate requests return the original result where safe.
- JSON body `idempotency_key` is accepted for batch sub-actions, but the HTTP header is preferred for whole-request idempotency.
- Screenshots and status reads do not require sequencing.
- Logs include `call_id`, route, duration, success/failure, redaction metadata, and process state.


## 10. Python SDK API specification

### 10.1 Namespaces

`ComputerSandbox` exposes namespaces as properties:

```python
computer.mouse.click(...)
computer.keyboard.type(...)
computer.clipboard.set_text(...)
computer.screenshots.full(...)
computer.recordings.start(...)
computer.display.info()
computer.windows.list()
computer.browser.open_url(...)
computer.apps.launch(...)
computer.processes.status("xvfb")
computer.actions.run([...])
computer.actions.validate([...])
computer.artifacts.list()
computer.input.release_all()
computer.commands.run("firefox", "https://example.com")
```

Collection-like namespaces are plural: `processes`, `recordings`, `screenshots`, `actions`, `artifacts`, `windows`, `apps`, and `commands`. Device or concept namespaces are singular: `mouse`, `keyboard`, `clipboard`, `display`, `browser`, `input`, `session`, and `debug`.

### 10.2 Actions API

```python
computer.actions.apply(action: ComputerAction) -> ActionResult
computer.actions.run(
    actions: list[ComputerAction],
    *,
    continue_on_error: bool = False,
    screenshot_after: bool = False,
    screenshot_options: ScreenshotOptions | None = None,
    idempotency_key: str | None = None,
) -> ActionBatchResult
computer.actions.validate(actions: list[ComputerAction]) -> ValidationResult
```

The implementation should normalize to the same internal code paths as `mouse`, `keyboard`, `clipboard`, and `screenshots` primitives.

### 10.3 Lifecycle API

```python
computer.start() -> LifecycleResult
computer.stop() -> LifecycleResult
computer.restart() -> LifecycleResult
computer.status() -> ComputerStatus
computer.wait_until_ready(timeout: float = 120.0) -> None
computer.terminate() -> None
computer.detach() -> None
```

### 10.4 Processes API

```python
computer.processes.status(name: str) -> ProcessStatus
computer.processes.restart(name: str) -> ProcessStatus
computer.processes.logs(name: str, tail: int = 200) -> str
computer.processes.stderr(name: str, tail: int = 200) -> str
computer.processes.errors(name: str, tail: int = 200) -> str  # deprecated alias
```

### 10.5 Mouse API

```python
computer.mouse.click(
    x: int | None = None,
    y: int | None = None,
    button: Literal["left", "middle", "right"] = "left",
    double: bool = False,
    modifiers: list[str] | None = None,
) -> Point

computer.mouse.move(x: int, y: int) -> Point
computer.mouse.drag(
    start_x: int | None = None,
    start_y: int | None = None,
    end_x: int | None = None,
    end_y: int | None = None,
    *,
    path: list[Point] | None = None,
    duration_ms: int = 500,
    modifiers: list[str] | None = None,
) -> Point
computer.mouse.scroll(direction: Literal["up", "down", "left", "right"], amount: int = 1, x: int | None = None, y: int | None = None) -> ActionResult
computer.mouse.down(button: Literal["left", "middle", "right"] = "left", x: int | None = None, y: int | None = None) -> ActionResult
computer.mouse.up(button: Literal["left", "middle", "right"] = "left", x: int | None = None, y: int | None = None) -> ActionResult
computer.mouse.position() -> Point
```

Coordinates are required for explicit script clicks by default, but adapters may call `click()` without coordinates to reproduce provider schemas that click at the current cursor position.

### 10.6 Keyboard API

```python
computer.keyboard.type(text: str, delay_ms: int = 10, method: Literal["auto", "xdotool", "clipboard"] = "auto") -> ActionResult
computer.keyboard.press(key: str, modifiers: list[str] | None = None, duration_ms: int = 0) -> ActionResult
computer.keyboard.hotkey(*keys: str, duration_ms: int = 0) -> ActionResult
computer.keyboard.hold(key: str, duration_ms: int | None = None, actions: list[ComputerAction] | None = None) -> ActionResult
computer.keyboard.supported_keys() -> SupportedKeys
```

### 10.7 Clipboard API

```python
computer.clipboard.get_text() -> str
computer.clipboard.set_text(text: str) -> ActionResult
computer.clipboard.clear() -> ActionResult
```

Clipboard methods are redaction-sensitive. The SDK and daemon should avoid logging plaintext by default.

### 10.8 Screenshots API

```python
shot = computer.screenshots.full(
    format: Literal["png", "jpeg", "webp"] = "png",
    quality: int = 90,
    scale: float = 1.0,
    show_cursor: bool = False,
    processing: Literal["daemon", "client", "auto"] = "auto",
    storage: Literal["inline", "artifact", "auto"] = "inline",
) -> Screenshot

shot = computer.screenshots.region(
    x: int,
    y: int,
    width: int,
    height: int,
    format: Literal["png", "jpeg", "webp"] = "png",
    quality: int = 90,
    scale: float = 1.0,
    show_cursor: bool = False,
    processing: Literal["daemon", "client", "auto"] = "auto",
    storage: Literal["inline", "artifact", "auto"] = "inline",
) -> Screenshot

shot = computer.screenshots.zoom(
    region: Region,
    scale: float = 2.0,
    format: Literal["png", "jpeg", "webp"] = "png",
    quality: int = 90,
    show_cursor: bool = True,
    storage: Literal["inline", "artifact", "auto"] = "inline",
) -> Screenshot

shot = computer.screenshots.zoom_around(
    center: tuple[int, int],
    width: int,
    height: int,
    scale: float = 2.0,
    format: Literal["png", "jpeg", "webp"] = "png",
    quality: int = 90,
    show_cursor: bool = True,
) -> Screenshot

shot.save("screen.png")
shot.to_pil()
shot.to_base64()
shot.coordinate_space.to_desktop(Point(x=10, y=10))
```

Compression is a parameter, not a separate method. Deprecated Daytona-compatibility aliases may exist for one major version with warnings.

### 10.9 Recordings API

```python
rec = computer.recordings.start(name: str | None = None, fps: int = 12, format: str = "mp4")
rec = computer.recordings.stop(recording_id: str)
recordings = computer.recordings.list()
rec = computer.recordings.get(recording_id)
computer.recordings.download(recording_id, local_path: str | pathlib.Path) -> pathlib.Path
computer.recordings.delete(recording_id) -> None
```

### 10.10 Display and windows API

```python
info = computer.display.info()
windows = computer.windows.list()
active = computer.windows.active()
computer.windows.activate(window_id)
computer.windows.close(window_id)
computer.windows.wait_for(title_regex="Firefox", timeout=10)
```

### 10.11 Browser and apps API

```python
computer.apps.launch("firefox")
computer.apps.open_artifact("downloads/report.pdf")
computer.browser.open_url("https://example.com", wait_for_window=True)
status = computer.browser.status()
```

These helpers are visual/desktop ergonomics only. DOM automation, Playwright, and browser protocol control remain optional examples.

### 10.12 Batch action API

```python
computer.actions.run(
    actions: list[ComputerAction | dict],
    *,
    screenshot_after: bool = False,
    continue_on_error: bool = False,
    idempotency_key: str | None = None,
) -> ActionBatchResult

computer.actions.validate(actions: list[ComputerAction | dict]) -> ValidationResult
```

Adapters should call `computer.actions.run(...)` when a provider returns multiple actions. Human-written scripts can use individual namespaces for readability.

### 10.13 Artifact API

```python
computer.artifacts.list(prefix: str = "") -> list[ArtifactInfo]
computer.artifacts.read_bytes(path: str) -> bytes
computer.artifacts.write_bytes(path: str, data: bytes, content_type: str | None = None) -> ArtifactInfo
computer.artifacts.download(path: str, local_path: str | pathlib.Path) -> pathlib.Path
computer.artifacts.upload(local_path: str | pathlib.Path, path: str) -> ArtifactInfo
computer.artifacts.delete(path: str) -> None
computer.artifacts.manifest(prefix: str = "") -> list[ArtifactInfo]
computer.artifacts.sync() -> ArtifactSyncResult
```

All artifact paths are relative. The SDK should validate paths client-side before sending them to the daemon, but the daemon remains the source of truth and must repeat validation. The public SDK should not expose a separate `computer.files` namespace in v1.

### 10.14 Debug and session API

```python
computer.session.metadata() -> SandboxRef
computer.session.refresh() -> SandboxRef
computer.debug.urls() -> DebugUrls
computer.debug.vnc_url(refresh: bool = False) -> str | None
```

`debug.urls()` must never create a public tunnel by surprise. It only returns URLs for channels explicitly exposed at sandbox creation time, and noVNC URLs should be treated as secrets.

### 10.15 Command API: narrow, optional

A command API is useful but should not be confused with computer-use primitives:

```python
result = computer.commands.run("firefox", "https://example.com", timeout=10)
```

Do not document `computer.exec(...)` in quickstarts. If implemented, keep it as a deprecated alias that warns users to prefer `computer.commands.run(...)`.

### 10.16 Async SDK

Provide async variants for model loops:

```python
from modal_computer_use import AsyncComputerSandbox

async with await AsyncComputerSandbox.create(config=ComputerConfig()) as computer:
    await computer.mouse.click(100, 100)
    shot = await computer.screenshots.full()
```

The async SDK should use the same model classes and route semantics as the sync SDK.


## 11. Data models

### 11.1 Core models

```python
class Point(BaseModel):
    x: int
    y: int

class Region(BaseModel):
    x: int
    y: int
    width: int
    height: int

class CoordinateSpace(BaseModel):
    desktop_width: int
    desktop_height: int
    image_width: int
    image_height: int
    scale_x: float = 1.0
    scale_y: float = 1.0
    source_region: Region | None = None

    def to_desktop(self, point: Point) -> Point: ...
    def to_image(self, point: Point) -> Point: ...

class ActionResult(BaseModel):
    ok: bool = True
    message: str | None = None
    elapsed_ms: float | None = None

class ProcessStatus(BaseModel):
    name: str
    status: Literal["starting", "running", "stopped", "failed", "unknown"]
    pid: int | None = None
    started_at: datetime | None = None
    uptime_seconds: float | None = None
    restart_count: int = 0
    exit_code: int | None = None
    last_error: str | None = None

class ComputerStatus(BaseModel):
    status: Literal["starting", "running", "stopped", "degraded", "failed"]
    ready: bool
    display: str
    width: int
    height: int
    processes: dict[str, ProcessStatus]
    resources: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)

class Screenshot(BaseModel):
    format: Literal["png", "jpeg", "webp"]
    width: int
    height: int
    size_bytes: int
    bytes: bytes | None = None
    data_base64: str | None = None
    artifact_uri: str | None = None
    sha256: str | None = None
    captured_at: datetime
    coordinate_space: CoordinateSpace
    cursor_visible: bool = False
    cursor_position: Point | None = None

class Recording(BaseModel):
    id: str
    name: str | None
    status: Literal["recording", "stopped", "failed"]
    format: str
    fps: int
    path: str
    artifact_uri: str | None = None
    size_bytes: int
    sha256: str | None = None
    started_at: datetime
    stopped_at: datetime | None = None
    duration_seconds: float | None = None

class DisplayGeometry(BaseModel):
    id: str
    x: int
    y: int
    width: int
    height: int
    scale: float = 1.0

class DisplayInfo(BaseModel):
    primary_display: DisplayGeometry
    total_displays: int
    displays: list[DisplayGeometry]

class X11Window(BaseModel):
    id: str
    title: str
    pid: int | None = None
    x: int
    y: int
    width: int
    height: int
    workspace: int | None = None
    is_active: bool = False

class ArtifactInfo(BaseModel):
    path: str
    uri: str
    kind: Literal["file", "directory"]
    size_bytes: int | None = None
    content_type: str | None = None
    sha256: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    created_by_call_id: str | None = None
    retention_class: Literal["ephemeral", "persistent", "debug", "trace"] = "ephemeral"

class ArtifactSyncResult(BaseModel):
    ok: bool
    persistent: bool
    synced_paths: list[str] = Field(default_factory=list)
    message: str | None = None

class SandboxRef(BaseModel):
    sandbox_id: str
    app_name: str
    name: str | None = None
    run_id: str | None = None
    config_hash: str | None = None
    status: Literal["created", "scheduled", "started", "ready", "finished", "unknown"]
    tags: dict[str, str] = Field(default_factory=dict)
    vnc_url: str | None = None
    artifacts_dir: str = "/home/desktop/artifacts"

class DebugUrls(BaseModel):
    vnc: str | None = None
    daemon: str | None = None
    recording_dashboard: str | None = None

class ActionDecision(BaseModel):
    decision: Literal["allow", "deny", "ask_user", "handoff"]
    reason: str | None = None

class ComputerAction(BaseModel):
    type: str
    # Concrete action subclasses should be discriminated by `type`.

class ActionItemResult(BaseModel):
    index: int
    type: str
    ok: bool
    elapsed_ms: float | None = None
    error: str | None = None

class ActionBatchResult(BaseModel):
    ok: bool
    call_id: str | None = None
    results: list[ActionItemResult]
    screenshot: Screenshot | None = None
```

Model naming notes:

- `DisplayGeometry` avoids colliding with the `computer.display` namespace.
- `X11Window` avoids ambiguity with browser windows in browser-heavy examples.
- `call_id` identifies one API call. `run_id` identifies a sandbox/run lifetime. Do not use `request_id` for either concept in new public models.
- `Screenshot.bytes` is the SDK bytes field. HTTP uses `data_base64`.

### 11.2 Provider-neutral action models

Use discriminated unions, not one giant optional-field model, in implementation. The conceptual schema is:

```python
class MoveAction(BaseModel):
    type: Literal["move"]
    x: int
    y: int

class ClickAction(BaseModel):
    type: Literal["click"]
    x: int | None = None
    y: int | None = None
    button: Literal["left", "right", "middle"] = "left"
    modifiers: list[str] = Field(default_factory=list)

class DragAction(BaseModel):
    type: Literal["drag"]
    start_x: int | None = None
    start_y: int | None = None
    end_x: int | None = None
    end_y: int | None = None
    path: list[Point] | None = None
    button: Literal["left", "right", "middle"] = "left"
    duration_ms: int = 500

class WaitAction(BaseModel):
    type: Literal["wait"]
    duration_ms: int

class ScreenshotAction(BaseModel):
    type: Literal["screenshot"]
    options: ScreenshotOptions | None = None
```

Implementation should define concrete classes for every supported action and a `ComputerAction` union with discriminator `type`. This prevents invalid field combinations from passing validation.

### 11.3 Trace models

```python
class TraceEntry(BaseModel):
    ts: datetime
    run_id: str | None = None
    call_id: str
    sequence: int | None = None
    source: str
    provider_action: dict[str, Any] | None = None
    normalized_action: ComputerAction | None = None
    result: ActionItemResult | ActionBatchResult | None = None
    elapsed_ms: float | None = None
    screenshot_before_uri: str | None = None
    screenshot_after_uri: str | None = None
    coordinate_space: CoordinateSpace | None = None
    redactions: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
```

Traces are stored as NDJSON in `artifacts/traces/actions.ndjson`.

### 11.4 Versioning

The SDK and daemon should expose versions:

- SDK package version: `modal_computer_use.__version__`
- Daemon version: `/v1/version`
- API version: `/v1`
- Compatibility check: SDK validates daemon major version.


## 12. OpenAI Computer Use adapter

### 12.1 Adapter purpose

The adapter should translate actions from OpenAI's computer-use loop into core SDK calls. It should not call the OpenAI API itself. The user's app owns the model call and policy.

### 12.2 Supported action mapping

| OpenAI action | Generic action / SDK call |
|---|---|
| `click` | `mouse.click(x, y, button=..., modifiers=...)` |
| `double_click` | `mouse.click(x, y, double=True)` |
| `scroll` | `mouse.scroll("up"/"down"/"left"/"right", amount, x, y)` |
| `type` | `keyboard.type(text)` |
| `keypress` | `keyboard.hotkey(...)` or `keyboard.press(...)` |
| `drag` | `mouse.drag(..., path=...)` if path is present, otherwise start/end drag |
| `move` | `mouse.move(x, y)` |
| `screenshot` | no-op or `screenshots.full()` depending loop shape |
| `wait` | `ComputerAction(type="wait", duration_ms=...)` through batch executor |

### 12.3 Adapter sketch

```python
from modal_computer_use.adapters.openai import OpenAIAdapter

adapter = OpenAIAdapter(computer)

# model call is owned by user code
adapter.apply_many(computer_call.actions)

shot = computer.screenshots.full(format="png")
# user sends shot.to_base64() back to OpenAI as computer_call_output
```

The adapter class shape should be consistent across providers: `OpenAIAdapter`, `AnthropicAdapter`, and `ActionExecutor` should all expose `.apply(...)`, and batch-capable adapters should also expose `.apply_many(...)`. Avoid a mix of top-level `apply_*` functions and adapter classes in public docs.

### 12.4 Coordinate fidelity

The adapter should never silently rescale coordinates unless the caller explicitly passes a coordinate transform. For example:

```python
adapter = OpenAIAdapter(
    computer,
    coordinate_space=CoordinateSpace(
        desktop_width=1440,
        desktop_height=900,
        image_width=720,
        image_height=450,
        scale_x=0.5,
        scale_y=0.5,
    ),
)

adapter.apply_many(actions)
```

When screenshots are downscaled before being sent to a model, the adapter should provide a transform helper that maps model coordinates back to desktop coordinates.

### 12.5 Safety hooks

The adapter should support a synchronous or async callback before executing actions:

```python
def before_action(action, screenshot, context) -> ActionDecision:
    return ActionDecision(decision="allow")

adapter = OpenAIAdapter(computer, before_action=before_action)
adapter.apply_many(actions)
```

This lets user applications pause at risky actions. The core daemon should not infer policy from screen content. The adapter should provide hooks and examples only.

### 12.6 OpenAI fixture requirements

`tests/fixtures/openai_actions.json` must cover:

- `click`
- `double_click`
- `scroll`
- `type`
- `wait`
- `keypress`
- `drag` with start/end
- `drag` with path
- `move`
- `screenshot`
- multiple actions in one turn
- modifier keys on click/drag/key actions
- unknown action failure


## 13. Anthropic and generic adapters

### 13.1 Adapter principle

The adapter layer should normalize provider-specific computer-use actions into one generic action schema and then call the core primitive SDK. The adapter should not own the model call by default.

```text
Provider action JSON
  -> provider adapter
    -> ComputerAction
      -> ActionExecutor
        -> ComputerSandbox primitives
```

This keeps the package usable with Anthropic, OpenAI, local policies, browser QA scripts, or deterministic test suites.

### 13.2 Generic action schema

Implementation should use discriminated unions, but the conceptual generic actions are:

```python
class ComputerAction(BaseModel):
    type: Literal[
        "click",
        "double_click",
        "triple_click",
        "move",
        "drag",
        "scroll",
        "mouse_down",
        "mouse_up",
        "type",
        "keypress",
        "hotkey",
        "hold_key",
        "wait",
        "screenshot",
        "zoom",
        "cursor_position",
        "release_all",
    ]
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 13.3 Anthropic action compatibility

The public reference repo's computer tool uses the original Anthropic-style actions (`mouse_move`, `left_click_drag`, `key`, `type`, click variants, `screenshot`, and `cursor_position`). Anthropic's later computer-use schemas add enhanced low-level actions such as scroll, mouse down/up, hold key, wait, triple click, and zoom. The adapter should support the reference-repo actions exactly and support enhanced actions behind explicit schema-version flags.

| Anthropic-style action | Generic action | SDK primitive |
|---|---|---|
| `mouse_move` | `move` | `mouse.move(x, y)` |
| `left_click_drag` | `drag` | `mouse.drag(current_x, current_y, x, y)` or explicit start/end when provided |
| `key` | `keypress`/`hotkey` | `keyboard.press(...)` or `keyboard.hotkey(...)` |
| `type` | `type` | `keyboard.type(text)` |
| `left_click` | `click` | `mouse.click(button="left")` at current cursor or supplied coordinate |
| `right_click` | `click` | `mouse.click(button="right")` |
| `double_click` | `double_click` | `mouse.click(double=True)` |
| `triple_click` | `triple_click` | three serialized `mouse.click(...)` calls or one batch |
| `middle_click` | `click` | `mouse.click(button="middle")` |
| `left_mouse_down` | `mouse_down` | `mouse.down(button="left")` |
| `left_mouse_up` | `mouse_up` | `mouse.up(button="left")` |
| `mouse_down` | `mouse_down` | compatibility alias when older schemas use it |
| `mouse_up` | `mouse_up` | compatibility alias when older schemas use it |
| `hold_key` | `hold_key` | `keyboard.hold(key, actions=...)` or keydown/keyup wrapper |
| `scroll` | `scroll` | `mouse.scroll(direction, amount, x, y)` |
| `wait` | `wait` | daemon-traced wait action or adapter sleep through batch executor |
| `screenshot` | `screenshot` | `screenshots.full(...)` |
| `zoom` | `zoom` | `screenshots.zoom(...)` for compatible schemas |
| `cursor_position` | `cursor_position` | `mouse.position()` |

Recommended schema-version behavior:

| Adapter tool version | Required support |
|---|---|
| `computer_20241022` | Reference-repo-compatible actions: move, drag, key/type, click variants, screenshot, cursor position. |
| `computer_20250124` | Add enhanced scroll, right/middle/double/triple click variants, mouse down/up, hold key, and wait when present. |
| `computer_20251124` | Add zoom-style behavior where the provider schema asks for it; keep coordinate transforms explicit. |

Important details:

- Anthropic `left_click`, `right_click`, `double_click`, and `middle_click` are often coordinate-less actions that operate at the current cursor position. The adapter must preserve that behavior.
- `left_click_drag` may contain only the destination coordinate in some Anthropic schemas. The adapter should query `mouse.position()` before executing the drag when no start coordinate exists.
- `key` text may contain combinations such as `ctrl+c`; route these through the key normalization layer used by `keyboard.hotkey()`.
- The adapter should return provider-compatible tool results but store screenshots in the package's native `Screenshot` model first.
- Keep Anthropic action-schema versions explicit. The adapter should expose a `tool_version` option and document which actions are supported for each version rather than silently accepting unknown future fields.
- Unknown actions raise `UnsupportedActionError` unless `allow_unknown=True` is explicitly configured.
- Enhanced actions such as `left_mouse_down`, `left_mouse_up`, `hold_key`, `scroll`, `wait`, `triple_click`, and `zoom` should be normalized into the same provider-neutral action union used by `/v1/actions/run`.
- Coordinate scaling must be explicit. If screenshots are downsampled before being sent to the model, the adapter should scale returned coordinates back to desktop coordinates and include both coordinate spaces in metadata.

Suggested API:

```python
from modal_computer_use.adapters.anthropic import AnthropicAdapter

adapter = AnthropicAdapter(
    computer,
    tool_version="computer_20251124",
    beta_header="computer-use-2025-11-24",
    enable_zoom=True,
)

result = adapter.apply({"action": "mouse_move", "coordinate": [500, 300]})
```

### 13.4 Optional Anthropic server example

To show migration from `anthropic-computer-use-modal`, include an example server, but keep it outside core:

```text
examples/anthropic_message_server.py
```

It may expose a familiar shape:

```python
@app.cls(image=manager_image(), secrets=[anthropic_secret])
class AnthropicMessageServer:
    @modal.method()
    async def messages_create(self, run_id: str, user_messages: list[dict], **kwargs):
        # Calls Anthropic, receives tool_use blocks, applies actions through
        # modal_computer_use.adapters.anthropic, returns provider-shaped messages.
        ...
```

This example is allowed to depend on Anthropic. The core package must not.

### 13.5 Generic executor

```python
from modal_computer_use.adapters.generic import ActionExecutor

executor = ActionExecutor(
    computer,
    before_action=policy_callback,
    coordinate_space=CoordinateSpace(
        desktop_width=1440,
        desktop_height=900,
        image_width=1440,
        image_height=900,
    ),
)

for action in actions:
    result = executor.apply(action)
```

Executor responsibilities:

- Validate required fields for each action.
- Normalize coordinates if the caller explicitly supplies a transform.
- Serialize actions when the caller sends a batch.
- Support `before_action` and `after_action` hooks.
- Return structured action results, including screenshots when requested.
- Fail closed on unknown actions by default.

### 13.6 Text editor and Bash compatibility

The reference repo includes Bash and Edit tools because Anthropic's original demo exposes those tools. `modal-computer-use` should treat them as optional compatibility modules:

- `computer.commands.run(...)` may exist as a narrow, explicit debug/terminal primitive.
- `adapters.anthropic.editor` may provide fuzzy matching for text-edit workflows.
- Bash session persistence should not be required for core desktop primitives.
- File read/write should prefer Modal Sandbox filesystem APIs and the package's path-safe `artifacts` helper.

This avoids broadening v1 into a full code-agent runtime while still making migration easy for users who liked the reference repo.

### 13.7 Anthropic tool-version strategy

Anthropic's computer-use schemas evolve, so the adapter should not hard-code one dated tool version into core behavior. Implement a small registry:

```python
AnthropicAdapter(
    computer,
    tool_version="computer_20251124",
    beta_header="computer-use-2025-11-24",
    enable_zoom=True,
)
```

Adapter requirements:

- Support the older action names used by the public Modal reference repo for compatibility.
- Support newer action fields through a registry and fixture tests rather than scattered conditionals.
- Implement `zoom` as a screenshot-region helper when `enable_zoom=True`; do not treat it as a separate desktop primitive.
- Keep text editor and bash adapters in optional extras, for example `pip install modal-computer-use[anthropic]`.
- Preserve provider-shaped outputs for examples, but keep the native primitive result as the canonical internal representation.
- Use `left_mouse_down` and `left_mouse_up` as official enhanced action names where applicable, while retaining `mouse_down`/`mouse_up` compatibility aliases.

Suggested package split:

```text
modal_computer_use/adapters/anthropic/computer.py
modal_computer_use/adapters/anthropic/bash.py
modal_computer_use/adapters/anthropic/editor.py
modal_computer_use/adapters/anthropic/versions.py
```

### 13.8 Anthropic fixture requirements

Fixtures must cover:

- `mouse_move`
- `left_click_drag`
- `key`
- `type`
- `left_click`
- `right_click`
- `double_click`
- `triple_click`
- `middle_click`
- `left_mouse_down`
- `left_mouse_up`
- `scroll`
- `hold_key`
- `wait`
- `screenshot`
- `zoom`
- `cursor_position`
- unknown action failure
- coordinate-less click behavior
- destination-only drag behavior


## 14. Recording dashboard, trace viewer, and replay tooling

The core SDK must provide recording APIs. A dashboard is useful but should be minimal in v1.

### 14.1 Recording dashboard

Recommended v1 dashboard:

- Served by daemon under `/recordings/ui`.
- Requires the same Modal Connect Token as other daemon routes, not a public unauthenticated tunnel.
- Lists recordings with name, size, duration, created time, status, SHA-256, and artifact URI.
- Provides playback and download links.
- Provides delete button.

Do not overbuild this as a full UI product. The dashboard is a debugging convenience and parity feature inspired by Daytona's recording dashboard.

### 14.2 Trace/replay format

Trace/replay is one of the best ways for this open-source project to be meaningfully better than just using a managed provider. It should be concrete, not aspirational.

Trace layout:

```text
/home/desktop/artifacts/traces/
  actions.ndjson
  screenshots/
    before_call_001.png
    after_call_001.png
```

`actions.ndjson` line schema:

```json
{
  "ts": "2026-05-11T14:00:00Z",
  "run_id": "run-123",
  "call_id": "call_abc",
  "sequence": 1,
  "source": "openai-adapter",
  "provider_action": {"type": "click", "x": 300, "y": 240},
  "normalized_action": {"type": "click", "x": 300, "y": 240, "button": "left"},
  "result": {"ok": true, "elapsed_ms": 47},
  "elapsed_ms": 47,
  "screenshot_before_uri": "artifact://screenshots/before_call_abc.png",
  "screenshot_after_uri": "artifact://screenshots/after_call_abc.png",
  "coordinate_space": {
    "desktop_width": 1440,
    "desktop_height": 900,
    "image_width": 1440,
    "image_height": 900
  },
  "redactions": ["typed_text"],
  "error": null
}
```

CLI plan:

```bash
computer-use trace validate artifacts/traces/actions.ndjson
computer-use trace replay artifacts/traces/actions.ndjson --dry-run
computer-use trace replay artifacts/traces/actions.ndjson --target run-456
```

v0.2 should implement validation and dry-run replay. v1.0 should implement controlled replay into a fresh sandbox.

### 14.3 Trace safety

Traces may contain sensitive typed text, clipboard text, URLs, screenshots, and errors. Default behavior:

- Redact typed text and clipboard text.
- Store length and SHA-256 hash of text where useful.
- Store screenshots only if `trace_screenshots=True` or screenshot artifacts are already being produced.
- Redact noVNC URLs and connect tokens.
- Allow explicit opt-in debug mode for full plaintext traces only in local/dev environments.


## 15. Security model

### 15.1 Threat model

This package is built for AI agents and untrusted desktop workflows, so assume:

- The desktop may display malicious web pages or documents.
- The agent may read screenshots containing untrusted instructions.
- The sandbox may run user-supplied code or browser content.
- Screenshots, traces, clipboard contents, and recordings may contain secrets or personal data.
- noVNC URLs are sensitive because they expose a live desktop.
- The daemon API can click, type, read the screen, read/write artifacts, and launch apps, so it must not be public unauthenticated.

### 15.2 Secure-by-default controls

1. **Daemon uses Modal Connect Tokens.** Programmatic API access should go through authenticated connect-token requests to port `8080`.
2. **noVNC is opt-in.** Users must pass `expose_vnc="view_only"` or `expose_vnc="control"` to create an external noVNC tunnel.
3. **Generated VNC password.** If noVNC is exposed, generate a random password unless the user provided one.
4. **View-only mode.** Provide a noVNC mode that permits observation without remote control where technically supported.
5. **No model API keys by default.** Core package should not require OpenAI/Anthropic/other model credentials inside the sandbox.
6. **Network controls exposed.** Surface Modal network controls through `ComputerConfig.network`, which maps to Modal parameters such as `block_network` and `cidr_allowlist` where supported.
7. **Input validation.** Validate coordinates, regions, key names, buttons, screenshot size, recording fps, paths, and durations.
8. **Rate limiting.** Add simple per-sandbox action rate limiting to prevent accidental runaway loops.
9. **Budget limits.** Add action, screenshot, byte, recording, batch, and idle budgets.
10. **Recording retention.** Do not auto-upload recordings. Provide explicit download/delete/sync and document Volume behavior.
11. **Call IDs and audit trail.** Log action type, call ID, dimensions, timings, and redactions. Avoid logging typed text by default; log text length and hash only.
12. **Human confirmation hooks.** Provide adapter hooks so user apps can pause before risky actions.
13. **URL and token redaction.** Never log full noVNC URLs, connect tokens, query-token URLs, provider API keys, or artifact bytes by default.

### 15.3 Sensitive action policy belongs above the core

The core package should not decide whether a form submission, email send, purchase, password change, file deletion, permissions change, or acceptance of terms is allowed. Those are product-policy decisions. The core should provide:

- Pre-action hooks.
- Optional screenshot/context passed to hooks.
- A standard `ActionDecision` model: `allow`, `deny`, `ask_user`, `handoff`.
- Examples that implement confirmation around risky actions.

### 15.4 Modal-specific security notes

- Default Modal Sandboxes cannot accept incoming network connections unless explicitly configured.
- For HTTP/WebSocket access to a server inside a sandbox, Modal Connect Tokens are the preferred authenticated path on port `8080`.
- Raw TCP/HTTP tunnels should be used for services that manage their own authentication, such as noVNC with a generated password.
- Any exposed noVNC URL should be treated as sensitive. Do not log it publicly.
- Use Modal Secrets only for credentials that must be available in the sandbox.
- `user_metadata` in Connect Tokens must remain compact and non-secret.

### 15.5 Browser/domain policy examples

Network CIDR allowlists and browser domain policies solve different problems. The docs should include:

- `examples/domain_allowlist_policy.py`: adapter-level URL/domain confirmation before browser actions.
- A network-blocked example where only known domains are reachable when Modal supports the desired egress controls.
- A warning that screen content and webpages are untrusted input and may attempt prompt injection.

## 16. Persistence, artifacts, snapshots, and volumes

### 16.1 Default ephemeral mode

Default sandboxes are ephemeral. Screenshots are returned to the caller. Recordings, traces, downloads, and artifacts exist inside the sandbox until downloaded, synced to a mounted Volume, or the sandbox terminates.

This is the safest default.

### 16.2 Recording persistence with Volumes

For persistent recordings:

```python
recordings = modal.Volume.from_name("computer-use-recordings", create_if_missing=True)
computer = ComputerSandbox.create(
    volumes={"/home/desktop/recordings": recordings},
    config=ComputerConfig(storage={"recordings_dir": "/home/desktop/recordings"}),
)
```

The SDK should not automatically commit/sync Volumes after every recording unless Modal's Volume semantics require it for caller visibility. Provide `computer.artifacts.sync()` and document Volume commit/reload semantics clearly.

### 16.3 Artifact API over sandbox filesystem

Expose path-safe artifact helpers, not a second public `files` namespace:

```python
computer.artifacts.read_bytes(path) -> bytes
computer.artifacts.write_bytes(path, data: bytes) -> ArtifactInfo
computer.artifacts.download(path, local_path) -> pathlib.Path
computer.artifacts.upload(local_path, path) -> ArtifactInfo
computer.artifacts.list(prefix="") -> list[ArtifactInfo]
```

This surface should be optional and small. Modal already has first-class sandbox filesystem APIs; the wrapper should only make common computer-use workflows easier and should keep path validation strict. The daemon must repeat server-side validation even when the SDK validates paths client-side.

### 16.4 Snapshots

Expose snapshot helpers:

```python
image = computer.snapshot_filesystem()
computer2 = ComputerSandbox.create(image=image)
```

Use cases:

- Preinstalled apps/browser extensions.
- Reduced cold starts after a desktop has initialized.
- Reproducible QA baselines.
- Debugging a failed run.

Avoid promising that memory snapshots or GUI state persistence will work perfectly. Filesystem snapshots are the first reliable target.

### 16.5 Updated storage recommendation after reference repo review

The reference implementation's "one NFS per sandbox" pattern is operationally useful, but the new repo should update the primitive because Modal currently deprecates `NetworkFileSystem`.

Recommended storage hierarchy:

1. **Ephemeral live sandbox files:** use `sandbox.filesystem.read_bytes`, `write_bytes`, `copy_to_local`, `copy_from_local`, `remove`, and `make_directory` where available.
2. **Persistent artifacts:** mount a `modal.Volume` under `/home/desktop/artifacts` or `/home/desktop/recordings`.
3. **Large shared datasets or browser profiles:** use `modal.Volume`, CloudBucketMount, or bake immutable assets into the image.
4. **Legacy compatibility only:** mention `NetworkFileSystem` only when explaining why older examples use it. Do not use it in v1 defaults.

For Volumes v2, the daemon can run `sync` on the mountpoint after saving important artifacts when the caller asks for immediate persistence. Otherwise document that Volume sync/commit/reload semantics affect when files become visible outside the sandbox.

### 16.6 Artifact layout

Standardize file paths so users can debug runs consistently:

```text
/home/desktop/artifacts/
  manifest.ndjson
  screenshots/
    2026-05-10T120000Z_call_abc.png
  recordings/
    rec_abc123.webm
  logs/
    xvfb.log
    window_manager.log
    x11vnc.log
    novnc.log
    daemon.log
  downloads/
  traces/
    actions.ndjson
```

Artifact manifest line schema:

```json
{
  "ts": "2026-05-11T12:00:00Z",
  "path": "screenshots/2026-05-11T120000Z_call_abc.png",
  "uri": "artifact://screenshots/2026-05-11T120000Z_call_abc.png",
  "kind": "file",
  "content_type": "image/png",
  "size_bytes": 123456,
  "sha256": "...",
  "created_by_call_id": "call_abc",
  "retention_class": "trace"
}
```

The daemon should append an action trace when enabled. Traces are valuable for reproducing failed computer-use loops and must be redaction-aware because they may include typed text.

---

## 17. Cold-start and warm-pool strategy

### 17.1 v1 baseline

Optimize v1 with:

- A prebuilt Modal image with all apt/pip dependencies.
- Minimal desktop startup sequence.
- Readiness probe on port `8080`.
- Optional XFCE vs Openbox mode.

### 17.2 Optional warm-pool helper

Do not put warm pools in core object lifecycle for v1. Provide an example or helper module.

Warm-pool pattern:

1. Maintain a Modal Queue of ready sandbox references.
2. Periodically create sandboxes to keep target pool size.
3. Health-check daemon and noVNC readiness.
4. Claim a sandbox with enough TTL remaining.
5. Remove unhealthy/expiring sandboxes.

Example:

```python
from modal_computer_use.pool import WarmSandboxPool

pool = WarmSandboxPool(name="qa-desktops", size=5, min_ttl_seconds=900)
computer = pool.claim()
```

This should be a later milestone because pooling adds operational complexity.



### 17.3 Performance checklist from the Modal computer-use reference repo

Add these as documented options, not mandatory defaults:

- **Browser prewarm:** initialize Firefox/Chromium profile during image build and optionally at startup.
- **Optional GPU:** allow `gpu="T4"` or equivalent for browser-heavy workloads.
- **Client-side screenshot processing:** avoid blocking the desktop sandbox with expensive resize/encode work when the caller can do it outside.
- **Split images:** use a lightweight manager/control image and a heavier desktop sandbox image.
- **Prepared image variants:** publish or document `base`, `browser`, and `browser-gpu` image recipes.
- **Warm pool:** keep a queue of ready sandboxes only for users who need low first-action latency.
- **Action batching:** support `POST /v1/actions/run` for short batches such as move+click+wait, while still logging each sub-action.

Do not prematurely optimize by hiding costs. The SDK should expose timing metadata:

```python
result = computer.mouse.click(100, 200)
print(result.timing.daemon_ms, result.timing.total_ms)
```

---


## 18. Testing strategy

### 18.1 Unit tests

- Pydantic schema validation.
- Discriminated action union validation.
- Coordinate bounds and region validation.
- CoordinateSpace transform tests.
- Key normalization and alias mapping.
- Mouse button and scroll direction mapping.
- Screenshot option validation.
- Recording metadata model.
- Artifact path-safety validation.
- Error model serialization.
- OpenAI/generic adapter action mapping.
- Anthropic version registry mapping.
- Budget counter behavior.

### 18.2 Local integration tests

Run daemon in a local Linux container or CI environment with Xvfb installed.

Tests:

1. Start Xvfb and daemon.
2. Verify `/healthz`, `/readyz`, `/v1/version`, and `/v1/computer/status`.
3. Move mouse and assert position.
4. Type text into a terminal or deterministic app and verify via clipboard/window state where possible.
5. Clipboard get/set/clear.
6. Screenshot returns correct dimensions and coordinate metadata.
7. Region screenshot returns requested dimensions.
8. Zoom screenshot returns scaled dimensions and correct source region.
9. Start and stop a short recording; assert output file exists and size > 0.
10. Window listing returns at least one window after launching terminal.
11. App launch and browser URL open helpers work in a simple case.
12. `input.release_all()` succeeds after synthetic hold/down tests.

### 18.3 Modal integration tests

Mark these tests as requiring Modal credentials.

Tests:

1. Create sandbox from default image.
2. Wait until ready.
3. Fetch version/status/capabilities.
4. Take screenshot.
5. Run mouse/keyboard/clipboard actions.
6. Start/stop/download recording.
7. Optionally expose noVNC and assert tunnel metadata exists.
8. Attach by sandbox ID and by `run_id`.
9. Config hash conflict behavior.
10. Artifact sync behavior when a Volume is mounted.
11. Terminate/detach.

Use small timeouts and cleanup in `finally` blocks.

### 18.4 Golden image tests

For screenshot correctness:

- Launch a deterministic simple app/window.
- Capture screenshot.
- Verify dimensions and rough image hash or pixel regions.
- Verify coordinate-space transform maps model/image coordinates back to desktop coordinates.

Do not rely on exact full-image hashes for XFCE themes because minor package updates can change pixels.

### 18.5 Type checking and linting

- `ruff`
- `mypy` or `pyright`
- `pytest`
- `pytest-asyncio` if async routes or transports are used
- `respx` or `pytest-httpx` for HTTP transport tests
- `pydantic` schema round-trip tests

### 18.6 Provider/reference compatibility tests

Add tests specifically inspired by `anthropic-computer-use-modal`, OpenAI computer-use loops, Daytona parity, and E2B desktop ergonomics:

- OpenAI action JSON fixtures parse into the generic action schema, including `click`, `double_click`, `scroll`, `type`, `wait`, `keypress`, `drag`, `move`, `screenshot`, multi-action turns, and modifiers.
- Anthropic action JSON fixtures parse into the generic action schema, including enhanced actions such as `scroll`, `left_mouse_down`, `left_mouse_up`, `hold_key`, `wait`, `triple_click`, and `zoom` where supported.
- Coordinate-less click actions execute at the current cursor position.
- `left_click_drag` queries current cursor position when no start coordinate is provided.
- Screenshot adapter returns provider-shaped output while preserving native screenshot metadata.
- Browser prewarm example starts and captures a first screenshot without requiring model credentials.
- Artifact helpers can list/download screenshots and recordings via daemon APIs and Sandbox filesystem APIs.
- A test confirms no core package import requires `anthropic` or `openai`.
- A test confirms the default implementation does not import or instantiate `modal.NetworkFileSystem`.

### 18.7 Benchmarks and acceptance budgets

Add a benchmark suite early because the public Modal reference repo explicitly warns that Modal-translated tool calls are slow, and this project's daemon design should prove it improves the hot path.

Suggested benchmark cases:

| Benchmark | Metric | Initial target |
|---|---:|---:|
| cold create to daemon ready | seconds | reported, not gated in v0 |
| warm attach to health check | milliseconds/seconds | < 5 s in common cases |
| screenshot full screen | milliseconds | measure p50/p95 |
| compressed screenshot | milliseconds and bytes | measure p50/p95 and size |
| move+click action | milliseconds | measure p50/p95 |
| type 100 characters | milliseconds | measure p50/p95 |
| batch of 5 actions | milliseconds | faster than 5 separate HTTP round trips |
| equivalent `Sandbox.exec` actions | milliseconds | daemon should be faster on hot path |
| recording start/stop | milliseconds plus file size | measure correctness and overhead |

Benchmark rules:

- Report Modal region, image variant, resource profile, browser choice, and whether GPU is enabled.
- Separate daemon time from SDK/network time.
- Keep benchmark scripts runnable without model-provider API keys.
- Include a comparison mode that uses `Sandbox.exec` for equivalent actions, so the daemon choice is justified empirically.
- Every public release should include or regenerate a benchmark report.

## 19. Documentation plan

### 19.1 README

README should include:

1. What `modal-computer-use` is.
2. Why Modal users need it.
3. Install command.
4. Modal authentication note.
5. 20-line quickstart.
6. Local daemon quickstart.
7. noVNC viewing example with view-only/control warning.
8. OpenAI/Anthropic adapter examples.
8. Security warnings.
9. Current limitations.
10. Direct SDK mode vs optional deployed manager mode.
11. Why this project differs from `anthropic-computer-use-modal`.
12. Storage guidance: Sandbox filesystem APIs and Volumes, not new NFS.
13. Storage guidance: Sandbox filesystem APIs and Volumes, not new NFS.
14. Performance profile guidance: browser prewarm, optional GPU, screenshot processing, action batching.
15. Trace/replay debugging example.

### 19.2 Architecture doc

Explain:

- Modal Sandbox + image + daemon.
- Control channel vs view channel.
- Process supervisor.
- Screenshot/action loop.
- Coordinate spaces.
- Persistence, recordings, artifacts, and traces.
- Direct mode vs manager mode.
- Security boundaries.

### 19.3 API doc

Document Python SDK and HTTP API.

### 19.4 Security doc

Document:

- Connect Tokens.
- Local auth mode.
- noVNC sensitivity and view-only/control distinction.
- Secrets.
- Network controls.
- Agent confirmation hooks.
- Recording/trace data retention.
- Log redaction.
- Artifact safety.

### 19.5 Troubleshooting doc

Common issues:

- Sandbox fails readiness.
- `/healthz` passes but `/readyz` fails.
- Xvfb not responding.
- noVNC tunnel opens but shows blank screen.
- Keyboard input not appearing.
- Unicode typing issues.
- Screenshots black/empty.
- Coordinate mismatch after screenshot scaling.
- Recording file corrupted because ffmpeg was killed hard.
- Modal timeout/idle timeout.
- VNC URL shared accidentally.
- Volume artifacts not visible until sync/commit/reload.

### 19.6 Comparison doc

`docs/comparison.md` should explain:

- When to use Daytona.
- When to use E2B.
- When to use `anthropic-computer-use-modal`.
- When `modal-computer-use` is preferable.
- Which facts are documented and which are implementation inferences.

---


## 20. Versioned roadmap

### 20.1 v0.1: daemon-backed Modal desktop MVP

Included features:

- Owned Modal image helper.
- `computer-use-daemon` with `/healthz`, `/readyz`, `/v1/version`.
- Xvfb + XFCE/Openbox + x11vnc + optional noVNC.
- `ComputerSandbox.create`, `attach`, `terminate`, context-manager support.
- Connect Token HTTP client.
- Mouse: move, click, position.
- Keyboard: type, press, hotkey.
- Screenshots: full and region, PNG only initially.
- Basic status with display dimensions and process readiness.
- Path-safe artifacts: list, read_bytes, write_bytes, download.
- Structured errors with `call_id`.
- Local daemon client for tests: `DaemonClient(base_url=...)` and `ComputerSandbox.local(...)`.
- Unit tests, local Xvfb integration tests, one Modal smoke test.

Excluded features:

- Recordings.
- Provider adapters.
- Warm pool.
- GPU/browser prewarm.
- Snapshots beyond documentation.
- Recording dashboard.
- Replay CLI.
- OpenTelemetry.
- Full Daytona process restart/log parity.

Success criteria:

- Fresh Modal account can run quickstart.
- Screenshot-click-type-screenshot loop works.
- noVNC optional URL works when explicitly enabled.
- No core import requires OpenAI or Anthropic.
- No `NetworkFileSystem` usage.
- Artifact traversal tests pass.

Tests required:

- Coordinate validation.
- Keyboard normalization.
- Screenshot dimensions.
- Path traversal/symlink escape rejection.
- Health/readiness behavior.
- Connect Token refresh mock.
- Modal create/terminate smoke test.

Example code that should work:

```python
from modal_computer_use import ComputerSandbox, ComputerConfig

with ComputerSandbox.create(
    config=ComputerConfig(desktop={"resolution": (1440, 900)}),
    expose_vnc="view_only",
) as computer:
    computer.wait_until_ready()
    computer.mouse.click(100, 100)
    computer.keyboard.hotkey("ctrl", "l")
    computer.keyboard.type("https://example.com")
    computer.keyboard.press("enter")

    shot = computer.screenshots.full()
    shot.save("screen.png")
```

Competitor parity level:

- Near E2B minimal desktop primitive loop.
- Behind Daytona on recordings/process/display full parity.
- Ahead of `anthropic-computer-use-modal` only on primitive-first/daemon-first architecture, not feature completeness yet.

### 20.2 v0.2: Daytona-core parity plus provider action compatibility

Included features:

- Process status/restart/logs/stderr.
- Display info and windows list.
- Recordings start/stop/list/get/delete/download.
- Screenshots: PNG/JPEG/WebP, quality, scale, cursor overlay, artifact-backed capture.
- CoordinateSpace model and screenshot metadata.
- Clipboard get/set/clear.
- Action batch executor with wait, per-action timeout, per-action results, final screenshot.
- `input.release_all()`.
- OpenAI adapter with exact action fixtures.
- Anthropic adapter with versioned action registry.
- Trace NDJSON writer.
- Basic replay dry-run validator.
- Browser/app helpers: `browser.open_url`, `apps.launch`.
- Benchmark CLI comparing daemon vs separate HTTP calls vs `Sandbox.exec`.

Excluded features:

- Warm pool in core.
- Full local GUI dashboard.
- OTel spans beyond optional structured logs.
- DOM automation as a core primitive.
- Windows/macOS.

Success criteria:

- Daytona Linux primitive categories are covered or explicitly marked unsupported.
- OpenAI and Anthropic fixture tests pass.
- Batch of five simple actions is measurably faster than five separate HTTP calls.
- Recording download works and corrupted ffmpeg stop case is tested.
- Trace file can be replay-validated without model credentials.

Tests required:

- Daytona-like primitive parity tests.
- OpenAI action fixture tests.
- Anthropic `20241022`, `20250124`, `20251124` fixture tests.
- Batch stop-on-error/continue-on-error tests.
- Recording lifecycle tests.
- Display/windows tests.
- Clipboard tests.
- Benchmark report generation.

Example code that should work:

```python
from modal_computer_use import ComputerSandbox
from modal_computer_use.adapters.openai import OpenAIAdapter

computer = ComputerSandbox.create()
computer.wait_until_ready()

adapter = OpenAIAdapter(computer)

result = adapter.apply_many(
    [
        {"type": "move", "x": 300, "y": 240},
        {"type": "click", "x": 300, "y": 240, "button": "left"},
        {"type": "type", "text": "hello"},
        {"type": "wait", "duration_ms": 500},
    ],
    screenshot_after=True,
)

print(result.screenshot.width, result.screenshot.height)
computer.terminate()
```

Competitor parity level:

- Strong Daytona Linux Computer Use parity.
- Better than the Anthropic Modal reference repo for provider-neutral primitive architecture.
- Still behind E2B on polished desktop streaming/open/launch/window ergonomics unless browser/app helpers are solid.

### 20.3 v1.0: production-grade Modal-native computer-use harness

Included features:

- Stable OpenAPI and SDK API.
- `ComputerSandboxManager` deployed Modal class.
- `attach_or_create` with config hash, owner, TTL, cleanup.
- Volume-backed artifacts/recordings with explicit sync.
- Snapshot example and documented limitations.
- Warm pool example/helper with lease TTL.
- Browser profiles: base/browser/browser-gpu.
- noVNC view-only/manual takeover mode.
- Trace/replay CLI with screenshot/artifact references.
- Policy hooks and human-confirmation examples.
- Optional OpenTelemetry.
- Full docs: architecture, security, troubleshooting, performance, adapters.
- CI: unit, local integration, optional Modal smoke, adapter fixtures, benchmark report.

Excluded features:

- Mandatory chat UI.
- Provider-owned message server in core.
- Windows/macOS.
- DOM automation as core.
- Persistent bash/editor agent runtime as core.
- Full remote desktop product.

Success criteria:

- Users can build, test, and deploy from README.
- No provider dependency in core.
- No deprecated Modal NFS usage.
- Security docs clearly explain noVNC, screenshots, recordings, credentials, network, and policy hooks.
- Trace/replay catches coordinate bugs in fixtures.
- Benchmarks justify daemon-first design.
- Modal manager can create/list/attach/terminate sandboxes reliably.

Example code that should work:

```python
from modal_computer_use import ComputerSandbox, ComputerConfig
from modal_computer_use.adapters.anthropic import AnthropicAdapter

computer = ComputerSandbox.attach_or_create(
    run_id="ticket-123",
    config=ComputerConfig(
        desktop={"resolution": (1440, 900)},
        browser={"kind": "firefox", "prewarm": True},
        actions={"trace_actions": True},
    ),
    expose_vnc="view_only",
)

adapter = AnthropicAdapter(
    computer,
    tool_version="computer_20251124",
    enable_zoom=True,
)

adapter.apply({"action": "mouse_move", "coordinate": [500, 300]})
adapter.apply({"action": "left_click"})
screenshot = adapter.apply({"action": "screenshot"})

computer.artifacts.sync()
computer.terminate()
```

Competitor parity level:

- Daytona Linux primitive parity.
- E2B-like basic desktop/file/browser ergonomics for Modal users.
- Clearly ahead of the reference repo as a reusable primitive library.
- Differentiated on trace/replay, Modal-native persistence/snapshots/warm pools, typed adapters, and open-source daemon protocol.

## 21. Minimal implementation snippets

### 21.1 SDK quickstart

```python
from modal_computer_use import ComputerSandbox, ComputerConfig

computer = ComputerSandbox.create(
    name="demo-desktop",
    config=ComputerConfig(
        desktop={"resolution": (1440, 900)},
        runtime={"timeout_seconds": 1800},
    ),
    expose_vnc="view_only",
)
computer.wait_until_ready()

print({"remote_view_enabled": computer.debug.urls().vnc is not None})

computer.mouse.click(100, 100)
computer.keyboard.hotkey("ctrl", "l")
computer.keyboard.type("https://example.com")
computer.keyboard.press("enter")

shot = computer.screenshots.full(format="png")
shot.save("example.png")

computer.terminate()
```


### 21.2 Local daemon quickstart

```python
from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(
    base_url="http://127.0.0.1:8080",
    token="dev-token",
)
computer.wait_until_ready()
computer.mouse.click(100, 100)
print(computer.screenshots.full().width)
```

### 21.3 OpenAI-style loop skeleton

```python
import base64
from openai import OpenAI
from modal_computer_use import ComputerSandbox, ComputerConfig
from modal_computer_use.adapters.openai import OpenAIAdapter

client = OpenAI()
computer = ComputerSandbox.create(config=ComputerConfig(desktop={"resolution": (1440, 900)}))
computer.wait_until_ready()
adapter = OpenAIAdapter(computer)

response = client.responses.create(
    model="<computer-use-capable-model>",
    tools=[{"type": "computer"}],
    input="Open Firefox and verify the homepage loads. Use the computer tool for UI interaction.",
)

while True:
    computer_call = next((item for item in response.output if item.type == "computer_call"), None)
    if computer_call is None:
        break

    adapter.apply_many(computer_call.actions)
    screenshot = computer.screenshots.full(format="png")

    response = client.responses.create(
        model="<computer-use-capable-model>",
        tools=[{"type": "computer"}],
        previous_response_id=response.id,
        input=[
            {
                "type": "computer_call_output",
                "call_id": computer_call.call_id,
                "output": {
                    "type": "computer_screenshot",
                    "image_url": f"data:image/png;base64,{screenshot.to_base64()}",
                    "detail": "original",
                },
            }
        ],
    )

computer.terminate()
```

This is an example, not core behavior. The user application owns model calls, confirmation policy, and domain allowlists.

### 21.4 Anthropic compatibility sketch

```python
from modal_computer_use import SandboxRegistry, ComputerConfig
from modal_computer_use.adapters.anthropic import AnthropicAdapter

registry = SandboxRegistry(app_name="modal-computer-use")
computer = registry.attach_or_create(
    run_id="run-123",
    config=ComputerConfig(desktop={"resolution": (1024, 768)}),
)

adapter = AnthropicAdapter(computer)

# Tool inputs come from Anthropic; model calls are owned by the user app.
adapter.apply({"action": "mouse_move", "coordinate": [300, 200]})
adapter.apply({"action": "left_click"})
shot_result = adapter.apply({"action": "screenshot"})
```

This reproduces the useful behavior of `anthropic-computer-use-modal` while keeping the primitive implementation provider-neutral.

### 21.5 Daemon route example

```python
from fastapi import APIRouter
from modal_computer_use.daemon.schemas import ClickRequest, Point

router = APIRouter(prefix="/v1/mouse")

@router.post("/click", response_model=Point)
async def click(req: ClickRequest) -> Point:
    display = get_display_info()
    validate_coordinate(req.x, req.y, display.width, display.height)
    async with input_lock:
        await xdotool_click(req.x, req.y, req.button, req.double, req.modifiers)
    return Point(x=req.x, y=req.y)
```


### 21.6 Trace/replay sketch

```python
from modal_computer_use.tracing import ComputerTrace

trace = ComputerTrace.load("artifacts/traces/actions.ndjson")
trace.validate()

with ComputerSandbox.create(config=trace.suggested_config()) as computer:
    trace.replay(computer, dry_run=False)
```

---

## 22. Risks and mitigations

| Risk | Mitigation |
|---|---|
| X11 tools are flaky under concurrent calls. | Serialize input actions; add timeouts and retries for display readiness. |
| Unicode typing through xdotool is unreliable. | Use clipboard-paste fallback for Unicode/multiline text. |
| noVNC exposes a powerful live desktop. | Make noVNC opt-in, use encrypted tunnel, generate password, support view-only mode, redact URLs. |
| Daemon API can type/click arbitrary UI. | Use Modal Connect Tokens and no public raw control endpoint by default. |
| Local daemon auth accidentally exposed. | Bind local mode to localhost by default and require `COMPUTER_USE_LOCAL_TOKEN` for non-test runs. |
| Cold starts are too slow for agents. | Prebuild image, keep image small, use snapshots, optional warm pool. |
| Recordings consume CPU/disk. | Default fps 12, max duration, status visibility, explicit stop/delete, optional Volume. |
| Screen coordinates mismatch model screenshot size. | Preserve native resolution by default; provide explicit coordinate transforms and metadata for every screenshot. |
| Modal API changes. | Pin supported Modal SDK range; integration tests; isolate Modal calls in `sandbox.py`. |
| Sandbox lifetime capped/idle timeout. | Surface timeout config; document snapshots/volumes for longer workflows. |
| Prompt injection from screen content. | Keep policy above core; provide examples/hooks that treat screen content as untrusted. |
| Action batching hides partial failure. | Return per-action results, stop on first error by default, and require explicit `continue_on_error`. |
| Low-level down/up or hold actions can leave input stuck after errors. | Always implement down/up/hold with try/finally release logic and recovery endpoints. |
| Artifact API becomes a filesystem escape hatch. | Enforce relative paths in SDK and daemon; reject traversal and symlink escapes; stream large files safely. |
| Volumes appear stale. | Provide `artifacts.sync()` and document commit/reload semantics. |
| Provider schemas drift. | Version adapters, add fixtures, and fail closed on unknown actions. |
| Trace files leak secrets. | Redact typed/clipboard text and tokens by default; store hashes and lengths. |
| GPU/browser profiles raise cost unexpectedly. | Make GPU opt-in and expose resolved resources/cost-affecting settings in status. |

---

## 23. Release criteria

Before public release:

- Python package installs cleanly with `pip install modal-computer-use`.
- README quickstart works on a fresh Modal account with configured credentials.
- Local daemon quickstart works without Modal credentials.
- Modal image build is deterministic and documented.
- All Daytona-like Linux primitive categories are implemented or explicitly marked not implemented.
- Core routes have typed request/response models.
- OpenAPI schema is generated and checked into docs or build artifacts.
- Errors are structured and user-actionable.
- `/healthz`, `/readyz`, `/v1/version`, and `/v1/capabilities` are implemented.
- noVNC is opt-in, password-protected, and can be view-only where supported.
- Recording download streams reliably.
- Unit tests and local integration tests pass in CI.
- Modal smoke tests pass in a protected CI environment or documented manual test.
- Batch action route is implemented and tested.
- `input.release_all()` is implemented and tested.
- Artifact API path safety is implemented and tested.
- Trace NDJSON writer and validator are implemented.
- OpenAI adapter fixture matrix passes.
- Anthropic adapter covers all actions supported by `anthropic-computer-use-modal`, including `middle_click` and `cursor_position`, and documents enhanced action support by schema version.
- Security doc is published with clear warnings.
- `NetworkFileSystem` is not used in v1 core implementation.
- Browser prewarm/GPU behavior is documented as optional.
- CI or release checklist verifies all Modal API calls against the pinned supported Modal SDK range, including `Sandbox.create` parameters, readiness probes, `create_connect_token`, encrypted ports, filesystem APIs, Volume sync behavior, and snapshot helper.
- Benchmark output is generated for release candidates.

---


## 24. Recommended first PR sequence

1. **PR 1: repo scaffold and schemas**
   - `pyproject.toml`, package skeleton, daemon skeleton, Pydantic models, action discriminated unions, errors, README stub.
2. **PR 2: local daemon runner and tests**
   - Local Xvfb/daemon runner, `/healthz`, `/readyz`, `/v1/version`, `DaemonClient`, `ComputerSandbox.local()`, local integration tests.
3. **PR 3: Modal image and sandbox create**
   - `default_image()`, nested `ComputerConfig`, `ComputerSandbox.create()`, connect-token transport, noVNC opt-in/view-only/control config.
4. **PR 4: daemon supervisor and lifecycle/process API**
   - Xvfb/XFCE/x11vnc/noVNC startup, process status/restart/log/stderr routes, capabilities.
5. **PR 5: mouse, keyboard, clipboard, input recovery, and screenshot core**
   - Core event serialization, coordinate/key validation, `input.release_all`, screenshot variants, coordinate metadata, client-side screenshot post-processing option.
6. **PR 6: action batch, artifacts, trace skeleton**
   - Batch endpoint, idempotency, budgets, artifact layout, manifest, trace NDJSON append.
7. **PR 7: recordings, display, windows, browser/apps**
   - Daytona parity features for recordings, display info/windows, app launch/browser URL helpers, path-safe artifact APIs.
8. **PR 8: Modal manager, state, and persistence**
   - Optional `ComputerSandboxManager`, `run_id` tags, config hash, attach/recover flows, Volume-backed artifact example, `artifacts.sync()`.
9. **PR 9: adapters, examples, docs, and security**
   - OpenAI/generic/Anthropic adapters, fixtures, `anthropic_message_server.py`, noVNC example, recording example, security/policy docs.
10. **PR 10: performance and production examples**
   - Browser prewarm, optional GPU, warm-pool example, snapshot example, benchmarks, release checklist.

## 25. References reviewed

Official/user-provided docs and reference implementation sources:

1. Daytona Computer Use docs: https://www.daytona.io/docs/en/computer-use/
2. Daytona Python SDK ComputerUse docs: https://www.daytona.io/docs/en/python-sdk/sync/computer-use/
3. E2B Computer Use docs: https://e2b.dev/docs/use-cases/computer-use
4. E2B Desktop repository: https://github.com/e2b-dev/desktop
5. Modal guide: https://modal.com/docs/guide
6. Modal examples: https://modal.com/docs/examples
7. Modal API reference: https://modal.com/docs/reference
8. Modal Sandboxes guide: https://modal.com/docs/guide/sandboxes
9. Modal Running commands in Sandboxes: https://modal.com/docs/guide/sandbox-spawn
10. Modal Sandbox networking and security: https://modal.com/docs/guide/sandbox-networking
11. Modal Sandbox filesystem access: https://modal.com/docs/guide/sandbox-files
12. Modal Sandbox snapshots: https://modal.com/docs/guide/sandbox-snapshots
13. Modal Tunnels guide: https://modal.com/docs/guide/tunnels
14. Modal Volumes guide: https://modal.com/docs/guide/volumes
15. Modal `modal.Sandbox` reference: https://modal.com/docs/reference/modal.Sandbox
16. Modal `modal.Image` reference: https://modal.com/docs/reference/modal.Image
17. Modal Anthropic computer-use sandbox example: https://modal.com/docs/examples/anthropic_computer_use
18. Modal warm Sandbox pool example: https://modal.com/docs/examples/sandbox_pool
19. Modal Network File Systems deprecated guide: https://modal.com/docs/guide/network-file-systems
20. Modal `modal.NetworkFileSystem` reference: https://modal.com/docs/reference/modal.NetworkFileSystem
21. Modal changelog: https://modal.com/docs/reference/changelog
22. OpenAI Computer Use guide: https://developers.openai.com/api/docs/guides/tools-computer-use
23. Anthropic Computer Use tool docs: https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
24. `yasyf/anthropic-computer-use-modal` repository: https://github.com/yasyf/anthropic-computer-use-modal
25. `anthropic-computer-use-modal` README: https://raw.githubusercontent.com/yasyf/anthropic-computer-use-modal/main/README.md
26. Reference package metadata (`computer-use-modal`): https://raw.githubusercontent.com/yasyf/anthropic-computer-use-modal/main/pyproject.toml
27. Reference Modal image setup: https://raw.githubusercontent.com/yasyf/anthropic-computer-use-modal/main/computer_use_modal/app.py
28. Reference `SandboxManager`: https://raw.githubusercontent.com/yasyf/anthropic-computer-use-modal/main/computer_use_modal/sandbox/sandbox_manager.py
29. Reference `ComputerTool`: https://raw.githubusercontent.com/yasyf/anthropic-computer-use-modal/main/computer_use_modal/tools/computer/computer.py
30. Reference `ComputerUseServer`: https://raw.githubusercontent.com/yasyf/anthropic-computer-use-modal/main/computer_use_modal/server/server.py
31. Yasyf blog post, "Improving Claude Computer Use": https://musings.yasyf.com/improving-claude-computer-use/

---

## 26. Final recommendation

Build `modal-computer-use` as a **Modal-native computer-use harness**, not as another agent framework. The core value is making Modal Sandboxes feel like Daytona Computer Use: start a desktop, control it, see it, record it, inspect it, trace it, replay it, persist selected artifacts, and clean it up through a typed API.

The first public version should focus on Linux desktop control with excellent primitives and documentation. Model integrations, warm pools, snapshots, UI dashboards, provider-hosted servers, persistent bash sessions, and text-editor tools should be examples or optional modules until the primitive layer is stable.

The main v5 update is to turn the review into implementable best practices: split readiness from liveness, add local daemon mode, rename confusing API/config fields, make adapter compatibility explicit and versioned, add coordinate-space metadata, add stuck-input recovery, add browser/app/window helpers, formalize artifacts and traces, enforce budgets, and replace the broad milestone plan with a realistic v0.1/v0.2/v1.0 roadmap.

After the reference repo review, the most important design rule remains: **make orchestration Modal-native, but keep primitive execution daemon-native.** The repo should feel as easy to deploy as `computer-use-modal`, but its core API should feel like Daytona Computer Use rather than like an Anthropic message server. Borrow the reference repo's Modal-native session management, debug tunnels, browser/GPU optimization ideas, and Anthropic compatibility target. Do not copy its provider-first API, repeated Modal API hot path, Streamlit-centric demo shape, or NFS dependency.

The concrete refined target is: daemon-backed primitives, local and Modal modes, Modal registry/attach, artifact helpers, trace/replay, batch action execution, optional noVNC debugging, budget/security hooks, and provider adapters as extras. That is the smallest high-rigor scope that captures the reference repo's real-world lessons without over-scoping into an agent product.
