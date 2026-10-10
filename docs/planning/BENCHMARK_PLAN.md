# Benchmark and evidence plan

Planning date: 2026-09-28. No benchmark results exist yet.

Companion documents: [project scope](PROJECT_PLAN.md), [observability and metric contract](OBSERVABILITY_PLAN.md), [demo runbook](../../DEMO_RUNBOOK.md).

## 1. Claims we intend to test

1. A bounded privately hosted MoE agent can produce a correct, independently verified Stripe invoice-preview migration.
2. Admission and tenant budgets preserve useful service during overload rather than allowing unbounded queues and repair loops.
3. Cache reuse reduces repeated prefill work for the actual migration workflow, with tenant isolation preserved.
4. Real cross-host KV transfer works through the selected Mooncake integration and transport.
5. Disaggregated prefill changes latency, interference, goodput and cost in measurable ways; improvement is a hypothesis, not a required conclusion.
6. Independently scaling the constrained role can improve recovery when spare hardware exists, while stale metrics and cold starts are handled predictably.

Use three kinds of experiment: **product correctness**, **serving performance**, and **hardware/transport characterization**. A transport microbenchmark cannot establish application speedup; a fast model endpoint cannot establish patch correctness.

## 2. Data and workloads

### Product corpus

Use the frozen public Stripe sample, prepared baseline and target migration described in PROJECT_PLAN.md. Keep source license, revision and preparation diff. Construct supported billing scenarios with controlled price, quantity, discount and proration inputs. Freeze expected behavior and held-out cases before letting the agent edit application code.

Maintain three states: qualified baseline; target upgrade without repair; target upgrade with the candidate repair. A good evaluation must reject the uncorrected upgrade and deliberately defective patches as well as accepting valid behavior.

Useful negative controls include wrong subscription-item ID, omitted quantity, dropped discount, changed proration timestamp, successful-looking fallback response on provider error, retained old API version, and a forbidden mutating operation. Count these as constructed controls, not naturally occurring customer bugs.

Compare with a small deterministic migration baseline. Repeated runs and fixture variations on this source measure stability and scenario coverage; they are not independent migrations across many repositories. Broaden later only after the first case is sound. Purchased data is unnecessary for this phase.

### Serving workload

Record actual model-call envelopes from the bounded agent: system/tool prefix, repository context, retrieved API documentation, patch/test feedback, output budget, actual output length and repair step. Replay sanitized local inputs with fixed seeds/configuration where supported. Tokenize using the selected model's tokenizer; characters are not a token budget.

| Class | Initial characterization points | Purpose |
| --- | --- | --- |
| Short decision/tool call | Approximately 1k–2k prompt tokens, 128–256 output-token limit | Queue and streaming responsiveness |
| Repository analysis | Approximately 8k prompt, 512 output-token limit | Normal repeated context |
| Long analysis | Up to 32k prompt only if supported and affordable, 512 output-token limit | Dense prefill and interference |
| Patch/repair | Approximately 4k–8k prompt, 1k–2k output-token limit | Sustained decode and iterative reuse |

These are initial test points, not production traffic claims. Freeze the measured class mix after real agent traces exist. Keep content-meaningful replay for quality; a random-token workload may characterize raw serving but cannot measure useful output.

Use two output modes and label them: natural stopping for realistic behavior, and controlled-length generation for scheduler comparisons if supported. Do not silently force output lengths in a test presented as end-user latency.

### Privacy and billing boundary

Inference load and fault tests use local replay fixtures. A small separate Stripe sandbox qualification validates provider behavior. Do not generate benchmark traffic against Stripe or use production billing credentials. Keep source/code and judge inference within the private serving boundary.

## 3. Reproducibility manifest

Every named experiment bundle contains:

