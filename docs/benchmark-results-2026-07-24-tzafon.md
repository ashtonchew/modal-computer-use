# Tzafon provider comparison, 2026-07-24

## Conclusion

Tzafon/Lightcone Computers serves the same core infrastructure purpose as this project, Daytona,
and E2B: it creates an isolated graphical Linux computer and exposes lifecycle, screenshot, mouse,
keyboard, and shell primitives for a computer-use loop. Lightcone as a whole is broader because
Northstar can also own the agent loop through Tasks or the Responses API. This comparison measures
only the directly controlled Computers API, not Northstar.

The fresh provider-default run found Tzafon desktop fastest for product create-to-first-decoded-
screenshot at **317.8 ms p50**, versus 1,267.4 ms for E2B, 10,238.4 ms for Modal's neutral external
path, and 10,566.3 ms for Daytona. Tzafon's tweet claim of 71 ms for a desktop was not reproduced
because the tweet and this harness measure different boundaries.

The fresh separate-runner Modal optimized arm remained substantially faster on warm visual and
input operations. Against Tzafon it was 3.85x faster for a full screenshot, 31.16x for the
move/click semantic case, 49.97x for the sequence, 11.81x for 100-character typing, and 2.95x for
1,000-character typing. Tzafon was 1.06x faster for shell echo.

## Purpose and API fit

