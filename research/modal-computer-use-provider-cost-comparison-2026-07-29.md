# Modal computer-use provider and cost comparison

Research date: 2026-07-29

## Scope and evidence boundary

This memo answers seven questions raised by the current article draft. It uses only tracked repository code and first-party documentation or source repositories from Modal, E2B, Daytona, RustDesk, OpenAI, and Anthropic. It does not inspect environment files, credentials, private endpoints, ignored files, or raw/sanitized benchmark reports. Consequently, it can verify the harness design and public product contracts, but it does not independently revalidate the draft's measured latency values or the placement recorded by a completed run.

## Executive answers

| Question | Short answer |
| --- | --- |
| 1. What do E2B Desktop and Daytona Computer Use expose? | Both expose screenshots, mouse/keyboard actions, desktop streaming, process execution, and customizable environments. Daytona additionally documents accessibility, recording, and window/display APIs in its main Computer Use guide. |
| 2. Can E2B or Daytona own the caller/transport path? | Their managed high-level defaults do not document a Modal-style injectable transport for every desktop operation, but neither is categorically closed: both expose direct/raw APIs and open-source/self-hosted options. Daytona also exposes a configurable API endpoint, and its Go SDK documents a custom HTTP client. |
| 3. What RustDesk lesson applies? | Trace the entire interaction route and identify when relay placement enters the path. RustDesk is inspiration for that diagnostic model, not evidence that Modal Connect is peer-to-peer or copies RustDesk's design. |
| 4. Can one Modal Function serve several Sandbox daemons? | Yes as a design, including concurrently with `@modal.concurrent`, but the current benchmark does not implement that service. It uses one single-use Function input and addresses multiple target Sandboxes sequentially, then one reused warm target. |
| 5. What would the tracked 4-core/8-GiB runner plus target cost for 24 hours? | At current list prices, $24.30/day before regional multipliers, or $36.44-$42.52/day when both allocations use an explicit broad/narrow region. This is a continuously allocated hypothetical, not the benchmark's actual lifecycle. |
| 6. Do current model APIs support action batching? | OpenAI's GA `computer` tool explicitly returns an ordered `actions[]` array. Anthropic's Computer Use contract still presents one `action` per tool-use block; generic parallel tool calls are not an ordered computer-action batch contract. |
| 7. Does the current draft conflict with the evidence? | Mostly no, but the cost paragraph materially misdescribes the tracked Function as warm, the tunnel-pricing statement is attached to a Connect path, and claims of control or provider limitations should remain experiment-specific rather than exclusive. |

## 1. E2B and Daytona product surfaces

### E2B Desktop

