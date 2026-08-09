# Configuration

There are two configuration surfaces:

- `ComputerConfig` is the public SDK model used when the SDK creates a Modal Sandbox.
- Environment variables configure the daemon process. Most users should set `ComputerConfig`
  instead of constructing the SDK-generated daemon environment themselves.

`ComputerConfig` does not read environment variables or a config file. Pydantic defaults apply
first, and values passed to `ComputerConfig` replace those defaults. During
`ComputerSandbox.create(...)`, the SDK passes orchestration settings directly to Modal and maps
daemon-owned settings to environment variables. The explicit `expose_vnc=` argument to `create`
overrides `config.expose_vnc`; an explicit `image=` replaces SDK image selection.

A directly launched daemon reads its process environment when `DaemonSettings` is constructed.
The process environment therefore overrides daemon defaults. There is no daemon config file.

Treat bearer tokens, VNC passwords, browser startup URLs, daemon URLs, and noVNC URLs as secrets.
Do not commit them, put them in shared shell history, or include them in logs.

The SDK has one primary placed trajectory. It does not provide `optimized=True`, a performance
profile, or a hidden variable that switches between two defaults. Placement and every cost-bearing
choice remain explicit. Warm capacity is off unless the application sets a positive Function
minimum or configures a Sandbox warm pool.

`computer.step()` has no global enable flag. It is the primary action-to-frame Interface on a
borrowed computer. Its `continue_on_error`, `screenshot_options`, and action timeout values are
explicit per call. Region, resources, and warm capacity remain separate application choices.

## Inspect cost and placement choices

Set the Sandbox environment, exact region, CPU, memory, image, browser, and timeouts in
`ComputerConfig`. No SDK default selects a region, CPU, or memory value. Measure a region for your
workload and choose resources for its capacity and cost.

```python
from modal_computer_use import ComputerConfig

config = ComputerConfig(
    runtime={
        "modal_environment": "main",
        "modal_region": "us-west-2",
        "timeout_seconds": 900,
        "idle_timeout_seconds": None,
        "readiness_timeout_seconds": 120,
    },
    resources={"profile": "browser", "cpu": 1.0, "memory_mib": 2048},
    image={"source": "inline"},
    browser={"kind": "chromium", "prewarm": False, "gpu_mode": "off"},
)

print(config.resolved_cost_and_placement())
```

`resolved_cost_and_placement()` returns only placement and cost choices. It does not include
browser URLs, launch arguments, or profile paths. It also omits typed text, clipboard text,
screenshot bytes, artifact bytes, daemon URLs, and bearer tokens because those values do not
belong in this configuration report. Configuration validation errors hide rejected input values.

This report covers the Sandbox. A placed Modal Function has separate cost controls. Set and inspect
Function CPU, memory, image, retries, timeout, and container limits in the application that defines
the Function. The executable
[`modal_function_session_handoff.py`](../examples/modal_function_session_handoff.py) example keeps
the Function and Sandbox choices together and uses the same exact region for both. Its
`us-west-2` value is an application example, not an SDK default.

## Public SDK configuration

All models reject unknown keys. Nested models can be supplied as model instances or dictionaries.

### Desktop

| Field | Default | Allowed values and effect |
| --- | --- | --- |
| `desktop.resolution` | `(1024, 768)` | `(width, height)` in pixels. Width must be at least `320`, height at least `240`, and total pixels at most `8,294,400`. Maps to `COMPUTER_USE_DESKTOP_WIDTH` and `COMPUTER_USE_DESKTOP_HEIGHT`. |
| `desktop.dpi` | `96` | Integer `48..240`. Maps to `COMPUTER_USE_DESKTOP_DPI`. |
| `desktop.window_manager` | `"xfce"` | `"xfce"` or `"openbox"`. Maps to `COMPUTER_USE_WINDOW_MANAGER`. Named images require `"xfce"`. |
| `desktop.display_depth` | `24` | Integer `8..32`. Maps to `COMPUTER_USE_DISPLAY_DEPTH`. |

### Runtime

These fields configure Modal orchestration. They are not daemon environment variables, except
that `budgets.max_idle_seconds` is a separate daemon-side budget described below.

