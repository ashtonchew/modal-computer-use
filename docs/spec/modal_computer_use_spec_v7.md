# `modal-computer-use`: Daytona-style computer-use primitives on Modal

**Status:** implementation specification of the shipped v0.1 daemon and SDK, post-hardening
**Prepared:** 2026-05-11 (v4 baseline), 2026-05-12 (v5/v6), 2026-05-14 (v7 truth-up)
**Revision:** v7, shipped-daemon hardening on top of the v6 UV-first architecture
**Recommended repository name:** `modal-computer-use`
**Recommended Python import name:** `modal_computer_use`

**v5 delta:** v5 added explicit liveness/readiness/version/capability endpoints; a local daemon/test mode; renamed and split public config models; a public `ComputerSandboxManager`; stricter Connect Token and noVNC rules; a coordinate-space model; provider-versioned OpenAI and Anthropic adapters; clipboard, browser, apps, windows, and stuck-input recovery primitives; artifact manifests with content hashes; an actionable trace/replay schema; budget controls; Volume sync semantics; compatibility fixtures; and a sharply scoped v0.1/v0.2/v1.0 roadmap.

**v6 delta:** v6 kept the v5 product architecture but made the repository UV-first. Local development, dependency resolution, editable installs, lockfile management, CI commands, docs, and examples use `uv` instead of raw `pip`. The repository ships `uv.lock`; documentation uses `uv sync --extra dev` / `uv run ...`. Modal image helpers still call Modal SDK methods named `pip_install` because that is Modal's image API surface.

**v6 → v7 delta:** v7 is not a feature release. The product surface from v6 has been built and is shipping; v7 *truth-ups the spec against what shipped*. Between 2026-05-12 14:30 and 2026-05-14 the maintainer landed nine `fix(...)` commits hardening daemon contracts and security boundaries: auth/readiness preflight, action batch idempotency conflict detection and batch deadlines, screenshot pixel budgets, X11 cleanup paths, redaction in logs and traces, artifact path safety (percent-decoded traversal, symlink rejection, control-path segregation), and a secret budget at the route layer. v7 records those contracts in the spec with file:line citations so reviewers can verify the implementation against documented invariants, replaces aspirational "should" language in shipped sections with descriptive "does", and adds an implementation truth table at §27 mapping every v6 contract to its source file and pinning test.

**Positioning:** this project is not intended to beat managed desktop-sandbox providers for every user. It is intended to be the best open-source, Modal-native computer-use primitive layer for users who want to own the image, daemon protocol, adapters, artifacts, traces, replay, and Modal scaling/persistence strategy.

---

## 0. v6 → v7 hardening: shipped contract truth-up

The table below maps each post-v6 commit to the contract it locked in. Section numbers in the **Evidence** column point to where the contract is documented in this v7 spec; file:line citations point to the implementation. v7 contains no new product features — the changes are all `fix(...)` commits.

| Commit | Area | What changed | Why | Evidence |
|---|---|---|---|---|
| `4187940` | Daemon auth / action preflight | AuthMiddleware now rejects query-string `_modal_connect_token`, requires loopback for local-token mode, and refuses non-trusted-proxy traffic when `require_connect_user` is set. Action validation rejects unknown keys and out-of-bounds coordinates before execution. | Query tokens leak through logs and URLs; verified-user headers from untrusted proxies cannot be trusted; running unvalidated actions ties up the input lock and may leave keys held. | §15.2, §15.6; `src/modal_computer_use/daemon/auth.py:14-109`; `src/modal_computer_use/daemon/routes/actions.py:442-499`; `tests/test_auth_security.py`. |
| `96814a9` | Readiness and input contracts | `/readyz` and the action runner now refuse work when the desktop backend is not ready; nested `hold_key` actions are re-parsed and validated; key validation runs over modifiers and nested actions; the input lock is acquired before any input-emitting primitive. | Liveness ≠ usability. A live HTTP server with a half-started X server caused stuck-input and zero-byte screenshots in v0.1 dogfood. | §9.1, §8.4, §15.6; `src/modal_computer_use/daemon/routes/actions.py:846-924`; `tests/test_daemon_routes.py`, `tests/test_daemon_validation.py`. |
| `3532b1c` | Redaction of sync and credentials | `daemon/logging.py` JSON formatter sanitizes the record message and redacts sensitive keys in the `extra` dict (sha256 + length, never value); artifact sync errors no longer echo mountpoint paths verbatim. | Stack traces in panicked sync routines were leaking artifact roots and tokens to stdout. | §15.3; `src/modal_computer_use/daemon/logging.py:12-72`; `src/modal_computer_use/artifacts.py:274-315`; `tests/test_artifacts.py`, `tests/test_auth_security.py`. |
| `8721d7e` | Secret and budget boundaries | The budget module is now route-aware: `reserve_action`, `reserve_screenshot`, `enforce`, and `idle_reservation_error` are called inline by every mutating route before the lock is taken. Schemas hide `text`, `clipboard_text`, `data_base64`, `bytes`, `image`, `password`, `token`, and `*_token` keys. | Budgets enforced only at batch end let runaway loops accrue cost; trace and log records included clipboard text and screenshot bytes. | §15.2, §15.5, §17.5; `src/modal_computer_use/daemon/budgets.py:11-202`; `src/modal_computer_use/daemon/routes/actions.py:142-188`; `tests/test_budgets.py`, `tests/test_trace_and_budgets.py`. |
| `90bbab7` | Trace redaction & batch guardrails | The action batch route serializes `TypeAction.text` to `{redacted: true, length, sha256}`; the redactor walks the whole action payload and emits a `redactions[]` list of JSON paths; provider actions land under `provider_action` with their own redaction pass; batch-too-large is a 413, idempotency conflicts are 409, action validation failures are 422. | A single redaction pass at the call site was missing nested actions and provider metadata; ambiguous status codes prevented retry clients from making safe decisions. | §11.3, §14.2, §14.3, §9.8; `src/modal_computer_use/daemon/routes/actions.py:1042-1171`; `tests/test_adapters.py`, `tests/test_trace_replay.py`. |
| `2f91cbf` | Screenshot budgets & X11 cleanup | Screenshot pixel budgets are pre-validated for both `screenshot`/`zoom` actions in a batch and for the optional `screenshot_after`. X11 backend now releases buttons/keys on every failure path; `release_all()` runs inside the timeout handler. | A 5000×5000 zoom at scale 4 could OOM the daemon; held buttons after a backend exception bricked the desktop. | §9.6, §15.6, §17.5; `src/modal_computer_use/daemon/routes/actions.py:216-258, 460-517`; `src/modal_computer_use/daemon/desktop/x11.py`; `tests/test_x11_backend.py`. |
| `1e3cd3c` | Desktop action primitives | Settings module reorganized; `redaction.py` extracted as a public module (`sanitize_text`, `safe_exception_payload`, `RedactedException`); keyboard validation rejects unsupported key names with `is_supported_key`; recording start gates on the recording-duration budget. | The redaction code was duplicated in three places, drifting out of sync; unknown key names were silently passed to `xdotool`, producing flaky tests. | §15.3, §11.1; `src/modal_computer_use/redaction.py:6-37`; `src/modal_computer_use/actions.py`; `src/modal_computer_use/daemon/settings.py:31-149`; `tests/test_recordings.py`, `tests/test_settings.py`. |
| `d5ff798` | Artifact controls | `normalize_artifact_path` decodes percent-encoding up to three times to defeat double-encoded traversal; rejects absolute paths, `..`, control characters, and `CONTROL_PATHS`/`CONTROL_SEGMENTS`; `ArtifactStore._reject_symlink_components` walks each path component for symlinks. Manifest and trace paths are non-public. | A relative path like `..%2F..%2Fetc%2Fpasswd` decoded once still escaped the root; symlinks in user-writable directories could escalate to anywhere on the sandbox filesystem. | §9.9, §16.6, §15.5; `src/modal_computer_use/artifacts.py:17-100`; `tests/test_artifacts.py`, `tests/test_daemon_validation.py`. |
| `7f24ccf` | Primitive safety contracts | Auth, action, apps, browser, keyboard, lifecycle, supervisor, tracing, and the trace-replay path all now fail closed on missing readiness, missing tokens, and unsupported actions. Trace validation refuses entries whose `normalized_action.text` is not a redaction marker. | A trace whose typed text was *not* redacted before serialization was indistinguishable from a benign trace; the replayer could re-type the secret. | §14.2, §14.3, §15.6, §17.5; `src/modal_computer_use/daemon/auth.py`; `src/modal_computer_use/tracing.py:20-770`; `tests/test_trace_replay.py`, `tests/test_auth_security.py`. |

The CI commit `64ffe9c ci(release): fail closed on missing modal smoke secrets` is operational, not a contract change, and is listed here for completeness.

---

## 1. Executive summary

`modal-computer-use` is a thin, high-rigor open-source wrapper that turns a Modal Sandbox into a remotely controllable Linux desktop with a stable SDK and API surface modeled after Daytona's Computer Use primitives.

The repository does not try to be a full agent framework. It provides the substrate that agents need: a desktop, a screenshot loop, mouse and keyboard controls, recording, display/window metadata, lifecycle/process management, safe networking, traceable actions, reproducible artifacts, and strongly typed client APIs. The LLM loop lives in examples and optional adapters, not in the core package.

As of v7, the **v0.1 primitive layer has shipped and been hardened** (see §0). The daemon, SDK namespaces, OpenAI and Anthropic adapters, trace writer and validator, budget enforcement, redaction, and artifact storage are in `src/modal_computer_use/` with pinning tests in `tests/`. v0.2 work (replay CLI completion, recording dashboard, full provider fixture matrix) and v1.0 work (warm-pool helper, snapshot examples, OpenTelemetry, deployable manager) remain in the roadmap (§20).

