# API Steward: design and evidence

A tool-using agent (Track B) served by a control plane I wrote, in front of a Qwen3-30B-A3B MoE
running on vLLM with prefill/decode disaggregation, a real NIXL KV hop, KEDA scaling and
Grafana. Every claim below points at a file or a scrape in this repository.

| Where | What |
| --- | --- |
| `app/` | The agent (`agent.py`, `tools.py`) and the load generator (`loadgen.py`). It talks only to the gateway. |
| `control/` | The gateway: `guard`, `admit`, `place`, `queue`, `hop`, `fleet` (snapshots, warm/ramp), `overflow`, `serve`. 30 tests in `tests/`. |
| `cluster/` | Engine image, k3s/HAMi/KEDA/Prometheus/Grafana bring-up, engine manifests (`render_engines.py`), dashboards, probes and the experiment runner. |
| `notebook/` | Part 5 notebook, executed against the A10 rehearsal (`part5_queues.ipynb`) and the A100 host (`part5_queues_a100.ipynb`). |
| `plots/`, `metrics/` | Figures from both clusters; per-experiment load files, analyses, gateway scrapes, `/debug/state`, hop dictionary, engine startup logs. |

**Hardware actually run.** A Lambda 1x A10 (24 GB) for the rehearsal, then one Lambda **8x A100
40 GB SXM4** host (Israel, `me-west-1`) for the results. The design targets two 8x A100 hosts
(prefill pool on one, decode pool on the other, KV over RoCE); on the night of the runs Lambda had
exactly one 8x A100 instance available across all regions, so both pools run on one host: prefill
on GPUs 0-3, decode on GPUs 4-7. The two-host scripts are in `cluster/` and were rehearsed up to
the network test. NIXL's KV transfer between the two engines went **through the RoCE NIC in
loopback** (`metrics/a100-hop-probe.json`: NIC rx bytes ≈ NIXL bytes), so the hop measurements use
the same 50 Gb/s NIC a two-host cluster would. They are single-host loopback results: a switch hop
and cross-host RDMA are not exercised.

---

## Part 0: the application

**API Steward** is a private, tool-using migration-assessment prototype: given a breaking upgrade (the test case is the public
Stripe Python SDK v11 → v12 "Basil" change, on synthetic fixtures; the agent never calls Stripe), it
runs **inspect integration → read migration guide → check proposed mapping → answer**. One user
turn is four model calls; context grows each step.

