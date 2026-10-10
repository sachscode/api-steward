"""Build notebook/part5_queues.ipynb from the cells below.

    python notebook/build_part5.py
    GATEWAY_URL=http://127.0.0.1:18080 PROM_URL=http://127.0.0.1:19090 NB_TAG=a10 \
        jupyter nbconvert --to notebook --execute --inplace notebook/part5_queues.ipynb
"""

from pathlib import Path

import nbformat as nbf

CELLS: list[tuple[str, str]] = []


def md(text: str) -> None:
    CELLS.append(("md", text.strip()))


def code(text: str) -> None:
    CELLS.append(("code", text.strip()))


md("""
# Part 5: what runs next, and what does not

API Steward's serve path is `admit → place → our queue → engine (waiting / running / preempt)`.
This notebook answers the Part 5 questions with scrapes from the running cluster: the gateway's
`/debug/state` and `/metrics`, Prometheus (vLLM, KEDA, DCGM), and the per-request traces and load
runs in `metrics/`. Where a question needs a controlled comparison, it runs a small live
experiment through the gateway, the same way the app sends requests.

**Run it** with two SSH tunnels from the Mac (gateway on 18080, Prometheus NodePort 30090 on 19090):

```bash
ssh -i $LAMBDA_SSH_KEY -f -N -L 18080:127.0.0.1:8080 -L 19090:127.0.0.1:30090 $HOST
GATEWAY_URL=http://127.0.0.1:18080 PROM_URL=http://127.0.0.1:19090 NB_TAG=a10 \\
  jupyter nbconvert --to notebook --execute --inplace notebook/part5_queues.ipynb
```

`NB_TAG` names the cluster (`a10` rehearsal, `a100` final); plots go to `plots/` and scrapes to `metrics/`.
""")

code("""
import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd().parent if Path.cwd().name == "notebook" else Path.cwd()))
from notebook import steward_nb as nb
import pandas as pd, matplotlib.pyplot as plt
nb.style()
TAG = os.environ.get("NB_TAG", "a10")
RUN = os.environ.get("NB_RUN", "a10-stress-2")     # the load run most sections analyse
jobs = nb.load_run(RUN); steps = nb.steps(jobs)
START, END = nb.run_window(jobs)
phase_starts = jobs.groupby("phase").start.min()
print(f"cluster={TAG}  run={RUN}: {len(jobs)} jobs, {len(steps)} model calls, "
      f"{pd.to_datetime(START, unit='s'):%H:%M:%S} to {pd.to_datetime(END, unit='s'):%H:%M:%S} UTC")
nb.state_table()
""")

md("""
## 1. Who sits in our queue, and who sits in vLLM's waiting queue?

Every engine has a fixed number of **dispatch slots** in the gateway (`slot_cap`, 16 for decode on
the A10, below vLLM's `max_num_seqs` of 32). A request that finds no free slot waits in **our**
queue. Only dispatched requests reach vLLM, so vLLM's own waiting queue should stay near zero: the
gateway, not the engine, decides what runs next. The table above is the live picture; below is
the same comparison through the load run.
""")

code("""
ours = nb.prom_range("sum by (pod) (orch_replica_queue_depth)", START, END, "15s", label="pod")
vllm_wait = nb.prom_range("vllm:num_requests_waiting", START, END, "15s", label="app")
fig, axes = plt.subplots(1, 2, figsize=(12, 3.6), sharey=True)
ours.plot(ax=axes[0], title="Our queue depth (gateway)"); vllm_wait.plot(ax=axes[1], title="vLLM waiting (engine)")
for ax in axes:
    for t in phase_starts: ax.axvline(pd.to_datetime(t, unit="s"), color="grey", ls=":", lw=1)
    ax.set_xlabel("")
nb.save(fig, f"{TAG}-queue-ours-vs-vllm")
print("peak our queue:", ours.max().round(1).to_dict())
print("peak vLLM waiting:", vllm_wait.max().round(1).to_dict())
""")

md("""
## 2. Waiting, running, preempted

`running` is what vLLM is batching right now; `waiting` is vLLM's own queue; preemptions are
sequences vLLM evicted from KV to make room (they recompute later). With the gateway capping
dispatch, running should plateau at the slot cap and preemptions should stay at zero.
""")

