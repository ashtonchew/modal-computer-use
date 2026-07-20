# Configuration

The daemon reads its configuration from environment variables. Defaults are sourced from `src/modal_computer_use/daemon/settings.py`.

Precedence: process environment overrides defaults. There is no config file. Secrets such as `COMPUTER_USE_LOCAL_TOKEN` should never be checked into source control or set in shared shells.

## Display and runtime

| Variable | Type | Default | Description |
|---|---|---|---|
| `DISPLAY` | string | `:99` | X display address the daemon and tools attach to. |
| `COMPUTER_USE_DESKTOP_WIDTH` | int | `1024` | Xvfb width in pixels. |
| `COMPUTER_USE_DESKTOP_HEIGHT` | int | `768` | Xvfb height in pixels. |
| `COMPUTER_USE_DESKTOP_DPI` | int | `96` | DPI passed to Xvfb. |
| `COMPUTER_USE_DISPLAY_DEPTH` | int | `24` | Display color depth. |
| `COMPUTER_USE_WINDOW_MANAGER` | string | `xfce` | Window manager to launch. |
| `COMPUTER_USE_BROWSER` | string | unset | Browser command to use for `apps.launch("browser")` and `browser.open_url`. |
| `COMPUTER_USE_BROWSER_PREWARM` | bool | `false` | Start the browser at daemon boot to cut first-action latency. |
| `COMPUTER_USE_BROWSER_PROFILE_DIR` | path | `/home/desktop/.cache/modal-computer-use/browser-profile` | Shared browser profile directory used by prewarm and `browser.open_url`. |
| `COMPUTER_USE_BROWSER_LAUNCH_ARGS` | JSON list | `[]` | Extra browser command-line args appended to prewarm and `browser.open_url` launches. |
| `COMPUTER_USE_BROWSER_OPEN_URL_ON_START` | URL | unset | Optional startup URL. If set, daemon startup opens this URL instead of `about:blank` prewarm. |
| `COMPUTER_USE_BROWSER_GPU_MODE` | string | `auto` | Browser GPU launch mode. One of `auto`, `off`, or `chromium-vulkan`. |
| `COMPUTER_USE_RUN_ID` | string | unset | Caller-supplied run identifier echoed in traces and logs. |
| `COMPUTER_USE_IMAGE_PROFILE` | string | `standard` | Image profile label reported by `/v1/capabilities`. Common values: `standard`, `browser`, `browser-gpu`. |

## Backend selection

| Variable | Type | Default | Description |
|---|---|---|---|
| `COMPUTER_USE_BACKEND` | string | `auto` | Desktop backend. `auto` uses `x11` on POSIX systems so readiness fails closed when desktop tools are missing. Set explicitly to `mock` for deterministic local tests. |
| `COMPUTER_USE_VNC_MODE` | string | `off` | VNC exposure. One of `off`, `view_only`, `control`. The daemon refuses to start a VNC server when `off`. |

## Auth and access control

| Variable | Type | Default | Description |
|---|---|---|---|
| `COMPUTER_USE_LOCAL_TOKEN` | string | unset | Bearer token clients must send as `Authorization: Bearer <token>`. Required for the local backend. Never set in production. |
| `COMPUTER_USE_TUNNEL_TOKEN` | string | unset | Static bearer token accepted on Modal tunnel ingress. Set by raw `ingress="tunnel"` mode; attested tunnel mode mints short-lived in-memory tokens instead. |
| `COMPUTER_USE_TUNNEL_TOKEN_TTL_SECONDS` | int | `3600` | Lifetime for tokens minted by `/v1/session/tunnel-authorize` after Modal Connect authentication. |
| `COMPUTER_USE_REQUIRE_CONNECT_USER` | bool | `true` | In Modal mode, require Modal's `X-Verified-User-Data` header on control requests. `/healthz` and `/readyz` are unauthenticated probes. |
| `COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY` | bool | `false` | Trust verified-user headers from private or link-local client IPs. Modal-created sandboxes set this to `true` because Modal Connect can reach the daemon from private network space; leave disabled for arbitrary deployments. |
| `COMPUTER_USE_REJECT_QUERY_TOKENS` | bool | `true` | Reject auth tokens passed in URL query strings. URLs leak into logs and browser history; leave this on. |

