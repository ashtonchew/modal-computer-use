# Evidence for revising the Modal low-latency article

Research date: 2026-07-28. This is a non-normative writing memo. The accepted dated benchmark
report and its tracked inputs remain authoritative for performance claims.

## Bottom line

The opener can honestly begin with a concrete problem: the default E2B Desktop and Daytona
Computer Use paths made the repeated part of a computer-using agent loop feel slow. In the accepted
comparison, an E2B full screenshot had a 191.70 ms median and a Daytona full screenshot had a
588.74 ms median. A coordinate click had a 221.15 ms E2B median and a 381.63 ms Daytona median.
Modal optimized measured 32.42 ms p50 for the screenshot and 4.43 ms p50 for the click. These are
warm public-path timings, not full model turns, and the provider-default arms used three samples
while Modal optimized used 30 ([provider report](../docs/benchmark-results-2026-07-26-provider-results.md)).

The strongest explanation for choosing Modal is not that E2B or Daytona lacks a usable computer-use
surface. Both products do, and both support customized environments. Modal instead put the two
pieces of this particular optimization in one programmable system:

- a Sandbox target running an owned desktop daemon;
- a separate Modal Function caller;
- the same explicit `region=` selector on both resources;
- authenticated HTTP and WebSocket access to the daemon through Sandbox Connect; and
- Python-defined, layer-cached Images for packaging the desktop and daemon.

That combination let the implementation shorten the caller-to-target route, keep target-side state
alive, and replace compatibility paths inside the target. This is a statement about why Modal fit
this engineering approach, not proof that equivalent work is impossible on another provider.

## The measured pain, with useful numbers

The accepted comparison reports these provider defaults and Modal optimized results
([report](../docs/benchmark-results-2026-07-26-provider-results.md)):

| Warm operation | Modal optimized p50, n=30 | E2B default median, n=3 | Daytona default median, n=3 | E2B / Modal | Daytona / Modal |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full screenshot | 32.42 ms | 191.70 ms | 588.74 ms | 5.91x | 18.16x |
| Coordinate click | 4.43 ms | 221.15 ms | 381.63 ms | 49.92x | 86.15x |
| Four coordinate clicks | 7.02 ms | 887.19 ms | 1,548.00 ms | 126.38x | 220.51x |
| Type 100 characters | 9.95 ms | 4,104.69 ms | 806.05 ms | 412.53x | 81.01x |
| Type 1,000 characters | 49.58 ms | 41,085.75 ms | 5,519.92 ms | 828.68x | 111.33x |
| Non-login shell command | 8.98 ms | 59.18 ms | 287.97 ms | 6.59x | 32.07x |

The four-click row needs an immediate qualification. Modal optimized sent one SDK and transport
request. The measured E2B default path sent four SDK calls and eight transport requests, while the
Daytona path sent four SDK and transport requests. This makes the row useful evidence for batching,
not a provider-wide claim about the fastest possible custom implementation. Screenshots also used
each provider's native/default format: the observed E2B and Daytona frames were 1024x768 PNG, as was
Modal. No codec normalization was applied.

An illustrative trajectory calculation can make the accumulated delay legible without pretending
that the benchmark ran a model. If a 50-turn trajectory consisted of one measured-style coordinate
click followed by one full screenshot per turn, simple arithmetic on the separate warm medians gives:

- Modal optimized: `(4.43 + 32.42) ms * 50 = 1.84 s`;
- E2B default: `(221.15 + 191.70) ms * 50 = 20.64 s`; and
- Daytona default: `(381.63 + 588.74) ms * 50 = 48.52 s`.

The implied differences are 18.80 seconds versus E2B and 46.68 seconds versus Daytona. These are
derived examples, not measured trajectory distributions. They exclude inference, application paint
and settle, model API transfer, retries, and every other action mix. Adding separate medians also
does not produce a measured median for their sum. If used in the article, label the calculation as a
thought experiment and keep it out of the main benchmark table.