| Tokens | Shared or unique | Size |
| --- | --- | --- |
| System prompt + tool schema | **Shared** by every request (the prefix-cache key `1a3c2cb82764e91c`) | 518 tokens |
| The conversation so far (this job's tool results) | Shared **within a job**, step to step | grows ~450 → ~1,700 tokens |
| "Repository notes" (load mix `--unique`) | **Unique** per job | ~750 tokens |
| Output | Unique | budget 512 (real ~80-200) |

Interactive = a user turn (`demo-a`, `demo-b`). Batch = a background sweep (`sweep`).
Completion on the A100: **94-95 % of jobs end `analysis_complete`**; the rest are the deliberate
5 % client aborts (`metrics/a100-sweep3-summary.txt`). The first A100 run completed 0 %: the
model passed the route name, then `Invoice.create_preview` without the `stripe.` prefix, to an
undescribed tool parameter checked by exact string. Fixed in the tool schema and checker
(`app/tools.py`, test `test_mapping_check_accepts_module_prefix_but_not_other_calls`), not the
model or serving.

## Part 1: capacity on paper vs measured

Qwen3-30B-A3B: 48 layers, 4 KV heads (GQA, 32 query heads), head_dim 128, BF16, 30.5 B
parameters (3.3 B active, 128 experts, 8 per token).

```
kv_bytes_per_token = 2 (K,V) × 48 × 4 × 128 × 2 B = 98,304 B = 96 KiB   (768 KiB without GQA)
weights            = 30.5e9 × 2 B = 56.8 GiB → 14.2 GiB per GPU at TP=4
KV per GPU         = 40 GiB × 0.90 − 14.2 − ~2 = 19.8 GiB → ~865,000 tokens per TP=4 engine
max_concurrent_seqs at 32,768 tokens = 26;  at the app's ~1,800 tokens = 480
```

| | Paper | Measured by vLLM (`metrics/a100-engine-vllm-decode-0-startup.log`) |
| --- | --- | --- |
| Weights per GPU | 14.2 GiB | **14.25 GiB** |
| Activations + CUDA graphs | ~2 GiB (estimate) | **1.12 GiB** |
| KV tokens per engine | ~865,000 | **878,464** |
| Sequences at 32k | 26 | **26.81** |

**Hypothesis:** at the app's length the scheduler (`max_num_seqs` 256) binds before KV (~480).
**Result: wrong, and instructively.** KV peaked at about **16 %** (15.8 %, autoscale burst) and vLLM
never ran more than **191** sequences (172 in the split sweep). The first limiter was **my gateway's prefill dispatch cap**, then **decode slots
and decode GPU time** (Part 8). Switching model from 0.6 B (A10) to 30B-A3B changed bytes/token
from 114,688 to 98,304: the MoE has fewer layers and KV heads per token despite 50× the weights.

## Part 2: the cluster

| Decision | Choice and reason |
| --- | --- |
| GPU | **A100 40 GB SXM4, 8 per host.** 61 GB of BF16 weights need ≥ 2 × 40 GB; NVLink/NVSwitch (`metrics/a100-topo.txt`: `NV12` all-to-all) carries TP all-reduce at ~227 GB/s busbw (`cluster/probe/`). A smaller card would force TP across PCIe or fewer KV heads per GPU. |
| Model | Qwen3-30B-A3B: memory set by 30.5 B params, compute by 3.3 B active. KV 96 KiB/token; agent prompts 0.5-2k tokens, so weights, not KV, decide GPU count. |
| Parallelism | **TP=4 per engine, never across hosts** (NVLink 300 GB/s vs NIC 6.25 GB/s per direction). TP ≤ `num_key_value_heads` (4): TP=8 would duplicate every KV head. Two TP=4 engines per 8-GPU host. |
| Topology | Built and measured both on the same 8 GPUs: **P/D split** (prefill GPUs 0-3 → decode 4-7) and **colocated** (two engines doing both). **Colocated is the right choice for this workload** (Part 8, E4). |
| Slicing | HAMi. On the A10 it splits one GPU into 4 slots for the rehearsal engines; on the A100, 1 slot = 1 whole GPU, in-pod CUDA interception off (`CUDA_DISABLE_CONTROL`), and engines pinned with `CUDA_VISIBLE_DEVICES` because privileged pods (needed for the RDMA device) see every GPU. Without the pin both TP=4 engines took GPUs 0-3. |
| Engine limits | `max_num_seqs` 256, `max_model_len` 32,768, prefix caching on, Hermes tool parser, `gpu-memory-utilization` 0.90, chunked prefill with vLLM's default batch-token budget. |
| Hop backend | **NIXL** (`NixlConnector`, pull mode) over UCX; `kv_lease_duration` 30 s. Not a metadata ledger: real KV bytes, verified below. |
| Overflow | **Superlinked's OpenAI-compatible API, model `Qwen/Qwen3.5-4B`**, only for 503/529, only tenants that allow it, token bucket 0.5 req/s burst 2, ≤ 2 in flight, output clamped to 256 tokens (`control/overflow.py`). It sends prompts outside the private cluster, so private tenants get the 503 instead. Tested against a mock (`test_overflow_only_for_503_and_allowed_tenants`); **not exercised live** (no API key configured on the cluster). |
| Two boxes | Gateway = `orch-serve` pod (`control/`). Engine = vLLM pods. admit/place/queue live only in the gateway; waiting/block table/preempt/kernels only in vLLM. |
| Scaling | KEDA scales a pool's slot-1 engine 0↔1 on that pool's own signal: **prefill on the number of requests waiting in vLLM** (a request count; uncached prompt tokens would be the better signal and is not implemented), **decode on running + waiting sequences** (hot decode slots). A missing series reads as 0. Never "add a replica" of everything: that multiplies KV pressure. |

## Part 3: guardrails, admission, stay vs leave

`control/guard.py` `inspect(payload) → Guard`: model must be `steward`; tenant known; role order;
system prompt and tool schema must equal the app contract; step ≤ 8; `max_tokens` ≤ 512; context ≤
48k chars; **estimated prompt + output ≤ `max_model_len`** (413 `context_too_long`; before this,
prefill accepted such prompts, built KV, and decode rejected them: ghost KV). The engine body is a
projection: clients cannot set sampling or `kv_transfer_params`. It forces
`parallel_tool_calls: false` and `enable_thinking: false`.

`control/admit.py` `should_shed(req, snap) → (shed?, code, reason, retry_after)`, cheapest first:

| Reason | Code | Leaves? | Live count (scrape) |
| --- | --- | --- | --- |
| `tenant_tokens` (tokens/min window, reserve at admission, settle to real usage) | 429 | stay | 1,214 (`a100-sweep-gateway.prom`), 327 (`a100-noisy`) |
| `tenant_concurrency` (in-flight per tenant) | 429 | stay | 214 (`a100-noisy`), 169 (`a100-autoscale`) |
| `no_eligible_pod` | 503 | may leave | |
| `kv_free` (every decode engine past 92 % interactive / 80 % batch) | 503 | may leave | 6-8 in the A10 long-prompt experiment |
| `timeout_queue` (predicted wait > half the deadline) | 503 | may leave | |

Overflow gate (`control/overflow.py`): 429, 500 and `slice_oom` stay local; 503/529 may leave.

## Part 4: placement

`control/place.py` `pick(req, workers, *, policy) → Snapshot | Shed`. Eligible = scraped < 2 s
ago (stale only as a counted fallback), healthy, not cold, KV below the class water mark, predicted
queue wait within the deadline. Scores: **prefill** +3 job affinity (this conversation's KV is
cached there), +1 shared-prefix affinity, −2 × (our queue + in flight)/slots, −0.5 × vLLM waiting,
+KV free; **decode** +1 job affinity, −2 × running/slots, −2 × our queue/slots, +2 × KV free.
Policies `least_loaded`, `p2c`, `prefix_then_load` (prefill default). Queue depth is a scorer, not
only an admission input. Placed once; no retry elsewhere after dispatch.

## Part 5: the queue

Answered with scrapes in `notebook/part5_queues_a100.ipynb` (and the A10 run in
`part5_queues.ipynb`). Short answers:

* **Our queue vs vLLM's waiting queue.** Each engine has dispatch slots and a KV token budget in
  the gateway. Under load our queue peaked at **236** on decode-0 while vLLM's waiting peaked at
  **8** (`plots/a100-queue-ours-vs-vllm.png`).
* **Waiting / running / preempted.** Running peaked at 191 of 256 (172 in the split sweep); **0 preemptions in every A100
  run**.
* **`orch_replica_queue_depth` by pod and mix.** `plots/a100-queue-depth-by-pod.png`; batch waits
  15 s (aging promotion) while interactive waits ~1-2 s at overload.
* **A long retrieve and a short agent step are both ready: who goes first?** **We do, but only
  when we are holding them.** Our queue ranks interactive first and ages batch after 15 s. On the
  A100, 48 requests fitted inside 192 decode slots, our queue never held them, and vLLM's FCFS ran
  the long batch prompts first: interactive engine TTFT **2.4 s** vs batch **1.65 s** (priority
  inversion inside the engine). The fix is to pass class as a per-request priority with
  `--scheduling-policy priority`; not done yet.
* **PagedAttention vs prefix cache.** PagedAttention turns 26 sequences (reserving 32k each) into
  **704** at the real mean length (1,247 tokens). The prefix cache stored the 518-token shared
  prefix once instead of 172 times at peak (**8.1 GiB**) and served 78-86 % of prompt tokens.
* **Engine flags.** `max_num_seqs` 256, `max_model_len` 32,768, default `max_num_batched_tokens`
  (chunked prefill). Gateway slots below `max_num_seqs` (192) so vLLM always has room.
* **KV full after admit.** Shed at the door (`kv_free`) and hold at our door: dispatch reserves
  prompt + output tokens against vLLM's block capacity × 92 %. On the A10 a burst of long prompts
  produced **5 preemptions** without the budget and **0** with it.
* **Client gone.** In our queue: removed, no GPU work. In decode: the gateway closes the engine
  stream, vLLM aborts and frees blocks (`orch_aborted_total{stage="decode"}`). Between prefill and
  decode: the KV is a ghost until NIXL's 30 s lease expires (`vllm:nixl_num_kv_expired_reqs_total`
  = 1 on the A10 for one such abort).