code("""
running = nb.prom_range("vllm:num_requests_running", START, END, "15s", label="app")
preempt = nb.prom_range("rate(vllm:num_preemptions_total[1m])", START, END, "15s", label="app")
kv = nb.prom_range("vllm:kv_cache_usage_perc", START, END, "15s", label="app")
fig, axes = plt.subplots(1, 3, figsize=(15, 3.4))
running.plot(ax=axes[0], title="vLLM running"); kv.plot(ax=axes[1], title="KV cache used (0-1)")
preempt.plot(ax=axes[2], title="Preemptions per second")
for ax in axes: ax.set_xlabel("")
nb.save(fig, f"{TAG}-running-kv-preemptions")
print("peak running:", running.max().to_dict()); print("peak KV used:", kv.max().round(3).to_dict())
print("preemptions in run:", float(preempt.sum().sum()) * 15)
""")

md("""
## 3. `orch_replica_queue_depth`: which engine, under which mix?

The load run has three phases (light, knee, burst), a 70/30 interactive/batch mix, and half the
jobs carry unique repository notes (long uncached prompts). Queue depth by engine and class shows
where the pressure lands; the table shows who waited, by phase, class and prompt type.
""")

code("""
depth = nb.prom_range("orch_replica_queue_depth", START, END, "15s")
depth.columns = [c.replace("pod=", "").split(",klass=")[0] + " " + c.split("klass=")[1].split(",")[0]
                 for c in depth.columns]
depth = depth.loc[:, depth.max() > 0]
ax = depth.plot(title="orch_replica_queue_depth by engine and class", figsize=(12, 3.6))
for t in phase_starts: ax.axvline(pd.to_datetime(t, unit="s"), color="grey", ls=":", lw=1)
ax.set_xlabel(""); nb.save(ax.figure, f"{TAG}-queue-depth-by-pod")
wait = (steps.dropna(subset=["queue_wait_decode_s"])
        .groupby(["phase", "rate", "klass", "unique"]).queue_wait_decode_s
        .agg(calls="count", p50="median", p90=lambda s: s.quantile(0.9)).round(3))
wait
""")

md("""
## 4. A long uncached request and a short agent step are both ready. Who goes first?

**We do, then the engine.** Our queue orders by class: interactive before batch, and batch is
promoted after `batch_aging_s` (15 s) so it is never starved. Once dispatched, both run in vLLM's
continuous batch, and chunked prefill splits a long prompt into pieces so it does not stall
decode steps of short requests already running.

Live experiment: fill the decode slots with **long batch** requests (about 2,400 prompt tokens of
real integration source, unique per request), then send **short interactive** agent steps one second later, while batch is
queued. Interactive arrives later but is dispatched first. Some batch requests are also refused:
at admission when decode KV passes the batch high-water mark (503 `kv_free`, section 7), and at
guardrails when prompt + output cannot fit `max_model_len` (413 `context_too_long`), before any
GPU work.
""")

code("""
FIXTURE = "\\n".join((nb.ROOT / "fixtures" / "stripe_preview" / f).read_text()
                     for f in ("integration.txt", "contract.txt", "migration.md"))
def long_text(i, target_chars=9_600):   # ~2,400 tokens of realistic source + notes, unique per request
    reps = target_chars // len(FIXTURE) + 1
    body = "\\n".join([FIXTURE] * reps)[:target_chars]
    return f"Customer {i} integration, pasted {reps} times:\\n{body}\\nSummarise the risks of this upgrade in detail."
payloads = [(0.0, nb.step_payload(long_text(i), tenant=("sweep" if i % 2 else "demo-b"), klass="batch",
                                  max_tokens=512)) for i in range(40)]
payloads += [(1.0 + 0.1 * i, nb.step_payload("Assess the Stripe invoice-preview upgrade.", tenant="demo-a",
                                             klass="interactive", max_tokens=64)) for i in range(8)]
pre_before = nb.prom('sum(vllm:num_preemptions_total{pool="decode"})').value.sum()
exp = nb.post_all(payloads)
time.sleep(20)   # let Prometheus scrape the engines once more
pre_after = nb.prom('sum(vllm:num_preemptions_total{pool="decode"})').value.sum()
print(f"decode preemptions during the experiment: {pre_after - pre_before:.0f}")
print("outcome by class:"); display(exp.assign(reason=exp.get("error", pd.Series(index=exp.index)).fillna("served"))
                                    .groupby(["klass", "status", "reason"]).size().to_frame("requests"))
ok = exp[exp.status == 200]
queued = ok[ok.queue_wait_decode_s > 0]
pd.DataFrame({
    "served": ok.groupby("klass").size(),
    "had to queue": queued.groupby("klass").size(),
    "queue wait p50 (queued only)": queued.groupby("klass").queue_wait_decode_s.median(),
    "queue wait max": queued.groupby("klass").queue_wait_decode_s.max(),
    "engine TTFT p50": ok.groupby("klass").engine_ttft_s.median(),
}).round(3)
""")

