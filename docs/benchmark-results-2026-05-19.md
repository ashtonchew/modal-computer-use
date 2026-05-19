# Provider Screenshot Payload Debug, 2026-05-19

This run was focused on screenshot payload accounting for Daytona and E2B after adding provider
payload metadata instrumentation. It used the provider benchmark worktree on
`research/external-provider-benchmarks` and loaded provider credentials from the existing untracked
worktree env file:

```sh
uv run computer-use benchmark compare \
  --providers daytona,e2b \
  --env-file /Users/ashtonchew/projects/modal-computer-use/.worktrees/provider-benchamark/.env \
  --iterations 10 \
  --output benchmark-results/provider-screenshot-debug-daytona-e2b-10x-20260519.json
```

The raw benchmark output is in `benchmark-results/`, which is ignored. The table below records the
safe, non-secret screenshot metadata needed to compare payload sizes.

## Screenshot Payloads

Both provider screenshot cases completed 10/10 iterations. The overall provider run was marked
failed because Daytona had one transient cold-create `502 Bad Gateway`, and E2B used its default
300-second timeout and expired during the long text-entry case. Those failures do not affect the
screenshot payload rows below.

| Provider | Source | Format | Dimensions | Mean latency | Mean transport bytes | Mean decoded bytes | Last decoded bytes |
|---|---|---|---:|---:|---:|---:|---:|
| Daytona | `ScreenshotResponse.screenshot.base64_string` | PNG | 1024x768 | 609.2 ms | 154612 | 115958 | 115958 |
| E2B | `raw_bytes` | PNG | 1024x768 | 177.6 ms | 14350.6 | 14350.6 | 10521 |

Modal attested tunnel, measured separately on `main`, returned decoded PNG bytes for the same
`1024x768 @ 96 DPI` desktop size:

| Provider | Source | Format | Dimensions | Decoded bytes |
|---|---|---|---:|---:|
| Modal attested tunnel | daemon screenshot response | PNG | 1024x768 | 258319 |

## Interpretation

The original Daytona screenshot byte value was mostly an accounting artifact: the SDK returned a
base64 string, and the old benchmark measured the base64 transport string length. The decoded PNG is
`115958` bytes, not `154612` bytes.

E2B already returns raw PNG bytes, so its transport and decoded byte counts match. Its screenshots
are much smaller than Modal's because the image content and compression output are lower entropy,
not because of base64 accounting.

Modal and E2B are now both being compared as decoded PNG bytes. Daytona must be compared using
`payload.decoded_size_bytes`, not the base64 `transport_size_bytes`.