| Field | Default | Allowed values and effect |
| --- | --- | --- |
| `runtime.timeout_seconds` | `3600` | Integer `1..86400`; passed to Modal as the Sandbox lifetime timeout. |
| `runtime.idle_timeout_seconds` | unset | Integer `1..86400` or `None`; passed to Modal as its Sandbox idle timeout. Warm capacity does not support an explicit value. |
| `runtime.readiness_timeout_seconds` | `120` | Integer `1..900`; SDK wait deadline for Modal and daemon readiness. It does not map to `COMPUTER_USE_READINESS_CACHE_TTL_MS`. |
| `runtime.modal_environment` | unset | Non-empty Modal environment name or `None`; selects the environment for `App.lookup`. It is independent of `image.environment_name`, which selects a published named image. |
| `runtime.modal_region` | unset | Non-empty Modal region string or `None`; passed to Modal as requested placement. |

### Resources and image

| Field | Default | Allowed values and effect |
| --- | --- | --- |
| `resources.profile` | `"standard"` | `"standard"`, `"browser"`, `"browser-gpu"`, or `"custom"`. Selects the managed image recipe and maps to the daemon's informational `COMPUTER_USE_IMAGE_PROFILE`. |
| `resources.cpu` | provider default | Positive number or `None`; passed to Modal as `cpu`. Modal counts physical cores, not vCPU, and applies a per-container floor of `0.125`. |
| `resources.memory_mib` | provider default | Integer at least `128` or `None`; passed to Modal as `memory`. `memory_mb` is an accepted compatibility input alias. |
| `resources.gpu` | provider default | Modal GPU request string or `None`; passed through to Modal. |
| `image.source` | `"inline"` | `"inline"` builds/selects the SDK recipe; `"named"` selects a published revision-tagged image. |
| `image.revision` | unset | For `source="named"`, required and exactly 40 lowercase hexadecimal Git characters. Invalid for inline images. |
| `image.environment_name` | unset | Optional non-empty Modal environment for named-image and app lookup. Invalid for inline images. |

Named images do not support `resources.profile="custom"` and require XFCE. The `browser` and
`browser-gpu` profiles also require an explicit `browser.kind`. `browser.prewarm` remains an
explicit application choice and may be `false`. Passing an explicit `image=` to
`ComputerSandbox.create` bypasses the `image.source` selection step.

