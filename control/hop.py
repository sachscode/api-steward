"""Hop: should the prompt's KV be built on a prefill engine and moved to the decode engine?

decide(req, prefill, decode) -> (mode, reason)
  local  prefill_pool_empty    no prefill engine is eligible; decode computes the prompt itself
  local  short_prompt          uncached tokens below hop_min_tokens: recomputing is cheaper
                               than a prefill round trip plus a KV transfer
  local  decode_has_prefix     this job's previous turn was decoded on the same engine, so its
                               prefix cache already holds most of the prompt; only the new turn
                               is uncached, and that is short
  nixl   long_uncached_prompt  build KV on prefill, NIXL pulls it into decode

Same engine (src == dst) never hops: the KV is already there. A hop record says who moved
what: src, dst, prompt tokens, blocks, prefix key, backend, estimated bytes.
What is NOT copied: the prefix-cache index (decode re-indexes the pulled blocks), sampler
state, and any KV for tokens generated later; decode owns those from the first token on.
"""

from __future__ import annotations

from .config import Settings
from .fleet import Fleet
from .types import Request, Snapshot


def uncached_estimate(req: Request, decode: Snapshot, fleet: Fleet) -> int:
    last = fleet.job_prompt_tokens(req.job_id, decode.engine)
    return max(0, req.prompt_tokens - last) if last else req.prompt_tokens


def decide(req: Request, prefill: Snapshot | None, decode: Snapshot, fleet: Fleet) -> tuple[str, str]:
    settings: Settings = fleet.settings
    if prefill is None:
        return "local", "prefill_pool_empty"
    if prefill.engine == decode.engine:
        return "local", "same_engine"
    if req.prompt_tokens < settings.hop_min_tokens:
        return "local", "short_prompt"
    if uncached_estimate(req, decode, fleet) < settings.hop_min_tokens:
        return "local", "decode_has_prefix"
    return "nixl", "long_uncached_prompt"


def record(req: Request, src: str, dst: str, kv_params: dict, prompt_tokens: int, settings: Settings) -> dict:
    blocks = kv_params.get("remote_block_ids") or []
    n_blocks = sum(len(b) for b in blocks) if blocks and isinstance(blocks[0], list) else len(blocks)
    return {
        "src": src,
        "dst": dst,
        "tokens": prompt_tokens,
        "blocks": n_blocks,
        "prefix": req.prefix_key,
        "backend": "nixl",
        "bytes_estimated": n_blocks * settings.block_size * settings.kv_bytes_per_token,
        "remote_host": kv_params.get("remote_host"),
        "remote_port": kv_params.get("remote_port"),
    }
