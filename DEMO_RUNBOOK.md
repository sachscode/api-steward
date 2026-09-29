# Demo and technical defense runbook

Status: planned demonstrations. Populate measured values and evidence links after running [BENCHMARK_PLAN.md](BENCHMARK_PLAN.md).

## 1. The story to demonstrate

“This privately hosted agent upgrades a specific API integration, checks that the customer-visible behavior is preserved, and provides a reviewable patch. I can trace its cost and latency from the job to inference, cache transfer and hardware, then show what happens under contention and failure.”

Lead with the user outcome. Use observability to explain why the architecture can deliver it. Do not claim an enterprise customer base, generalized migration correctness or production SLOs from this controlled prototype.

## 2. Pre-demo checklist

- Pin a known-good experiment/configuration and preserve its evidence bundle; state whether the current screen is live or replayed.
- Verify the model endpoint and a small generated response, not just Running pods. Verify protected acceptance tests and the target-version evidence.
- Check Prometheus scrape/freshness health, time synchronization, graph units and sample counts, and links from dashboard to trace to evidence artifact.
- Confirm actual connector/backend and transport. Demonstrate a real transfer probe before the session; do not rely on an old hop counter.
- Have a deliberate incorrect patch ready for rejection. Keep the correct expected contract outside the editable agent workspace.
- Use synthetic billing data and local workload replay. Redact credentials and code content from operational dashboards.
- Set GPU spending, request, token, wall-time and repair-loop limits. Verify there is spare capacity before promising a live autoscale-up.
- Keep a recorded trace and result table for long/costly experiments. A replayed result is legitimate when clearly identified.

## 3. Suggested 12-minute flow

| Time | Demonstration | Evidence to show |
| --- | --- | --- |
| 0:00–1:00 | Broken API upgrade and required customer behavior | Failing preview case, explicit SDK/API target, preserved behavior contract |
| 1:00–3:00 | Agent impact map, bounded plan and generated patch | Source references, change scope, test/repair steps |
| 3:00–4:00 | Independent verification and negative control | Accepted patch plus rejected plausible-but-wrong patch; provider/local distinction |
| 4:00–6:00 | One job across application and inference | Job E2E versus model latency; queue, prefill, transfer, decode and tool trace |
| 6:00–8:00 | Colocated versus disaggregated result | Equal-resource goodput/tail table; actual transfer and reuse evidence; tradeoff |
| 8:00–10:00 | Tenant burst and capacity response | Per-tenant service, limiting queue/pool, KEDA input and warm-capacity timeline |
| 10:00–11:00 | Cancellation or controlled failure | Cleanup, bounded retry, visible terminal outcome and recovery |
| 11:00–12:00 | Cost and limits | Accepted patches per GPU-hour/cost, quality boundary, next justified optimization |

Do not wait live for lengthy model downloads or unstable p99 estimates. Show their recorded startup timeline and raw-run provenance.

## 4. Questions you should be able to answer

