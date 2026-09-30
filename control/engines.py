"""HTTP client for one vLLM engine: /metrics scrape, remote prefill, streamed decode."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from .config import EngineSpec


class EngineError(Exception):
    def __init__(self, status: int, reason: str, detail: str = ""):
        super().__init__(f"{status} {reason} {detail}"[:300])
        self.status, self.reason, self.detail = status, reason, detail


WATCHED = {
    "vllm:kv_cache_usage_perc": "kv_usage",
    "vllm:gpu_cache_usage_perc": "kv_usage",
    "vllm:num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
    "vllm:num_preemptions_total": "preemptions",
    "vllm:prefix_cache_queries_total": "prefix_queries",
    "vllm:prefix_cache_hits_total": "prefix_hits",
    "vllm:nixl_bytes_transferred_sum": "nixl_bytes",
    "vllm:nixl_num_failed_transfers_total": "nixl_failed",
    "vllm:nixl_num_kv_expired_reqs_total": "nixl_expired",
    "process_start_time_seconds": "start_time",
}


def parse_metrics(text: str) -> dict[str, float]:
    """Sum each watched metric across label sets (one engine may report per-rank series)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        key = WATCHED.get(name)
        if key is None:
            continue
        try:
            value = float(line.rsplit(" ", 1)[-1])
        except ValueError:
            continue
        if key == "start_time":
            out[key] = value
        else:
            out[key] = out.get(key, 0.0) + value
    if out.get("kv_usage", 0.0) > 1.0:  # some versions report percent
        out["kv_usage"] /= 100.0
    return out


def _error(resp: httpx.Response, body: bytes) -> EngineError:
    reason = {429: "engine_rate_limited", 503: "engine_unavailable", 529: "engine_overloaded"}.get(
        resp.status_code, "engine_error" if resp.status_code >= 500 else "engine_rejected"
    )
    return EngineError(resp.status_code, reason, body[:300].decode("utf-8", "replace"))


class Engine:
    def __init__(self, spec: EngineSpec, client: httpx.AsyncClient):
        self.spec, self.client = spec, client

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def pool(self) -> str:
        return self.spec.pool

    async def scrape(self) -> dict[str, float]:
        resp = await self.client.get(f"{self.spec.url}/metrics", timeout=2.0)
        resp.raise_for_status()
        return parse_metrics(resp.text)

    async def prefill(self, body: dict) -> tuple[dict, dict, float]:
        """Run only the prompt (max_tokens=1) and ask vLLM to keep the KV for a remote decode.
        Returns (kv_transfer_params, usage, seconds)."""
        req = dict(body, max_tokens=1, stream=False)
        req["kv_transfer_params"] = {
            "do_remote_decode": True,
            "do_remote_prefill": False,
            "remote_engine_id": None,
            "remote_block_ids": None,
            "remote_host": None,
            "remote_port": None,
        }
        t0 = time.perf_counter()
        resp = await self.client.post(f"{self.spec.url}/v1/chat/completions", json=req, timeout=60.0)
        if resp.status_code != 200:
            raise _error(resp, resp.content)
        data = resp.json()
        return data.get("kv_transfer_params") or {}, data.get("usage") or {}, time.perf_counter() - t0

    async def stream(self, body: dict, kv_transfer_params: dict | None = None) -> AsyncIterator[dict]:
        """Stream a chat completion as parsed SSE chunks. Closing the iterator early closes the
        HTTP connection, and vLLM aborts the request and frees its KV blocks."""
        req = dict(body, stream=True, stream_options={"include_usage": True})
        if kv_transfer_params:
            req["kv_transfer_params"] = kv_transfer_params
        async with self.client.stream(
            "POST", f"{self.spec.url}/v1/chat/completions", json=req, timeout=httpx.Timeout(120.0, connect=5.0)
        ) as resp:
            if resp.status_code != 200:
                raise _error(resp, await resp.aread())
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    return
                yield json.loads(data)


class Assembler:
    """Rebuild a non-streamed chat.completion from streamed chunks, including tool calls.
    The gateway always streams from the engine so it can measure TTFT for every request."""

    def __init__(self) -> None:
        self.id = self.model = ""
        self.created = 0
        self.content: list[str] = []
        self.tool_calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage: dict = {}
        self.first_token_at: float | None = None

    def feed(self, chunk: dict) -> None:
        self.id = self.id or chunk.get("id", "")
        self.model = self.model or chunk.get("model", "")
        self.created = self.created or chunk.get("created", 0)
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if (delta.get("content") or delta.get("tool_calls")) and self.first_token_at is None:
                self.first_token_at = time.perf_counter()
            if delta.get("content"):
                self.content.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = self.tool_calls.setdefault(
                    tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def result(self) -> dict:
        message: dict = {"role": "assistant", "content": "".join(self.content) or None}
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason}],
            "usage": self.usage,
        }