The [official Computers guide](https://docs.lightcone.ai/guides/computers/) describes a computer as
an isolated Lightcone OS environment that Northstar operates or that a caller controls directly.
Desktop mode provides a full desktop and filesystem; browser mode places Chromium in the foreground
and adds browser-only tab and proxy capabilities. The API exposes:

- create, keepalive, persistence, and terminate lifecycle operations;
- click, double-click, right-click, drag, key, type, scroll, and screenshot actions;
- native action batches and fused action-plus-screenshot responses;
- synchronous and streaming desktop shell execution; and
- a CDP endpoint for browser computers.

That is the same infrastructure layer this benchmark targets. Tzafon does not expose a standalone
pointer-move action, so the adapter records both logical and provider action counts instead of
pretending the action surfaces are identical.

The benchmark pins `tzafon==2.44.1`, creates a nonpersistent `kind="desktop"` computer, asks for
1024x768, requests inline screenshot data, and leaves the SDK's default two retries enabled. The
live desktop returned 1280x720 JPEG screenshots despite the 1024x768 create request. Modal, Daytona,
and E2B returned 1024x768 PNG screenshots, so screenshot latency is not pixel- or codec-normalized.

## Why the tweet is not this benchmark

The supplied July 23, 2026 tweet reports 63 ms for a Tzafon browser, 71 ms for a Tzafon desktop, and
188 ms for an E2B base sandbox. Its stated metric is server-side work only: TTFB minus the TLS
handshake, median of five runs from San Francisco.

This repository's canonical cold metric starts immediately before the provider product create call
and stops only after the first full-screen image is returned inline, base64-decoded, parsed as an
image, and validated. It therefore includes the client request, network path, provider allocation,
desktop readiness, screenshot capture and encoding, response transfer, and caller-side decode. Each
sample's cleanup is outside the latency timer, while cleanup failure still fails the run.

The tweet's 71 ms and this run's 317.8 ms are not competing measurements. Likewise, its 188 ms E2B
base-sandbox TTFB-derived result is not comparable to the 1,267.4 ms E2B desktop
create-to-first-screenshot result here.

## Fresh provider-default run

All values are p50 milliseconds from one warmup plus three measured iterations. All four providers
passed every case, exact cursor readback, controlled type readback, and cleanup.

| Case | Modal default | Daytona | E2B | Tzafon |
| --- | ---: | ---: | ---: | ---: |
| Product create to first decoded screenshot | 10,238.4 | 10,566.3 | 1,267.4 | **317.8** |
| Full screenshot | 198.3 | 193.0 | 185.7 | **149.6** |
| Move/click semantic case | **155.2** | 354.7 | 211.9 | 169.8 |
| Sequence | **150.3** | 1,415.1 | 838.1 | 490.7 |
| Type 100 characters | 153.6 | 621.4 | 4,057.4 | **124.9** |
| Type 1,000 characters | 192.6 | 5,306.5 | 40,904.8 | **159.3** |
| Shell echo | 190.0 | 112.0 | **55.4** | 86.0 |

Tzafon was 3.99x faster than E2B for the full create-to-pixels lifecycle in this run. Tzafon also
led the neutral screenshot and typing cases. Modal's neutral daemon path led the two pointer cases,
and E2B led shell echo.

The pointer labels require care. Modal, Daytona, and E2B represent move-plus-click semantics.
Tzafon's single case is one coordinate click because the provider has no standalone move call. Its
sequence is four coordinate clicks submitted in one native batch, representing the four destination
clicks of the harness's eight logical move/click actions.

## Fresh Modal optimized context

The optimized arm is a distinct 30-sample run on the same clean source revision. It used a separate
Modal runner and target in requested region `us-west-2`, Connect ingress, HTTP/1.1, a 4 CPU / 8 GiB
Chromium target, a 1 CPU / 1 GiB runner, XTest, zero input throttling, and zero typing delay. It is an
explicit optimized system configuration, not the provider-default Modal product path.

| Case | Modal optimized | Daytona default | E2B default | Tzafon default | Modal optimized vs Tzafon |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot | **38.83** | 193.02 | 185.65 | 149.61 | **3.85x faster** |
| Move/click semantic case | **5.45** | 354.75 | 211.87 | 169.77 | **31.16x faster** |
| Sequence | **9.82** | 1,415.12 | 838.06 | 490.69 | **49.97x faster** |
| Type 100 characters | **10.57** | 621.39 | 4,057.44 | 124.87 | **11.81x faster** |
| Type 1,000 characters | **53.93** | 5,306.55 | 40,904.83 | 159.35 | **2.95x faster** |
| Shell echo | 91.42 | 111.97 | **55.42** | 86.04 | Tzafon **1.06x faster** |

The provider-default values and optimized Modal values are fresh and share source revision
`4f861eeb12981669a9abda1e5ad71daff102b2b5`, but they are separate runs with different caller and
ingress configurations. Ratios are useful configuration context, not a universal provider ranking.

## Reproduction and evidence

Provider-default run:

```bash
uv run computer-use benchmark compare \
  --create-modal-sandbox \
  --provider modal-daemon \
  --provider daytona \
  --provider e2b \
  --provider tzafon \
  --modal-ingress attested-tunnel \
  --resource-profile browser \
  --browser chromium \
  --input-backend xtest \
  --iterations 3 \
  --env-file /path/to/.env \
  --output benchmark-results/candidates/provider-compare-tzafon-2026-07-24.json \
  --json
```

Modal optimized run:

```bash
uv run computer-use benchmark modal-colocated-client \
  --iterations 30 \
  --modal-region us-west-2 \
  --modal-ingress connect \
  --runner-path connect \
  --surface daemon-http \
  --caller-region-label dev-laptop-us-west \
  --browser chromium \
  --modal-cpu 4 \
  --modal-memory-mib 8192 \
  --runner-cpu 1 \
  --runner-memory-mib 1024 \
  --input-rate-limit-per-sec 0 \
  --input-backend xtest \
  --output benchmark-results/candidates/modal-optimized-tzafon-context-2026-07-24.json \
  --json
```

The tracked provider artifact is
[`benchmark-data/provider-compare-2026-07-24-current.json`](../benchmark-data/provider-compare-2026-07-24-current.json).
The compact optimized context is
[`benchmark-data/tzafon-competitive-context-us-west-2-2026-07-24.json`](../benchmark-data/tzafon-competitive-context-us-west-2-2026-07-24.json).
Credential-bearing and ephemeral raw results remain ignored under `benchmark-results/`. The
sanitizer removes endpoint URLs, sandbox IDs, run IDs, and screenshot payloads before an artifact is
tracked. Tzafon pricing remains `unknown` because this run did not expose the resource usage needed
to apply its public usage-based rates.
