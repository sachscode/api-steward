"""Send one request through prefill -> KV hop -> decode, the way the gateway will, and show
the evidence that KV actually moved.

    python3 pd_smoke.py --prefill http://127.0.0.1:8000 --decode http://127.0.0.1:8001

Flow (vLLM NixlConnector, pull mode):
  1. POST to the prefill engine with kv_transfer_params.do_remote_decode=true and max_tokens=1.
     Prefill computes the prompt's KV and returns kv_transfer_params: which blocks, on which
     engine, at which host and port.
  2. POST the same request to the decode engine with those kv_transfer_params. Decode pulls
     the KV blocks from prefill over NIXL/UCX instead of recomputing the prompt.
  3. Scrape both engines' /metrics: vllm:nixl_* counters prove real bytes moved.
Stdlib only, so it runs on a fresh host without installing anything.
"""

import argparse
import json
import time
import urllib.request

SYSTEM = (
    "You are API Steward, a private API maintenance agent. You inspect an integration, read the "
    "relevant upgrade notes, check the proposed mapping, and return a migration assessment."
)


def post(url: str, body: dict, timeout: float = 120) -> tuple[dict, float]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data, time.perf_counter() - t0


def metrics(base: str, prefixes: tuple[str, ...]) -> list[str]:
    with urllib.request.urlopen(f"{base}/metrics", timeout=10) as resp:
        lines = resp.read().decode().splitlines()
    return [ln for ln in lines if ln.startswith(prefixes)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefill", default="http://127.0.0.1:8000")
    ap.add_argument("--decode", default="http://127.0.0.1:8001")
    ap.add_argument("--model", default="steward")
    ap.add_argument("--repeat", type=int, default=40, help="repeat the context to make the prompt longer")
    ap.add_argument("--max-tokens", type=int, default=64)
    args = ap.parse_args()

    context = " ".join(["The /invoice-preview route calls stripe.Invoice.upcoming with subscription_items."] * args.repeat)
    request = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": context + "\nWhich call replaces it in API version 2025-03-31.basil?"},
        ],
        "max_tokens": args.max_tokens,
        "temperature": 0,
    }

    watch = ("vllm:nixl_", "vllm:prefix_cache", "vllm:prompt_tokens_total", "vllm:kv_cache_usage_perc")
    before = {"prefill": metrics(args.prefill, watch), "decode": metrics(args.decode, watch)}

    prefill_body = dict(request, max_tokens=1, stream=False)
    prefill_body["kv_transfer_params"] = {
        "do_remote_decode": True,
        "do_remote_prefill": False,
        "remote_engine_id": None,
        "remote_block_ids": None,
        "remote_host": None,
        "remote_port": None,
    }
    pre, t_pre = post(f"{args.prefill}/v1/chat/completions", prefill_body)
    kvp = pre.get("kv_transfer_params") or {}
    usage = pre.get("usage", {})
    print(f"[prefill] {t_pre * 1000:.0f} ms  prompt_tokens={usage.get('prompt_tokens')}")
    if not kvp:
        print("[prefill] no kv_transfer_params returned: the connector is not active on prefill")
        return 1
    blocks = kvp.get("remote_block_ids") or []
    nblocks = sum(len(b) for b in blocks) if blocks and isinstance(blocks[0], list) else len(blocks)
    print(f"[hop]     engine={kvp.get('remote_engine_id')} host={kvp.get('remote_host')} "
          f"port={kvp.get('remote_port')} blocks={nblocks}")

    dec, t_dec = post(f"{args.decode}/v1/chat/completions", dict(request, kv_transfer_params=kvp))
    text = dec["choices"][0]["message"]["content"]
    print(f"[decode]  {t_dec * 1000:.0f} ms  completion_tokens={dec.get('usage', {}).get('completion_tokens')}")
    print(f"[decode]  {text[:200]!r}")

    time.sleep(2)
    for name, base in (("prefill", args.prefill), ("decode", args.decode)):
        after = metrics(base, watch)
        changed = [ln for ln in after if ln not in before[name]]
        print(f"\n[{name} /metrics changed]")
        print("\n".join(changed) if changed else "  (no watched metric changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
