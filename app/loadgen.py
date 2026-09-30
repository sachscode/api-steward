"""Agent-shaped load: open-loop arrivals of real API Steward jobs through the gateway.

    python3 -m app.loadgen --phases 30:1,60:4,30:10 --out metrics/load/knee.jsonl

Every job is the real tool loop (think -> tool -> observe -> answer), so each step's context
grows and shares the system + tool-schema prefix. Traffic mixes, as in the Class 7 labs:
  --interactive   share of jobs that are interactive (tenants demo-a/demo-b); rest are batch
                  sweeps (tenant sweep)
  --unique        share of jobs carrying their own repository notes: a long, uncached prefix
                  (hops, KV pressure). The rest ask the identical task (prefix-cache hits).
  --abort         share of jobs whose client gives up after --abort-after-s (client-gone path)
Arrivals are Poisson and do not wait for earlier jobs (open loop), so overload shows up as
queueing and shedding instead of the generator politely slowing down.
Standard library only, so it runs on the cluster hosts without installing anything.
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agent import DEFAULT_TASK, GatewayClient, run_agent

VOCAB = (
    "subscription invoice preview proration price quantity customer item billing cycle anchor "
    "coupon discount tax currency schedule webhook idempotency retry route handler serializer "
    "template dashboard estimate upgrade downgrade trial renewal ledger migration contract"
)
WORDS = VOCAB.split(" ")


def repo_notes(rng: random.Random, chars: int) -> str:
    """Unique per-job context, like a customer's own integration notes."""
    out, n = [], 0
    while n < chars:
        line = " ".join(rng.choice(WORDS) for _ in range(12)) + "."
        out.append(line)
        n += len(line) + 1
    return "\n".join(out)


class AbortingClient(GatewayClient):
    """A client that stops waiting after a fixed time: the socket closes mid-request."""

    def __init__(self, base_url: str, abort_after_s: float):
        super().__init__(base_url)
        self.abort_after_s = abort_after_s

    def complete(self, messages, *, tenant, workload_class, job_id, step, timeout):
        return super().complete(
            messages,
            tenant=tenant,
            workload_class=workload_class,
            job_id=job_id,
            step=step,
            timeout=min(timeout, self.abort_after_s),
        )


def percentile(ordered: list[float], q: float) -> float:
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def parse_phases(raw: str) -> list[tuple[float, float]]:
    """'30:1,60:4' -> [(30 s at 1 job/s), (60 s at 4 jobs/s)]"""
    return [(float(d), float(r)) for d, r in (p.split(":") for p in raw.split(","))]


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent-shaped open-loop load through the gateway")
    ap.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    ap.add_argument("--phases", default="30:1,60:4,30:10", help="seconds:jobs_per_second,...")
    ap.add_argument("--interactive", type=float, default=0.7)
    ap.add_argument("--unique", type=float, default=0.5)
    ap.add_argument("--unique-chars", type=int, default=3000)
    ap.add_argument("--abort", type=float, default=0.05)
    ap.add_argument("--abort-after-s", type=float, default=0.3)
    ap.add_argument("--max-workers", type=int, default=256)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="metrics/load/run.jsonl")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    tally: Counter[str] = Counter()
    ttfts: dict[str, list[float]] = {}

    def job(n: int, phase: int, rate: float, klass: str, unique: bool, abort: bool) -> None:
        tenant = ("demo-a" if n % 2 else "demo-b") if klass == "interactive" else "sweep"
        question = DEFAULT_TASK
        if unique:
            question += "\n\nRepository notes:\n" + repo_notes(random.Random(n), args.unique_chars)
        client = AbortingClient(args.gateway_url, args.abort_after_s) if abort else GatewayClient(args.gateway_url)
        t0 = time.time()
        result = run_agent(client, question=question[:4000], tenant=tenant, workload_class=klass)
        record = {
            "n": n,
            "phase": phase,
            "rate": rate,
            "start": t0,
            "klass": klass,
            "tenant": tenant,
            "unique": unique,
            "abort": abort,
            "status": result["status"],
            "reason": "client_aborted" if abort and result["reason"] == "endpoint_unavailable" else result["reason"],
            "http_status": result.get("http_status"),
            "duration_s": result["duration_s"],
            "steps": [
                {
                    k: s.get(k)
                    for k in (
                        "step",
                        "decode",
                        "hop_mode",
                        "hop_reason",
                        "ttft_s",
                        "e2e_s",
                        "prompt_tokens",
                        "completion_tokens",
                        "queue_wait_decode_s",
                        "via",
                    )
                }
                | {"hop_blocks": (s.get("hop") or {}).get("blocks")}
                for s in result["steps"]
            ],
        }
        with lock:
            with out.open("a") as f:
                f.write(json.dumps(record) + "\n")
            tally[f"{klass}:{result['status']}:{record['reason']}"] += 1
            for s in record["steps"]:
                if s["ttft_s"] is not None:
                    ttfts.setdefault(f"{klass}/{s['hop_mode']}", []).append(s["ttft_s"])

    phases = parse_phases(args.phases)
    print(f"load: {phases} (seconds, jobs/s) -> {out}")
    n = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        for i, (duration, rate) in enumerate(phases):
            end = time.monotonic() + duration
            print(f"phase {i}: {rate} jobs/s for {duration:.0f} s", flush=True)
            while time.monotonic() < end:
                klass = "interactive" if rng.random() < args.interactive else "batch"
                pool.submit(job, n, i, rate, klass, rng.random() < args.unique, rng.random() < args.abort)
                n += 1
                time.sleep(rng.expovariate(rate))
        print(f"submitted {n} jobs; waiting for the last ones to finish", flush=True)

    print("\njobs by outcome:")
    for key, count in tally.most_common():
        print(f"  {count:5d}  {key}")
    print("\nstep TTFT (s) by class/path:")
    for key, vals in sorted(ttfts.items()):
        vals.sort()
        p50, p90, p99 = (percentile(vals, q) for q in (0.5, 0.9, 0.99))
        print(f"  {key:22s} n={len(vals):5d}  p50={p50:.3f}  p90={p90:.3f}  p99={p99:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
