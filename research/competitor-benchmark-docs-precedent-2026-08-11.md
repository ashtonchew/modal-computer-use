# Competitor benchmark documentation precedent, 2026-08-11

> **Status:** Non-normative research
> **Research cutoff:** 2026-08-11
> **Scope:** Public benchmark methodology, result tables, and source repositories from first-party developer-tool and infrastructure vendors.
> **Authority:** This note informs presentation choices. Product contracts and measured evidence remain authoritative.

## Decision summary

Use a dated benchmark landing page with one compact matched-workload table. Put p50 and p95 in the primary cells. Show sample count, run date, timer boundary, and evidence status beside the table. Follow the table with one configuration block per provider. Include hardware or cloud instance, topology, region, software versions, workload version, warmup, retry policy, and failure count. Link each row to sanitized raw artifacts, the source commit, and the exact runner command.

Keep history immutable. Give each refresh a new dated report. Separate measured results from arithmetic, diagnostic runs, and results with different timer boundaries. State the workload fit and the limits of the comparison in the result page itself.

## Current local documentation shape

The local repository already has a useful evidence hierarchy:

- [`docs/README.md`](../docs/README.md) separates the public Mintlify site from engineering contracts, benchmark evidence, procedures, and release records. Its Benchmark section links the general procedure, dated reports, and archive policy.
- [`docs/benchmarking.md`](../docs/benchmarking.md) owns preregistration and publication rules. It fixes caller topology, target, placement, resources, image, ingress, HTTP version, input backend, screenshot format, action payload, warmup, connection reuse, and capacity. It requires raw observations, failure and cleanup records, and explicit gates.
- [`docs/benchmark-results-2026-07-30-warm-paths.md`](../docs/benchmark-results-2026-07-30-warm-paths.md) is the local competitor article. It publishes a p50 and p95 table, states 30 successful samples per cell, defines percentile interpolation and ratios, describes each path, separates timer boundaries, and links tracked JSON artifacts plus a deterministic figure check.
- [`README.md`](../README.md) is a short public entry point. It shows a p50 chart and links the dated action-to-frame report for the timer boundary, path configuration, and p95 values.
- [`research/computer-use-landscape.md`](computer-use-landscape.md) classifies providers by role. It names Daytona, Scrapybara, E2B Desktop, Browserbase, and browser frameworks, then points readers to the measured provider report. This keeps product positioning separate from benchmark evidence.

A stable public landing page should put the measured cases, evidence status, and configuration summary in one view. The current report already contains the data and provenance.

## First-party precedent

### Modal endpoint benchmarks