- Application and harness commit, uncommitted diff if any, corpus manifest/hash and acceptance-test hash.
- Exact model revision, tokenizer revision, license, total/active parameters, weight precision, KV precision, context limit and sampling settings.
- Container digests; vLLM/Mooncake/connector versions; CUDA/driver/NCCL/DCGM versions; Prometheus/Grafana/KEDA versions and relevant configs.
- GPU model/count and exclusive versus shared allocation; hosts, GPU/NIC/NUMA topology, link speeds, transport/backend evidence, CPU/RAM and storage.
- Serving configuration: TP/EP/PP/DP, P/D ratio, memory utilization limit, block size where configurable, maximum sequences, batched-token limit, chunked prefill, prefix caching, graph mode and transfer settings.
- Offered schedule, actual dispatch timestamps, request IDs in raw records, prompt/output lengths, cache state, timeouts, retry/cancellation rules and randomness seeds.
- SLO file frozen before comparisons; admission policy, tenant weights/limits and KEDA configuration.
- Raw per-request results including failed/rejected/incomplete attempts, quality ledger, representative traces, metrics snapshots and dashboard exports.
- Warmup/measurement/drain windows, observation counts, actual costs and limitations.

Suggested bundle layout: `manifest.json`, `requests.jsonl`, `jobs.jsonl`, `quality.jsonl`, `metrics/`, `traces/`, `configs/`, `artifacts/`, `report.md`, `SHA256SUMS`. Paths are a proposed convention; no result files are created until experiments run. Preserve failed runs as well as successful ones.

## 4. Experimental method

Start with fixed replicas and disabled autoscaling. Warm model loading/graph capture separately from data-cache warming. Record cold start as its own test. Use a pilot to choose safe load points and measurement duration.

- **Closed-loop concurrency sweep:** 1, 2, 4, 8, etc., stopping when a resource/SLO/budget boundary is reached. This finds the throughput knee and latency-versus-concurrency curve. The values are proposals, not guaranteed feasible concurrency.
- **Open-loop arrival test:** issue requests on a predeclared constant or seeded Poisson schedule, plus a step/burst scenario. Record scheduled and actual send times. The load generator must not hide overload by waiting for a previous response. Report client scheduling lag and dropped dispatches.
- Start the measured interval only after the designated warmup, and then allow a bounded drain. Report incomplete requests at cutoff. Do not quietly discard timeouts or latency from failed requests.
- Repeat key comparisons at least three times if budget permits; alternate/randomize A/B order to reduce thermal/cache/time-order bias. Report fewer repetitions honestly when budget constrains them.
- Target enough observations for the statistic: 100 requests put only about one observation in the top 1%; 10,000 put about 100 there. Report p99 with sample count and uncertainty. Product jobs will often be too few for a credible p99; show individual durations, median/range and verdicts instead.
- Confidence intervals should reflect independent runs/cases. Thousands of calls replaying one fixture do not create thousands of independent migration problems. Bootstrap at an appropriate run/case boundary, not across correlated token intervals as though they were jobs.