## Why repeated latency is a product problem

OpenAI's current Computer use guide defines an iterative loop: inspect `computer_call`, execute all
returned actions in order, capture the updated screen, return it, and repeat until the model stops
calling the tool. It explicitly supports multiple actions in one `actions[]` array
([OpenAI Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)).
The recurring infrastructure wait therefore accumulates across a trajectory, while sandbox
creation ordinarily happens once.

OpenAI says GPT-5.6 Sol on Cerebras can produce up to 750 tokens per second. The same announcement
describes improved agentic and long-horizon capabilities and prices Sol by input and output tokens
([GPT-5.6 Sol preview](https://openai.com/index/previewing-gpt-5-6-sol/)). It is fair to infer that,
as inference becomes faster, hundreds of milliseconds in the computer interface become more visible
in total turn time. Do not claim that 750 tokens per second was observed in these benchmarks, or
that any benchmark measured token savings.

Computer-using agents are valuable precisely because they can complete multi-step work through a
general screen, mouse, and keyboard interface. OpenAI describes the loop as perception, reasoning,
and action, and gives examples such as form filling and multistage workflows
([Computer-Using Agent](https://openai.com/index/computer-using-agent/)). The article can connect
that value to the latency problem in plain terms: every infrastructure pause sits between useful
steps and delays the user waiting for the final result.

## What the competitor products actually expose

### E2B Desktop

E2B's official Computer use guide exposes a Linux desktop with screenshots, mouse and keyboard
actions, commands, and VNC streaming. Its example loop calls one action method at a time, then takes
another screenshot ([E2B Computer use](https://e2b.dev/docs/use-cases/computer-use)). The public
Desktop repository documents `write()` with a default `chunk_size=25` and `delay_in_ms=75`, plus
customizable typing parameters ([E2B Desktop](https://github.com/e2b-dev/desktop)). The benchmark
pinned `e2b-desktop==2.3.1` and called the public defaults
([project dependencies](../pyproject.toml), [benchmark driver](../src/modal_computer_use/benchmarks/provider_comparison/e2b.py)).

E2B is customizable. Templates can start from supported base images, an existing template, or a
Dockerfile-derived definition, and can install packages and run a start command before snapshotting
([base images](https://e2b.dev/docs/template/base-image),
[template quickstart](https://e2b.dev/docs/template/quickstart)). Its public create API does not
show a per-create region field
([create API](https://e2b.dev/docs/api-reference/sandboxes/create-sandbox)). E2B documents a shared
EU cluster for Pro customers and above via a support-assisted setup
([EU region](https://e2b.dev/docs/faq/eu-region)). This supports the narrow claim that Modal's
symmetric, in-code region selector was a better fit for this experiment. It does not prove E2B has
no placement controls under other plans or arrangements.

Neither the E2B computer-use guide nor its Desktop README documents an ordered action-batch method.
The tracked harness therefore used sequential public calls for four clicks. Phrase this as a fact
about the documented and measured default path, not as “E2B cannot batch.” The same sources do not
specify the screenshot capture or input injection backend. Do not attribute E2B's result to a
particular process, library, file path, or network topology.

E2B charges per second while a sandbox is running. Its pricing page lists two vCPUs as the default
and separately prices memory ([E2B pricing](https://e2b.dev/pricing)). That makes reduced runtime a
real cost lever at a fixed resource configuration, but the benchmark did not reconcile bills or
compare equal resource profiles.

### Daytona Computer Use

Daytona's official surface is broad. It supports Linux and Windows computer use, with macOS in
private alpha, and includes mouse, keyboard, full and regional screenshots, compressed screenshot
options, accessibility operations, recordings, and display/window operations. Starting computer
use launches Xvfb, XFCE, x11vnc, and noVNC
([Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/)). Its typing API accepts an
optional inter-character delay. The documentation does not state the default delay or the internal
input and capture mechanisms, so the article should describe the observed public behavior rather
than guess at implementation.

Daytona is also customizable. Snapshots can be built from images or Dockerfiles, captured from
sandboxes, and backed by warm pools ([Daytona snapshots](https://www.daytona.io/docs/snapshots/)).
It lets clients target managed `us` or `eu` regions, as well as dedicated or customer-managed custom
regions ([Daytona regions](https://www.daytona.io/docs/en/regions/)). These facts rule out a broad
claim that Daytona is not feature-complete or cannot support custom optimization.

Daytona documents default Sandbox resources of 1 vCPU, 1 GiB of memory, and 3 GiB of disk
([Daytona Sandboxes](https://www.daytona.io/docs/en/sandboxes/)). The benchmark used the provider's
default snapshot and recorded those documented defaults rather than overriding resources.

The Computer Use guide documents individual action endpoints and contains no ordered action-batch
surface. The tracked four-click benchmark called the documented click path four times
([benchmark driver](../src/modal_computer_use/benchmarks/provider_comparison/daytona.py)). As with
E2B, say “the measured default path used four requests,” not “Daytona cannot batch.”

Daytona bills reserved CPU, RAM, and disk while a sandbox is started and during lifecycle
transitions; its usage views report CPU-seconds, RAM GB-seconds, and disk GB-seconds
([Daytona billing](https://www.daytona.io/docs/billing)). Public pricing is per second for those
resources ([Daytona pricing](https://www.daytona.io/pricing)). Shorter wall-clock time can therefore
reduce sandbox resource-seconds at fixed resources, but this benchmark did not measure a bill.

## Why Modal fit this implementation

Modal does not supply the finished computer-use interface compared in this repository. The project
built that interface on Modal Sandboxes. That distinction makes the architecture story stronger:
the speed came from using lower-level primitives to own the recurring path.

The relevant official Modal capabilities are:

- Functions are independent execution units and can be invoked remotely. Apps may be ephemeral or
  deployed and group Functions into one namespace
  ([Apps and Functions](https://modal.com/docs/guide/apps),
  [invoking Functions](https://modal.com/docs/guide/trigger-deployed-functions)).
- Functions and Sandboxes accept the same `region=` argument. Modal describes these as broad or
  narrow geographic selectors, not host or availability-zone placement
  ([region selection](https://modal.com/docs/guide/region-selection)).
- Sandbox Connect Tokens provide authenticated HTTP and WebSocket access to a server running in a
  Sandbox ([Sandbox networking](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets)).
- Modal Images are defined in Python, support system and Python package installation or existing
  container images, and cache builds by layer ([Images](https://modal.com/docs/guide/images),
  [existing images](https://modal.com/docs/guide/existing-images)).
- Sandboxes accept custom Images and arbitrary commands, while readiness probes let the caller wait
  for a service boundary rather than merely a created object
  ([Sandboxes](https://modal.com/docs/guide/sandboxes)).

The repository uses those primitives directly. Its Image recipe installs the desktop stack and
packages the daemon in a revision-addressed image ([`image.py`](../src/modal_computer_use/image.py)).
The optimized evidence uses one Modal Function runner and target Sandboxes requesting the same Modal
region, then communicates through Connect. The evidence gate also requires the observed cloud and
region labels to match, but this still does not establish the same availability zone, host, private
network, or loopback path
([benchmark report](../docs/benchmark-results-2026-07-26-provider-results.md),
[`modal_optimized_provider.py`](../src/modal_computer_use/benchmarks/modal_optimized_provider.py)).

This is the ergonomic argument to make in first person: “Modal let me express the caller, target,
image, region request, readiness condition, and authenticated daemon connection in one Python
system. That made the latency path inspectable and replaceable.” It is more precise than calling
Modal generally ergonomic, and more credible than diminishing the other products.

## Cost claims and their boundary

E2B, Daytona, and Modal all publish usage-based or per-second Sandbox billing. Modal says Sandboxes
are billed by the second based on the greater of requested and actual resource usage; explicitly
selected regions carry a multiplier
([Modal Sandbox resources](https://modal.com/docs/guide/sandbox-resources),
[Modal region pricing](https://modal.com/docs/guide/region-selection#pricing)). At a fixed provider,
resource profile, and rate, fewer seconds of active work means fewer billed resource-seconds.

The article should stop there. The optimized Modal arm used 4 physical CPU cores, 8 GiB of memory,
an explicit region, a Modal Function caller, and a different topology and sample count. E2B and
Daytona defaults used different resources. The accepted comparison did not call a model, meter
tokens, reconcile invoices, or estimate user-value dollars. It therefore does not establish that
Modal optimized was cheaper per trajectory than E2B or Daytona.

A safe product-stakes formulation is: “Removing repeated waits shortens the user's trajectory and
reduces the amount of time a metered desktop must stay busy. At scale that can matter to cost, but I
measured latency here, not a provider bill or model-token savings.”

## Recommended article claims

- “The E2B and Daytona default computer-use paths I tested were easy to call, but slow in the
  repeated screenshot and input loop.” Pair this with the exact warm numbers and methodology note.
- “Modal optimized returned a 1024x768 PNG in 32.42 ms p50, versus 191.70 ms median for E2B default
  and 588.74 ms median for Daytona default.” State n=30 versus n=3.
- “A coordinate click took 4.43 ms p50, versus 221.15 ms and 381.63 ms on those measured defaults.”
- “On a purely illustrative 50-turn click-and-screenshot trajectory, those separate medians amount
  to about 1.84 seconds of primitive time on Modal optimized, 20.64 seconds on E2B default, and
  48.52 seconds on Daytona default.” Immediately say this was not an end-to-end agent run.
- “Modal gave me a Function caller and Sandbox target with the same requested region selector, while
  Connect preserved authenticated HTTP/WebSocket access to the target daemon.”
- “The point was not merely to pick a faster hosted desktop. It was to own the recurring route and
  target-side implementation.”
- “Only the Modal arm was tuned. The E2B and Daytona columns represent the public default paths used
  by the pinned benchmark harness.”
- “Faster inference makes infrastructure latency easier to see.” Attribute the 750 tokens-per-second
  figure to OpenAI and describe it as an upper bound for a limited Cerebras launch.
- “Less wall-clock work can reduce metered resource-seconds at a fixed configuration.” Keep this as a
  billing mechanism, not a measured savings result.

## Claims to avoid

- “E2B and Daytona are not feature-complete.” Daytona's documented surface includes accessibility,
  recordings, display/window control, and multiple screenshot modes; both providers support custom
  environments.
- “Modal is the only platform where this can be built.” The evidence shows a good Modal fit, not
  exclusivity.
- “E2B or Daytona cannot batch.” Their public computer-use guides do not document ordered batches,
  and the benchmark used sequential calls. That is narrower than impossibility.
- “Competitors use xdotool, subprocesses, files, MSS, XShm, or a particular network route.” Their
  public docs do not expose enough internals to support those attributions.
- “Modal colocated the caller in the same availability zone, host, or private network.” The design
  requested the same Modal region and still used Connect.
- “This saved tokens, model time, or dollars.” No model ran and no provider invoice was reconciled.
- “The 50-turn example is a benchmark result.” It is arithmetic over separate warm medians.
- “Modal won startup.” E2B and Daytona had faster product create-to-validated-screenshot results in
  this comparison. Startup is a separate frontier with non-equivalent templates and substrates.
- “These ratios rank the providers universally.” The rows compare specific defaults, request shapes,
  formats, resources, caller topologies, and sample counts on 2026-07-26.

## Suggested opener spine

1. Begin with the felt problem and two paired numbers: on the defaults tested, the computer could
   spend hundreds of milliseconds merely accepting a click and returning the next frame.
2. Show the accumulation with the explicitly hypothetical 50-turn example.
3. Introduce the product stake: the user waits for the trajectory, and active Sandboxes are metered
   by time and resources.
4. Add the inference pressure: OpenAI advertises GPT-5.6 Sol on Cerebras at up to 750 tokens per
   second, so the computer path can become the visible bottleneck.
5. State the build goal: make the recurring computer path as close to immediate as possible.
6. Explain why Modal: not a prefab computer-use feature, but one Python-native substrate for the
   Function caller, Sandbox target, Image, region request, readiness, and authenticated daemon path.
7. Then deliver the thesis and result, with “only Modal was tuned” close enough that readers cannot
   miss it.

## Persistent connection and Modal cost follow-up, 2026-07-28

### Answer

Keeping one HTTP connection open does not, by itself, establish a higher Modal compute bill than
the measured Modal default. The thing that consumes compute is the running Function or Sandbox
container behind the connection. Modal says an active tunnel has no separate charge, while Sandbox
CPU and memory are billed per second at the greater of requested and actual use
([Tunnels](https://modal.com/docs/guide/tunnels#pricing),
[Sandbox resources](https://modal.com/docs/guide/sandbox-resources#pay-for-what-you-use)). Modal's
Connect documentation establishes authenticated HTTP and WebSocket access, but does not document a
separate Connect-connection price
([Sandbox networking](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets)).
Do not broaden the tunnel statement into “all network transfer is free”: the cited pages do not make
that claim.

Keeping the *Sandbox target* running longer can increase billed resource-seconds. A persistent
daemon is simply a process inside that running Sandbox, not a separately metered product in the
evidence. Modal documents a five-minute default Sandbox maximum lifetime, configurable up to 24
hours, and an optional `idle_timeout`. The Sandbox is considered active while an exec command runs,
stdin is being written, or a TCP connection over one of its Tunnels is open
([Sandbox lifecycle and timeouts](https://modal.com/docs/guide/sandboxes#lifecycle)). That last rule
can make a tunnel connection indirectly affect lifetime when `idle_timeout` is configured. The docs
do not say the same thing specifically about a Connect-token HTTP keep-alive, so the article should
not claim that a Connect connection defeats `idle_timeout`.

The *Modal Function runner* is another container and another resource-time term. Functions scale to
zero by default when they have no inputs. Their containers normally remain idle for at most 60
seconds, although the autoscaler may remove them sooner; increasing the window or maintaining warm
containers can increase cost
([Scaling out](https://modal.com/docs/guide/scale),
[Cold-start performance](https://modal.com/docs/guide/cold-start#keep-containers-warm-for-longer-with-scaledown_window)).
The optimized benchmark ran one Function invocation that owned the benchmark workflow. A reused
HTTP session inside that active invocation should not be described as a second persistent service.
Function execution has its own timeout, separate from post-input container idleness
([Function timeouts](https://modal.com/docs/guide/timeouts)).

### What the tracked evidence actually fixes

The accepted default and optimized arms are not a controlled cost comparison:

- **Modal default target.** The provider-default artifact says it used unmodified
  `ComputerConfig` defaults. Tracked configuration sets the maximum Sandbox lifetime to 3,600
  seconds, leaves `idle_timeout` unset, and passes `cpu=None`, `memory=None`, and `gpu=None` so Modal
  chooses its provider defaults. The sanitized run explicitly records the resolved allocation as
  unavailable. It does record 103.865268499 seconds of measured resource lifetime including cleanup
  ([default artifact](../benchmark-data/provider-compare-coordinate-command-2026-07-26.json),
  [`ComputerConfig` defaults](../src/modal_computer_use/config.py),
  [configuration reference](../docs/configuration.md)). Current Modal documentation describes a
  general default request of 0.125 physical CPU cores and 128 MiB, but those current docs must not be
  retrofitted as a resolved allocation for this historical run
  ([Modal resources](https://modal.com/docs/guide/resources)).
- **Modal optimized target.** The accepted artifact records an explicit request of 4 physical CPU
  cores and 8,192 MiB, Connect ingress, and requested `us-west-2`. The tracked harness configured a
  900-second maximum Sandbox lifetime and no `idle_timeout`. It created 31 fresh targets for the
  product-create samples, terminating each after its first validated screenshot, then created one
  separate warm target, reused it for all warm operation samples, and terminated it after the suite
  ([optimized artifact](../benchmark-data/modal-optimized-provider-2026-07-26.json),
  [optimized harness](../src/modal_computer_use/benchmarks/modal_optimized_provider.py),
  [benchmark procedure](../docs/benchmarking.md)). “Persistent” therefore means reuse within a
  bounded benchmark phase, not a target left running indefinitely.
- **Modal optimized runner.** The accepted artifact records a separate Function runner requesting 4
  physical CPU cores and 8,192 MiB in the same requested region. For the accepted 30+1 schedule,
  the harness gave that Function invocation a 4,650-second execution ceiling. A ceiling is not
  actual runtime: the invocation returned when the benchmark and target cleanup finished. Function
  startup was excluded from the optimized product-create timing
  ([optimized artifact](../benchmark-data/modal-optimized-provider-2026-07-26.json),
  [provider report](../docs/benchmark-results-2026-07-26-provider-results.md)).
- **Missing optimized duration evidence.** The sanitized optimized artifact verifies target cleanup
  and a final sweep with zero remaining Sandboxes, but it does not publish the target and runner
  durations needed to total billed resource-seconds. The comparison therefore cannot say whether
  persistent reuse cost more or less than the 103.865-second Modal-default run, and it cannot derive
  dollar savings.

### Billing variables that must remain separate

Modal bills Sandbox CPU and memory each second using `max(request, actual)`. An idle process is not
automatically free: its request and residual actual use still matter. The same resource guide
applies the request-versus-actual rule to Function and Sandbox containers; GPU reservations have
their own per-second rates on the live pricing page
([CPU, memory, and disk](https://modal.com/docs/guide/resources#billing),
[Modal pricing](https://modal.com/pricing)). Thus the relevant quantities are at least:

1. target Sandbox billed CPU-, memory-, and any GPU-seconds;
2. runner Function billed CPU-, memory-, and any GPU-seconds;
3. each container's actual lifetime, including any billed idle tail;
4. the applicable region multiplier; and
5. any separately documented data-transfer charge, if a transfer path has one.

An explicitly selected broad region currently multiplies base Function or Sandbox usage by 1.5,
and a narrow region by 1.75. The optimized artifact requested a specific region for both runner and
target, while the Modal-default artifact used provider-default placement. Apply a multiplier only
through Modal's current documented classification and pricing, not by guessing from proximity
([Region pricing](https://modal.com/docs/guide/region-selection#pricing)).

Faster completion can lower resource-seconds at a fixed resource profile because Modal bills by the
second with no minimum usage-time increment
([Billing](https://modal.com/docs/guide/billing)). More requested resources can still cost more if
their higher per-second rate outweighs the duration reduction. Conversely, keeping a target warm
between closely spaced actions may avoid startup work and allow the overall job to finish sooner.
Both are mechanisms, not results of this benchmark. The accepted evidence measures latency, not
actual Modal usage records or an invoice.

### Safe article wording

> I kept one Sandbox daemon and its authenticated HTTP session warm across the repeated operation
> samples. The connection itself is not the cost claim: Modal meters the Function runner and
> Sandbox resources behind it by time and usage. Reuse can avoid repeated startup work, while a
> longer-lived target can add idle resource-seconds. My optimized run also requested larger
> resources, an explicit region, and a separate Function runner, so these latency measurements do
> not show dollar savings versus Modal's measured default.

Also safe, when a shorter version is needed: “A persistent connection is not a separate compute
resource, but the containers it keeps useful may remain billable. Faster completion can reduce
resource-seconds at a fixed configuration; this benchmark did not measure a bill.”