* **Slam or ramp after a worker returns.** Ramp 25 → 50 → 100 % of slots while the engine's own
  p99 TTFT holds; engines up > 5 min skip it. Gating the ramp on client TTFT (which includes our
  queue) held a healthy engine at 25 % on the A10: decode queue p50 12 s while vLLM TTFT was 40 ms.

## Part 6: the hop, and "is it actually warm?"

`control/hop.py` decides per request: same engine → no hop; prefill pool empty → local; estimated
uncached tokens < `hop_min_tokens` (512) → local (`decode_has_prefix` when this job's earlier turn
is cached on decode); otherwise NIXL. The decode slot is reserved **before** prefill so KV never
waits for a decode slot. A hop record (`metrics/a100-hop_dict.json`):

```json
{"src": "prefill-0", "dst": "decode-0", "tokens": 1706, "blocks": 107, "prefix": "1a3c2cb82764e91c",
 "backend": "nixl", "bytes_estimated": 168296448, "remote_host": "10.19.103.185", "remote_port": 5600}
```

**Proof the KV moved and was used.** First hop: 3,068 tokens → 192 blocks → **301,989,888 bytes
= 192 × 16 × 98,304**, over 4 TP ranks, 48 descriptors each. Across the A100 runs, vLLM measured
4.61 M tokens' worth of KV pulled and decode counted 4.51 M prompt tokens received from outside
(`external_prefix_cache_hits`): ratio 1.023, the gap is whole-block rounding. Decode's own prompt
sources: 4.5 M external, 45.7 M local cache, 3.4 M computed. **Not copied:** the prefix-cache
index (decode re-indexes pulled blocks), sampler state, and KV for generated tokens.

