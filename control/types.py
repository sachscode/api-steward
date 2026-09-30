"""Values that move between the gateway stages."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

CLASS_RANK = {"interactive": 0, "batch": 1}


@dataclass
class Request:
    id: str
    tenant: str
    klass: str  # interactive | batch
    job_id: str
    step: int
    body: dict  # the OpenAI request the engines will see (after guardrails)
    prompt_tokens: int  # estimate; the engine reports the real count
    max_tokens: int
    prefix_key: str  # hash of the shared system prompt + tool schema
    arrival: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    client_stream: bool = False

    @property
    def cost_tokens(self) -> int:
        return self.prompt_tokens + self.max_tokens

    def remaining_s(self, now: float | None = None) -> float:
        return self.deadline - (time.monotonic() if now is None else now)


@dataclass
class Shed:
    """A refusal. stage says where it died; code is the HTTP status the client sees."""

    status: int
    reason: str
    stage: str
    retry_after_s: float = 0.0
    message: str = ""

    @property
    def may_leave(self) -> bool:
        # 429 (tenant limits), 500 and slice_oom stay local; only 503/529 may overflow.
        return self.status in (503, 529) and self.reason != "slice_oom"

    def body(self) -> dict:
        return {
            "error": {
                "type": self.reason,
                "code": self.reason,
                "stage": self.stage,
                "message": self.message or self.reason,
                "retry_after_s": self.retry_after_s,
            }
        }


@dataclass
class Snapshot:
    """What the gateway believes about one engine, from its last /metrics scrape plus the
    gateway's own counters. age_s says how old the engine-side part is."""

    engine: str
    pool: str
    ok: bool = False
    age_s: float = float("inf")
    kv_usage: float | None = None  # vllm:kv_cache_usage_perc, 0..1
    running: int | None = None  # vllm:num_requests_running
    waiting: int | None = None  # vllm:num_requests_waiting (vLLM's queue, not ours)
    preemptions: float | None = None
    prefix_hit_rate: float | None = None
    queue_depth: int = 0  # our queue for this engine
    inflight: int = 0  # dispatched by us, not yet finished
    slot_cap: int = 1
    warm_state: str = "cold"
    ramp_weight: float = 0.0

    @property
    def kv_free(self) -> float | None:
        return None if self.kv_usage is None else max(0.0, 1.0 - self.kv_usage)