md("""
## 5. PagedAttention packs KV; the prefix cache reuses it. Which saved memory on our mix?

* **PagedAttention** allocates KV in 16-token blocks as a sequence grows, instead of reserving
  `max_len` per sequence up front. Saving at the peak = running sequences × (max_len − real
  length) × KV bytes per token.
* **Prefix cache** stores the shared system prompt + tool schema once and lets every running
  sequence point at the same blocks. Saving at the peak = (running − 1) × shared prefix tokens ×
  KV bytes per token. It also saves prefill compute on every request that hits it.

On this mix PagedAttention is what makes concurrency possible at all (capacity); the prefix
cache mostly saves prefill compute, and a smaller amount of memory.
""")

code("""
cfg = nb.prom("vllm:cache_config_info")
show = [c for c in ("app", "block_size", "num_gpu_blocks", "gpu_memory_utilization", "enable_prefix_caching",
                    "cache_dtype") if c in cfg]
display(cfg[show]) if len(cfg) else print("no cache_config_info")
src = nb.prom("sum by (pool, source) (vllm:prompt_tokens_by_source_total)")
display(src.pivot_table(index="pool", columns="source", values="value", aggfunc="sum").fillna(0).astype(int))

KV_BYTES = int(os.environ.get("KV_BYTES_PER_TOKEN", "114688"))   # Qwen3-0.6B on the A10; 98,304 for Qwen3-30B-A3B
MAX_LEN = int(os.environ.get("MAX_LEN", "4096"))
blocks = int(cfg[cfg.app.str.contains("decode")].num_gpu_blocks.astype(int).iloc[0]) if len(cfg) else 0
kv_tokens = blocks * 16
real_len = float((steps.prompt_tokens + steps.completion_tokens).dropna().mean())
shared_prefix = int(steps[steps.step == 1].prompt_tokens.min())    # step-1 prompt of the shared task
peak_running = float(running.max().max())
by_src = src.pivot_table(index="pool", columns="source", values="value", aggfunc="sum").fillna(0)
pre = by_src.loc["prefill"] if "prefill" in by_src.index else None
print(f"decode KV: {blocks} blocks x 16 = {kv_tokens:,} tokens")
print(f"reserve max_len ({MAX_LEN}) per sequence: {kv_tokens // MAX_LEN} sequences fit")
print(f"PagedAttention at the real mean length ({real_len:.0f} tokens): {int(kv_tokens // real_len)} sequences fit")
print(f"prefix cache, memory: at the peak ({peak_running:.0f} running) the {shared_prefix}-token shared prefix is "
      f"stored once, not {peak_running:.0f} times: {(peak_running - 1) * shared_prefix * KV_BYTES / 2**30:.2f} GiB")
if pre is not None:
    print(f"prefix cache, compute: prefill served {pre.get('local_cache_hit', 0) / pre.sum():.0%} of its prompt tokens from "
          f"cache and computed {pre.get('local_compute', 0) / pre.sum():.0%}")
""")

md("""
## 6. Chunked prefill and continuous batching: which engine flags, and why

The engine flags come from `cluster/render_engines.py` (one profile per cluster). The gateway's
slot caps sit **below** `max_num_seqs` so vLLM always has room for what we dispatch and its
waiting queue stays short. `max_num_batched_tokens` (the per-step token budget that chunked
prefill splits long prompts into) is left at vLLM's default here; the A100 run records the value
vLLM chose at startup.
""")

code("""
sys.path.insert(0, str(nb.ROOT / "cluster"))
from render_engines import PROFILES
flags = pd.DataFrame({name: {"model": p["model"], "tensor_parallel": p["tp"], "max_model_len": p["max_len"],
                              "max_num_seqs": p["max_num_seqs"], "gpu_memory_utilization": p["util"],
                              "gateway decode slots": p["gateway"]["decode_slot_cap"],
                              "gateway prefill slots": p["gateway"]["prefill_slot_cap"],
                              "hop_min_tokens": p["gateway"]["hop_min_tokens"]}
                      for name, p in PROFILES.items()})
flags
""")