Sources: [Benchmark an endpoint](https://modal.com/docs/guide/endpoint-benchmarks) and [Endpoint metrics](https://modal.com/docs/guide/endpoint-metrics).

Modal distinguishes live endpoint benchmarks from recipe preview benchmarks. The live benchmark drives a standard load generator against the user endpoint from a Sandbox. It offers two explicit workload shapes:

- Real-time generation: about 3,000 input tokens and 100 output tokens with randomized prompts.
- Agentic multi-turn: a shared prefix of about 45,000 tokens, a 5,000-token question, and about 200 output tokens.

The page says recipe values were measured ahead of time on a known GPU configuration. It tells users to use recipe values for model selection and endpoint runs for validation in their own region and settings. Caveats cover real traffic and cost, point-in-time fleet and cold-start effects, warmup, region, and workload mismatch.

The metrics page defines p50, p95, and p99 for TTFT, inter-token latency, and end-to-end latency. It warns that cold starts skew early windows and that percentiles need enough request volume. The pages show a concise pattern table, a clear distinction between reference and deployment measurements, and a short caveat list.

For this project, publish the workload shape and deployment context with each result. Label synthetic reference data separately from deployed endpoint measurements. State cost and point-in-time limits.

### Browserless hosted browser benchmark

Sources: [How fast is your hosted browser?](https://www.browserless.io/blog/hosted-browser-benchmarking) and the [browserless/benchmarks source repository](https://github.com/browserless/benchmarks).

The article compares Browserless, Anchor Browser, Browserbase, and Hyperbrowser with one Puppeteer flow. It measures three sequential lifecycle points: `puppeteer.connect`, `browser.newPage()`, and `page.goto(URL)`. Each metric is reported as average, fastest, and slowest. The article identifies the same URL, same script, same region, and repeated runs as the comparison controls. It says geography and provider changes can shift the ranking.

The repository makes the harness executable. Its sample configuration sets `TOTAL=10` and a target URL. The output records provider, browser version, total executions, URL, and a table for connection, page creation, and navigation. Provider adapters isolate session setup and teardown. The article provides clone, install, environment, and run commands. It also reports concrete values, such as 692.5 ms average connection time for Hyperbrowser and 166.2 ms average navigation time for Browserless.

The article does not publish p50 or p95, a client hardware description, a run date in the result block, or raw result files. Fastest and slowest are a useful tail signal for a ten-run smoke comparison. They are weaker than percentile estimates for a larger campaign.

For this project, use one provider adapter per row and one shared workload. Record the run count, version, p50, p95, caller and target topology, timestamp, and sanitized JSON or CSV.

### PlanetScale Postgres benchmark

Sources: [Benchmarking Postgres](https://planetscale.com/blog/benchmarking-postgres) and [PlanetScale vs Amazon Aurora benchmarks](https://planetscale.com/benchmarks/aurora).

PlanetScale publishes a methodology page plus one result page per competitor. The Aurora result page starts with a configuration table:

| Field | Published value or rule |
| --- | --- |
| Target instances | PlanetScale M-320 and Aurora `db.r8g.xlarge` |
| Region | `us-east-1` for both targets |
| Target resources | 4 vCPUs and 32 GB RAM for both |
| Storage | 937 GB NVMe for PlanetScale, autoscaling for Aurora |
| Benchmark client | AWS `c6a.xlarge` in `us-east-1` |
| Workload | Percona TPCC-like, 500 GB, 32 and 64 connections, 300 seconds |
| Read workload | Sysbench `oltp_read_only` and point selects, about 300 GB |
| Query-path check | `SELECT 1;` 200 times on one connection |
| Tail metric | p99 latency over the run |

The methodology explains why the product and competitors receive matched or greater vCPU and RAM, where resource ratios force a CPU advantage, and how storage IOPS are selected. It states that defaults are retained except for connection limits and timeouts used for benchmarking. It links scripts and instructions for reproducing the dataset and runs. The result pages publish QPS and p99 views, workload duration, connection counts, and pricing assumptions. They also disclose same-AZ caveats for direct query-path comparisons and the fact that only the primary was used even when a production configuration includes replicas.

The companion [On benchmarking](https://planetscale.com/blog/on-benchmarking) article gives explicit reporting guidance. It recommends p50, p90, p95, p99, variance or error bars, and full time series. It calls for cache warmup, repeated runs, exact hardware or cloud instance, OS, software versions, build flags, benchmark tool, configuration, and command line. It warns about coordinated omission, noisy neighbors, client bottlenecks, missing hardware, average-only reports, and comparisons across unlike workloads.

For this project, put hardware, region, resource matching, workload size, duration, and timer metric beside the result. Explain resource asymmetry and availability topology. Link a runnable reproduction path. Include tail latency, throughput, and cost when the experiment measures them.

### ClickHouse ClickBench and Hardware Benchmark

Sources: [ClickBench source repository](https://github.com/ClickHouse/ClickBench), [ClickHouse benchmark hub](https://clickhouse.com/benchmarks), and [How to test your hardware with ClickHouse](https://clickhouse.com/docs/concepts/features/performance/troubleshoot/performance-test).

ClickBench is a public cross-database benchmark. The repository defines 43 queries over an anonymized production-derived dataset with 99,997,497 rows. It uses mostly standard SQL and a common schema. The default machine is an AWS `c6a.4xlarge` with a 500 GB gp2 disk. A submission stores JSON results under `results/YYYYMMDD/<machine>.json`; older dated runs remain available. The repository includes scripts for install, data load, query execution, checks, and result generation. The project says a run can be reproduced in about 20 minutes for many systems.

The execution rules are explicit:

- Every query runs three times.
- The first run is cold. The smaller of runs two and three is the hot result.
- True cold runs clear operating-system and database caches. A restart is required when a database has no cache-clear command.
- Query result caches are disabled. Source-data and intermediate caches follow stated rules.
- Timings include client send, server processing, and result transfer. Output suppression that makes result transfer free is disallowed.
- Unsupported or failed queries remain visible as `null` in raw results. The summary applies a documented penalty.
- The dashboard supports cold, hot, load-time, data-size, and combined metrics. Combined uses a documented weighted geometric mean.

The README names important limits: one flat table, one-node bias in many results, sequential queries, few repetitions, and difficult direct comparisons across unlike systems. It advises readers to treat scoreboards cautiously. The Hardware Benchmark adds a separate public table of machines, hot or cold mode, relative time, hardware notes, contributor identity, and warnings about runs with unflushed caches.

For this project, date evidence artifacts, retain history, publish workload and cache rules, measure end to end, keep failed cases visible, and state aggregation formulas. Use a separate hardware table when the experiment compares machines.

### Deno serverless cold-start benchmark

Sources: [Benchmarking AWS Lambda cold starts across JavaScript runtimes](https://deno.com/blog/aws-lambda-coldstart-benchmarks) and the [denoland/serverless-coldstart-benchmarks source repository](https://github.com/denoland/serverless-coldstart-benchmarks).

Deno compares Deno, Node, and Bun for Express, Fastify, and Hono applications. The Lambda repository README records AWS region `us-west2`, 512 MB memory, Deno 1.45.2, Bun 1.1.19, and Node.js 22.5.1. The runner forces a cold start, parses `Init Duration` from CloudWatch, and writes CSV output. The article states 20 iterations for each runtime and framework combination. The repository stores raw CSV files by framework and architecture.

The article adds a second check on a Google Cloud `e2-medium` VM in `us-west1`. It uses the same applications, exits after initialization, and runs `hyperfine --warmup 2`. The published blocks show mean, standard deviation, range, and run count. The article explains that Lambda `Init Duration` excludes code-artifact copy time and client network RTT. It treats that metric as a proxy because images are under 85 MB and initialization dominates the tested workload.

For this project, record runtime and framework versions, region, resources, architecture, warm or cold policy, run count, sanitized evidence, and excluded phases. Use a second environment when the experiment tests whether a result generalizes.

## Cross-source comparison

| Source | Matched workload and topology | Hardware, version, date, sample disclosure | Percentiles or variance | Raw artifacts and reproducibility | Caveats made visible |
| --- | --- | --- | --- | --- | --- |
| Modal endpoint docs | Two named prompt shapes. Live endpoint load from a Sandbox. | Recipe benchmark uses a known GPU configuration. Endpoint region and fleet affect results. Page is current documentation, not a dated result report. | Endpoint metrics define p50, p95, p99. | Dashboard stores run metrics. Public page does not expose a raw file or command line. | Cost, autoscaling, cold starts, point-in-time fleet, and workload mismatch. |
| Browserless | One Puppeteer script, one URL, one region, same lifecycle steps across four providers. | `TOTAL=10`, browser version, URL, and provider in runner output. Client hardware and exact run date are absent. | Average, fastest, slowest. No p50 or p95. | Open source Node runner and provider adapters. | Geography, provider changes, and workload differences. |
| PlanetScale | Same-region client and target. Matched or greater resources and explicit connection counts. | Instance, vCPU, RAM, storage, IOPS, region, client VM, defaults, 300-second duration. Published 2025 methodology and dated result pages. | QPS plus p99. Methodology recommends p50, p95, p99, variance, and time series. | Per-competitor instructions, Percona and sysbench scripts, reproducible data generation. | Resource-ratio asymmetry, AZ limits, replicas and pricing, defaults, noisy neighbors. |
| ClickBench | Common dataset and mostly standard SQL. Default AWS VM. Sequential 43-query sweep. | Dated JSON path, machine, cache mode, and source scripts. | Per-query times, cold and hot runs, geometric aggregation. | Public scripts, queries, data, dated JSON, generated dashboard. | Flat table, limited repetitions, one-node bias, unsupported queries, unlike systems. |
| Deno | Same applications and runtime entry points across Deno, Node, and Bun. Lambda plus VM validation. | AWS region, memory, runtime versions, architecture-specific raw files, 20 cold iterations. VM machine, region, warmup, and hyperfine runs. | Mean, standard deviation, range, and run count. | Open source Dockerfiles, apps, runner, CSV, and raw results. | Init Duration excludes artifact copy and network RTT. VM startup is a separate boundary. |

## Recommended presentation format for this project

Use four linked layers:

1. **Dated landing page.** Put evidence status, run date, source commit, sample count, workload name, and a compact p50 and p95 table above the fold. Mark unavailable or non-comparable cells as such.
2. **Configuration and method block.** Show caller topology, target topology, requested and observed region, resources, image or runtime version, browser or SDK version, ingress and protocol, connection reuse, warmup, rate limits, timeout, retry and replacement policy, and timer boundaries.
3. **Per-provider detail sections.** For each provider, state the exact API path, action or script, target URL or fixture digest, screenshot or output format, setup and cleanup phases, failures, survivors, and cost scope. Explain resource asymmetry and any provider-specific defaults.
4. **Evidence links.** Link sanitized raw JSON or CSV, source commit, benchmark runner, command line, schema, figure generator, and a deterministic check. Keep historical reports immutable and place rejected or diagnostic runs in an archive.

The primary table should use one row per matched case and one column per provider. Use a two-line cell when needed: `p50 / p95 ms` on line one and `n=30` on line two. Keep ratios in a separate column or detail table. Put arithmetic combinations in a separate section with a label that says they are arithmetic. Do not mix a fused action-to-frame result with two-request sums.

For every public claim, answer these questions in the landing page or a linked detail page:

- What was measured and when did the timer start and stop?
- Which workload, payload, browser or application state, and output format were held constant?
- Where did the caller and target run? Which hardware, resources, image, runtime, and versions were used?
- How many warmups and measured samples were run? Were failures, retries, and replacements retained or excluded?
- What are p50, p95, p99, variance, confidence intervals, or full raw rows?
- Where are the raw artifacts and exact source commit?
- Which result is eligible, historical, diagnostic, or arithmetic only?
- Which workload and topology limits prevent generalization?

Modal, Browserless, PlanetScale, ClickHouse, and Deno all publish the workload, configuration, statistical context, reproducible source, and limits near the result. This project should use the same structure for external provider reports.

## Primary sources

- [Modal, Benchmark an endpoint](https://modal.com/docs/guide/endpoint-benchmarks)
- [Modal, Endpoint metrics](https://modal.com/docs/guide/endpoint-metrics)
- [Browserless, Benchmarking Hosted Browsers](https://www.browserless.io/blog/hosted-browser-benchmarking)
- [Browserless benchmark source](https://github.com/browserless/benchmarks)
- [PlanetScale, Benchmarking Postgres](https://planetscale.com/blog/benchmarking-postgres)
- [PlanetScale, On benchmarking](https://planetscale.com/blog/on-benchmarking)
- [PlanetScale, vs Amazon Aurora](https://planetscale.com/benchmarks/aurora)
- [ClickHouse, ClickBench source](https://github.com/ClickHouse/ClickBench)
- [ClickHouse benchmark hub](https://clickhouse.com/benchmarks)
- [ClickHouse, Hardware Benchmark](https://benchmark.clickhouse.com/hardware/)
- [ClickHouse, How to test your hardware](https://clickhouse.com/docs/concepts/features/performance/troubleshoot/performance-test)
- [Deno, AWS Lambda cold-start benchmarks](https://deno.com/blog/aws-lambda-coldstart-benchmarks)
- [Deno benchmark source](https://github.com/denoland/serverless-coldstart-benchmarks)
