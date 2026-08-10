# Run gateway

The [`modal_run_gateway.py`](../../examples/modal_run_gateway.py) example shows how an application
can expose a bounded spawn-and-poll control plane to a non-Python client. Applications own this
control plane, including its authentication, authorization, storage, and model policy.

## Required application components

- a principal resolver;
- desktop and task ownership catalogs;
- an atomic durable run store;
- one deployed trajectory Function dispatcher;
- a scheduled reconciler.

The public request uses opaque `desktop_key` and `task_key` values plus a required
`idempotency_key`. Public responses contain a stable application run ID and sanitized state. Keep
provider call identities, session handles, task text, results, endpoints, and tokens private.

## Admit one run

The closed run lifecycle begins with `reserved -> dispatching -> running`. It then reaches a
terminal state or `cancellation_requested`.

One application-store transaction handles replay, quota, exclusive ownership of the desktop
identity, run creation, and a payload-free pending dispatch intent. Versioned HMAC-SHA256 bindings
keep raw idempotency keys and internal desktop or task identities out of the run record.

A matching replay returns the existing run before capacity checks. Mismatched replay, desktop
contention, and tenant quota exhaustion return sanitized errors without writes. A second atomic
claim selects the sole dispatcher. A stale dispatch claim becomes `indeterminate` and is never
spawned automatically.

Modal Function dispatch and durable persistence do not form one transaction. The stable
application run ID fences a repeated `borrow_async(run_id=run_id, ...)`, but it cannot reconstruct a
missing FunctionCall identity after a dispatch and persistence gap.

The dispatcher calls the application-owned trajectory Function with
`(handle, task, run_id, deadline_at)`. `deadline_at` is the original timezone-aware value from
admission. A Function retry or container replacement cannot restart the wall-clock budget.

## Reconcile and cancel

`GET /v1/runs/{run_id}` reads durable state. It does not poll Modal or advance the run. The cancel
route records `cancellation_requested`, the request time, and a bounded cancellation deadline.

A scheduled reconciler polls Modal and requests provider cancellation. It keyset-scans bounded
pages and leases each due record atomically. Every write requires the opaque lease token and the
expected record version, so multiple reconciler containers can run safely. A single-container
setting controls cost and does not provide correctness.

Pending polls reset the consecutive provider-error counter. Transient errors use capped
exponential backoff. The run becomes `indeterminate` at the configured error cap. At the absolute
deadline, recovery persists cancellation intent, polls, and then requests
`cancel(terminate_containers=False)` when needed.

Missing call identity, provider ambiguity, the error cap, or the cancellation deadline seals the
run as `indeterminate`. The run continues to hold its quota and desktop claim.

## Recover and retain records

`SAFE_RELEASE` and `SAFE_REPLACE` require a sealed `indeterminate` record, its expected version,
and non-empty actor, reason, and audit identity. Replacement seals the old record and creates a
successor with new run and idempotency identities. It never reopens the ambiguous run.

Terminal `succeeded`, `failed`, and `cancelled` transitions release quota and the desktop claim
once. Retention can compact only released terminal records after the configured interval. Active,
leased, cancellation, unresolved-audit, and `indeterminate` records remain protected. Replay
tombstones must remain authoritative through their fencing window.

The example has no production admin route, migration worker, or database adapter. A previous
`reserve_if_absent` store needs an explicit migration, drain, or backfill. Existing SHA-only rows
cannot safely infer desktop claims.

For HMAC rotation, add the new key as active and retain each prior key referenced by a row or
tombstone. Remove a retiring key only after migration, drain, or expiry has removed every
reference. Admission fails closed when a referenced key version is missing.

## Security boundary

A public gateway must authenticate the caller and authorize the target before it invokes the
Function. The Function's Modal identity resolves fresh desktop access. Never treat a serialized
session handle as a bearer credential or authorization decision.

See [API](../api.md#modal-function-session-handoff) for the handle and borrow contract and
[Security](../security.md) for the runtime threat model.
