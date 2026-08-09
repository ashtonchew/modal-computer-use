# Weighted input-capacity result — 2026-08-08

## Decision

Promote a default refill of 100 normalized input-work tokens per second with a 400-token burst.

The minimum tested runtime completed three independent gates above the 200-token promotion floor.
The slowest run sustained 380.704 tokens per second. This is 3.8 times the product refill and 1.9
times the gate floor.

Do not promote the earlier `500/1000` or `200/400` candidates. Fixed-topology diagnostic runs ranged
from 246.495 to 579.158 tokens per second. Those candidates did not retain a two-times margin across
runtime variance.

## Results

| Run | Weighted tokens/sec | Batch p50 | Batch p95 | CPU seconds/token | RSS growth | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 527.398 | 113.414 ms | 131.890 ms | 0.007156250 | 0 B | Pass |
| 2 | 505.135 | 116.777 ms | 148.103 ms | 0.007408333 | 3,817,472 B | Pass |
| 3 | 380.704 | 149.609 ms | 206.166 ms | 0.008381250 | 29,941,760 B | Pass |

Each run completed 80 measured batches. Each batch contained 48 ordered mixed input actions with a
normalized cost of 60 tokens. All 240 measured batches returned 48 ordered successful results with
native XTest attribution and the expected pointer sentinel.

The artifacts contain no failures or retries. Every lease released cleanly with zero survivors.

## Fixed configuration

- Source revision: `eee2b9456c76474a5b50a857af899ff11ca70a32`.
- Caller topology: one application-owned Modal Function.
- Function and Sandbox placement: AWS `us-west-2`.
- Function and Sandbox resources: 1 CPU and 2,048 MiB each.
- Ingress and transport: attested tunnel over pooled HTTP/1.1.
- Input backend: native XTest.
- Diagnostic limiter: 2,000-token refill and 4,000-token burst.
- Warm capacity: zero Function minimum containers and zero Sandbox pool capacity.
- Borrow/client lifecycle: one borrow and one pooled async client per run.
- Retries and replacement samples: zero.

The diagnostic limiter is higher than the product default so it does not define measured backend
capacity. The product default remains 100/400.

## Promotion gates

A run passes only when it meets every condition:

- at least 200 normalized tokens per second;
- complete ordered results and exact pointer sentinels;
- native XTest attribution;
- no X11, input, timeout, or cleanup failure;
- no retry or replacement;
- stable tail latency;
- no more than 0.02 aggregate cgroup CPU-seconds per normalized token;
- no more than 128 MiB RSS growth;
- exact placement, resources, source revision, ingress, limiter, and warm-capacity configuration;
- clean lease release with zero survivors.

## Scope

This result measures mixed native-input capacity. It does not measure screenshot latency, a fused
Computer Step, first visual change, or application readiness. The separate Computer Step report
measured 44.29 ms p50 for action to immediate frame. A 100-token refill is about 4.4 times the
reciprocal of that single-action median; it is not a latency promise.

## Artifacts

- [`input-capacity-run-1-2026-08-08.json`](../benchmark-data/input-capacity-run-1-2026-08-08.json)
- [`input-capacity-run-2-2026-08-08.json`](../benchmark-data/input-capacity-run-2-2026-08-08.json)
- [`input-capacity-run-3-2026-08-08.json`](../benchmark-data/input-capacity-run-3-2026-08-08.json)

These dated artifacts are immutable. Publish a new dated artifact for any later default change.