The shipped implementation is:

1. **A Modal-managed Linux Sandbox** launched with a custom Modal `Image` that contains X11 desktop dependencies: Xvfb, XFCE or a minimal window manager, x11vnc, noVNC/websockify, xdotool, wmctrl, maim, ffmpeg, xclip/xsel, and the package's daemon.
2. **An in-sandbox daemon** (`computer-use-daemon`) listening on port `8080`, exposed through Modal Sandbox Connect Tokens. The daemon owns process supervision (`src/modal_computer_use/daemon/supervisor.py:13`), input serialization (`src/modal_computer_use/daemon/app.py:68` — `app.state.input_lock = asyncio.Lock()`), screenshots, recordings, display/window inspection, and structured logs (`src/modal_computer_use/daemon/logging.py`).
3. **A Python client SDK** (`src/modal_computer_use/sandbox.py`, `src/modal_computer_use/namespaces/`) that creates or attaches to Modal Sandboxes, obtains connect tokens, calls the daemon over HTTP, optionally exposes a noVNC tunnel for manual viewing, and presents a Daytona-like API.
4. **A session and artifact layer** generalized around one canonical `run_id`, sandbox lookup by ID/name/tags, safe artifact roots, optional Volume persistence, and explicit cleanup semantics.
5. **Optional adapters** for OpenAI Computer Use actions (`adapters/openai.py`), Anthropic-style action schemas (`adapters/anthropic/computer.py`), and generic tool-calling actions (`adapters/generic.py`).
6. **Performance profiles** for browser-heavy agents: browser prewarm during image build/startup, optional GPU, raw-screenshot fast paths, client/control-plane screenshot post-processing, and warm-pool examples in `examples/04_warm_pool.py`.
7. **Production defaults:** secure-by-default API access via Modal Connect Tokens, no exposed raw control API by default, optional encrypted noVNC tunnel with generated password, strict coordinate/key validation, serialized input events, call IDs, structured logs, artifact path restrictions, and recording retention controls.

---

## 2. Source-grounded current state

