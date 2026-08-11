# Modal Sandbox CPU cost and X11-SHM evidence

Research date: 2026-08-11

Status: research note. Recheck the live Modal pricing page before
publishing a customer-facing number.

## Short answer

For the target desktop Sandbox used by the X11-SHM CPU ablation, moving from
`cpu=1.0` to `cpu=2.0` adds one Sandbox physical-core rate: **$0.141912 per
hour**, or **$3.405888 for 24 hours**, before credits, regional multipliers,
memory, or other resources. The CPU component doubles. Memory and other
resource charges stay the same, so the total Sandbox cost increases by about
75% for the tested 2-GiB configuration.

Two important interpretations travel with that number:

1. Modal's `cpu` value is in physical cores; one physical core is two vCPUs.
   This ablation compares 2 vCPUs with 4 vCPUs.
2. Modal bills each resource by the second using the higher of the requested
   amount and actual usage. The extra core sets a billing floor for the full
   lifetime of the Sandbox.

Sources: [Modal Sandbox + Notebooks pricing](https://modal.com/pricing),
[Sandbox resources and pricing](https://modal.com/docs/guide/sandbox-resources),
and [CPU, memory, and disk configuration](https://modal.com/docs/guide/resources).

## Current list rates

The rate relevant to a desktop `modal.Sandbox` is the rate in Modal's
"Sandbox + Notebooks Pricing" section:

| Resource | Current list rate | Contract detail |
| --- | ---: | --- |
| Sandbox CPU | `$0.00003942 / physical core / second` | 1 physical core = 2 vCPUs; minimum 0.125 cores/container |
| Sandbox memory | `$0.00000667 / GiB / second` | Charged independently using the same max(request, actual) rule |

Modal's standard compute section shows `$0.0000131 / physical core / second`.
That lower rate applies to a separately billed standard Modal Function runner.
The target Sandbox uses the Sandbox rate above. See [Modal pricing](https://modal.com/pricing).

## CPU-only arithmetic for one target Sandbox

The calculations below assume the requested and actual CPU are at most the
configured value, so the request is the billable floor. Values are rounded for
display; arithmetic uses the exact listed rate.

| Lifetime | `cpu=1.0` | `cpu=2.0` | Increment for 2 CPUs |
| --- | ---: | ---: | ---: |
| 1 second | $0.00003942 | $0.00007884 | **$0.00003942** |
| 1 minute | $0.00236520 | $0.00473040 | **$0.00236520** |
| 1 hour | $0.141912 | $0.283824 | **$0.141912** |
| 24 hours | $3.405888 | $6.811776 | **$3.405888** |
| 720 hours (30 × 24 h) | $102.176640 | $204.353280 | **$102.176640** |

Formula: `billable_seconds × configured_physical_cores × $0.00003942`.

## The ablation's 2-GiB memory footprint

The CPU ablation keeps memory at 2 GiB for both arms. At Modal's listed
`$0.00000667 / GiB / second` memory rate, that is `$0.048024/hour`. This charge
stays constant when CPU increases. The resulting target-Sandbox totals are:

| Lifetime | 1 CPU + 2 GiB | 2 CPU + 2 GiB | Increment |
| --- | ---: | ---: | ---: |
| 1 hour | $0.189936 | $0.331848 | **$0.141912** |
| 24 hours | $4.558464 | $7.964352 | **$3.405888** |
| 720 hours | $136.753920 | $238.930560 | **$102.176640** |

Thus the 2-CPU arm is about 1.75× the total resource cost for this exact
2-GiB shape, while its CPU-only component is exactly 2×.

## If the separate Modal Function runner also changes CPU

If a design raises a separately billed standard Modal Function from one to two
physical cores for the same lifetime, add the standard-rate increment below.
This is separate from the target Sandbox calculation:

| Lifetime | Extra standard-Function CPU for 1 additional core |
| --- | ---: |
| 1 hour | $0.047160 |
| 24 hours | $1.131840 |
| 720 hours | $33.955200 |

The runner's memory and the target Sandbox's CPU/memory are additional
resources; this table is only the incremental Function CPU component.

## Credits and multipliers

- **Starter:** `$0/month` plan fee and `$30/month` free compute credits.
- **Team:** `$250/month` plan fee and `$100/month` free compute credits.
- **Enterprise:** custom pricing and credits.
- The pricing page currently lists **1.5 to 1.75 times base prices** for explicit
  region selection and **3× base prices** for non-preemptible execution.

Ignoring memory, the `$30` Starter credit covers about 211.4 one-CPU-hours or
105.7 two-CPU-hours of one target Sandbox. The `$100` Team credit covers about
704.7 one-CPU-hours or 352.3 two-CPU-hours. Credits apply to the workspace's
aggregate eligible usage.

At the published 1.5 to 1.75 times region multiplier, the target-Sandbox
incremental CPU cost is `$0.212868` to `$0.248346` per hour, or `$5.108832` to
`$5.960304` per day.
Non-preemptible execution is listed separately as 3× base pricing. Confirm how
any selected region and execution mode combine for the specific deployment.

Sources: [Modal pricing](https://modal.com/pricing) and [Modal billing](https://modal.com/docs/guide/billing).

## Deployment guidance

For latency-sensitive sessions that use X11-SHM, prefer 2 physical CPUs when
the additional active-Sandbox cost is acceptable. Keep MSS as the default for
cost-sensitive or long-lived sessions.

The counterbalanced diagnostic ran the CPU profiles in both orders. The 1-CPU
profiles completed 3,208 captures. They had 13 successful calls of at least
500 ms, a rate of 0.41%, and one native X11 reply timeout. The 2-CPU profiles
completed 4,000 captures. They had no calls of at least 500 ms and no native
timeouts. Their maximum call time was about 428 ms.

An earlier three-run diagnostic showed the same direction. Its 1-CPU profiles
had 19 calls of at least 500 ms across 4,561 completed captures, a rate of
0.42%, and two native timeouts. Its 2-CPU profiles had 2 calls of at least
500 ms across 6,000 captures, a rate of 0.033%, and no native timeouts. This
earlier diagnostic used a different source revision and protocol. Its counts
remain separate from the counterbalanced result.

The evidence supports a lower observed tail and timeout risk with 2 CPUs.
Effective cgroup and scheduler run-queue evidence was unavailable, so the cause
remains unresolved. Possible causes include CPU quota, host
scheduling, X11 waiting, and process or executor scheduling. Two CPUs are an
operational mitigation. The SDK has no two-CPU requirement, and the native
deadline and fallback remain necessary.

The extra CPU costs `$0.023652` for a 10-minute Sandbox or `$0.141912` per hour
at the base rate. The published explicit-region multiplier raises the hourly
increment to `$0.212868` to `$0.248346`. A continuously active 30-day Sandbox
adds `$102.176640` at the base rate, before region multipliers and credits.

Keep the 750 ms native deadline and automatic MSS fallback for both CPU
configurations. Use a measured service-level target and active Sandbox lifetime
to decide whether the lower observed risk is worth the added cost.

## Evidence provenance

The sanitized JSON artifacts remain local. This table records their identities.
The SHA-256 digest covers the complete artifact file.

| Diagnostic | Source revision | Execution order | Status | Artifact SHA-256 |
| --- | --- | --- | --- | --- |
| Counterbalanced forward | `0b131011deca53a6a5c619a1ecdaa9127cf363c9` | 1 CPU, then 2 CPUs | exploratory | `c39ddf0143499de426b5faf1034eabdbfc7f68ddf0cf6167666bf2673fc1c7fb` |
| Counterbalanced reverse | `0b131011deca53a6a5c619a1ecdaa9127cf363c9` | 2 CPUs, then 1 CPU | rejected after the 1-CPU timeout | `87c79712732fc20a36442712e7cc6ab4d9d85f23615c58c7c993de95e15fae93` |
| Earlier baseline | `9259a81b6ee7d9b2a436e169f5e33f563bc8ae8c` | 1 CPU, then 2 CPUs | exploratory | `63698f14c567c21528b05d3b4a61ba6238c726a66f90993a63452ad835251d0d` |
| Earlier repeat 2 | `9259a81b6ee7d9b2a436e169f5e33f563bc8ae8c` | 1 CPU, then 2 CPUs | rejected after the 1-CPU timeout | `5fc959254f310e593c6313adb807275607a945d8dbd145c7d72eea39e234b67b` |
| Earlier repeat 3 | `9259a81b6ee7d9b2a436e169f5e33f563bc8ae8c` | 1 CPU, then 2 CPUs | rejected after the 1-CPU timeout | `73fcd6610cffc56b9f7864caff2a1ed2944413d4b3b7f80510669922da70804f` |

The counterbalanced protocol added timeout scheduler evidence and reversed the
profile order. Its measurements stay separate from the earlier three-run
diagnostic.

Modal also states that there are no minimum usage-time increments and that
Workspaces are billed monthly, so these examples scale with actual Sandbox
lifetime instead of rounding every call to a minute or hour. See [Modal
billing](https://modal.com/docs/guide/billing).