md("""
## 7. KV is full after admission: do we shed at the door, or does the engine preempt?

**We hold and shed at our door; the engine should not have to preempt.** Two mechanisms:

1. **Admission** refuses with 503 `kv_free` when every decode engine is above the class's KV
   high-water mark (92 % interactive, 80 % batch). Batch is refused first, so interactive keeps
   the last KV headroom.
2. **Dispatch** reserves each request's tokens (prompt + output budget) against the engine's KV
   budget (vLLM's `num_gpu_blocks` × 16 × 92 %). A request that would overfill KV waits in our
   queue even when a slot is free.

Admission alone was not enough: in the first run of the section 4 experiment, 40 long requests
arrived within one second, inside a single 0.5 s snapshot, so admission saw empty KV and let
them in; the request-count slot cap then dispatched 16 × ~2,900 tokens into 25,328 tokens of KV
and vLLM preempted 5 sequences. The token budget closes that gap. Preemption inside vLLM remains
the backstop: a preempted sequence throws away its KV and recomputes it.
""")

code("""
g = nb.gateway_metrics()
sheds = nb.counter_table(g, "orch_shed_total")
print("refusals by reason:"); display(sheds.groupby(["stage", "reason", "code"]).value.sum().to_frame() if len(sheds) else "none")
kv_peak = nb.prom(f"max_over_time(vllm:kv_cache_usage_perc[{int(END - START)}s] @ {END})")
pre = nb.prom("sum by (app) (vllm:num_preemptions_total)")
print("peak KV used during the run:", kv_peak.set_index("app").value.round(3).to_dict() if len(kv_peak) else {})
print("preemptions since engine start:", pre.set_index("app").value.to_dict() if len(pre) else {})
""")

md("""
## 8. The client is gone. Who frees the KV, and how?

* **In our queue:** the waiter is removed; no GPU work happened.
* **During decode:** the gateway closes the engine stream; vLLM aborts the request and frees its
  blocks.
* **Between prefill and decode:** prefill has built KV for a remote decode that will never pull
  it. The NIXL connector frees it when its lease expires (`kv_lease_duration`, 30 s), counted in
  `vllm:nixl_num_kv_expired_reqs_total`. That is the ghost we cannot free directly.

Live experiment: start a streamed request and hang up after a few chunks.
""")

code("""
def aborted():
    t = nb.counter_table(nb.gateway_metrics(), "orch_aborted_total")
    return t.set_index("stage").value.to_dict() if len(t) else {}
before = aborted()
out = nb.abandon_stream("Explain, step by step and at length, how invoice previews work.", after_chunks=3)
time.sleep(2)
after = aborted()
print("read", out["chunks_read"], "chunks, then hung up")
print("orch_aborted_total before:", before); print("orch_aborted_total after: ", after)
exp_ghost = nb.prom("vllm:nixl_num_kv_expired_reqs_total")
print("expired NIXL leases (ghost KV freed by timeout):",
      exp_ghost.set_index("app").value.to_dict() if len(exp_ghost) else {})
nb.state_table()[["slots open", "vLLM running"]]
""")

md("""
## 9. A worker comes back: slam it at 100 %, or ramp while p99 holds?

**Ramp**, but judge the engine, not the queue. A new engine is warmed with requests carrying the
real system prompt and tool schema, then its slots open 25 % → 50 % → 100 %, one step every 15 s,
holding a step while the engine's own p99 TTFT (dispatch → first token) breaks the SLO. An engine
process that has been up for more than 5 minutes skips the ramp after a gateway restart.

Below: a KEDA scale-out of `decode-1` (`NB_SCALE_RUN`), and a before/after comparison of two runs
(`NB_COMPARE`). On the A10 the comparison is the ramp fix: the first stress run fed client TTFT
(which included our queue) into the ramp, and held
`decode-0` at 25 % while its own queue grew.
""")

