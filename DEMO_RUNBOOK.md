# Demo runbook: 12 minutes

The A100 host is terminated, so this demo is a **replay** of recorded runs plus a live run of the
gateway against a fake vLLM fleet. Say so up front. Every number below is in `metrics/` or
`DESIGN.md`; regenerate the charts with `python3 cluster/article_figures.py`.

## Before the session

```bash
.venv/bin/python -m pytest -q                      # 30 passed
.venv/bin/python cluster/replay_job.py colo 12     # one recorded four-call job
.venv/bin/python cluster/article_figures.py        # rebuilds plots/article-*.png from metrics/
```

Open: `plots/article-*.png`, `notebook/part5_queues_a100.ipynb`, `DESIGN.md` (Part 8 table).

## 1. The application and its scope (1 min)

* API Steward is a **private, tool-using migration-assessment prototype**. Test case: the Stripe
  Python SDK change from `Invoice.upcoming` to `Invoice.create_preview`, on synthetic fixtures.
* One job = four non-streaming model calls: inspect, read guide, check mapping, answer.
* It checks a proposed mapping. It does **not** generate patches or verify behavior
  (`behavior_preservation_verified=False`). Those, and Mooncake, are future work.
* Served by Qwen3-30B-A3B (MoE, TP=4) on vLLM, with my gateway in front; KV moved with **NIXL**.

## 2. One job through the serve path (2 min)

* Live: `pytest -q -k agent_runs_end_to_end` runs the real agent through the real HTTP gateway
  against a fake vLLM fleet; the client rejects any reply without the gateway's stage list.
* Recorded: `cluster/replay_job.py colo 12` prints one A100 job's four calls (prompt growing
  518 → 1,252 tokens, per-call TTFT, queue wait, ~7 s per job).
* Point at the stages: guard → admit → place → queue → hop decision → engine (`control/`).

## 3. Paper sizing vs measured (2 min)

* 2 × 48 × 4 × 128 × 2 B = 96 KiB per token; ~865k tokens per engine predicted, **878,464**
  reported by vLLM (within 2 %). Overhead guess was the only error (1.1 GiB, not 2).
* Under load KV never passed about **16 %**, zero preemptions. KV was not the limiter.

## 4. Why colocated beat the split here (3 min)

* `plots/article-1`: for **fresh prompts, one at a time**, the full split path (remote prefill +
  transfer + first token) was slower at every size. Transfer + receiver first token alone (0.67 s
  at 25k) beats recompute (1.25 s), so existing KV can be worth moving; not tested.
* KV produced at ~2 GB/s vs ~4.5 GB/s link → the split adds ~44 % to prefill for a fresh prompt.
  The hop went through the RoCE NIC **in loopback on one host**, not a switch between hosts.
* `plots/article-2`: at the **20 jobs/s target**, per-call TTFT p99 16.7 s split vs 1.2 s colocated;
  completed-job median 29.0 s vs 12.1 s; similar whole-run mean ITL.
* Raise the caveat yourself: the load generator stamps starts when a thread frees up, so it fell
  behind under overload (started 18.5 vs 19.9 at target 20; 13.7 vs 22.2 at 28). The 28 step is
  not like-for-like. Cost per job is an estimate (1.1-1.6× for the split), not a measurement.

## 5. Gateway bottlenecks and tenant protection (2 min)

* `plots/article-3`: prefill dispatch cap of 8 (set on an A10) left prefill GPUs ~32 % busy with
  277 queued; cap 32 halved median TTFT. `plots/article-d3`: decode slot reserved first, so
  requests waiting for prefill hold decode capacity.
* A10 fixes: ramp on engine TTFT (not client TTFT), KV budget in tokens, reserve/settle tenant
  billing. Review found two more, now fixed with tests: engine TTFT included the prefill-slot
  wait; queue timeouts were missing from `orch_shed_total`.
* Noisy tenant: 541 refused at admission, others' p99 0.38 s. `plots/article-6`: priority holds
  while the gateway queues; inside vLLM it is FCFS.

## 6. Startup limits and what's next (2 min)

* `plots/article-4`: KEDA scaled at +21 s; the replica was ready at +239 s, 83 s after the burst,
  and served nothing. Times from k8s events recorded after the run
  (`metrics/a100-autoscale-events.txt`). First prompt after start: 1,014 ms prefill (MoE kernel
  compile).
* Next controlled experiment: open-loop load generator (stamp scheduled arrival), finer steps
  between 12 and 20 jobs/s, long-prompt mix (16-32k) where the split should help, two hosts.
* Part 2: automating the loops in `plots/article-d4`.