**Is it warm?** Not when the health check says so. The first 1,739-token prompt after start took
**1,014 ms** of prefill against ~100 ms warm: a Triton JIT compile of `fused_moe_kernel` for an
unseen shape (`vllm` log). Re-quoted warm: 1,740 tokens hop 196 ms, local 118 ms. Warm-up must
cover the prompt-size range, not one small request. A new 30B replica takes **~107 s** of engine
init (load 55-63 s, `torch.compile` 54 s) before that.

## Part 7: the wired path

```
app turn (agent step) → guardrails.inspect → admit (tenant window, fleet) → place (decode, then prefill)
→ our queue (decode slot + KV budget, then prefill slot) → same engine | NIXL transfer(prefill → decode)
→ vLLM /v1/chat/completions (streamed; TTFT measured at the gateway) → overflow gate (429/500 stay, 503/529 may leave)
```

The app's `GatewayClient` rejects any reply that does not carry the gateway's stage list
(`took_serve_path`); a direct engine call would fail it.

## Part 8: measured on the A100 with agent traffic

Experiments run with `cluster/experiment.sh`, summarised by `cluster/analyze_run.py`
(`metrics/a100-*-analysis.json`).

| # | Experiment | Result |
| --- | --- | --- |
| E5a | Hop vs local recompute, 0.5k-25k fresh tokens, one at a time (`a100-hop-probe.json`) | The full split path (remote prefill + transfer + first token) was slower than local prefill at every size (1.5-2.3×). The transfer + receiver first token alone (0.67 s at 25k) beat recompute (1.25 s), so KV that already exists can be worth moving; not tested. NIXL ~4.5 GB/s; the transfer adds ~44 % to prefill time because prefill makes KV at ~2 GB/s (20k tokens/s × 96 KiB). |
| E3 | Load sweep, P/D split (`sweep2`, `sweep3`) | At 20 jobs/s, prefill cap 8: TTFT p50 2.0 s, p99 27 s, prefill queue 65, decode queue 277, 120 sequences in vLLM. Cap 32 at the same rate: p50 1.1 s, p99 17 s, prefill queue 12, 172 sequences; the limit moves to decode slots/GPU. (Run-average GPU figures mix different load steps; compare the 20 jobs/s step.) Knee ≈ 12 jobs/s (TTFT p99 1.0 s). |
| E4 | P/D vs colocated, same 8 GPUs (`sweep3` vs `colo`) | Per-call gateway TTFT p99 at the 20 jobs/s target **16.7 s split vs 1.23 s colocated**; completed-job median 29.0 s vs 12.1 s; similar whole-run mean ITL (24.4 vs 24.8 ms). Highest step within a 2 s p99: split 12/s target (12.5 started), colocated 20/s (19.9 started). Estimated cost at those steps **$0.36 vs $0.22 per 1,000 jobs** ($15.92/h, from arrival rates, not measured per completed migration); the split's capacity is between 12.5 and 18.5/s, so 1.1-1.6× per job. The load generator (256 threads, start stamped when a thread frees up) fell behind under overload: started 18.5 vs 19.9/s at the 20 target and 13.7 vs 22.2/s at 28, so the 28 step is not like-for-like and overload latency is understated. |
| E8 | KEDA scale-out (`autoscale`) | Scaled 21 s into the burst; replica ready at +239 s; burst ended at +156 s; 0 requests served; scaled down at +381 s. Timeline derived from k8s events recorded after the run (`a100-autoscale-events.txt`; raw events expired); burst and run end reproduce from the load file. |
| E7 | Noisy tenant (`noisy`) | 541 of the flooder's requests refused at admission; other tenants TTFT p99 **0.38 s**. |