E2B's current Computer Use guide describes an Ubuntu 22.04/XFCE desktop and documents VNC streaming, screenshots, mouse movement and clicks, dragging, scrolling, typing, key presses, and shell commands. Its screenshot method returns binary image data. The guide's example loop sends one model-selected action at a time, but this is an integration example rather than a declared platform limit. See [E2B Computer Use](https://e2b.dev/docs/use-cases/computer-use) and the [open-source E2B Desktop repository](https://github.com/e2b-dev/desktop).

E2B Templates provide substantial environment control: callers can select or derive a base image, set environment values and files, install packages, run build commands, and snapshot a start process after a ready check. The base-image documentation supports Debian-derived registry images and Dockerfile conversion, with documented limits such as no multi-stage Dockerfiles. See [Template quickstart](https://e2b.dev/docs/template/quickstart), [base images](https://e2b.dev/docs/template/base-image), and [start/ready commands](https://e2b.dev/docs/template/start-ready-command).

Process execution is available directly through `sandbox.commands.run`. Stateful notebook-style execution is a separate E2B Code Interpreter SDK surface: it has `run_code` and persistent code contexts, rather than being a special Desktop method. See [E2B code contexts](https://e2b.dev/docs/code-interpreting/contexts).

### Daytona Computer Use

Daytona's current Computer Use guide is broader than the draft needs. It documents lifecycle/status operations for the desktop stack; click, move, drag, scroll, and cursor position; typing, key presses, and hotkeys; accessibility-tree lookup and actions; full/region/compressed screenshots; recording; and display/window inspection. It supports Linux and Windows, with macOS described as private alpha. See [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/).

The process module documents stateless Python/JavaScript/TypeScript execution, persistent Python interpreter contexts, shell commands, and long-lived process sessions. Snapshots can be built from images, Dockerfiles, local images, or existing Sandboxes, and Daytona documents per-region warm pools derived from snapshots. See [Process and Code Execution](https://www.daytona.io/docs/en/process-code-execution/), [Snapshots](https://www.daytona.io/docs/snapshots/), and [Warm Pools](https://www.daytona.io/docs/en/warm-pools/).

### Comparison conclusion

The public feature inventory does not support a general claim that Modal alone provides screenshots, input, commands, custom images, or stateful processes. The defensible distinction is narrower: this repository owns a particular daemon implementation and ordered batch contract inside a Modal Image. The tracked Image recipe installs the desktop stack and packages the daemon ([`image.py`](../src/modal_computer_use/image.py#L189-L217)); `ComputerSandbox.create` starts that daemon as the Sandbox entrypoint ([`sandbox.py`](../src/modal_computer_use/sandbox.py#L1040-L1079)); and one FastAPI app owns the desktop backend, input lock, caches, artifacts, recordings, and counters ([`daemon/app.py`](../src/modal_computer_use/daemon/app.py#L99-L141)).

## 2. Caller and transport ownership on E2B and Daytona

The answer depends on whether "own" means customizing a managed SDK call or operating the stack.

### Managed high-level defaults

- E2B Desktop documents high-level operation methods, not a supported per-operation transport callback. E2B does, however, document direct access to the in-Sandbox controller API with authenticated requests. The controller runs inside the Sandbox. See [E2B secured access](https://e2b.dev/docs/sandbox/secured-access).
- Daytona documents its Computer Use operations as individual SDK methods and raw API operations. Its Python configuration exposes an alternate API URL and target, while the Go configuration additionally permits a custom HTTP client. See [Daytona Python configuration](https://www.daytona.io/docs/en/python-sdk/sync/daytona/) and [Daytona Go configuration](https://www.daytona.io/docs/en/go-sdk/types/).

Neither provider's current main Computer Use guide documents one ordered list-of-actions endpoint equivalent to this repository's `ActionBatchRequest.actions`. Absence from the documentation is not proof that batching is impossible or will not be added. The safe article claim is only that the measured default harness used sequential calls.

That harness fact is directly visible in tracked code. E2B loops through four coordinate clicks and records four SDK calls over eight requests according to the pinned SDK implementation ([`e2b.py`](../src/modal_computer_use/benchmarks/provider_comparison/e2b.py#L149-L157), [`e2b.py`](../src/modal_computer_use/benchmarks/provider_comparison/e2b.py#L257-L283)). Daytona loops through the same four clicks and records four SDK/transport requests ([`daytona.py`](../src/modal_computer_use/benchmarks/provider_comparison/daytona.py#L193-L204), [`daytona.py`](../src/modal_computer_use/benchmarks/provider_comparison/daytona.py#L336-L362)). These counts describe this harness and pinned SDK behavior, not an immutable provider capability.

### Operating or replacing the platform path

The exclusivity claim becomes false at this level:

- E2B publishes its Desktop template and infrastructure source, including self-hosting guidance for its infrastructure. See the [E2B Desktop repository](https://github.com/e2b-dev/desktop) and [E2B infrastructure repository](https://github.com/e2b-dev/infra).
- Daytona publishes its platform under an open-source license and describes managed, open-source/self-operated, and customer-managed-compute deployments. See the [Daytona repository](https://github.com/daytonaio/daytona).

Therefore the strongest accurate wording is: **Modal made it convenient for this implementation to colocate a caller and a custom daemon in one Python-defined system, while this benchmark intentionally measured E2B and Daytona through their public managed defaults.** Avoid saying those providers cannot own or shorten the route.

## 3. The RustDesk mental model

RustDesk's self-hosted topology separates an ID/rendezvous/signaling service (`hbbs`) from a relay service (`hbbr`). Clients register their reachable address with the rendezvous service; it attempts a direct connection using hole punching; when that fails, the session uses the relay. RustDesk also recommends regionally closer relays when relay traffic is necessary. See [RustDesk self-hosting](https://rustdesk.com/docs/en/self-host/), [RustDesk Server OSS](https://rustdesk.com/docs/en/self-host/rustdesk-server-oss/), and [relay placement guidance](https://rustdesk.com/docs/en/self-host/rustdesk-server-pro/relay/).

The useful lesson is diagnostic:

1. Trace the path from controller input to remote execution and back to the observed frame.
2. Separate rendezvous/connection setup from the repeated data path.
3. Determine whether traffic is direct or relayed; if relayed, relay geography becomes part of latency.
4. Measure capture, encode, request admission, execution, and response transfer rather than labeling all unexplained time "network."

The analogy must stop there. Modal's Connect documentation describes authenticated HTTP/WebSocket access to a Sandbox server and verified caller metadata; it does not describe Connect as peer-to-peer, hole-punched, same-host, or a RustDesk-style relay protocol. See [Modal Sandbox networking](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets). Modal's separate tunnel guide calls tunnels direct connections implemented over Modal's relay network, but the optimized benchmark selects Connect, not tunnel ingress ([`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py#L426-L443)). The draft should present RustDesk as the source of the full-route question, not as the architecture copied by this project.

## 4. One Modal Function serving multiple Sandbox daemons

### Technical feasibility

Yes. One Function container can hold a routing table of session handles and make authenticated requests to several Sandbox daemons. Modal Functions accept one input per container by default. Adding `@modal.concurrent(max_inputs=...)` lets synchronous Functions process inputs on separate threads and async Functions process inputs as tasks on one event loop; Modal recommends this particularly for I/O-bound remote calls and notes that it can reduce cost. See [Modal input concurrency](https://modal.com/docs/guide/concurrent-inputs).

A production broker would need at least:

- an authorization check and session-to-target mapping for every request;
- separate client/auth material for each Sandbox, never exposed in logs;
- ordering or serialization per desktop, while allowing independent desktops to proceed concurrently;
- thread safety for a synchronous Function or nonblocking I/O for an async Function;
- durable ownership/lease state outside a Function container, because autoscaling, restart, and replacement make in-memory state non-authoritative;
- limits and backpressure so one noisy session cannot exhaust the shared runner;
- isolation-aware cancellation and observability, because synchronous concurrent-input cancellation can terminate the whole Function container and concurrent logs share a stream.

Each target daemon already has a per-process input lock, so one target's action batch is serialized across its ordered actions ([`daemon/app.py`](../src/modal_computer_use/daemon/app.py#L121-L136), [`daemon/actions/batch.py`](../src/modal_computer_use/daemon/actions/batch.py#L121-L180)). The request model is an ordered `actions` list and stops at the first top-level failure unless `continue_on_error` is set ([`models.py`](../src/modal_computer_use/models.py#L509-L525), [`daemon/actions/batch.py`](../src/modal_computer_use/daemon/actions/batch.py#L232-L234)). A broker should preserve that boundary rather than introduce a global lock across unrelated desktops.

### What the current benchmark actually does

The current code is not a concurrent multi-Sandbox service. It defines one Function with `min_containers=0`, `max_containers=1`, and `single_use_containers=True`, invokes it once, and lets it exit ([`sandbox.py`](../src/modal_computer_use/sandbox.py#L574-L619)). Inside that single invocation, lifecycle samples create and terminate fresh target Sandboxes sequentially; afterward, one separate warm target is reused for the warm surface suite and then terminated ([`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py#L228-L309), [`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py#L312-L423)). This proves that one runner can address multiple targets over time, not that the repository already serves multiple desktops concurrently.

### Cost and isolation implication

Concurrent inputs share one Function container's allocated CPU/memory and can reduce the number of Function containers needed for I/O-bound routing. Billing remains container resource-time based, not a special per-input fee. Each target Sandbox remains an independently billed and isolated allocation. A shared Function also creates a larger operational blast radius than one runner per session, so the cost saving must be weighed against tenant isolation, capacity, and cancellation behavior.

## 5. Twenty-four-hour Modal cost scenario

### Inputs

The optimized provider configuration requests 4 physical CPU cores and 8,192 MiB (8 GiB) for both the Function runner and target Sandbox ([`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py#L83-L100), [`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py#L126-L135), [`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py#L426-L443)). Modal states that CPU values are physical cores and that CPU/memory charges use the greater of requested and actual usage for Functions and Sandboxes. See [Modal resource billing](https://modal.com/docs/guide/resources#billing) and [Sandbox resource billing](https://modal.com/docs/guide/sandbox-resources).

Current list rates on 2026-07-29 are:

- Function CPU: $0.0000131 per physical core-second
- Function memory: $0.00000222 per GiB-second
- Sandbox CPU: $0.00003942 per physical core-second
- Sandbox memory: $0.00000667 per GiB-second

Source: [Modal pricing](https://modal.com/pricing).

### Arithmetic

Assume both allocations remain continuously billable for 86,400 seconds, actual usage never exceeds the request, and no other billable resources are used.

| Allocation | Formula | Base daily cost | With explicit region |
| --- | ---: | ---: | ---: |
| Function runner | `86,400 * (4 * 0.0000131 + 8 * 0.00000222)` | $6.06 | $9.09-$10.61 |
| Target Sandbox | `86,400 * (4 * 0.00003942 + 8 * 0.00000667)` | $18.23 | $27.35-$31.91 |
| Combined | Sum | **$24.30** | **$36.44-$42.52** |

The regional range applies Modal's published 1.5x broad-region or 1.75x narrow-region multiplier to both Function and Sandbox. The tracked config requires a nonempty explicit region but does not fix its category, so a single multiplier cannot be selected from tracked code alone. See [Modal region selection and pricing](https://modal.com/docs/guide/region-selection).

### Caveats that must travel with the number

- This is **not actual benchmark cost**. The benchmark runner is a single-use container that exists for one active invocation, not a 24-hour warm Function pool. Its target Sandboxes are terminated after the relevant cases.
- Actual CPU or memory above the request is billed at the higher usage. The calculation assumes the request floor throughout.
- It excludes image builds, storage/Volumes, network or data-transfer charges, model/API usage, plan fees, credits, negotiated discounts, taxes, and the additional fresh Sandboxes created by lifecycle samples.
- Modal documents no separate surcharge for an active **tunnel**. The optimized path uses **Connect**, and the tunnel statement does not establish that Connect or all data transfer is free. See [Modal tunnel pricing](https://modal.com/docs/guide/tunnels#pricing).
- List prices and platform contracts can change; recheck before publication.

Modal Functions scale to zero by default, and `min_containers` is the feature that maintains a warm floor at higher cost. See [Modal scaling](https://modal.com/docs/guide/scale) and [cold-start controls](https://modal.com/docs/guide/cold-start). That distinction is central to the draft correction below.

## 6. Model-generated action batching

OpenAI's current GA `computer` tool is direct evidence that a public model API supports ordered action batching. A `computer_call` contains `actions[]`; the harness is instructed to execute all returned actions in order before returning the next screenshot, and the guide shows a click and type in one call. The migration table contrasts this with the legacy `computer-use-preview` shape, which returned one action per call. See [OpenAI Computer Use](https://developers.openai.com/api/docs/guides/tools-computer-use).

Anthropic's current Computer Use tool has a built-in schema that callers cannot modify. Its implementation guide reads a singular `block.input["action"]`, and Anthropic's tool-combination guidance describes computer use as requiring a screenshot roundtrip for each action. See [Anthropic Computer Use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool) and [Anthropic tool combinations](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-combinations).

Anthropic can return several generic `tool_use` blocks in one assistant turn, but its API does not prescribe their execution order; the application may run them concurrently or sequentially. That facility is not the same as an ordered, dependent desktop `actions[]` contract. See [Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use).

The article should therefore avoid a generic statement that computer-use models emit only one action at a time. A current, provider-neutral sentence would be:

> Some current computer-use interfaces can emit multiple ordered actions in one model turn. Whether those actions become one transport request depends on the harness and primitive API.

This repository's batch endpoint is model-agnostic: the route accepts an action list, validates it, holds one per-daemon input lock, and executes in order ([`daemon/routes/actions.py`](../src/modal_computer_use/daemon/routes/actions.py#L73-L103), [`daemon/actions/batch.py`](../src/modal_computer_use/daemon/actions/batch.py#L137-L180)). An OpenAI `actions[]` response can be mapped to that endpoint, subject to action-schema translation and safety policy. A Claude integration should not assume an ordered batch unless the application deliberately defines and validates one.

## 7. Draft conflicts and recommended corrections

### Material correction: the Function is not kept warm

The draft currently says, "Keeping the Function and Sandbox warm changes the cost shape" and that the optimized path adds a Function runner whose idle warm session accrues duration ([draft line 39](../docs/drafts/modal-optimized-low-latency.md#L39)). The tracked benchmark Function is instead single-use, has no warm-container floor, receives one long-running benchmark input, and exits. The warm-path measurements reuse a target Sandbox only while that input is active.

Suggested replacement:

> The benchmark keeps one Function invocation active while it runs the suite and reuses one warm target Sandbox for the repeated-operation cases; both allocations are billed while they exist. This is not a maintained Function warm pool. A production design that kept a Function container and Sandbox continuously available would trade additional resource time for lower admission and startup latency.

### Material correction: tunnel pricing does not substantiate Connect pricing

The same paragraph says an active tunnel has no separate charge, but the optimized path uses Connect. The statement is correct for the draft's default tunnel path according to Modal's tunnel guide; it should be attached specifically to that path or removed from the optimized-cost discussion. Do not infer a Connect or data-transfer price from the tunnel documentation.

### Tighten: control over both ends is an implementation choice, not exclusivity

The first-person claim that Modal let the author define the Function, Sandbox, Image, placement request, readiness, and daemon is supported by tracked code ([draft line 15](../docs/drafts/modal-optimized-low-latency.md#L15)). "I needed control over both ends" is acceptable as a project-selection explanation. Avoid upgrading it to "E2B and Daytona cannot provide control over both ends," because both publish customizable/open-source/self-hosted paths.

### Keep scoped: same cloud/region placement

The harness requests the same explicit region for runner and target and fails a sample if their reported cloud/region dictionaries do not match ([`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py#L426-L460)). That supports the measurement contract. This memo did not inspect the excluded run artifact, so it cannot independently confirm the draft's statement that the completed run reported a match ([draft line 31](../docs/drafts/modal-optimized-low-latency.md#L31)). The draft already correctly avoids claiming the same availability zone, host, private network, loopback, or direct path. It should also avoid implying that a matching compute region proves the route taken by Connect. Modal separately documents Function input `routing_region`; the benchmark does not set it, and runner dispatch is outside the warm measurement boundary.

### Keep, with pinned-harness wording: provider request counts

The four-click statement is supported by tracked harness code, but should say "in this benchmark with the pinned SDK implementations." It must not imply E2B or Daytona fundamentally lack batching. The current draft's sentence is already close because it says what each measured row used and separately notes that a four-separate-request optimized counterfactual was not measured ([draft line 65](../docs/drafts/modal-optimized-low-latency.md#L65)).

### Supported by tracked implementation

These architectural statements match the code:

- One daemon app owns persistent desktop/session state and an input lock ([draft line 33](../docs/drafts/modal-optimized-low-latency.md#L33); [`daemon/app.py`](../src/modal_computer_use/daemon/app.py#L99-L141)).
- A batch is one ordered action list, validated before execution, serialized under one input lock, and stopped on first error by default ([draft lines 61-67](../docs/drafts/modal-optimized-low-latency.md#L61); [`daemon/actions/batch.py`](../src/modal_computer_use/daemon/actions/batch.py#L121-L180)).
- The optimized harness uses one Modal Function invocation, one requested compute region for caller and target, and Connect ingress ([`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py#L126-L200)).

### Claims not revalidated in this memo

All draft latency percentiles, ratios, lifecycle times, and claims about what a completed artifact observed remain outside this review because the requested evidence boundary excludes benchmark reports and data artifacts. No contradiction was found in tracked code, but absence of a contradiction is not independent measurement validation. Retain dates, sample counts, configuration/topology qualifiers, and differences in screenshot/typing semantics alongside those numbers.

## Publication-safe bottom line

The current evidence supports an article about a deliberately optimized Modal implementation: a custom daemon, persistent desktop primitives, colocated requested compute region, authenticated Connect access, and a one-request ordered batch. It does not support claims that E2B or Daytona lack comparable primitive categories or can never own their transport stack. The cost story should describe an active single-use runner plus temporary/reused Sandboxes in the benchmark, then present any continuously warm 24-hour amount as a separate hypothetical.