Use `vllm bench serve` for engine-serving characterization where the pinned version supports the necessary load and goodput options; archive its `--help` and command/config. Use a separate job-level driver for tool loops, tenant identity, cancellations and quality outcomes. Verify how streaming chunks are timestamped before calling client measurements token ITL. [vLLM benchmark CLI](https://docs.vllm.ai/en/latest/cli/bench/serve/)

## 5. Minimum experiment matrix

| ID | Experiment and controlled change | Required measurements | What establishes completion |
| --- | --- | --- | --- |
| B00 | Correctness qualification: baseline, broken upgrade, candidate, deliberately bad candidates | Independent verdicts, version/request evidence, side effects, provider-check boundary | Valid behavior passes; meaningful negative controls fail; limitations recorded |
| B01 | Fixed colocated engine, concurrency then open-loop rate sweep | Client/engine latency, goodput, queue, fresh/reused tokens, KV/preemption, GPU/host | Sustainable operating envelope and bottleneck explanation |
| B02 | Cache disabled/cold/warm repeated prefix, then changed-prefix control | Computed/cached tokens, TTFT, hit denominator, HBM/block pressure | Actual computation saved with correct output; altered prefixes invalidate as expected |
| B03 | Same-resource colocated versus real P/D | Goodput, TTFT, ITL, TPOT, E2E, transfer time/bytes, GPU-hours | Matched comparison with real-transfer evidence and no hidden extra GPUs |
| B04 | Long-prefill arrivals mixed with short/ongoing decode calls | Short-request tail and decode stalls, phase queueing, role utilization | Quantified interference and whether disaggregation/chunking changes it |
| B05 | Three tenants; isolated runs versus contention and a noisy tenant | Per-tenant latency/goodput/rejection, budgets, wait age, fairness | Bounded interference, no starvation in the tested workload, isolation checks |
| B06 | Independent prefill-heavy and decode-heavy bursts with KEDA | Scaler input, desired/warm replicas, startup time, recovery, billable time | Correct role responds; usable capacity arrives; no false autoscaling claim |
| B07 | Cancellation, slow client and worker failure | Cleanup time, active reservations, interrupted transfers, retries, terminal ledger | No orphaned work; bounded recovery and correctly labeled failed jobs |
| B08 | Frozen quality suite at low load and near safe capacity | Final/first-pass acceptance, false acceptance, attempts, tokens/cost | Serving changes do not create an unexamined quality regression |
| B09 | NCCL/RDMA microbenchmarks and integrated transport validation | Relevant collective/transfer bandwidth and latency, topology and protocol | Actual selected cross-host data path demonstrated; application effect measured separately |
| B10 | Telemetry down/stale and scaler failure | Alert timing, demand freshness, fallback capacity, rejection, recovery | Missing telemetry is not interpreted as no demand; system remains bounded |
| B11 | Single attempt versus bounded repair versus up to three alternative candidates | Candidate coverage, selected-patch success, false acceptance, actual tokens/GPU-time/cost, cache reuse and tenant interference | Compare fixed-model policies under declared budgets; select with development checks before held-out evaluation |

Run B00/B01 first. B02/B03/B04 establish the inference story. B05/B06/B07/B10 establish production behavior. B08 joins output quality to performance. B09 is a prerequisite characterization for interpreting B03, not a standalone substitute for it.

Do not run a full factorial over every model, precision, context, tenant mix, transport and scheduler knob. Fix a useful baseline, identify a bottleneck, change one dimension, then test important interactions.

B11 is the bounded inference-compute comparison discussed after reviewing CS329A. Reuse the existing migration loop, local checks and serving stack. Keep model weights fixed, isolate candidate workspaces and account for selection/verification costs. Begin with fixed policies; an adaptive policy remains optional if results justify it. Equal call counts do not imply equal compute. Held-out acceptance results must not feed repair or selection; score retained candidates afterward to measure coverage separately from the chosen patch's correctness.

## 6. Disaggregated prefill: defensible comparison

Define `R` as one complete logical model replica, possibly spanning multiple GPUs. If `R` needs `g` GPUs, compare:

| Configuration | Resource budget | Role |
| --- | --- | --- |
| C0 | One colocated R | Simple diagnostic baseline; not the equal-budget comparator for two replicas |
| C1 | Two colocated R replicas, same total GPUs as P1 | Main cost/hardware comparator, balanced routing |
| P1 | One prefill R plus one decode R | Initial disaggregated experiment with actual transfer |
| P2, optional | Different P:D ratios under a declared new budget | Capacity allocation study, not automatically comparable to C1 |

If a model/topology cannot support this shape, revise the matrix before booking hardware. Match GPU types, total allocated GPUs, interconnect, weight/KV precision, model revision, requests and admission policy. Record unavoidable differences in graph/batch/memory configuration. Normalize for actual GPU-hours, including duplicated model residency and warmup. Compare under multiple rates: light load, near the colocated knee, and a burst.

Test cache states separately: no reuse/cold; local prefix warm; transferred context; optional shared-store warm. Warm model and warm cache are distinct conditions. Keep the amount of reusable prefix fixed within a matched comparison. Randomly changing prompts to defeat cache is a separate workload change.

Measure client TTFT/E2E and decode ITL/TPOT against: prefill queue/compute, transfer initiation-to-completion, decode waiting for usable KV, scheduling delay, recomputation and first-token ownership. Trace overlap; do not sum all spans if transfer and compute overlap.

For payload bytes `B_KV` and measured effective bandwidth `BW`, `B_KV/BW` is an idealized payload-transfer time, not the full overhead. Setup, registration, synchronization, queueing and contention can dominate small transfers. Estimate saved interference/queue time and compare with added transfer and coordination on the critical path.

The result may be: better decode tails but worse light-load TTFT; better goodput only for long contexts; or no gain on the available fabric. Any of those can be a strong result if measured cleanly. vLLM documents disaggregated prefill as experimental; do not promise universal throughput improvements. [vLLM disaggregation](https://docs.vllm.ai/en/latest/features/disagg_prefill/)

### Proof of real KV transfer

All of the following are required:

1. Pinned compatible connector configuration and worker logs establish the intended sender/receiver relationship on different hosts.
2. The completion path records nonzero tensor payload bytes and transfer duration, not merely metadata/RPC intent.
3. The receiving request uses the transferred KV, with measured external-token reuse or a supported equivalent and reduced local prefill work. A completed copy that is never consumed is insufficient.
4. NIC/backend evidence identifies actual RDMA or other transport and any fallback. Control-plane TCP traffic may coexist with RDMA payload traffic.
5. A forced miss or controlled transfer failure changes reuse/recompute/error behavior as expected, while the output contract remains checked.

Do not infer GPUDirect RDMA from RDMA alone. If claiming it, establish GPU-buffer registration and the actual direct data path with supported backend diagnostics/profiling; CPU-buffer RDMA benchmarks are insufficient. Likewise, NCCL can select different transports: log the actual network path.

## 7. Hardware and fabric characterization

Before application benchmarking, capture `nvidia-smi topo -m`, GPU/NIC affinity, RDMA device/link information and provider-supported network configuration. Multi-host containers on an ordinary network do not imply RDMA access. Hardware reservation remains gated on a verified provider topology and budget.

Use the appropriate [NCCL tests](https://github.com/NVIDIA/nccl-tests) for the chosen parallelism: all-reduce for relevant tensor-parallel communication, and supported all-to-all variants for expert-parallel dispatch. Record payload sizes, ranks, GPUs/host, in-place/out-of-place mode, warmup, correctness checks and actual transport logs. Explain algorithm bandwidth versus bus bandwidth; neither is automatically an observed NIC port rate.

Use [perftest](https://github.com/linux-rdma/perftest) for bandwidth/latency appropriate to the real read/write direction, message-size range, connection count and supported GPU-buffer mode. Match Mooncake payload sizes. Run one-way and bidirectional/contended cases where relevant. An isolated peak establishes a ceiling, not application transfer performance.

For MoE, inspect tokens dispatched per expert/rank, max-to-mean load imbalance, straggler rank time and communication share when supported by instrumentation. These are not assumed stock vLLM Prometheus metrics. Use short sampled traces/counters without source text. An underused GPU can be waiting for an imbalanced expert or collective, not lacking incoming requests.

Similarly, do not assume NCCL supplies a universal Prometheus collector for collective duration or rank skew. Obtain those measurements from the chosen profiler, framework hooks or an explicitly implemented adapter. Annotate sampled versus continuous evidence and include instrumentation overhead.

Use Nsight Systems for short representative GPU/CPU/communication timelines when aggregate metrics cannot explain a bottleneck. Profile separately from published performance runs and measure collection overhead. Keep profiler traces private. Add kernel-level Nsight Compute or custom CUDA work only after a repeatable hot spot exists.

## 8. Failure and recovery drills

| Drill | Expected bounded behavior | Proof |
| --- | --- | --- |
| Long jobs flood one tenant | Tenant budget/fair queue limits blast radius | Other tenants' wait/goodput and rejection reasons |
| Client abort during prefill, transfer or decode | Cancel downstream work; release active ownership | Trace and cleanup time; no leaked reservation |
| Slow streaming reader | Bounded outbound buffering/backpressure and deadline | Buffer/connection diagnostics, bounded resource growth |
| Decode worker terminates | Fail or safely retry according to declared policy | One terminal job result, new attempt identity, recomputation/cost |
| Mooncake transfer or Store becomes unavailable | Bounded timeout; explicit retry/recompute/failure | Actual failure signal, no silent hosted-model overflow |
| Cache fills or worker changes | Eviction/cold miss affects work predictably | Hit/recompute/latency changes; isolation intact |
| No spare GPU for scale-up | Queue/admission protects service; pending capacity visible | Desired versus warm-ready gap and scheduling reason |
| Prometheus/exporter unavailable or frozen | No false scale-to-zero; health alert and tested fallback | Scaler error/freshness and bounded capacity |
| Repeated tool failure | Retry budget terminates loop | Attempt count, time/token budget and honest failure package |

Perform faults only in the dedicated experiment deployment, one at a time initially. Reconcile interrupted jobs before the next run. Retry model calls only within budget; avoid duplicating mutating tools. The initial Stripe preview workflow forbids billing mutations.

## 9. Optional experiments after the minimum evidence is complete

- Chunked prefill and `max_num_batched_tokens`: mixed-workload tail versus throughput, with concurrency/context fixed.
- Prefix-aware routing versus least-loaded placement: fresh compute saved versus queue delay; include worker churn.
- Mooncake Store capacity/eviction/restart: warm reuse, CPU RAM pressure, spill if supported, recovery and stale-entry compatibility.
- KV precision or weight quantization: memory/concurrency improvement, transfer volume and independent quality regression; hold the other precision constant.
- CUDA graphs: eager versus supported captured shapes; graph memory, startup/capture cost, steady latency and unsupported-shape behavior.
- TP versus EP placement: only if resources permit a fair comparison; MoE rank imbalance and collective overhead.
- Evaluation interference: evaluator disabled versus bounded shared evaluation load versus separate capacity, measuring user goodput and verdict lag.
- Observability overhead: minimal counters versus full metrics/tracing versus short profiling; latency, goodput, host CPU and dropped telemetry.
- Admission policy: FIFO versus weighted scheduling and deadline-aware rejection, with a declared common workload and quality gate.

These extensions are selected by observed bottlenecks. The project does not require every optimization to improve performance.

## 10. Result table and reporting rules

For every comparison publish configuration, hardware budget, cache state, offered and achieved rate, rejection/error/timeout counts, sample count, client TTFT p50/p95/p99, request TPOT and token ITL distribution where valid, E2E, generated and computed token rates, SLO goodput, actual transfer bytes/time, GPU-hours, final quality verdicts and cost per accepted patch.

Use plots of **offered rate versus latency/goodput**, **concurrency versus throughput/KV pressure**, **time-aligned burst versus warm capacity**, and **per-tenant slowdown**. Include the simple baseline. Explain the first bottleneck reached and any shift after optimization.

State unsupported claims explicitly: no real users yet; one integration family; limited provider checks; simulated versus real traffic; software/version compatibility limits; unavailable profiler features; insufficient samples for a stable p99; or hardware that could not demonstrate a requested transport. Evidence should be strong enough to reproduce the claim actually made.

Use [FINAL_REPORT_PLAN.md](FINAL_REPORT_PLAN.md) to capture observations after each experiment and synthesize the detailed report after the project concludes.
