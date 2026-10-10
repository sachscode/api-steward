"""Hop vs recompute, per prompt size: when does moving KV beat computing it again?

    python3 hop_probe.py --sizes 512,2048,8192,16384,30000 --reps 3 --out ~/api-steward/metrics/a100-hop-probe.json

For each size, every repetition uses a fresh prompt (a unique nonce first, so no prefix-cache
hit anywhere) and measures two paths to the first decode token:
  hop    prefill engine computes the prompt (max_tokens=1), decode pulls the KV over NIXL,
         decode streams the first token
  local  decode computes the prompt itself and streams the first token (no transfer)
It also records what NIXL reports (bytes, summed transfer time over TP ranks) and the RoCE
NIC's byte counters, which show whether the KV left the GPUs through the NIC.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
import uuid
from pathlib import Path

COUNTERS = Path("/sys/class/infiniband/mlx5_0/ports/1/counters")
SENTENCE = "The /invoice-preview route calls stripe.Invoice.upcoming with subscription_items. "  # ~18 tokens


def post(url: str, body: dict, timeout: float = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def first_token_s(url: str, body: dict) -> float:
    """Seconds from sending until the first streamed token arrives."""
    req = urllib.request.Request(
        url, data=json.dumps(dict(body, stream=True)).encode(), headers={"Content-Type": "application/json"}
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode().strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line[6:])
                if any((c.get("delta") or {}).get("content") for c in chunk.get("choices") or []):
                    return time.perf_counter() - t0
    return time.perf_counter() - t0


def nixl(base: str) -> tuple[float, float]:
    text = urllib.request.urlopen(f"{base}/metrics", timeout=10).read().decode()
    vals = {"bytes": 0.0, "secs": 0.0}
    for line in text.splitlines():
        if line.startswith("vllm:nixl_bytes_transferred_sum"):
            vals["bytes"] += float(line.rsplit(" ", 1)[1])
        elif line.startswith("vllm:nixl_xfer_time_seconds_sum"):
            vals["secs"] += float(line.rsplit(" ", 1)[1])
    return vals["bytes"], vals["secs"]


def nic() -> tuple[int, int]:
    if not COUNTERS.exists():
        return 0, 0
    return int((COUNTERS / "port_xmit_data").read_text()) * 4, int((COUNTERS / "port_rcv_data").read_text()) * 4


def prompt(tokens: int) -> str:
    return f"nonce {uuid.uuid4().hex}. " + SENTENCE * max(1, tokens // 18)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefill", default="http://127.0.0.1:8000")
    ap.add_argument("--decode", default="http://127.0.0.1:8001")
    ap.add_argument("--sizes", default="512,2048,8192,16384,30000")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tp", type=int, default=4, help="TP ranks; NIXL sums transfer time across them")
    ap.add_argument("--out", default="hop_probe.json")
    args = ap.parse_args()

    rows = []
    for size in (int(s) for s in args.sizes.split(",")):
        for rep in range(args.reps):
            msgs = [{"role": "user", "content": prompt(size)}]
            base = {
                "model": "steward",
                "messages": msgs,
                "temperature": 0,
                "max_tokens": 4,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            b0, s0 = nixl(args.decode)
            tx0, rx0 = nic()
            t0 = time.perf_counter()
            pre = post(
                f"{args.prefill}/v1/chat/completions",
                dict(
                    base,
                    max_tokens=1,
                    kv_transfer_params={
                        "do_remote_decode": True,
                        "do_remote_prefill": False,
                        "remote_engine_id": None,
                        "remote_block_ids": None,
                        "remote_host": None,
                        "remote_port": None,
                    },
                ),
            )
            prefill_s = time.perf_counter() - t0
            decode_first = first_token_s(
                f"{args.decode}/v1/chat/completions", dict(base, kv_transfer_params=pre["kv_transfer_params"])
            )
            hop_total = time.perf_counter() - t0
            time.sleep(1)
            b1, s1 = nixl(args.decode)
            tx1, rx1 = nic()
            # Same size, fresh prompt, decode computes it itself.
            local = first_token_s(
                f"{args.decode}/v1/chat/completions", dict(base, messages=[{"role": "user", "content": prompt(size)}])
            )
            row = {
                "target_tokens": size,
                "rep": rep,
                "prompt_tokens": pre["usage"]["prompt_tokens"],
                "blocks": len(pre["kv_transfer_params"].get("remote_block_ids") or []),
                "hop_prefill_s": round(prefill_s, 4),
                "hop_decode_first_token_s": round(decode_first, 4),
                "hop_total_s": round(hop_total, 4),
                "local_first_token_s": round(local, 4),
                "nixl_bytes": b1 - b0,
                "nixl_xfer_s_per_rank": round((s1 - s0) / args.tp, 4),
                "nic_tx_bytes": tx1 - tx0,
                "nic_rx_bytes": rx1 - rx0,
            }
            row["nixl_gbps"] = (
                round(row["nixl_bytes"] / row["nixl_xfer_s_per_rank"] / 1e9, 2) if row["nixl_xfer_s_per_rank"] else None
            )
            rows.append(row)
            print(json.dumps(row))

    print(
        f"\n{'tokens':>7} {'hop total':>10} {'local':>8} {'winner':>7} {'NIXL MB':>8} {'xfer ms':>8} {'GB/s':>6} {'NIC tx MB':>10}"
    )
    for size in sorted({r["target_tokens"] for r in rows}):
        g = [r for r in rows if r["target_tokens"] == size]
        hop = statistics.median(r["hop_total_s"] for r in g)
        loc = statistics.median(r["local_first_token_s"] for r in g)
        print(
            f"{statistics.median(r['prompt_tokens'] for r in g):>7.0f} {hop * 1000:>9.0f}ms {loc * 1000:>7.0f}ms "
            f"{'hop' if hop < loc else 'local':>7} {statistics.median(r['nixl_bytes'] for r in g) / 1e6:>8.1f} "
            f"{statistics.median(r['nixl_xfer_s_per_rank'] for r in g) * 1000:>8.1f} "
            f"{statistics.median(r['nixl_gbps'] or 0 for r in g):>6.2f} "
            f"{statistics.median(r['nic_tx_bytes'] for r in g) / 1e6:>10.1f}"
        )
    Path(args.out).expanduser().parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).expanduser().write_text(json.dumps(rows, indent=2) + "\n")
    print(f"\nsaved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