## Limits and budgets

| Variable | Type | Default | Description |
|---|---|---|---|
| `COMPUTER_USE_MAX_BATCH_ACTIONS` | int | `50` | Hard cap on actions per batch request. |
| `COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC` | int | `20` | Rolling one-second per-sandbox limit for executable input actions. Set `0` to disable in trusted benchmark or local test harnesses. |
| `COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS` | int | `5000` | Per-action timeout when the request does not specify one. |
| `COMPUTER_USE_MAX_ACTION_TIMEOUT_MS` | int | `300000` | Upper bound a request can ask for. |
| `COMPUTER_USE_POST_ACTION_DELAY_MS` | int | `0` | Explicit action/batch timing control inserted after UI-mutating actions before a following action or `screenshot_after`; the Alpha visual-change composition does not bypass it. |
| `COMPUTER_USE_IDEMPOTENCY_CACHE_MAX_ENTRIES` | int | `1000` | Idempotency-key cache size. Set to `0` to disable caching instead of retaining an unbounded cache. |
| `COMPUTER_USE_IDEMPOTENCY_CACHE_TTL_SECONDS` | int | `3600` | Idempotency-key cache TTL. |
| `COMPUTER_USE_MAX_ACTIONS` | int | unset | Optional ceiling on attempted executable desktop actions per run. Failed and timed-out actions count; validation failures, idempotency replays, screenshots, zooms, and cursor-position queries do not. |
| `COMPUTER_USE_MAX_SCREENSHOTS` | int | unset | Optional ceiling on total screenshots per run. |
| `COMPUTER_USE_MAX_ARTIFACT_BYTES` | int | unset | Optional ceiling on artifact bytes written per run. |
| `COMPUTER_USE_MAX_RECORDING_SECONDS` | int | unset | Optional ceiling on recording duration per run. |
| `COMPUTER_USE_SCREENSHOT_MAX_PIXELS` | int | `8294400` | Reject `screenshots.region` requests larger than this (default is 4K). |

## Recording, artifacts, and traces

| Variable | Type | Default | Description |
|---|---|---|---|
| `COMPUTER_USE_ARTIFACTS_DIR` | path | `/home/desktop/artifacts` | Root for screenshots, downloads, recordings, and traces. |
| `COMPUTER_USE_RECORDINGS_DIR` | path | `/home/desktop/recordings` | Recording output directory. |
| `COMPUTER_USE_TRACE_DIR` | path | `/home/desktop/artifacts/traces` | Trace NDJSON directory. |
| `COMPUTER_USE_TRACE_ACTIONS` | bool | `false` | Append every action to `actions.ndjson`. |
| `COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION` | string | `auto` | Where to resize and encode screenshots. `auto`, `daemon`, or `client`. |

## Observability

| Variable | Type | Default | Description |
|---|---|---|---|
| `COMPUTER_USE_OTEL_ENABLED` | bool | `false` | Enables optional OpenTelemetry spans when `opentelemetry-api` is installed. Disabled mode does not import or require OpenTelemetry packages. Spans use bounded route/action/artifact metadata only. |

## Boolean parsing

Boolean variables accept `1`, `true`, `yes`, `on` (case-insensitive). Anything else, including unset, reads as false.

## Related docs

- [security.md](security.md) for token handling and noVNC exposure rules.
- [glossary.md](glossary.md) for what `CoordinateSpace`, `artifact`, and `Connect Token` mean.
- [experimental-visual-change-observation.md](experimental-visual-change-observation.md) for the
  synchronization decision ladder and Alpha limitations.
- [performance.md](performance.md) for measurement details for
  `COMPUTER_USE_POST_ACTION_DELAY_MS` and the screenshot processing location.
- [modal-deployment.md](modal-deployment.md) for setting these on a Modal Sandbox.