| Likely question | Defensible answer | Evidence to open |
| --- | --- | --- |
| What does concurrency mean here? | Count jobs, model calls, engine sequences and decode sessions separately. One user job can produce several calls. | Admission dashboard and lifecycle trace |
| How many concurrent requests can it serve? | It depends on context/output/cache mix. Give the measured SLO-safe envelope and KV/admission limit for a frozen class. | Capacity curve, workload histogram and block budget |
| Why not scale on GPU utilization? | Busy time does not identify queue pressure, phase bottleneck or KV availability. Scale the constrained logical pool using calibrated work demand. | KEDA input versus role queue and GPU panels |
| How do you know KEDA worked? | The signal changed, desired capacity changed, a model loaded and warmed, and recovery improved. A larger desired count alone is not proof. | Event-to-warm-ready timeline and recovery comparison |
| What if all GPUs are occupied? | Pending replicas cannot add service. Admission must bound the queue; node provisioning or preallocated spare capacity is separate. | Pending reason, rejection and capacity panels |
| Does 90% HBM mean nearly full KV? | No. The process may reserve weights and a KV pool. Inspect engine block occupancy and reservations separately. | DCGM memory beside vLLM KV/preemptions |
| Does a high prefix hit ratio prove lower latency? | It proves token reuse under its denominator. Measure saved compute, transfer overhead and added routing wait to establish latency benefit. | Source-token counters and matched cold/warm run |
| How do you prove real Mooncake transfer? | Show completion bytes/time, cross-host sender/receiver, actual transport, receiving KV reuse and a failure/miss control. | Transfer trace, logs, token-source accounting and NIC evidence |
| Is Mooncake Store the P/D connector? | They are distinct integration paths. Identify exactly which is deployed and which metrics belong to it. | Architecture and version/connector manifest |
| Why are some transfer metrics custom? | The inspected direct connector exposes internal stats but lacks a finished Prometheus adapter. Show our adapter's authoritative completion hook and probes. | Source diff and metric conformance test once implemented |
| Does RDMA prove GPUDirect? | No. Direct GPU-buffer transfer needs additional registration/path evidence; CPU-buffer RDMA only proves that path. | Backend diagnostics and supported profiling |
| Does NCCL imply InfiniBand/RDMA? | No. Inspect the selected transport and topology. Measure the actual relevant collective. | NCCL logs and microbenchmark manifest |
| Did disaggregation improve throughput? | State the measured result and its workload/resource boundary. It may improve decode tails while adding light-load TTFT overhead. | Equal-budget comparison, not one GPU versus two |
| Why is decode slow with low GPU compute activity? | Investigate memory bandwidth, KV/context length, communication waits, host starvation and batching. Low tensor activity alone is not a diagnosis. | ITL/TPOT, DRAM, CPU/PSI and timeline profile |
| How is MoE different for sizing? | Total resident parameters drive weight memory; active parameters affect compute. Expert/rank imbalance and all-to-all can limit scaling. | Paper memory budget, rank/communication profile |
| How are tenants protected? | Authenticated quotas, weighted queues, bounded context/output/tool budgets and tenant-scoped artifacts/cache identity, tested under a noisy neighbor. | Per-tenant slowdown, admission reasons and isolation tests |
| Is model confidence your quality metric? | No. Independent version and behavior checks decide acceptance; sampled review/judging helps investigate gaps. | Acceptance ledger and known-bad control |
| What is online evaluation without users? | Checks attached to actual prototype jobs, labeled as replay or constructed data. We do not claim production-user observations. | Provenance and evaluation queue/ledger |
| Can faster inference make patches worse? | Timeouts, truncation, retries or precision changes can affect output. Run the frozen suite under both configurations and load levels. | Paired quality/performance comparison |
| What is the cost metric? | Total experiment/deployment cost per independently accepted patch, including idle time and failed attempts; reviewer effort is separate. | Cost ledger and denominator |
| Can you trust this p99? | Show the observation count, duration, workload mix and repetitions. If samples are few, state uncertainty and show individual cases. | Raw records and interval/summary plot |
| What happens when metrics disappear? | A health/freshness failure invokes the tested safe capacity policy; it is not treated as zero demand. | Stale-exporter drill |
| Is a successful request a correct migration? | Engine completion only establishes serving outcome. A patch needs independent contract and version verification. | Job versus engine counters and verifier output |

## 5. Diagnostic drills: reason from symptoms

### TTFT rises while ongoing output remains smooth

Compare client and engine TTFT first. If only the client grows, inspect gateway queue, routing, network and stream buffering. If engine queue grows, compare incoming fresh prompt work with prefill service and warm-ready capacity. If compute time grows, check prompt length, cache misses, batching, clocks and host tokenization. A long prompt or cold cache is not automatically an autoscaler bug.

### Tokens arrive unevenly when long prompts arrive

Align ITL spikes with prefill work, scheduler batches and GPU activity. Check chunked prefill configuration and the controlled mixed-workload comparison. In P/D mode inspect KV wait, transfer contention and decode-side queue before claiming interference has been eliminated.

### KV usage and preemption rise

Inspect active context lengths, output reservations, actual generated lengths and block occupancy per engine. Distinguish reusable cached blocks from live-sequence demand. Look for affinity concentrating work on one worker. Tighten admission/output limits or change placement before assuming that adding a pod can immediately move active KV.

### Desired replicas increase but latency does not recover

Walk desired -> scheduled -> model loaded -> warm ready -> receiving traffic. Inspect GPU availability, startup phase time, model readiness, router discovery and cold-cache penalties. Check whether the wrong pool was scaled and whether the bottleneck is actually fabric or tools.

### A patch looks convincing but is wrong

Open the independent failing case and actual request/response evidence. Check explicit API version, item identity, quantity, discount/proration handling and provider-error behavior. Distinguish reasoning/retrieval failure from tool failure or test gaps. Record a new regression case without silently altering held-out results for the original experiment.

## 6. Paper-math rehearsal sequence

Work one exercise at a time with the actual selected model/hardware; Sachin calculates before the next step is introduced.

1. Weight memory from total parameters and precision; contrast active MoE compute parameters.
2. KV bytes/token for the actual attention layout; round sequences to blocks and reserve output.
3. Maximum admission under memory constraints, then compare with measured SLO-safe concurrency.
4. Arrival rate, residence time and Little's law using consistent boundaries.
5. Fresh prompt/output demand and independent P/D capacity estimates, including a burst drain window.
6. Actual transferred KV bytes, measured bandwidth and critical-path transfer cost.
7. Goodput and dollars per accepted patch, including failures and idle/warmup time.
8. Explain one mismatch between the paper estimate and measurement using a trace or profile.

The goal is to derive and defend a number, including its assumptions, rather than memorize a dashboard name.

## 7. Final evidence acceptance

Before declaring the demo ready, every major claim must link to a run/configuration, raw evidence and a limitation. Confirm that one can follow **customer behavior -> verification result -> job trace -> inference phase -> hardware/transport**. Keep all unrun experiments, unsupported metrics and proposed optimizations explicitly labeled.