`resources.cpu` and `resources.memory_mib` are requests, not caps. Modal charges whichever is
higher, the request or the actual usage, so a request above real usage costs the difference
([Resources](https://modal.com/docs/guide/resources), accessed 2026-07-29). These fields size the
Sandbox that this configuration creates. A separate process that drives that Sandbox, such as a
Modal Function acting as the client, carries its own request and is not covered here.

### Network and ingress

| Field | Default | Allowed values and effect |
| --- | --- | --- |
| `network.block_all` | `false` | Boolean passed to Modal as `block_network`. `blocked` is an accepted compatibility input alias. It cannot be combined with an allowlist and requires `ingress="connect"` with VNC off. |
| `network.daemon_http_version` | `"1.1"` | `"1.1"` or `"2"`. Selects Modal port exposure and maps to `COMPUTER_USE_DAEMON_HTTP_VERSION`. Version `2` uses cleartext HTTP/2 (`h2c`) inside the Sandbox. |
| `network.outbound_cidr_allowlist` | unset | List of valid, non-empty CIDR strings or `None`; passed to Modal. `cidr_allowlist` and `allowlist` are compatibility input aliases. The `cidr_allowlist` property is deprecated. |
| `network.outbound_domain_allowlist` | unset | List of non-empty domain strings or `None`; passed to Modal. |
| `network.inbound_cidr_allowlist` | unset | List of valid, non-empty CIDR strings or `None`; passed to Modal. |
| `ingress` | `"attested-tunnel"` | `"attested-tunnel"`, `"connect"`, or `"tunnel"`. Attested tunnel mints a short-lived session from an SDK-managed bootstrap bearer; connect stays on Modal Connect; raw tunnel uses the bootstrap bearer directly. |

### Storage

| Field | Default | Allowed values and effect |
| --- | --- | --- |
| `storage.recordings_dir` | `/home/desktop/recordings` | Daemon path mapped to `COMPUTER_USE_RECORDINGS_DIR`. `recording_dir` is an accepted compatibility input alias. |
| `storage.artifacts_dir` | `/home/desktop/artifacts` | Daemon artifact root mapped to `COMPUTER_USE_ARTIFACTS_DIR`. |
| `storage.persist_artifacts` | `false` | Maps to `COMPUTER_USE_ARTIFACTS_PERSISTENT`. When true, creation requires a Modal Volume mounted at `artifacts_dir` or one of its parents. The SDK separately computes `COMPUTER_USE_ARTIFACTS_VOLUME_MOUNTED`; callers do not set that proof flag. |
| `storage.trace_dir` | `/home/desktop/artifacts/traces` | Daemon NDJSON trace directory mapped to `COMPUTER_USE_TRACE_DIR`. |

### Browser

`browser` defaults to `None`, which means that the SDK does not select or prewarm a browser. If a
`BrowserConfig` object is supplied, its field defaults apply.

| Field | `BrowserConfig` default | Allowed values and effect |
| --- | --- | --- |
| `browser.kind` | unset | `"firefox"`, `"chromium"`, or `None`. Maps to `COMPUTER_USE_BROWSER`; unset daemon behavior uses `xdg-open` for generic URL opening. |
| `browser.prewarm` | `true` | Maps to `COMPUTER_USE_BROWSER_PREWARM`. With no `browser` object, the SDK maps `false`. |
| `browser.profile_dir` | unset | Optional daemon path. Maps to `COMPUTER_USE_BROWSER_PROFILE_DIR`; unset resolves at use time to `/home/desktop/.cache/modal-computer-use/browser-profile`. |
| `browser.launch_args` | `[]` | List of strings, none containing a NUL byte. Encoded as JSON in `COMPUTER_USE_BROWSER_LAUNCH_ARGS`. |
| `browser.open_url_on_start` | unset | Optional startup URL mapped to `COMPUTER_USE_BROWSER_OPEN_URL_ON_START`. It takes precedence over blank-page prewarm. Treat it as secret-bearing. |
| `browser.gpu_mode` | unset | `"auto"`, `"off"`, `"chromium-vulkan"`, or `None`. The SDK resolves `None` to `"auto"` and maps it to `COMPUTER_USE_BROWSER_GPU_MODE`. |

### Actions

| Field | Default | Allowed values and effect |
| --- | --- | --- |
| `actions.post_action_delay_ms` | `0` | Integer `0..10000`; maps to `COMPUTER_USE_POST_ACTION_DELAY_MS`. |
| `actions.screenshot_after` | `false` | Retained SDK configuration field. It is not projected into daemon environment and is not a global default for `actions.run`; use the per-call `screenshot_after=` argument. |
| `actions.trace_actions` | `false` | Maps to `COMPUTER_USE_TRACE_ACTIONS`. `action_trace` is an accepted compatibility input alias. |
| `actions.screenshot_processing_location` | `"auto"` | `"auto"`, `"daemon"`, or `"client"`; maps to `COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION`. The daemon currently records this setting but no route branches on it. `screenshot_processing` is an accepted compatibility input alias. |
| `actions.max_batch_actions` | `50` | Integer `1..500`; maps to `COMPUTER_USE_MAX_BATCH_ACTIONS`. Nested actions count toward the daemon cap. |
| `actions.max_batch_duration_ms` | `30000` | Integer `1..600000`; maps to `COMPUTER_USE_MAX_BATCH_DURATION_MS`, the wall-clock cap for one batch. |
| `actions.default_action_timeout_ms` | `5000` | Integer `1..300000`; maps to `COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS`. |
| `actions.max_action_timeout_ms` | `300000` | Integer `1..600000`; maps to `COMPUTER_USE_MAX_ACTION_TIMEOUT_MS`. It must be at least `default_action_timeout_ms`. |
| `actions.input_rate_limit_per_sec` | `100` | Integer `0..10000`; maps to `COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC`. This is the token-bucket refill rate in normalized input-work tokens per second. Zero disables input rate limiting. |
| `actions.input_rate_limit_burst` | `400` | Integer `1..100000`; maps to `COMPUTER_USE_INPUT_RATE_LIMIT_BURST`. This is the maximum cost that one atomic batch can reserve. |
| `actions.input_backend` | `"auto"` | `"auto"`, `"xtest"`, or `"xdotool"`; maps to `COMPUTER_USE_INPUT_BACKEND`. |
| `actions.subprocess_backend` | `"isolated-asyncio"` | `"asyncio"`, `"threaded"`, or `"isolated-asyncio"`; maps to `COMPUTER_USE_SUBPROCESS_BACKEND`. This selects subprocess-backed command and compatibility execution, not native XTest input. |

The `normalized-input-work-v1` policy uses these costs:

| Input action | Tokens |
| --- | ---: |
| Move, mouse down/up, keypress, release all | `1` |
| Click, double click, triple click | Click count |
| Hotkey | `max(1, ceil(keys / 4))` |
| Type | `1 + ceil(characters / 32)` |
| Scroll | `1 + ceil(amount / 32)` |
| Coordinate drag | `1` |
| Path drag | `1 + ceil(path points / 32)` |
| Hold key with nested actions | `1 + sum(nested action costs)` |
| Wait, screenshot, zoom, cursor query | `0` |

These are normalized admission units, not native X11 event counts. The daemon uses one bucket across
leases and direct routes. It does not reset the bucket when ownership changes.
The 100/400 values are portable defaults for the minimum tested 1 CPU, 2,048 MiB Sandbox. Input
capacity also depends on browser load, action mix, X11, capture work, and CPU scheduling. The SDK
does not infer a hidden limit from CPU or memory. For a faster setup, run the same-runtime capacity
gate, then set both fields explicitly.
With the default burst, one `type` action can contain about 12,768 characters before its normalized
cost exceeds capacity. Larger schema-valid input requests need an explicit larger burst or a
different application-owned request shape.

### Daemon budgets

All unset budgets are unlimited. Each configured value must be an integer of at least `1`.

| Field | Environment mapping | What counts |
| --- | --- | --- |
| `budgets.max_actions` | `COMPUTER_USE_MAX_ACTIONS` | Attempted executable desktop actions. Failed and timed-out attempts count; validation failures, idempotency replays, screenshots, zooms, and cursor queries do not. |
| `budgets.max_screenshots` | `COMPUTER_USE_MAX_SCREENSHOTS` | Reserved screenshot operations. |
| `budgets.max_artifact_bytes` | `COMPUTER_USE_MAX_ARTIFACT_BYTES` | Projected bytes in the artifact store, accounting for replacement of an existing artifact. |
| `budgets.max_recording_seconds` | `COMPUTER_USE_MAX_RECORDING_SECONDS` | Recording duration. |
| `budgets.max_idle_seconds` | `COMPUTER_USE_MAX_IDLE_SECONDS` | Time since the last budget-counted activity. This is enforced by daemon requests and is distinct from Modal's `runtime.idle_timeout_seconds`. |

### Identity and VNC

| Field | Default | Allowed values and effect |
| --- | --- | --- |
| `run_id` | generated at creation | Optional string mapped to `COMPUTER_USE_RUN_ID` and safe lifecycle tags. |
| `request_id` | unset | Deprecated compatibility field. If `run_id` is absent, it warns and supplies `run_id`. It is excluded from serialized configuration. |
| `expose_vnc` | `"off"` | `"off"`, `"view_only"`, or `"control"`; compatibility booleans map `false` to `"off"` and `true` to `"control"`. Maps to `COMPUTER_USE_VNC_MODE` and Modal port `6080` exposure. |
| `vnc_password` | generated when needed | Optional secret mapped to `COMPUTER_USE_VNC_PASSWORD`. When VNC is enabled and it is unset, the SDK generates a password. It is excluded from model serialization, repr output, configuration hashes, and tags. Warm capacity does not accept an explicit password. |

## Daemon and operator environment

The SDK bounds above are enforced by `ComputerConfig`. A directly launched daemon validates the
security-owned ranges documented below during settings construction. Other integer settings still
use `int()` and the documented range remains authoritative.

### Process, display, and browser

| Variable | Direct-daemon default | Accepted values and ownership |
| --- | --- | --- |
| `COMPUTER_USE_DAEMON_HOST` | `127.0.0.1` for local-token or explicit unauthenticated-loopback mode; otherwise `0.0.0.0` | Bind address consumed by the daemon entry point. Startup fails when authentication is unconfigured. Explicit unauthenticated mode may bind only to loopback. Modal orchestration sets `0.0.0.0` or `::` with authentication. |
| `COMPUTER_USE_DAEMON_PORT` | `8080` | Integer TCP port consumed by the daemon entry point. |
| `COMPUTER_USE_DAEMON_HTTP_VERSION` | HTTP/1.1 unless exactly `"2"` | `"1.1"` or `"2"`; `"2"` selects Hypercorn h2c, otherwise Uvicorn HTTP/1.1. SDK-generated from `network.daemon_http_version`. |
| `DISPLAY` | `:99` | X display address. |
| `COMPUTER_USE_RUN_ID` | unset | Optional run identifier. SDK-generated from `run_id`. |
| `COMPUTER_USE_DESKTOP_WIDTH` | `1024` | Integer pixels; SDK range is governed by `desktop.resolution`. |
| `COMPUTER_USE_DESKTOP_HEIGHT` | `768` | Integer pixels; SDK range is governed by `desktop.resolution`. |
| `COMPUTER_USE_DESKTOP_DPI` | `96` | Integer DPI; SDK range `48..240`. |
| `COMPUTER_USE_DISPLAY_DEPTH` | `24` | Integer color depth; SDK range `8..32`. |
| `COMPUTER_USE_WINDOW_MANAGER` | `xfce` | Supported: `xfce`, `openbox`. Other direct values currently take the XFCE launch path and are unsupported. |
| `COMPUTER_USE_BROWSER` | unset | Browser command/name. SDK emits `firefox`, `chromium`, or empty. |
| `COMPUTER_USE_BROWSER_PREWARM` | `false` | Boolean; start the configured browser at daemon boot unless a startup URL is set. |
| `COMPUTER_USE_BROWSER_PROFILE_DIR` | unset | Optional path; browser use resolves unset to `/home/desktop/.cache/modal-computer-use/browser-profile`. |
| `COMPUTER_USE_BROWSER_LAUNCH_ARGS` | `[]` | JSON array containing only strings. Invalid JSON or non-string entries fail settings construction. |
| `COMPUTER_USE_BROWSER_OPEN_URL_ON_START` | unset | Optional startup URL. Treat as secret-bearing. It takes precedence over blank-page prewarm. |
| `COMPUTER_USE_BROWSER_GPU_MODE` | `auto` | Supported: `auto`, `off`, `chromium-vulkan`. |
| `COMPUTER_USE_IMAGE_PROFILE` | `standard` | Informational capability/resource profile label. SDK emits `standard`, `browser`, `browser-gpu`, or `custom`. |

### Desktop backends and action policy

| Variable | Default | Accepted values and ownership |
| --- | --- | --- |
| `COMPUTER_USE_BACKEND` | `auto` | `auto`, `x11`, or `mock`; validated at daemon settings construction. `auto` selects X11 on POSIX and fails closed if required desktop tools are unavailable. Use `mock` only for deterministic local tests. |
| `COMPUTER_USE_INPUT_BACKEND` | `auto` | `auto`, `xtest`, or `xdotool`; validated. `auto` prefers persistent XTest/XKB and permits compatibility fallback only before native emission. |
| `COMPUTER_USE_SUBPROCESS_BACKEND` | `isolated-asyncio` | `asyncio`, `threaded`, or `isolated-asyncio`; validated. SDK-generated from `actions.subprocess_backend`. |
| `COMPUTER_USE_MAX_BATCH_ACTIONS` | `50` | Integer; SDK range `1..500`. |
| `COMPUTER_USE_MAX_ACTION_DEPTH` | `32` | Integer `1..128`. Bounds nested `hold_key` action trees. It cannot be disabled. |
| `COMPUTER_USE_MAX_BATCH_DURATION_MS` | `30000` | Integer milliseconds; SDK range `1..600000`. Caps the whole action batch, including nested execution. |
| `COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS` | `5000` | Integer milliseconds; SDK range `1..300000`. Used when neither an action nor request supplies a timeout. |
| `COMPUTER_USE_MAX_ACTION_TIMEOUT_MS` | `300000` | Integer milliseconds; SDK range `1..600000`. Rejects larger request/action timeouts. |
| `COMPUTER_USE_POST_ACTION_DELAY_MS` | `0` | Integer milliseconds; SDK range `0..10000`. Applied after UI-mutating actions before the next action or screenshot-after. |
| `COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC` | `100` | Integer; SDK range `0..10000`. Sets the weighted-token refill rate. Zero disables input rate limiting. |
| `COMPUTER_USE_INPUT_RATE_LIMIT_BURST` | `400` | Integer; SDK range `1..100000`. Sets the maximum weighted cost admitted at once. |
| `COMPUTER_USE_SCREENSHOT_MAX_PIXELS` | `8294400` | Integer output-pixel ceiling for region and screenshot-after captures. This has no `ComputerConfig` field. |
| `COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION` | `auto` | Supported label: `auto`, `daemon`, `client`. SDK-generated from `actions.screenshot_processing_location`; the daemon stores it but currently has no route behavior that reads it. |
| `COMPUTER_USE_READINESS_CACHE_TTL_MS` | `1000` | Integer milliseconds for successful desktop-readiness probe caching. Values at or below zero disable caching. This has no `ComputerConfig` field. |
| `COMPUTER_USE_IDEMPOTENCY_CACHE_MAX_ENTRIES` | `1000` | Integer cache capacity. `0` disables retention. This has no `ComputerConfig` field. |
| `COMPUTER_USE_IDEMPOTENCY_CACHE_TTL_SECONDS` | `3600` | Integer cache TTL in seconds. This has no `ComputerConfig` field. |

### Transport and collection bounds

These settings bound attacker-controlled allocation or work. A zero value disables a limit only
where stated. Artifact uploads are streamed and do not use the JSON request-body ceiling.

| Variable | Default | Accepted values and ownership |
| --- | --- | --- |
| `COMPUTER_USE_MAX_JSON_BODY_BYTES` | `16777216` | Non-negative integer. Applies at the ASGI receive boundary. `0` explicitly disables the ceiling. Oversized requests return `413 request_body_too_large`. |
| `COMPUTER_USE_MAX_WEBSOCKET_MESSAGE_BYTES` | `16777216` | Non-negative integer passed to the Uvicorn or Hypercorn protocol implementation. `0` explicitly disables the ceiling. |
| `COMPUTER_USE_MAX_HOT_SESSION_CONNECTIONS` | `64` | Non-negative global hot-session WebSocket cap. `0` means unlimited. Rejected connections close with code `1013`. |
| `COMPUTER_USE_MAX_OBSERVATION_CONNECTIONS` | `16` | Non-negative global observation-stream WebSocket cap. `0` means unlimited. Rejected connections close with code `1013`. |
| `COMPUTER_USE_MAX_COMMAND_ARGUMENTS` | `65536` | Non-negative argument-count cap for command and app launch vectors. `0` means unlimited. Each argument remains subject to the Linux encoded-byte limit. |
| `COMPUTER_USE_MAX_DRAG_POINTS` | `1024` | Non-negative drag-path point cap for direct and batch actions. `0` means unlimited. |
| `COMPUTER_USE_MAX_KEY_COLLECTION_SIZE` | `64` | Non-negative cap for modifier and hotkey collections. `0` means unlimited. |

### Authentication and VNC

| Variable | Default | Accepted values and ownership |
| --- | --- | --- |
| `COMPUTER_USE_LOCAL_TOKEN` | unset | Static bearer token for local/direct deployments. It also changes the default bind host to loopback. Secret. |
| `COMPUTER_USE_TUNNEL_TOKEN` | unset | Static daemon bearer token for raw tunnel access and the SDK's attested-tunnel bootstrap. The SDK generates it for Modal-created Sandboxes. Secret. |
| `COMPUTER_USE_TUNNEL_TOKEN_TTL_SECONDS` | `3600` | Positive integer lifetime for tokens minted after `/v1/session/tunnel-authorize`. There is no arbitrary maximum. |
| `COMPUTER_USE_MAX_TUNNEL_SESSIONS` | `0` | Non-negative active minted-session cap. `0` means unlimited. Expired sessions are pruned on access; reaching a configured cap rejects minting and does not evict active sessions. |
| `COMPUTER_USE_REQUIRE_CONNECT_USER` | `true` | Boolean. Require Modal's verified-user header on protected control requests. The SDK sets this only for pure Connect ingress. Health and readiness probes remain unauthenticated. |
| `COMPUTER_USE_ALLOW_UNAUTHENTICATED_LOOPBACK` | `false` | Explicit local-only escape hatch when no token or Connect authentication is configured. The daemon refuses a non-loopback bind in this mode. |
| `COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY` | `false` | Boolean. The SDK sets `true` only for pure Connect ingress. Raw tunnel deployments must leave it off because tunnel clients control request headers. |
| `COMPUTER_USE_REJECT_QUERY_TOKENS` | `true` | Boolean. Reject credentials in URL queries; keep enabled because URLs leak into logs and history. |
| `COMPUTER_USE_VNC_MODE` | `off` | Supported: `off`, `view_only`, `control`. The SDK generates this from `expose_vnc`. |
| `COMPUTER_USE_VNC_PASSWORD` | unset | noVNC/x11vnc password. If VNC is enabled and unset, the daemon supervisor generates one in its private runtime directory; the Modal SDK normally generates and injects one first. Secret. |

### Artifacts, recordings, traces, and runtime state

| Variable | Default | Accepted values and ownership |
| --- | --- | --- |
| `COMPUTER_USE_ARTIFACTS_DIR` | `/home/desktop/artifacts` | Artifact root path. |
| `COMPUTER_USE_ARTIFACTS_PERSISTENT` | `false` | Boolean declaration that artifact persistence is requested. SDK-generated from `storage.persist_artifacts`. |
| `COMPUTER_USE_ARTIFACTS_VOLUME_MOUNTED` | `false` | Boolean proof that a Volume covers the artifact path. Computed internally by the SDK from actual mounts; do not set it merely to claim persistence. Persistent sync is verified only when both persistence flags are true. |
| `COMPUTER_USE_RECORDINGS_DIR` | `/home/desktop/recordings` | Recording output path. |
| `COMPUTER_USE_RUNTIME_DIR` | `/tmp/modal-computer-use` | Runtime process state and private VNC password-file directory. This has no `ComputerConfig` field. |
| `COMPUTER_USE_TRACE_DIR` | `/home/desktop/artifacts/traces` | Action trace directory. |
| `COMPUTER_USE_TRACE_ACTIONS` | `false` | Boolean; append action trace entries. SDK-generated from `actions.trace_actions`. |

### Run budgets and observability

| Variable | Default | Accepted values and ownership |
| --- | --- | --- |
| `COMPUTER_USE_MAX_ACTIONS` | unset | Optional positive integer action budget. Empty or unset means unlimited. |
| `COMPUTER_USE_MAX_SCREENSHOTS` | unset | Optional positive integer screenshot budget. Empty or unset means unlimited. |
| `COMPUTER_USE_MAX_ARTIFACT_BYTES` | unset | Optional positive integer artifact-byte budget. Empty or unset means unlimited. |
| `COMPUTER_USE_MAX_RECORDING_SECONDS` | unset | Optional positive integer recording-duration budget. Empty or unset means unlimited. |
| `COMPUTER_USE_MAX_IDLE_SECONDS` | unset | Optional positive integer daemon activity budget in seconds. Empty or unset means unlimited. |
| `COMPUTER_USE_OTEL_ENABLED` | `false` | Boolean. Enables bounded OpenTelemetry spans when the optional API package is installed; disabled mode does not require it. |

### SDK-generated runner variables

These are transport variables for SDK-started helper processes, not `DaemonSettings` fields. They
are reserved by `modal_daemon_env`; a caller-supplied runner environment cannot override them.

| Variable | Purpose |
| --- | --- |
| `COMPUTER_USE_DAEMON_BASE_URL` | Target daemon URL. Treat as secret-bearing even after query credentials are stripped from reporting. |
| `COMPUTER_USE_DAEMON_TOKEN` | Target bearer token. Secret. |
| `COMPUTER_USE_DAEMON_RUNNER_PATH` | Safe label for the selected transport path. |
| `COMPUTER_USE_TARGET_SANDBOX_ID` | Target Modal Sandbox identifier when available. Treat resource identifiers as sensitive. |

`COMPUTER_USE_BENCHMARK_*` variables and result sentinels are private benchmark-harness protocol,
not product configuration, and are intentionally not part of this reference.

## Boolean and empty-value parsing

Daemon booleans are true only for `1`, `true`, `yes`, or `on`, ignoring case and surrounding
whitespace. Any other non-empty value is false. An unset boolean uses its documented default.

Required integers use their default when unset or empty and otherwise use Python integer parsing.
Optional budget integers interpret unset or empty as unlimited. Browser JSON arguments interpret
unset or empty as `[]`.

## Related documentation

- [Security](security.md) covers tokens, noVNC exposure, URLs, and artifact safety.
- [API](api.md) covers per-request actions, screenshots, and timeouts.
- [Modal deployment](modal-deployment.md) covers Sandbox creation and ingress.
- [Performance](performance.md) explains action timing and screenshot processing tradeoffs.
- [Observe the first visual change](experimental-visual-change-observation.md) covers the Alpha
  synchronization controls, which are request parameters rather than global daemon settings.