**Point at a file or scrape for each question:**

| Question | Answer |
| --- | --- |
| What is the app; shared vs unique tokens? | Part 0 table; `app/tools.py`, `app/loadgen.py`. |
| What dies at guardrails vs admit vs place vs queue? | Guardrails: contract, budget, `context_too_long`. Admit: tenant 429s, `kv_free`/`timeout_queue` 503s. Place: no eligible engine. Queue: `timeout_queue` at dequeue. `orch_shed_total{stage,reason,code}` (outcomes and admission dashboards). |
| Where do I prevent work that will time out? | `admit.py` (predicted wait > deadline/2 → 503 `timeout_queue`) and `queue.py` (expired at dequeue). |
| Where do I protect KV? | Admission water marks (`admit.py`), placement skips engines past them (`place.py`), dispatch token budget (`queue.py`), guardrail on `max_model_len` (`guard.py`). |
| Where do I prioritise interactive? (p99_spread) | `queue.py` class rank with batch aging; Admission dashboard "TTFT spread p99 − p50". Interactive vs batch TTFT p99 1.5 s vs 6.6 s in `a100-sweep`. Limitation: inside vLLM it is FCFS (Part 5). |
| Where do I stop one tenant owning the GPU? | `admit.py` token window + in-flight cap; E7. |
| Where do I hop, and what is not copied? | `hop.py`; Part 6. |
| Where do I evict, and what becomes a ghost if I skip it? | `fleet.py` evicts job/prefix affinity when an engine restarts or fails (`orch_affinity_evictions_total`), else the router sticks to a cache that no longer exists. KV pulled by nobody is freed only by the NIXL lease (ghost). |
| Engine scheduler vs my admit/place/queue? | Gateway decides who enters, where, and in what order it is dispatched; vLLM decides batching, chunked prefill, blocks and preemption for what it has been given. No scheduler reimplemented. |
| What limited concurrency on this GPU for this app? | Not KV (≤ ~16 %) and not `max_num_seqs`. First my prefill dispatch cap, then decode slots and decode GPU time (ITL 21 → 33 ms as load rose). |
| Four production alerts | (1) Client TTFT p99 by class > SLO for 5 min. (2) `orch_shed_total{code="503"}` rate > 1 % of requests (capacity, not tenants). (3) Our queue depth or queue-wait p99 rising while engine GPU busy < 60 % (the control plane is the bottleneck: both A10 and A100 incidents). (4) `vllm:num_preemptions_total` or `nixl_num_kv_expired_reqs_total` increasing (KV over-committed or ghost KV). |
| If I scale, which pool? | Decode, on running + waiting (decode slots hot). Prefill should scale on uncached prompt tokens waiting; the implemented trigger counts waiting requests. For bursts shorter than replica startup (~3.6 min here), keep a warm standby instead. |
| At 10× traffic, what changes, and three wrong knobs? | Change: colocated engines on more hosts behind the same gateway; class priority passed into vLLM; warm standby plus a startup cut (compile cache, fewer graph sizes). Wrong: (1) raise gateway slots/`max_num_seqs` without an ITL SLO (more batching, worse tokens); (2) "KV is the limit, add replicas of everything" (KV was 15 %); (3) split prefill/decode across hosts on a 50 Gb NIC for 2k-token agent prompts (E4, E5a). |

