"""Summarise one experiment run: per load step from the load file, and the resource that
saturated from Prometheus over the run's window.

    python3 analyze_run.py sweep2            # reads metrics/a100-sweep2-meta.json + load file
    python3 analyze_run.py sweep2 --json     # also writes metrics/a100-sweep2-analysis.json

Standard library only, Python 3.10 compatible (runs on the Lambda host).
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROM = "http://127.0.0.1:30090"


def pct(values: list[float], p: float) -> float:
    return values[min(len(values) - 1, int(p * len(values)))] if values else float("nan")


def prom(query: str) -> dict[str, float]:
    url = PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    out = {}
    for r in json.load(urllib.request.urlopen(url, timeout=20))["data"]["result"]:
        m = r["metric"]
        if "gpu" in m:
            key = f"gpu{m['gpu']}"
        elif m.get("app") == "orch-serve" or not m.get("app"):
            key = m.get("pod") or "all"  # gateway series describe engines through their pod label
        else:
            key = m["app"]
        out[key] = round(float(r["value"][1]), 4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--tag", default="a100")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    tag = f"{args.tag}-{args.name}"
    meta = json.loads((ROOT / "metrics" / f"{tag}-meta.json").read_text())
    jobs = [json.loads(line) for line in (ROOT / "metrics" / "load" / f"{tag}.jsonl").open()]

    steps_out = []
    header = ("rate", "jobs", "done%", "calls", "ttft p50", "ttft p99", "qwait p50", "qwait p99", "job p50")
    print("per load step (agent model calls):")
    print("".join(f"{h:>10}" for h in header))
    for phase in sorted({j["phase"] for j in jobs}):
        js = [j for j in jobs if j["phase"] == phase]
        calls = [s for j in js for s in j["steps"] if s.get("ttft_s") is not None]
        ttft = sorted(s["ttft_s"] for s in calls)
        qwait = sorted(s.get("queue_wait_decode_s") or 0.0 for s in calls)
        done = [j for j in js if j["status"] == "analysis_complete"]
        dur = sorted(j["duration_s"] for j in done)
        row = {
            "rate": js[0]["rate"],
            "jobs": len(js),
            "done_pct": round(100 * len(done) / len(js), 1),
            "calls": len(calls),
            "ttft_p50": round(pct(ttft, 0.5), 3),
            "ttft_p99": round(pct(ttft, 0.99), 3),
            "qwait_p50": round(pct(qwait, 0.5), 3),
            "qwait_p99": round(pct(qwait, 0.99), 3),
            "job_p50": round(pct(dur, 0.5), 2),
        }
        steps_out.append(row)
        print("".join(f"{v:>10}" for v in row.values()))

    window = f"[{meta['end'] - meta['start']}s] @ {meta['end']}"
    sub = f"[{meta['end'] - meta['start']}s:15s] @ {meta['end']}"
    queries = {
        "peak vLLM running": f"max_over_time(vllm:num_requests_running{window})",
        "peak vLLM waiting": f"max_over_time(vllm:num_requests_waiting{window})",
        "peak KV used (0-1)": f"max_over_time(vllm:kv_cache_usage_perc{window})",
        "preemptions": f"increase(vllm:num_preemptions_total{window})",
        "peak our queue": f"max_over_time(sum by (pod) (orch_replica_queue_depth){sub})",
        "peak slots in use": f"max_over_time(orch_replica_inflight{window})",
        "slot cap": f"max_over_time(orch_replica_slot_cap{window})",
        "peak KV tokens reserved": f"max_over_time(orch_replica_tokens_inflight{window})",
        "KV token budget": f"max_over_time(orch_replica_token_cap{window})",
        "mean ITL (s)": f"increase(vllm:inter_token_latency_seconds_sum{window})"
        f" / increase(vllm:inter_token_latency_seconds_count{window})",
        "mean engine TTFT (s)": f"increase(vllm:time_to_first_token_seconds_sum{window})"
        f" / increase(vllm:time_to_first_token_seconds_count{window})",
        "generated tokens/s": f"rate(vllm:generation_tokens_total{window})",
        "prompt tokens/s": f"rate(vllm:prompt_tokens_total{window})",
        "prefix cache hit rate": f"increase(vllm:prefix_cache_hits_total{window})"
        f" / increase(vllm:prefix_cache_queries_total{window})",
        "NIXL GB moved": f"increase(vllm:nixl_bytes_transferred_sum{window}) / 1e9",
        "GPU util avg (%)": f"avg_over_time(DCGM_FI_DEV_GPU_UTIL{window})",
        "tensor active avg (0-1)": f"avg_over_time(DCGM_FI_PROF_PIPE_TENSOR_ACTIVE{window})",
    }
    resources = {}
    print(f"\nresources over the run ({meta['mode']}, {meta['phases']}):")
    for label, q in queries.items():
        try:
            resources[label] = prom(q)
        except Exception as exc:  # noqa: BLE001 - keep summarising if one query fails
            resources[label] = {"error": str(exc)[:80]}
        print(f"  {label:26s} {resources[label]}")
    if args.json:
        path = ROOT / "metrics" / f"{tag}-analysis.json"
        path.write_text(json.dumps({"meta": meta, "steps": steps_out, "resources": resources}, indent=2) + "\n")
        print(f"\nsaved {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