(Sections 2.1–2.5 from v6 carry forward unchanged; the Daytona, E2B, Modal, and reference-repo comparisons are still accurate. The summary below replaces v6's product positioning paragraph.)

### 2.1 Daytona Computer Use primitives

Daytona Computer Use exposes programmatic desktop control inside sandboxes: lifecycle, process management, mouse, keyboard, screenshots, screen recording, and display operations. The Daytona implementation starts Xvfb, xfce4, x11vnc, and noVNC. Daytona's docs frame VNC as the human visual interface and Computer Use as the programmatic API.

### 2.2 E2B Computer Use primitives

E2B's docs describe agents that operate Ubuntu 22.04 XFCE desktops through screenshots, clicks, typing, scrolling, and VNC streaming.

### 2.3 Modal primitives

Modal Sandboxes, `Sandbox.create`, custom Images, runtime command execution (`Sandbox.exec`), Connect Tokens, Tunnels, Filesystem APIs, Snapshots, Volumes, tags, and readiness probes are the primitives this package composes.

### 2.4 Modal examples close to computer use

`anthropic_computer_use` and the warm Sandbox pool examples are the closest official Modal references; both inspired the orchestration patterns documented in §17.

### 2.5 Reference implementation `yasyf/anthropic-computer-use-modal`

Validates Modal as a computer-use substrate. v7 still treats it as a source of operational patterns, not core architecture (see v6 §2.5 for the long-form analysis).

---

## 3. Design goals and non-goals

### 3.1 Goals

1. **Expose high-quality computer-use primitives on Modal.** Users do not manually wire Xvfb, VNC, screenshots, ffmpeg, or xdotool.
2. **Mirror Daytona's practical primitive surface.**
3. **Stay agent-model agnostic.** Works with OpenAI Computer Use, Anthropic, custom vision models, browser agents, QA agents, and deterministic scripts.
4. **Preserve Modal-native advantages.** Sandboxes, Images, Connect Tokens, Tunnels, Filesystem API, Snapshots, Volumes, tags, and readiness probes.
5. **Be safe by default.** The daemon is reached through Connect Tokens; noVNC is opt-in; secrets and typed text are redacted in logs and traces. See §15.
6. **Be deterministic and observable.** Validate inputs, serialize GUI events, return structured results, expose process logs/errors, make screenshots/recordings inspectable.
7. **Support both direct SDK mode and deployed manager mode.**
8. **Support provider compatibility without provider lock-in.**
9. **Prefer current Modal storage primitives.** `modal.Volume` and Sandbox filesystem APIs; no `NetworkFileSystem`.
10. **Make performance visible.** Return timing metadata; expose action batching; document tradeoffs.
11. **Make replay/debugging excellent.** Redaction-aware traces, artifact manifests, hashes, coordinate metadata, replay validation.
12. **Ship as a small library with strong tests.** The shipped v0.1 has ~25 test modules; see §27.

### 3.2 Non-goals

(Unchanged from v6.) No full autonomous agent framework in core; no Windows/macOS in v1; no DOM automation as a core primitive; no hidden credential management; no generic remote desktop product; no overbroad network permissions; no provider-first server API in core; no new `NetworkFileSystem` dependency; no forced GPU/browser bundle; no mandatory deployed service; no silent coordinate scaling; no implicit public debug channels.

---

## 4. Best-practice implementation plan

(§4.1–§4.10 narrative carries forward from v6; the design rationale has not changed. The summary below pulls the shipped state forward.)

### 4.1 Core architectural choice: in-sandbox daemon over repeated `Sandbox.exec`

The daemon-first decision is in production. `Sandbox.exec` is available for bootstrap/debug only.

### 4.2 Desktop stack

Default stack: `Xvfb :99 -screen 0 {WIDTH}x{HEIGHT}x24 -nolisten tcp`, XFCE (default) or Openbox (light), `x11vnc` bound to localhost, noVNC/websockify on `6080`, `computer-use-daemon` on `8080`. Default resolution `1440x900`; `1280x720` and `1600x900` supported. Process startup is centralized in `src/modal_computer_use/daemon/supervisor.py:30-73`.

### 4.3 Transport model

Two channels — daemon control on port `8080` via Modal Connect Tokens, and optional noVNC view on `6080` via `encrypted_ports`. The daemon is never exposed via `encrypted_ports` by default.

### 4.4 API shape

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
```

Shipped namespaces (`src/modal_computer_use/namespaces/`): `mouse`, `keyboard`, `clipboard`, `screenshots`, `recordings`, `display`, `windows`, `processes`, `actions`, `artifacts`, `browser`, `apps`, `input`, `commands`, `debug`, `session`, `lifecycle`.

### 4.5 Package boundaries

Core extras as advertised: `modal-computer-use[openai]`, `modal-computer-use[anthropic]`, `modal-computer-use[dev]`. Core has no `openai` or `anthropic` imports; this is enforced by `tests/test_imports.py`.

### 4.6–4.10 (unchanged from v6)

Refinements from the reference repo, batch actions as a first-class primitive, local development mode, and naming/semantic rules adopted in v5 carry forward without change.

---

## 5. Repository layout

The shipped layout under `src/modal_computer_use/` is:

```text
src/modal_computer_use/
  __init__.py
  _version.py
  actions.py             — key normalization, normalize_actions, transform_point
  artifacts.py           — ArtifactStore, normalize_artifact_path, CONTROL_PATHS/SEGMENTS
  benchmarks.py          — benchmark CLI internals
  cli.py                 — `computer-use` CLI entrypoint
  client.py              — HTTP transport client wrapper
  config.py              — image profiles, retention classes
  errors.py              — ArtifactPathError, BudgetExceededError, UnsupportedActionError, ActionValidationError
  image.py               — image utilities
  manager.py             — SandboxManager
  models.py              — Pydantic models (Point, Region, ComputerAction discriminated union, TraceEntry, …)
  observability.py       — OptionalTracer / OpenTelemetry shim
  redaction.py           — sanitize_text, safe_exception_payload, RedactedException
  registry.py            — adapter version registry
  sandbox.py             — ComputerSandbox: create/attach/local/wait_until_ready/snapshot/terminate
  state.py               — internal session state
  tracing.py             — TraceWriter, ComputerTrace, ReplayStep, TraceReplayPlan
  adapters/
    generic.py           — ActionExecutor + before_action/after_action hooks
    openai.py            — OpenAIAdapter
    output.py            — action_result_summary, screenshot_metadata
    provenance.py        — provider action redaction
    anthropic/
      computer.py        — AnthropicAdapter
      versions.py        — ANTHROPIC_TOOL_VERSIONS registry
      schemas.py
  daemon/
    app.py               — create_app(); registers 17 routers and AuthMiddleware
    auth.py              — AuthMiddleware (three modes)
    budgets.py           — BudgetKind, reserve_action, reserve_screenshot, enforce, rate-limit window
    errors.py            — DaemonError
    logging.py           — JSON formatter, redact()
    settings.py          — DaemonSettings (env-var contract)
    supervisor.py        — Xvfb/window-manager/x11vnc/noVNC supervisor
    desktop/             — backend implementations
      x11.py             — primary X11 backend
      recordings.py      — RecordingRegistry, ffmpeg control
      apps.py, browser.py, clipboard.py, displays.py, keyboard.py, mouse.py, processes.py, screenshots.py
    routes/              — 17 FastAPI routers
      actions.py, apps.py, artifacts.py, browser.py, clipboard.py, commands.py, debug.py,
      display.py, health.py, input.py, keyboard.py, lifecycle.py, mouse.py, processes.py,
      recordings.py, screenshots.py, session.py, windows.py
  namespaces/            — SDK-side namespace classes
  transports/            — http.py, local.py
```

---

## 6. Modal image specification

Sections 6.1, 6.2, 6.4 carry forward from v6. The environment-variable contract in §6.3 is replaced with the actual `DaemonSettings` fields shipped in `src/modal_computer_use/daemon/settings.py:31-149`.

### 6.1–6.2 (unchanged from v6)

Image builder helper, image ownership, browser prewarm/GPU, and the daemon entrypoint (`python -m modal_computer_use.daemon`) are unchanged.

### 6.3 Environment variables (shipped)

Every variable below is read by `DaemonSettings` and is the source of truth for daemon configuration. The defaults come from `src/modal_computer_use/daemon/settings.py:31-149`.

| Variable | Default | Field | Purpose |
|---|---:|---|---|
| `COMPUTER_USE_RUN_ID` | `None` | `run_id` | Stable run identifier in traces/logs. |
| `DISPLAY` | `:99` | `display` | X11 display. |
| `COMPUTER_USE_DESKTOP_WIDTH` | `1440` | `desktop_width` | Desktop width. |
| `COMPUTER_USE_DESKTOP_HEIGHT` | `900` | `desktop_height` | Desktop height. |
| `COMPUTER_USE_DESKTOP_DPI` | `96` | `desktop_dpi` | Desktop DPI. |
| `COMPUTER_USE_DISPLAY_DEPTH` | `24` | `display_depth` | Xvfb color depth. |
| `COMPUTER_USE_WINDOW_MANAGER` | `xfce` | `window_manager` | `xfce` or `openbox`. |
| `COMPUTER_USE_BROWSER` | `None` | `browser` | Optional browser command. |
| `COMPUTER_USE_BROWSER_PREWARM` | `false` | `browser_prewarm` | Initialize browser profile at startup. |
| `COMPUTER_USE_ARTIFACTS_DIR` | `/home/desktop/artifacts` | `artifacts_dir` | Artifact root. |
| `COMPUTER_USE_ARTIFACTS_PERSISTENT` | `false` | `artifacts_persistent` | Persist artifacts via Volume sync. |
| `COMPUTER_USE_ARTIFACTS_VOLUME_MOUNTED` | `false` | `artifacts_volume_mounted` | Verifies a Volume v2 mount before persistent sync. |
| `COMPUTER_USE_RECORDINGS_DIR` | `/home/desktop/recordings` | `recordings_dir` | Recording output directory. |
| `COMPUTER_USE_RUNTIME_DIR` | `/tmp/modal-computer-use` | `runtime_dir` | Runtime sockets/secrets. |
| `COMPUTER_USE_TRACE_DIR` | `/home/desktop/artifacts/traces` | `trace_dir` | Trace NDJSON directory. |
| `COMPUTER_USE_TRACE_ACTIONS` | `false` | `trace_actions` | Append redaction-aware action traces. |
| `COMPUTER_USE_SCREENSHOT_MAX_PIXELS` | `8_294_400` | `screenshot_max_pixels` | Per-screenshot output pixel cap (3840×2160 = 8.29 MP). |
| `COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION` | `auto` | `screenshot_processing_location` | `daemon`, `client`, or `auto`. |
| `COMPUTER_USE_POST_ACTION_DELAY_MS` | `100` | `post_action_delay_ms` | Delay between batched actions and before `screenshot_after`. |
| `COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS` | `5_000` | `default_action_timeout_ms` | Default per-action timeout. |
| `COMPUTER_USE_MAX_ACTION_TIMEOUT_MS` | `300_000` | `max_action_timeout_ms` | Hard cap on per-action timeout. |
| `COMPUTER_USE_IDEMPOTENCY_CACHE_MAX_ENTRIES` | `1_000` | `idempotency_cache_max_entries` | LRU cap for the in-process idempotency cache. |
| `COMPUTER_USE_IDEMPOTENCY_CACHE_TTL_SECONDS` | `3_600` | `idempotency_cache_ttl_seconds` | TTL for cached idempotent results. |
| `COMPUTER_USE_LOCAL_TOKEN` | `None` | `local_token` | Loopback-only bearer for dev. |
| `COMPUTER_USE_REQUIRE_CONNECT_USER` | `true` | `require_connect_user` | Enforce verified-user header from trusted proxy. |
| `COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY` | `false` | `trust_private_connect_proxy` | Opt-in trust for verified-user headers from private or link-local proxy addresses. |
| `COMPUTER_USE_REJECT_QUERY_TOKENS` | `true` | `reject_query_tokens` | Reject `?_modal_connect_token=...`. |
| `COMPUTER_USE_VNC_MODE` | `off` | `vnc_mode` | `off`, `view_only`, or `control`. |
| `COMPUTER_USE_VNC_PASSWORD` | `None` | `vnc_password` | Generated if absent and VNC is enabled. |
| `COMPUTER_USE_BACKEND` | `auto` | `backend` | `auto`, `x11`, or `mock` (test). |
| `COMPUTER_USE_MAX_BATCH_ACTIONS` | `50` | `max_batch_actions` | Per-batch action count cap. |
| `COMPUTER_USE_MAX_BATCH_DURATION_MS` | `30_000` | `max_batch_duration_ms` | Per-batch wall-clock cap. |
| `COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC` | `20` | `input_rate_limit_per_sec` | Sliding-window rate limit; `0` disables. |
| `COMPUTER_USE_OTEL_ENABLED` | `false` | `otel_enabled` | Opt-in OpenTelemetry spans. |
| `COMPUTER_USE_MAX_ACTIONS` | `None` | `max_actions` | Optional run-scoped action budget. |
| `COMPUTER_USE_MAX_SCREENSHOTS` | `None` | `max_screenshots` | Optional run-scoped screenshot budget. |
| `COMPUTER_USE_MAX_ARTIFACT_BYTES` | `None` | `max_artifact_bytes` | Optional run-scoped artifact byte budget. |
| `COMPUTER_USE_MAX_RECORDING_SECONDS` | `None` | `max_recording_seconds` | Optional run-scoped recording duration budget. |
| `COMPUTER_USE_MAX_IDLE_SECONDS` | `None` | `max_idle_seconds` | Optional run-scoped idle-timeout budget. |
| `COMPUTER_USE_IMAGE_PROFILE` | `standard` | `image_profile` | `standard`, `browser`, `browser-gpu`. |

**v6 → v7 changes:**
- `COMPUTER_USE_REQUIRE_CONNECT_USER` default is now `true` (was documented `false` in v6).
- `COMPUTER_USE_REJECT_QUERY_TOKENS` default is now `true`.
- `COMPUTER_USE_SCREENSHOT_MAX_PIXELS` default lowered to `8_294_400` (3840×2160) from v6's documented `10_000_000`.
- Added: `idempotency_cache_max_entries`, `idempotency_cache_ttl_seconds`, `max_batch_duration_ms`, `default_action_timeout_ms`, `max_action_timeout_ms`, `artifacts_volume_mounted`, `backend`, `max_idle_seconds`, `otel_enabled`, `image_profile`.

### 6.4 Desktop user

Unchanged from v6: `desktop` user, `/home/desktop` home, recordings/artifacts/Downloads under that root.

---

## 7. Sandbox creation specification

### 7.1 Main class

(Surface unchanged from v6.) `ComputerSandbox.create / attach / from_id / from_name / from_run_id / attach_or_create / local / wait_until_ready / terminate / detach / snapshot_filesystem / snapshot_directory / mount_image` is in `src/modal_computer_use/sandbox.py`.

### 7.2 Configuration model

Unchanged from v6. The Pydantic `ComputerConfig` with nested `DesktopConfig`, `RuntimeConfig`, `ResourceConfig`, `NetworkConfig`, `StorageConfig`, `BrowserConfig`, `ActionConfig`, and `BudgetConfig` is the public surface. Daemon-side defaults are mirrored in `DaemonSettings` (§6.3) so the daemon can be configured without an SDK.

### 7.3–7.7 (unchanged from v6)

Modal call sketch, Connect Token lifecycle, optional Modal orchestration manager, resource profiles, and attach-or-create semantics are unchanged.

---

## 8. Daemon architecture

### 8.1 Runtime responsibilities

The daemon owns: process supervision, input serialization, screenshots, recordings, display/window inspection, structured logs, and the trace writer. `create_app()` (`src/modal_computer_use/daemon/app.py:55-183`) constructs the FastAPI instance, installs `AuthMiddleware`, registers seventeen routers, wires five exception handlers, and starts the `Supervisor` in the `lifespan` context.

### 8.2 Process supervisor

`Supervisor` (`src/modal_computer_use/daemon/supervisor.py:13-169`) starts `Xvfb`, the window manager (`startxfce4` or `openbox`), `x11vnc` (when `vnc_mode != "off"`), and `websockify`/noVNC. It captures stdout/stderr to per-process files under `<artifacts_dir>/logs/`, restarts processes on request, and reports `ProcessStatus` for `/v1/processes/{name}/status`. The `mock` backend short-circuits without spawning OS processes for tests.

### 8.3 Startup sequence

`asynccontextmanager lifespan(app)` calls `supervisor.start()` before the app accepts traffic and `supervisor.stop()` at shutdown. `/healthz` returns 200 as soon as the FastAPI process is alive; `/readyz` returns 200 only when the backend reports the desktop is usable. Routes that take the input lock check readiness before acquiring the lock (`src/modal_computer_use/daemon/routes/actions.py:60-90`).

### 8.4 Input serialization

A single `asyncio.Lock` (`app.state.input_lock`) guards every input-emitting route — mouse, keyboard, clipboard, action batch. The lock is taken *after* validation and *after* idempotency-cache lookup so cache hits never queue behind real work. `release_all()` is called inside the `TimeoutError` and exception paths of the action runner (`routes/actions.py:218-219, 257-258`) to guarantee modifiers and buttons are released even when an action fails.

### 8.5 Stuck-input recovery

`POST /v1/input/release-all` is the manual recovery route; it calls `backend.release_all()` directly. Held-key/button state is tracked inside the X11 backend.

### 8.6 Error response shape

Every error response is a JSON object with three keys — `code`, `message`, `details` — and a deterministic HTTP status. The exception handlers in `app.py:105-159` map:

| Exception | Status | Code | Notes |
|---|---:|---|---|
| `DaemonError` | as raised | from exception | Catch-all for typed daemon errors. |
| `ArtifactPathError` | 400 | `unsafe_artifact_path` | Path validation failed (traversal, symlink, control path). |
| `BudgetExceededError` | 429 | `budget_exceeded` | Per-run budget hit. |
| `RequestValidationError` | 422 | `validation_error` | Pydantic validation; **`details.errors[*].input` is stripped to avoid leaking request bodies into log/trace.** |
| `FileNotFoundError` | 404 | `not_found` | Missing artifact, missing recording. |
| `Exception` (catch-all) | 500 | `internal_error` | Body is `safe_exception_payload(exc)` → `{redacted: true, type: <class>}` only. |

The full error-code catalog the daemon can emit is in §9.13.

### 8.7 Screenshot/artifact fast paths

The screenshot backend writes to the artifact store with a `retention_class` (`ephemeral`, `trace`, or `persistent`). When a screenshot is requested through the action batch route with `screenshot_after=True`, daemon-written traces record a `screenshot_after` metadata pseudo-action with dimensions, coordinate-space data, timing, and redaction paths. Raw `artifact_uri` values remain redacted and `screenshot_after_uri` remains `null` in new daemon traces; replay can preserve ordering and post-batch metadata without receiving a reusable artifact reference.

---

## 9. HTTP API specification

The daemon's HTTP API is versioned under `/v1`. `GET /healthz` and `GET /readyz` are intentionally unversioned because probes expect simple paths. All 17 routers are registered in `src/modal_computer_use/daemon/app.py:161-181`.

### 9.1 Health, version, capabilities, and lifecycle

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Daemon process liveness. No backend check. Skips auth. |
| `GET` | `/readyz` | Backend readiness. Skips auth. |
| `GET` | `/v1/version` | `VersionInfo` (api_version, daemon_version, sdk_min/max). |
| `GET` | `/v1/capabilities` | `Capabilities` (supported action types, screenshot formats, adapter versions, image profile). |
| `GET` | `/v1/computer/status` | Full `ComputerStatus` including process map. |
| `POST` | `/v1/computer/start` | Start missing desktop processes. Idempotent. |
| `POST` | `/v1/computer/stop` | Stop processes. Idempotent. |
| `POST` | `/v1/computer/restart` | Restart all managed processes. |

### 9.2 Processes routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/processes/{name}/status` | `ProcessStatus`. |
| `POST` | `/v1/processes/{name}/restart` | Restart one process. |
| `GET` | `/v1/processes/{name}/logs?tail=200` | Sanitized stdout tail. |
| `GET` | `/v1/processes/{name}/stderr?tail=200` | Sanitized stderr tail. |
| `GET` | `/v1/processes/{name}/errors?tail=200` | Deprecated sanitized alias for stderr. |

### 9.3 Mouse routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/mouse/click` | Click at coordinates or current cursor. |
| `POST` | `/v1/mouse/move` | Move cursor. |
| `POST` | `/v1/mouse/drag` | Start/end or path drag. |
| `POST` | `/v1/mouse/scroll` | Scroll by ticks. |
| `POST` | `/v1/mouse/down` | Press button. |
| `POST` | `/v1/mouse/up` | Release button. |
| `GET` | `/v1/mouse/position` | Read cursor position after readiness preflight. |

### 9.4 Keyboard routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/keyboard/type` | Type text. Validates Unicode and translates newlines into Enter; rejects control characters. |
| `POST` | `/v1/keyboard/press` | Single key with optional modifiers. |
| `POST` | `/v1/keyboard/hotkey` | Key sequence. |
| `POST` | `/v1/keyboard/hold` | Hold key while executing nested actions; nested actions are re-parsed and validated. |
| `GET` | `/v1/keyboard/keys` | Supported key names and aliases (`src/modal_computer_use/actions.py`). |

### 9.5 Clipboard routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/clipboard/text` | Read clipboard. |
| `PUT` | `/v1/clipboard/text` | Write clipboard. |
| `DELETE` | `/v1/clipboard/text` | Clear clipboard. |

Clipboard contents are sensitive: logs record length/hash, never the text itself (`daemon/logging.py:12-46`).

### 9.6 Screenshots routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/screenshots/full` | Full screenshot. |
| `POST` | `/v1/screenshots/region` | Region screenshot. |
| `POST` | `/v1/screenshots/zoom` | Crop and scale region. |

Every screenshot route enforces `screenshot_max_pixels` *before* capture, both in the action runner (`routes/actions.py:460-517`) and at the route layer (`enforce_screenshot_options_pixels` in `routes/screenshots.py`). Output is `png`/`jpeg`/`webp`. The response is `Screenshot` (data inline or `artifact://` URI).

### 9.7 Recordings routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/recordings` | Start recording. |
| `POST` | `/v1/recordings/{id}/stop` | Stop recording. |
| `GET` | `/v1/recordings` | List. |
| `GET` | `/v1/recordings/{id}` | Metadata. |
| `GET` | `/v1/recordings/{id}/download` | Stream video file. |
| `DELETE` | `/v1/recordings/{id}` | Delete. |

A second mounted router serves `/recordings/ui` (the dashboard) — `recordings.dashboard_router` in `daemon/app.py:180`.

Stop behavior: SIGINT → wait 5s → SIGTERM. Metadata is updated atomically.
Recording stop and delete enforce the idle budget before mutating recording state.

### 9.8 Action batch routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/actions/run` | Execute an ordered batch. |
| `POST` | `/v1/actions/validate` | Validate without executing. |

Supported types: `move`, `click`, `double_click`, `triple_click`, `drag`, `scroll`, `mouse_down`, `mouse_up`, `type`, `keypress`, `hotkey`, `hold_key`, `wait`, `screenshot`, `zoom`, `cursor_position`, `release_all`.

**v7 batch contract (`src/modal_computer_use/daemon/routes/actions.py:66-382`):**

- Batch size > `max_batch_actions` → 413 `batch_too_large`.
- Body `idempotency_key` and `Idempotency-Key` header that disagree → 409 `idempotency_key_conflict`.
- Validation failure → 422 `action_validation_failed` with `details.errors[]` listing each issue.
- The whole batch runs under one input-lock acquisition.
- Each action has a deadline = `min(action.timeout_ms or max_action_timeout_ms or default_action_timeout_ms, remaining batch budget)`. Timeout produces `error_code: "timeout"` with `output.scope = "action"` or `"batch"`.
- Budget reservations occur per action via `budgets.reserve_action(request)` (`routes/actions.py:142-165`); screenshot actions reserve via `budgets.reserve_screenshot`.
- Idempotency cache is keyed by `Idempotency-Key`; the cached entry's fingerprint is a SHA-256 over the request body excluding `idempotency_key`. Conflicting fingerprints reuse the key are rejected with 409.
- `continue_on_error` applies between top-level batch actions. Compound actions such as `hold_key` are atomic: nested actions stop on the first failure, the held key is released, and the failed compound action may be followed by later top-level actions when `continue_on_error=true`.
- On any timeout or backend exception, `await request.app.state.backend.release_all()` runs inside `with suppress(Exception)` before the result is recorded.

### 9.9 Artifact routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/artifacts?prefix=...` | List artifacts under a safe relative prefix. |
| `GET` | `/v1/artifacts/{path:path}` | Download. |
| `PUT` | `/v1/artifacts/{path:path}` | Write. |
| `DELETE` | `/v1/artifacts/{path:path}` | Delete. |
| `POST` | `/v1/artifacts/sync` | Idle-budget-gated Volume v2 mountpoint sync; returns `ArtifactSyncResult`. |
| `GET` | `/v1/artifacts/manifest` | Stream manifest entries. |

Path safety (full details §15.5; implementation in `src/modal_computer_use/artifacts.py:24-100`):

- Reject absolute paths, `~`, `..`, control characters, double-encoded traversal.
- Reject `CONTROL_PATHS` (`manifest.ndjson`, `traces/actions.ndjson`) and `CONTROL_SEGMENTS` (`.control`, `_control`, `.modal-computer-use`, `.secrets`, `logs`) on public reads/writes.
- Reject any path component that is a symlink, even if the final target is inside root.
- Every write returns `ArtifactInfo` with SHA-256, content type, size, and `artifact://` URI.

### 9.10 Display, windows, apps, and browser routes

(Unchanged from v6.) `GET /v1/display/info`, `GET /v1/windows`, `GET /v1/windows/active`, `POST /v1/windows/{id}/activate`, `POST /v1/windows/{id}/close`, `POST /v1/windows/wait-for`, `POST /v1/apps/launch`, `POST /v1/apps/open-artifact`, `POST /v1/browser/open-url`, `GET /v1/browser/status`.

### 9.11 Input recovery route

`POST /v1/input/release-all` — manual reset for held keys/buttons.

### 9.12 Commands, debug, and session routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/commands/run` | Input-lock-serialized command for terminal-style debug with sanitized output strings. |
| `GET` | `/v1/debug/urls` | Daemon-owned `DebugUrls` metadata; Modal tunnel URLs are orchestration-owned and exposed through `ComputerSandbox.debug_urls()`. |
| `GET` | `/v1/session/metadata` | Session metadata (run_id, started_at, …). |
| `POST` | `/v1/session/refresh` | Refresh internal session state. |

### 9.13 Action batch envelope, sequencing, and idempotency

`/v1/actions/run` accepts optional `call_id`, `sequence`, and `source` metadata for
audited/replayable action batches:

```json
{ "call_id": "call_...", "sequence": 42, "source": "openai-adapter" }
```

Idempotency cache (`daemon/routes/actions.py:96-107, 406-418`):

- Stored as `OrderedDict` on `app.state.idempotency_cache`.
- TTL: `idempotency_cache_ttl_seconds` (default 3600).
- LRU cap: `idempotency_cache_max_entries` (default 1000).
- Body-key vs header-key conflict → 409 `idempotency_key_conflict`.
- Fingerprint = SHA-256 of `model_dump(mode="json", exclude={"idempotency_key"}, sort_keys=True)`. Same key + different fingerprint → 409.

Action batch logs include `call_id`, route, duration, success/failure, `error_code`, and redaction metadata; they never include `text`, `clipboard_text`, `data_base64`, `image`, or tokens (see §15.3). Direct primitive routes keep their narrower request models; callers that need replay/audit metadata should use `/v1/actions/run`.

### 9.14 Error code catalog (v7 addition)

The daemon emits the following `code` values. Each is structured `{code, message, details}` with the HTTP status shown.

| Code | Status | Where raised | Meaning |
|---|---:|---|---|
| `query_token_rejected` | 401 | `auth.py:20` | Client sent `_modal_connect_token` as query string. |
| `local_token_requires_loopback` | 401 | `auth.py:31` | Local-token mode but client is not on loopback. |
| `unauthorized` | 401 | `auth.py:40` | Bearer token missing or mismatched. |
| `connect_token_required` | 401 | `auth.py:65, 73` | `require_connect_user` mode missing a verified-user header from a trusted proxy. |
| `invalid_verified_user_data` | 401 | `auth.py:85, 91` | Verified-user header is malformed or unrecognized. |
| `unsafe_artifact_path` | 400 | `app.py:113` (`ArtifactPathError`) | Path validation failed. |
| `budget_exceeded` | 429 | `budgets.py:181`; `app.py:122` | Run-scoped budget hit. |
| `rate_limited` | 429 | `budgets.py:137` | Action rate-limit window full. `details.retry_after_seconds = 1`. |
| `batch_too_large` | 413 | `routes/actions.py:74` | Batch size > `max_batch_actions`. |
| `idempotency_key_conflict` | 409 | `routes/actions.py:101, 389` | Header/body key mismatch or fingerprint mismatch. |
| `action_validation_failed` | 422 | `routes/actions.py:83` | One or more actions failed pre-flight validation. |
| `validation_error` | 422 | `app.py:130` | Pydantic body validation failure. |
| `not_found` | 404 | `app.py:143` | Missing artifact or recording. |
| `unsupported_action` | 400 | `routes/actions.py:841` | Action type not handled by the runner. |
| `timeout` | (in `output.code`) | `routes/actions.py:228, 670` | Per-action or per-batch deadline elapsed. |
| `internal_error` | 500 | `app.py:150` | Catch-all; body is `{redacted: true, type: <class>}` only. |

---

## 10. Python SDK API specification

### 10.1 Namespaces

Unchanged from v6. The seventeen namespace modules are under `src/modal_computer_use/namespaces/`. Collection namespaces are plural (`processes`, `recordings`, `screenshots`, `actions`, `artifacts`, `windows`, `apps`, `commands`); device/concept namespaces are singular (`mouse`, `keyboard`, `clipboard`, `display`, `browser`, `input`, `session`, `debug`).

### 10.2–10.16

(Unchanged from v6.) Actions API, lifecycle, processes, mouse/keyboard/clipboard, screenshots, recordings, display/windows, browser/apps, batch, artifacts, debug/session, narrow command API, and async SDK are as v6 documents them. Pinning tests for the SDK surface: `tests/test_namespaces.py`, `tests/test_modal_sdk_boundary.py`.

---

## 11. Data models

### 11.1 Core models

(Unchanged from v6.) `Point`, `Region`, `CoordinateSpace`, `ActionResult`, `ProcessStatus`, `ComputerStatus`, `Screenshot`, `Recording`, `DisplayGeometry`, `DisplayInfo`, `X11Window`, `ArtifactInfo`, `ArtifactSyncResult`, `SandboxRef`, `DebugUrls`, `ActionDecision`, `ComputerAction`, `ActionItemResult`, `ActionBatchResult`, plus `ActionBatchRequest`, `ActionBatchTiming`, and `ValidationResult` are defined in `src/modal_computer_use/models.py`. The discriminated `ComputerAction` union includes `MoveAction`, `ClickAction`, `DoubleClickAction`, `TripleClickAction`, `DragAction`, `ScrollAction`, `MouseDownAction`, `MouseUpAction`, `TypeAction`, `KeyPressAction`, `HotkeyAction`, `HoldKeyAction`, `WaitAction`, `ScreenshotAction`, `ZoomAction`, `CursorPositionAction`, and `ReleaseAllAction`.

### 11.2 Provider-neutral action models

Unchanged from v6.

### 11.3 Trace models (v7 redaction contract)

Schema:

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

Storage: NDJSON at `<trace_dir>/actions.ndjson` via `TraceWriter` (`src/modal_computer_use/tracing.py:20`).

**v7 redaction contract** (implementation `routes/actions.py:1042-1171`):

- `TypeAction.text` is **not** stored as plaintext. It is serialized as `{"redacted": true, "length": <int>, "sha256": <hex>}` and the JSON path is appended to `redactions[]`.
- Any dict key in `_SENSITIVE_TRACE_KEYS` (`api_key`, `artifact_bytes`, `artifact_uri`, `authorization`, `bearer`, `bytes`, `clipboard`, `clipboard_text`, `connect_token`, `content`, `data`, `data_base64`, `image`, `image_bytes`, `no_vnc_url`, `novnc_url`, `password`, `raw_path`, `screenshot`, `screenshot_bytes`, `stderr`, `stdout`, `text`, `token`, `typed_text`, `url`, `vnc_url`) or ending in `_token` is replaced with `{"redacted": true, "length"|"size_bytes"|"items": <int>}` and its path is added to `redactions[]`.
- String values are also passed through `sanitize_text()` (`redaction.py:15-22`), which masks bearer tokens, query tokens, noVNC URLs, and `artifact://` URIs even when they appear inside non-sensitive fields.
- Provider action is *separated*: when an adapter writes a normalized action with `metadata.provider_action`, the writer pops it out and stores the redacted copy under `provider_action`, with its redactions prefixed `provider_action.`.
- `TraceEntry.error` is the `{code, message}` shape; `result.error` is the sanitized version of the underlying exception string.
- `ComputerTrace.validate()` rejects any entry whose `normalized_action.text` is a non-redacted string (`tracing.py:391-431`), so a malformed write fails closed at validation time.

### 11.4 Versioning

(Unchanged from v6.) `modal_computer_use.__version__`, `/v1/version`, `/v1`, daemon major-version check.

---

## 12. OpenAI Computer Use adapter

(Unchanged from v6.) `OpenAIAdapter` lives in `src/modal_computer_use/adapters/openai.py`. Action mapping, `apply` / `apply_many`, coordinate transforms, and the `before_action` safety hook are pinned by `tests/test_adapters.py`. Coordinate-space transforms remain explicit; the adapter never silently rescales.

---

## 13. Anthropic and generic adapters

(Updated post-v7 hardening.) `AnthropicAdapter` (`adapters/anthropic/computer.py`) supports tool versions `computer_20241022`, `computer_20250124`, `computer_20251124` via the registry in `adapters/anthropic/versions.py`. Provider adapters normalize provider-shaped payloads before they reach the generic `ActionExecutor` (`adapters/generic.py`). Unknown provider actions raise `UnsupportedActionError` by default, even if the future payload carries unknown fields; `allow_unknown=True` is only a provider-adapter compatibility escape hatch that maps unknown provider actions to a zero-duration wait with redacted provider provenance. The native `ActionExecutor` and daemon `ComputerAction` schema remain closed and reject unknown native action types as validation failures. Provider provenance is captured by `adapters/provenance.py` and surfaces as `provider_action` in the trace (see §11.3).

---

## 14. Recording dashboard, trace viewer, and replay tooling

### 14.1 Recording dashboard

Shipped as `recordings.dashboard_router` (`src/modal_computer_use/daemon/app.py:180`). Requires the same auth as other daemon routes — no public unauthenticated tunnel.

### 14.2 Trace/replay format

(Schema unchanged from v6.) Trace layout under `<trace_dir>/`:

```text
traces/
  actions.ndjson
  screenshots/
    before_call_001.png
    after_call_001.png
```

CLI: `computer-use trace validate <path>` and `computer-use trace replay <path>` ship in `src/modal_computer_use/cli.py`. v0.2's dry-run replay and v1.0's controlled replay both pass through `ComputerTrace.validate()` first (`tracing.py:195-431`).

### 14.3 Trace safety

The redaction contract from §11.3 is the canonical statement. Operationally:

- Typed text and clipboard text are stored as `{redacted, length, sha256}`.
- Sensitive keys (full list in §11.3) are masked with length/size/items metadata only.
- noVNC URLs, bearer tokens, query tokens, and `artifact://` URIs are masked inside string fields via `sanitize_text()`.
- `ComputerTrace.validate()` fails closed if any `type` action's text is not a redaction marker — the trace **cannot** carry plaintext typed text past validation.
- Replay (`ComputerTrace.replay`) skips redacted `type` actions by default and reports the skip in `ReplayStep.reason`.

---

## 15. Security model

### 15.1 Threat model

(Unchanged from v6.) The desktop displays untrusted content; agents read untrusted screenshots; the sandbox runs user code; traces and recordings may contain secrets; noVNC URLs are sensitive; the daemon API is privileged.

### 15.2 Secure-by-default controls (shipped state)

| Control | Shipped? | Where |
|---|---|---|
| Daemon uses Modal Connect Tokens. | Yes | `daemon/auth.py:45-98`; `require_connect_user` defaults to `true`. |
| Query tokens rejected by default. | Yes (new in v7) | `auth.py:20`; `reject_query_tokens` defaults to `true`. |
| noVNC is opt-in. | Yes | `DaemonSettings.vnc_mode` defaults to `"off"`; `supervisor.py:55` only starts VNC when enabled. |
| Generated VNC password. | Yes | `supervisor.py:153-162`; `secrets.token_urlsafe(24)` if none supplied. |
| View-only mode. | Yes | `supervisor.py:67-68`; `x11vnc -viewonly`. |
| No model API keys in core. | Yes | `tests/test_imports.py` pins absence of `openai`/`anthropic` imports in core. |
| Network controls (`block_network`, `cidr_allowlist`). | Yes (config surface) | `ComputerConfig.network`. |
| Input validation. | Yes | `routes/actions.py:846-924`: coordinate, region, key, holdkey-nested, modifier validation. |
| Action rate limiting. | Yes (new in v7) | `budgets.py:131-147`; sliding window keyed on `input_rate_limit_per_sec`. |
| Budget limits. | Yes (new in v7) | `budgets.py:11-202`; action, screenshot, artifact-byte, recording-duration, idle. |
| Recording retention. | Yes | Recordings never auto-uploaded; explicit start/stop/delete/sync. |
| Call IDs and audit trail. | Yes | `routes/actions.py:81-211` log structured records with `call_id`, route, duration, redactions. |
| Human confirmation hooks. | Yes | `adapters/generic.py`; `before_action`/`after_action` callbacks. |
| URL and token redaction. | Yes (new in v7) | `redaction.py:6-22`; `daemon/logging.py:12-65`; trace redaction in `routes/actions.py:1042-1171`. |

### 15.3 Redaction layers

Three layers, each defensible on its own:

1. **Request-time** (`src/modal_computer_use/redaction.py:15-22`): `sanitize_text()` masks bearer tokens, query tokens, noVNC URLs, and `artifact://` URIs inside *any* string. Used by action-error messages, command output strings, and process log tails.
2. **Log-time** (`src/modal_computer_use/daemon/logging.py:12-65`): the `JsonFormatter` sanitizes the record's rendered message and recursively redacts the `extra` dict via `redact()`. Sensitive keys (`api_key`, `authorization`, `bytes`, `clipboard`, `credential`, `data_base64`, `password`, `secret`, `text`, `token`, `vnc`) are stored as `{redacted, length, sha256}`. Exception info is replaced with `[redacted exception]` + `safe_exception_payload`.
3. **Trace-time** (`src/modal_computer_use/daemon/routes/actions.py:1042-1171`): every batch trace entry is built by `_redacted_action_and_paths`, which (a) replaces `type.text` with `{redacted, length, sha256}`, (b) walks all nested dicts/lists and applies the sensitive-key list from §11.3, (c) records a `redactions[]` path list, (d) separates `provider_action` and redacts it independently.

If any layer is bypassed, the others still hold. `ComputerTrace.validate()` is the final gate — a non-redaction-marker `text` in a trace fails validation.

### 15.4 Sensitive action policy belongs above the core

(Unchanged from v6.) The core provides `before_action` hooks, screenshots/context to hooks, the standard `ActionDecision` model (`allow`, `deny`, `ask_user`, `handoff`), and example confirmation patterns in `examples/adapter_policy_hook.py`.

### 15.5 Artifact path safety (shipped contract)

Implementation `src/modal_computer_use/artifacts.py:17-100`. `normalize_artifact_path(path, *, allow_empty, public)`:

1. Replace `\` with `/`.
2. Percent-decode up to **three times** to defeat double-encoded traversal (`%252e%252e` → `%2e%2e` → `..`).
3. Reject if starts with `/` or `~`.
4. Reject control characters (any byte < 0x20).
5. Reject any segment equal to `.` or `..`.
6. When `public=True`, reject if the normalized path equals any of `CONTROL_PATHS` (`manifest.ndjson`, `traces/actions.ndjson`) or contains any of `CONTROL_SEGMENTS` (`.control`, `_control`, `.modal-computer-use`, `.secrets`, `logs`).
7. `ArtifactStore.resolve()` realpaths the result and verifies `commonpath(root, candidate) == root`.
8. `ArtifactStore._reject_symlink_components()` walks each path component to refuse symlinks anywhere along the path (not just at the leaf).

This is pinned by `tests/test_artifacts.py` and `tests/test_daemon_validation.py`.

### 15.6 Daemon safety contracts (new in v7)

The post-v6 fix train shipped four orthogonal safety contracts that the daemon now enforces inline:

1. **Primitive safety.** Coordinates that exceed desktop geometry, regions that overflow, and unknown key names raise `action_validation_failed` or `unsupported_key` *before* the lock is taken. Direct mouse modifiers and batch mouse modifiers use the same key support policy. `hold_key`'s nested actions are re-parsed through `parse_action()` and validated with the same checks.
2. **Readiness preflight.** Every route that touches the desktop backend, including cursor position, checks backend readiness before reserving budget or reading backend state. `/readyz` returns 503 when the supervisor reports any required process as stopped or failed.
3. **Budget order.** Routes call `budgets.idle_reservation_error(request)` → `budgets.action_reservation_error(request)` (or screenshot equivalent) *before* the action runs. Mutating artifact and recording routes enforce idle budget before sync/stop/delete side effects. Failures return early with `budget_exceeded` or `rate_limited`. Successful actions call `budgets.touch_activity(request)`; budget snapshots include the projected state on rejection (`budgets.py:108-112`).
4. **Trace and batch guardrails.** Batch deadline is computed once at the start (`routes/actions.py:109-110`); each action's effective timeout is `min(action.timeout_ms or batch.max_action_timeout_ms or default, batch_remaining)`. On any exception, `backend.release_all()` runs inside `with suppress(Exception)` so a stuck-modifier or stuck-button bug cannot survive a single bad action.

### 15.7 Modal-specific security notes & browser/domain policy examples

(Unchanged from v6 §15.4, §15.5.)

---

## 16. Persistence, artifacts, snapshots, and volumes

(Unchanged from v6 §16.1–16.4 and §16.6.) Default ephemeral mode; Volume-backed recording persistence; artifact API over Sandbox filesystem; snapshot helpers; standard artifact layout under `/home/desktop/artifacts/`.

### 16.5 Updated storage recommendation (v7 truth-up)

The recommendation hierarchy is unchanged. The artifact sync contract is implemented in `src/modal_computer_use/artifacts.py:274-315`:

- `ArtifactStore.sync()` returns `{ok: True, persistent: False, message: "artifact sync is a no-op without configured Modal Volume semantics"}` when persistence is not configured.
- With `persistent=True` *and* `persistent_verified=True` (i.e., `COMPUTER_USE_ARTIFACTS_VOLUME_MOUNTED=true`), it runs `/bin/sync <mountpoint>` and reports success only after the subprocess returns 0.
- Without verification, it returns `{ok: False, persistent: True, message: "persistent artifact sync requested without a verified Modal Volume mount"}`. This refuses to *claim* a successful sync against a missing mount.

`NetworkFileSystem` is not used.

---

## 17. Cold-start and warm-pool strategy

(Unchanged from v6 §17.1, §17.2, §17.3.) v1 baseline: prebuilt image, minimal desktop startup, `/readyz` probe. Warm-pool helpers remain example-only (see `examples/04_warm_pool.py`). Performance checklist is unchanged.

---

## 18. Testing strategy

The shipped test matrix lives under `tests/`:

| File | Pins |
|---|---|
| `tests/test_action_batch.py` | Batch validation, batch-too-large, continue-on-error vs stop-on-error, screenshot_after blocking on budget rejection. |
| `tests/test_action_idempotency_and_timeouts.py` | Idempotency cache TTL/LRU, header/body conflict, per-action & per-batch timeouts. |
| `tests/test_adapters.py` | OpenAI & Anthropic action normalization across tool versions; provider-action redaction; unknown-action handling. |
| `tests/test_artifacts.py` | Path traversal (single + double-encoded), symlink rejection, control-path rejection, manifest write, sync semantics. |
| `tests/test_auth_security.py` | Query-token rejection, loopback requirement for local token, verified-user header validation, log redaction. |
| `tests/test_benchmark_cli.py` | Benchmark report generation. |
| `tests/test_budgets.py` | Action/screenshot/artifact-byte/recording-duration/idle budgets; rate-limit window. |
| `tests/test_daemon_routes.py` | Route surface and error-shape contracts. |
| `tests/test_daemon_validation.py` | Coordinate/region/key/holdkey-nested validation; readiness preflight. |
| `tests/test_imports.py` | Core has no provider SDK imports. |
| `tests/test_keys.py` | Key alias table. |
| `tests/test_modal_integration.py` | Modal Sandbox attach/create flow (gated). |
| `tests/test_modal_sdk_boundary.py` | SDK ↔ Modal boundary contracts. |
| `tests/test_models.py` | Pydantic model invariants. |
| `tests/test_namespaces.py` | SDK namespace surface. |
| `tests/test_observability.py` | OpenTelemetry shim opt-in. |
| `tests/test_openapi_schema.py` | Checked-in OpenAPI schema parity. |
| `tests/test_recordings.py` | Recording lifecycle, ffmpeg stop, duration budget. |
| `tests/test_sdk_local.py` | `ComputerSandbox.local()` happy path. |
| `tests/test_settings.py` | Env-var → `DaemonSettings` contract. |
| `tests/test_supervisor.py` | Supervisor lifecycle and restart counts. |
| `tests/test_trace_and_budgets.py` | Trace writer + budget coupling under batch. |
| `tests/test_trace_replay.py` | `ComputerTrace.validate()` and `replay()`; redaction-marker enforcement. |
| `tests/test_x11_backend.py` | X11 backend primitives; release_all cleanup. |
| `tests/test_openapi_schema.py` | OpenAPI snapshot. |

Sections 18.1–18.7 from v6 (unit tests, local integration, Modal integration, golden image, typing/lint, provider-compat, benchmarks/budgets) carry forward; the matrix above is the shipped truth.

---

## 19. Documentation plan

(Unchanged from v6 §19.1–§19.6.) README, architecture, API, security, troubleshooting, and comparison docs all exist under `docs/`. The OpenAPI schema is checked in at `docs/openapi.json`.

---

## 20. Versioned roadmap

### 20.1 v0.1: daemon-backed Modal desktop MVP — **SHIPPED 2026-05-12, HARDENED 2026-05-14**

All features and tests listed in v6 §20.1 are in `src/` and `tests/`. The nine post-v6 commits (see §0) hardened the contracts. Owned image, daemon with `/healthz`/`/readyz`/`/v1/version`, Xvfb/XFCE/x11vnc/noVNC stack, `ComputerSandbox` lifecycle, Connect Token transport, mouse/keyboard/screenshots/clipboard, path-safe artifacts, structured errors with `call_id`, local daemon mode, unit/local-integration/Modal smoke tests — all shipped.

### 20.2 v0.2: Daytona-core parity plus provider action compatibility — **IN PROGRESS**

Shipped now:

- Process status/restart/logs/stderr.
- Display info and windows.
- Recordings lifecycle (start/stop/list/get/delete/download).
- Screenshots in PNG/JPEG/WebP with quality, scale, cursor overlay, artifact-backed capture.
- `CoordinateSpace` model and screenshot metadata.
- Clipboard get/set/clear.
- Action batch executor with `wait`, per-action timeout, per-action results, final screenshot, idempotency.
- `input.release_all()`.
- OpenAI adapter with action fixtures (`tests/test_adapters.py`).
- Anthropic adapter with versioned action registry (`adapters/anthropic/versions.py`).
- Trace NDJSON writer + validator (`tracing.py`).
- Browser/app helpers.
- Benchmark CLI (`benchmarks.py`).

Outstanding for v0.2:

- Full dry-run replay coverage of the Anthropic enhanced action set.
- Recording dashboard UI polish (a minimal endpoint exists at `/recordings/ui`).
- Benchmark comparison report against `Sandbox.exec`-only baseline.

### 20.3 v1.0: production-grade Modal-native computer-use harness — **PLANNED**

(Unchanged from v6 §20.3.) Stable OpenAPI and SDK API, `ComputerSandboxManager` orchestration façade, `attach_or_create` with config hash/owner/TTL/cleanup, Volume-backed artifacts with sync, snapshot example, warm-pool helper, browser profiles, noVNC view-only/takeover, trace/replay CLI with screenshot/artifact references, policy hooks, optional OpenTelemetry, full docs, full CI.

---

## 21. Minimal implementation snippets

(Unchanged from v6 §21.1–§21.6.) SDK quickstart, local daemon quickstart, OpenAI loop skeleton, Anthropic compatibility sketch, daemon route example, trace/replay sketch.

---

## 22. Risks and mitigations (shipped state)

| Risk | Mitigation | Status (v7) |
|---|---|---|
| X11 tools are flaky under concurrent calls. | Serialize input actions; timeouts and retries for display readiness. | Mitigated. `tests/test_x11_backend.py`. |
| Unicode typing through xdotool is unreliable. | Clipboard-paste fallback for Unicode/multiline. | Mitigated. `daemon/desktop/keyboard.py`. |
| noVNC exposes a powerful live desktop. | Opt-in, encrypted tunnel, generated password, view-only mode, URL redaction. | Mitigated. `supervisor.py:55-73`, `redaction.py:6-22`. |
| Daemon API can type/click arbitrary UI. | Modal Connect Tokens; no public raw control endpoint by default. | Mitigated. `auth.py:45-98`; defaults force `require_connect_user=true`. |
| Local daemon auth accidentally exposed. | Bind local mode to localhost; require `COMPUTER_USE_LOCAL_TOKEN` for non-test runs. | Mitigated. `auth.py:30-44`; `tests/test_auth_security.py`. |
| Cold starts too slow. | Prebuild image, small image, snapshots, optional warm pool. | Partly shipped; warm pool is example-only. |
| Recordings consume CPU/disk. | Default fps cap, max duration, status visibility, explicit stop/delete, optional Volume. | Mitigated. `tests/test_recordings.py`; `budgets.recording_start_error`. |
| Screen coordinates mismatch model screenshot size. | Preserve native resolution; explicit coordinate transforms and metadata. | Mitigated. `models.CoordinateSpace`; `tests/test_adapters.py`. |
| Modal API changes. | Pin Modal SDK range; integration tests; isolate Modal calls in `sandbox.py`. | Mitigated. `tests/test_modal_sdk_boundary.py`. |
| Sandbox lifetime capped/idle timeout. | Surface timeout config; document snapshots/volumes for longer workflows. | Mitigated. `DaemonSettings.max_idle_seconds`; idle budget. |
| Prompt injection from screen content. | Policy above core; examples/hooks that treat screen content as untrusted. | Mitigated. `examples/adapter_policy_hook.py`. |
| Action batching hides partial failure. | Per-action results, stop on first error by default, explicit `continue_on_error`. | Mitigated. `tests/test_action_batch.py`. |
| Down/up/hold actions leave input stuck. | try/finally release; manual recovery endpoint. | Mitigated. `routes/actions.py:218-219, 257-258, 793`; `tests/test_x11_backend.py`. |
| Artifact API becomes a filesystem escape hatch. | Relative paths; reject traversal/symlink/control paths; stream large files. | Mitigated. `artifacts.py:17-100`; `tests/test_artifacts.py`. |
| Volumes appear stale. | `artifacts.sync()` and document Volume v2 semantics. | Mitigated. `artifacts.py:274-315`. |
| Provider schemas drift. | Version adapters; fixtures; fail closed on unknown actions. | Mitigated. `adapters/anthropic/versions.py`; `tests/test_adapters.py`. |
| Trace files leak secrets. | Redact typed/clipboard text and tokens; store hashes and lengths. | Mitigated. §11.3, §15.3; `tests/test_trace_replay.py`. |
| GPU/browser profiles raise cost unexpectedly. | Opt-in; resolved resources/cost-affecting settings in status. | Mitigated. `DaemonSettings.image_profile`. |
| Idempotency replay safety. | Header/body conflict detection; fingerprint comparison. | Mitigated (new in v7). `routes/actions.py:96-107, 385-394`. |
| Screenshot output OOMs daemon. | Pre-validate output pixels for all screenshot/zoom paths. | Mitigated (new in v7). `routes/actions.py:460-517`. |
| Logs leak typed text or tokens. | Sanitize render message; redact sensitive keys in `extra`. | Mitigated (new in v7). `daemon/logging.py:12-65`. |

---

## 23. Release criteria (shipped vs outstanding)

| Criterion | Shipped? | Pinning test / evidence |
|---|---|---|
| Python package installs cleanly through UV. | Yes | `pyproject.toml`; `uv.lock` checked in. |
| README quickstart works on a fresh Modal account. | Yes | `README.md`; `tests/test_modal_integration.py`. |
| Local daemon quickstart works without Modal credentials. | Yes | `examples/06_local_daemon.py`; `tests/test_sdk_local.py`. |
| Modal image build is deterministic and documented. | Yes | `docs/modal-deployment.md`. |
| Daytona-like primitives implemented or marked unsupported. | Yes | §9 + §10. |
| Core routes have typed request/response models. | Yes | `models.py`; FastAPI route signatures. |
| OpenAPI schema generated and checked in. | Yes | `docs/openapi.json`; `tests/test_openapi_schema.py`. |
| Errors are structured and user-actionable. | Yes | §9.14 catalog; `daemon/app.py:105-159`. |
| `/healthz`, `/readyz`, `/v1/version`, `/v1/capabilities` implemented. | Yes | `daemon/routes/health.py`, `lifecycle.py`. |
| noVNC opt-in, password-protected, view-only supported. | Yes | `supervisor.py:55-73`. |
| Recording download streams reliably. | Yes | `tests/test_recordings.py`. |
| Unit and local integration tests pass in CI. | Yes | `.github/workflows/`. |
| Modal smoke tests pass (protected CI or manual). | Yes (with `64ffe9c` fail-closed guard) | `tests/integration_modal/`. |
| Batch action route implemented and tested. | Yes | `tests/test_action_batch.py`, `tests/test_action_idempotency_and_timeouts.py`. |
| `input.release_all()` implemented and tested. | Yes | `routes/input.py`; `tests/test_x11_backend.py`. |
| Artifact API path safety implemented and tested. | Yes | `artifacts.py:17-100`; `tests/test_artifacts.py`. |
| Trace NDJSON writer and validator implemented. | Yes | `tracing.py`; `tests/test_trace_replay.py`. |
| OpenAI adapter fixture matrix passes. | Yes | `tests/test_adapters.py`. |
| Anthropic adapter covers all reference actions and versioned schemas. | Yes | `adapters/anthropic/versions.py`; `tests/test_adapters.py`. |
| Security doc is published with clear warnings. | Yes | `docs/security.md`. |
| `NetworkFileSystem` not used in v1 core. | Yes | Grep clean. |
| Browser prewarm/GPU documented as optional. | Yes | `docs/performance.md`. |
| CI verifies Modal API surface against pinned SDK range. | Yes | `tests/test_modal_sdk_boundary.py`. |
| Benchmark output generated for release candidates. | Yes | `benchmark-report.json`; `tests/test_benchmark_cli.py`. |

Outstanding for v0.2 / v1.0: dry-run replay coverage of Anthropic enhanced action set, recording dashboard UI polish, warm-pool helper, snapshot example, optional OpenTelemetry, deployable manager class.

---

## 24. Recommended first PR sequence

(Historical from v6, retained for traceability. All ten PRs have landed; the active queue is now v0.2 polish, replay coverage, and v1.0 manager work.)

1. Repo scaffold and schemas.
2. Local daemon runner and tests.
3. Modal image and sandbox create.
4. Daemon supervisor and lifecycle/process API.
5. Mouse, keyboard, clipboard, input recovery, and screenshot core.
6. Action batch, artifacts, trace skeleton.
7. Recordings, display, windows, browser/apps.
8. Modal manager, state, and persistence.
9. Adapters, examples, docs, and security.
10. Performance and production examples.

Post-v6 fix train (covered in §0): four daemon-hardening commits, one redaction commit, one trace-redaction-and-batch commit, one screenshot-budget commit, one desktop-primitive commit, one artifact-controls commit, plus a CI fail-closed commit.

---

## 25. References reviewed

(Unchanged from v6 §25; sources 1–31.)

---

## 26. Final recommendation

The v0.1 primitive layer has shipped and been hardened. The remaining work is incremental: complete the v0.2 trace-replay coverage and recording dashboard, then move to v1.0's deployable manager, warm-pool helper, snapshot example, optional OpenTelemetry, and full docs.

The architectural rule remains: **Modal-native orchestration, daemon-native primitive execution.** The repository is now a reusable open-source primitive library that is meaningfully better than treating Modal as a generic VM and meaningfully more open than managed providers like Daytona, E2B, and the provider-hosted Modal/Anthropic reference repo. Its differentiators are the daemon-first protocol, the redaction-aware trace/replay format, the Modal-native attach/snapshot/Volume semantics, and the provider-versioned adapter contract — all of which now have shipping implementations and pinning tests.

The discipline that produced v7 — `fix(...)` commits that lock in invariants rather than `feat(...)` commits that expand surface area — should continue through v0.2 and v1.0. Every new surface should land with: a request model, a route handler, a redaction rule, a budget reservation, a structured error code, and a pinning test. Any contract that does not appear in §27 of a future spec is not yet shipped.

---

## 27. v6 → v7 implementation truth table

| Contract (from v6) | Implementation | Pinned by |
|---|---|---|
| Liveness `/healthz` | `daemon/routes/health.py`; `daemon/auth.py:28-29` skips auth | `tests/test_daemon_routes.py` |
| Readiness `/readyz` | `daemon/routes/health.py`; preflight in action runner | `tests/test_daemon_routes.py`, `tests/test_daemon_validation.py` |
| `/v1/version`, `/v1/capabilities` | `daemon/routes/lifecycle.py` | `tests/test_daemon_routes.py` |
| Connect Token auth | `daemon/auth.py:45-98` | `tests/test_auth_security.py` |
| Loopback-only local token | `daemon/auth.py:30-44` | `tests/test_auth_security.py` |
| Query-token rejection | `daemon/auth.py:20-27` | `tests/test_auth_security.py` |
| Verified-user header | `daemon/auth.py:62-98` | `tests/test_auth_security.py` |
| Mouse routes | `daemon/routes/mouse.py`; `daemon/desktop/x11.py` | `tests/test_daemon_routes.py`, `tests/test_x11_backend.py` |
| Keyboard routes incl. `hold_key` nested | `daemon/routes/keyboard.py`; `daemon/routes/actions.py:846-924`, `753-794` | `tests/test_daemon_validation.py`, `tests/test_x11_backend.py` |
| Clipboard routes | `daemon/routes/clipboard.py` | `tests/test_daemon_routes.py` |
| Screenshot routes (full/region/zoom) + pixel budget | `daemon/routes/screenshots.py`; `daemon/routes/actions.py:460-517` | `tests/test_daemon_routes.py`, `tests/test_budgets.py` |
| Recordings lifecycle | `daemon/routes/recordings.py`; `daemon/desktop/recordings.py` | `tests/test_recordings.py` |
| Recording dashboard `/recordings/ui` | `daemon/routes/recordings.py:dashboard_router` (registered in `daemon/app.py:180`) | `tests/test_recordings.py` |
| Action batch `/v1/actions/run` | `daemon/routes/actions.py:66-382` | `tests/test_action_batch.py`, `tests/test_action_idempotency_and_timeouts.py` |
| Action batch `/v1/actions/validate` | `daemon/routes/actions.py:60-63` | `tests/test_action_batch.py` |
| Idempotency cache (TTL + LRU + fingerprint) | `daemon/routes/actions.py:96-107, 406-418` | `tests/test_action_idempotency_and_timeouts.py` |
| Per-action & per-batch deadlines | `daemon/routes/actions.py:109-184, 216-258` | `tests/test_action_idempotency_and_timeouts.py` |
| `release_all` on every failure path | `daemon/routes/actions.py:218-219, 257-258`; `daemon/desktop/x11.py` | `tests/test_x11_backend.py` |
| Artifact routes (list/read/write/delete) | `daemon/routes/artifacts.py`; `artifacts.py:54-272` | `tests/test_artifacts.py`, `tests/test_daemon_validation.py` |
| Artifact path safety (traversal, symlink, control) | `artifacts.py:17-100` | `tests/test_artifacts.py` |
| Artifact manifest | `artifacts.py:252-272` | `tests/test_artifacts.py` |
| Artifact sync (Volume v2) | `artifacts.py:274-315` | `tests/test_artifacts.py` |
| Display/windows/apps/browser routes | `daemon/routes/{display,windows,apps,browser}.py` | `tests/test_daemon_routes.py` |
| `input.release_all` | `daemon/routes/input.py` | `tests/test_x11_backend.py` |
| Process supervisor & restart counts | `daemon/supervisor.py:13-169` | `tests/test_supervisor.py` |
| Budget kinds (actions/screenshots/artifacts/recordings/idle) | `daemon/budgets.py:11-202` | `tests/test_budgets.py`, `tests/test_trace_and_budgets.py` |
| Action rate-limit window | `daemon/budgets.py:131-147, 190-202` | `tests/test_budgets.py` |
| OpenAI adapter | `adapters/openai.py` | `tests/test_adapters.py` |
| Anthropic adapter + versions | `adapters/anthropic/computer.py`, `adapters/anthropic/versions.py` | `tests/test_adapters.py` |
| Generic `ActionExecutor` & hooks | `adapters/generic.py` | `tests/test_adapters.py` |
| Provider provenance redaction | `adapters/provenance.py`; `daemon/routes/actions.py:1150-1171` | `tests/test_adapters.py`, `tests/test_trace_replay.py` |
| Trace writer | `tracing.py:20-28` | `tests/test_trace_and_budgets.py`, `tests/test_trace_replay.py` |
| Trace validator (redaction-marker enforcement) | `tracing.py:195-431` | `tests/test_trace_replay.py` |
| Trace replay (skip-redacted) | `tracing.py:65-119` | `tests/test_trace_replay.py` |
| Trace redaction (sensitive keys, sha256 typed text, paths) | `daemon/routes/actions.py:1042-1171` | `tests/test_trace_replay.py`, `tests/test_adapters.py` |
| Structured JSON logs + redaction | `daemon/logging.py:12-65` | `tests/test_auth_security.py`, `tests/test_observability.py` |
| OpenTelemetry shim | `observability.py:57-83`; `daemon/app.py:81-104` | `tests/test_observability.py` |
| Error code catalog (§9.14) | `daemon/app.py:105-159`; `daemon/errors.py`; `daemon/auth.py`; `daemon/routes/actions.py` | `tests/test_auth_security.py`, `tests/test_daemon_validation.py`, `tests/test_action_batch.py` |
| `DaemonSettings` env-var contract | `daemon/settings.py:31-149` | `tests/test_settings.py` |
| `ComputerSandbox.create/attach/local` | `sandbox.py` | `tests/test_modal_integration.py`, `tests/test_modal_sdk_boundary.py`, `tests/test_sdk_local.py` |
| SDK namespace surface | `namespaces/*.py` | `tests/test_namespaces.py` |
| Checked-in OpenAPI schema | `docs/openapi.json` | `tests/test_openapi_schema.py` |

(End of v7.)