## Scope decisions

The original plan was larger than what I built. What I cut, and why:

| Planned | Outcome | Why |
| --- | --- | --- |
| Generate the patch and verify behavior is preserved | Cut: the agent assesses a migration and checks a mapping | The assignment grades the serving shape; a fixture check is enough to make the agent's traffic real |
| Mooncake for the KV hop | NIXL (`NixlConnector`) instead | Built into vLLM, and its counters plus the decode engine's external prefix-cache hits prove KV actually moved |
| Prefill on one host, decode on another | Both pools on one 8x A100 host | Only one 8x A100 instance was available across Lambda's regions; two-host scripts are in `cluster/` |
| Long-prompt mix (16-32k) where P/D should win | Not run | Time; it is the next controlled experiment |
| Live overflow to Superlinked | Tested against a mock | Not exercised against the live API during the runs |

## Limitations

* One host, not two (capacity). The KV path used the RoCE NIC in loopback; inter-host switch
  behaviour and cross-host RDMA bandwidth are not measured (`cluster/two_host_net.sh` is ready).
* Long-prompt interference (16k-32k prompts mixed with agent steps), the case where P/D should win
  on ITL, was not run under load.
* Overflow tested with a mock, not live. Token estimates use chars ÷ 4 (6-9 % error on agent
  traffic, worse on unusual text); an exact `/tokenize` check near the limit is not implemented.
* Priority is not propagated into vLLM.
* The agent's mapping check is a fixture check, not proof the migrated code works.
* The load generator stamps a job's start when one of its 512 threads (`experiment.sh` passes `--max-workers 512`) frees up, so under overload
  it falls behind its target and hides that backlog from latency (E4). An open-loop generator that
  stamps the scheduled arrival time is the fix before re-measuring capacity.
* TTFT figures are per model call at the gateway; the app's calls are non-streaming, so a user
  waits for whole jobs (E4 gives completed-job medians).
* Before the fix that followed review, the gateway's "engine TTFT" for split requests included the wait for
  a prefill slot (fixed, with a test). The A100 runs predate the fix; their engines were up > 5 min,
  so the warm-up ramp it feeds was not active.