code("""
dash = nb.load_run(os.environ.get("NB_SCALE_RUN", "a10-dash")); ds, de = nb.run_window(dash)
warm = nb.prom_range("orch_replica_warm_state", ds, de, "15s", label="pod")
weight = nb.prom_range("orch_replica_ramp_weight", ds, de, "15s", label="pod")
reps = nb.prom_range('kube_deployment_status_replicas_available{deployment=~"vllm-decode-.*"}', ds, de, "15s",
                     label="deployment")
fig, axes = plt.subplots(1, 3, figsize=(15, 3.4))
reps.plot(ax=axes[0], title="Decode replicas available (KEDA)"); warm.plot(ax=axes[1], title="Warm state (3 = warm)")
weight.plot(ax=axes[2], title="Ramp weight (share of slots)")
for ax in axes: ax.set_xlabel("")
nb.save(fig, f"{TAG}-scale-out-warm-ramp")
cmp = []
for run in os.environ.get("NB_COMPARE", "a10-stress,a10-stress-2").split(","):
    s = nb.steps(nb.load_run(run)).dropna(subset=["ttft_s"])
    for k, g in s.groupby("klass"):
        cmp.append({"run": run, "klass": k, "calls": len(g), "ttft p50": g.ttft_s.median(),
                    "ttft p90": g.ttft_s.quantile(0.9), "ttft p99": g.ttft_s.quantile(0.99)})
pd.DataFrame(cmp).set_index(["run", "klass"]).round(3)
""")

md("""
## 10. The hop: where KV moved, how much, and proof it was used

Each hop record says `src → dst`, prompt tokens, blocks, prefix key, backend and an **upper
bound** on bytes (all prompt blocks × 16 × KV bytes per token). NIXL only pulls the blocks the
decode engine does not already hold in its prefix cache, so vLLM's measured bytes are lower.
Measured bytes ÷ KV bytes per token should match the tokens decode received from outside
(`external_prefix_cache_hits`): that is the proof the transferred KV was used.
""")

code("""
tr = nb.traces()
hops = pd.json_normalize(tr.dropna(subset=["hop"]).hop)
print(f"{len(hops)} hops in the trace; sample:")
display(hops.tail(5)[["src", "dst", "tokens", "blocks", "backend", "bytes_estimated"]])
meas = nb.prom("sum by (pool) (vllm:nixl_bytes_transferred_sum)")
ext = nb.prom("sum by (pool) (vllm:external_prefix_cache_hits_total)")
m = float(meas.set_index("pool").value.get("decode", 0)) if len(meas) else 0.0
x = float(ext.set_index("pool").value.get("decode", 0)) if len(ext) else 0.0
print(f"NIXL bytes measured by vLLM (decode pulls): {m / 1e9:.1f} GB = {m / KV_BYTES:,.0f} tokens of KV")
print(f"prompt tokens decode received instead of computing: {x:,.0f}")
print(f"gateway upper bound over the same hops (trace, all prompt blocks): {hops.bytes_estimated.sum() / 1e9:.1f} GB")
print(f"measured/received = {m / KV_BYTES / max(x, 1):.3f}: NIXL moves whole 16-token blocks, the hit counter counts tokens")
print("decode's own prompt sources (tokens):", by_src.loc["decode"].astype(int).to_dict() if "decode" in by_src.index else {})
""")

md("""
## Evidence written to `metrics/`

The gateway scrape, the engine state, the hop dictionary, eviction counts, and the 429/503
counts, tagged with the cluster name.
""")

code("""
g = nb.gateway_metrics()
sheds = nb.counter_table(g, "orch_shed_total")
codes = sheds.groupby("code").value.sum().to_dict() if len(sheds) else {}
evict = nb.counter_table(g, "orch_affinity_evictions_total")
paths = nb.dump(TAG, gateway_scrape=g, state=nb.state(), hop_dict=hops.tail(200).to_dict("records"),
                evictions=evict.to_dict("records") if len(evict) else [],
                refusals={"429": codes.get("429", 0), "503": codes.get("503", 0), "by_reason": sheds.to_dict("records")})
for p in paths: print(p.relative_to(nb.ROOT))
""")


def main() -> None:
    nb_ = nbf.v4.new_notebook()
    nb_.cells = [nbf.v4.new_markdown_cell(t) if kind == "md" else nbf.v4.new_code_cell(t) for kind, t in CELLS]
    nb_.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    out = Path(__file__).resolve().parent / "part5_queues.ipynb"
    nbf.write(nb_, out)
    print(f"wrote {out} ({len(CELLS)} cells)")


if __name__ == "__main__":
    main()
