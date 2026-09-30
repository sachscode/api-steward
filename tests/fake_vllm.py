"""A fake vLLM fleet behind httpx.MockTransport: /metrics, NixlConnector prefill handles and
streamed chat completions that walk the API Steward tool loop. Tests control its state."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx


@dataclass
class FakeEngine:
    id: str
    port: int
    kv_usage: float = 0.1
    running: int = 0
    waiting: int = 0
    start_time: float = 1000.0
    status: int = 200  # force errors on completions
    up: bool = True
    kv_blocks: int = 100_000
    requests: list[dict] = field(default_factory=list)

    def metrics(self) -> str:
        return (
            f'vllm:kv_cache_usage_perc{{engine="0",model_name="steward"}} {self.kv_usage}\n'
            f'vllm:num_requests_running{{engine="0",model_name="steward"}} {self.running}\n'
            f'vllm:num_requests_waiting{{engine="0",model_name="steward"}} {self.waiting}\n'
            f"process_start_time_seconds {self.start_time}\n"
            f'vllm:cache_config_info{{block_size="16",num_gpu_blocks="{self.kv_blocks}",enable_prefix_caching="True"}} 1.0\n'
        )


def agent_reply(messages: list[dict]) -> dict:
    """What a well-behaved model does at each step of the API Steward loop."""
    tools_done = sum(1 for m in messages if m.get("role") == "tool")
    plan = [
        ("inspect_integration", "{}"),
        ("read_migration_guide", "{}"),
        (
            "check_migration_plan",
            json.dumps(
                {
                    "operation": "stripe.Invoice.create_preview",
                    "items_parameter": "subscription_details.items",
                    "api_version": "2025-03-31.basil",
                }
            ),
        ),
    ]
    if tools_done < len(plan):
        name, args = plan[tools_done]
        return {"tool": (f"call_{tools_done}", name, args)}
    return {
        "content": "Replace Invoice.upcoming with Invoice.create_preview and move items under subscription_details."
    }


def sse(chunks: list[dict]) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


class FakeFleet:
    def __init__(self, engines: list[FakeEngine]):
        self.by_port = {e.port: e for e in engines}

    def url(self, engine: FakeEngine) -> str:
        return f"http://fake:{engine.port}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        eng = self.by_port.get(request.url.port)
        if eng is None or not eng.up:
            raise httpx.ConnectError("engine down", request=request)
        if request.url.path == "/metrics":
            return httpx.Response(200, text=eng.metrics())
        body = json.loads(request.content)
        eng.requests.append(body)
        if eng.status != 200:
            return httpx.Response(eng.status, json={"error": "forced"})
        prompt_tokens = len(json.dumps(body["messages"])) // 4
        kvp = body.get("kv_transfer_params") or {}
        if kvp.get("do_remote_decode"):
            blocks = -(-prompt_tokens // 16)
            return httpx.Response(
                200,
                json={
                    "id": "pre",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "length"}
                    ],
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1},
                    "kv_transfer_params": {
                        "do_remote_prefill": True,
                        "do_remote_decode": False,
                        "remote_engine_id": eng.id,
                        "remote_block_ids": list(range(blocks)),
                        "remote_host": "10.0.0.1",
                        "remote_port": 5600,
                    },
                },
            )
        reply = agent_reply(body["messages"])
        base = {"id": "cmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "steward"}
        chunks = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant"}}])]
        if "tool" in reply:
            cid, name, args = reply["tool"]
            chunks.append(
                dict(
                    base,
                    choices=[
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": cid,
                                        "type": "function",
                                        "function": {"name": name, "arguments": ""},
                                    }
                                ]
                            },
                        }
                    ],
                )
            )
            half = len(args) // 2
            for part in (args[:half], args[half:]):
                chunks.append(
                    dict(
                        base,
                        choices=[
                            {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": part}}]}}
                        ],
                    )
                )
            chunks.append(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]))
        else:
            for word in reply["content"].split(" "):
                chunks.append(dict(base, choices=[{"index": 0, "delta": {"content": word + " "}}]))
            chunks.append(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]))
        chunks.append(dict(base, choices=[], usage={"prompt_tokens": prompt_tokens, "completion_tokens": 20}))
        return httpx.Response(200, content=sse(chunks), headers={"content-type": "text/event-stream"})
