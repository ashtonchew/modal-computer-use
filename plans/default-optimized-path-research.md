# Archived superseded plan

Status: **SUPERSEDED — DO NOT EXECUTE**.

GitHub issue #207 and [the product specification](../docs/spec/product-spec.md) are authoritative.
The earlier body was removed because it prescribed `ComputerRuntime`/`Trajectory`, fused
action-plus-screenshot requests, WebSocket control, or HTTP/2 as version 2 article parity.

The accepted cutover uses this composition:

1. An async owner creates the Sandbox once and produces a versioned session handle.
2. An application-owned Modal Function and the Sandbox use one explicit exact region.
3. The Function enters `borrow_async()` once around the full trajectory.
4. One pooled authenticated async HTTP client carries semantic raw-binary screenshots and separate
   ordered action batches.
5. Missing placement or protocol prerequisites fail before lease acquisition or desktop mutation.

Fused requests, WebSocket control, HTTP/2, managed images, positive warm capacity, and universal
resource or region defaults remain separate follow-up experiments. They are not credited for the
article's 37.25 ms screenshot result or its arithmetic 47 ms screenshot-plus-click figure.

Use [the raw screenshot design research](raw-screenshot-default-design-research.md) only for the
binary-response-to-`Screenshot` design decision.
