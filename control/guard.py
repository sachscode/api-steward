"""Guardrails: the first "no". Nothing rejected here reaches a GPU or a queue.

inspect(payload) checks the API Steward application contract and returns the exact body the
engines will see. Clients cannot choose engines, sampling, or KV transfer settings.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from app.tools import SYSTEM, TOOLS

from .config import Settings
from .types import Request

PREFIX_KEY = hashlib.sha256(json.dumps([SYSTEM, TOOLS], sort_keys=True).encode()).hexdigest()[:16]
CHARS_PER_TOKEN = 4  # estimate for admission; the engine reports the real count


@dataclass(frozen=True)
class Guard:
    ok: bool
    status: int = 200
    reason: str = "ok"
    message: str = ""
    request: Request | None = field(default=None, compare=False)


def _no(status: int, reason: str, message: str) -> Guard:
    return Guard(False, status, reason, message)


def estimate_tokens(messages: list, tools: list) -> int:
    return max(1, len(json.dumps([messages, tools], ensure_ascii=False)) // CHARS_PER_TOKEN)


def inspect(payload: object, settings: Settings) -> Guard:
    if not isinstance(payload, dict):
        return _no(400, "invalid_request", "body must be a JSON object")
    if payload.get("model") != settings.served_model:
        return _no(400, "model_not_allowed", f"model must be {settings.served_model!r}")

    tenant = payload.get("tenant")
    if not isinstance(tenant, str) or settings.tenant(tenant) is None:
        return _no(403, "tenant_not_allowed", "unknown tenant")
    klass = payload.get("workload_class", "interactive")
    if klass not in ("interactive", "batch"):
        return _no(400, "invalid_workload_class", "workload_class is interactive or batch")

    job_id = payload.get("job_id") or str(uuid.uuid4())
    step = payload.get("step", 1)
    if not isinstance(job_id, str) or not 1 <= len(job_id) <= 64:
        return _no(400, "invalid_job_identity", "job_id must be 1-64 characters")
    if type(step) is not int or not 1 <= step <= settings.max_steps:
        return _no(400, "step_budget_exceeded", f"step must be 1-{settings.max_steps}")

    max_tokens = payload.get("max_tokens", settings.max_output_tokens)
    if type(max_tokens) is not int or not 1 <= max_tokens <= settings.max_output_tokens:
        return _no(400, "output_budget_invalid", f"max_tokens must be 1-{settings.max_output_tokens}")

    messages = payload.get("messages")
    if not isinstance(messages, list) or not 2 <= len(messages) <= 2 + 2 * settings.max_steps:
        return _no(400, "invalid_messages", "messages must hold the system prompt, the task and tool turns")
    if messages[0] != {"role": "system", "content": SYSTEM}:
        return _no(400, "application_contract_mismatch", "system prompt differs from the API Steward contract")
    if payload.get("tools", TOOLS) != TOOLS:
        return _no(400, "application_contract_mismatch", "tool schema differs from the API Steward contract")
    for m in messages[1:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant", "tool"):
            return _no(400, "invalid_message", "roles after the system prompt are user, assistant or tool")
        if m.get("content") is not None and not isinstance(m["content"], str):
            return _no(400, "invalid_message", "message content must be text")

    size = len(json.dumps([messages, TOOLS], ensure_ascii=False))
    if size > settings.max_context_chars:
        return _no(413, "context_too_large", f"context is {size} characters, limit {settings.max_context_chars}")

    stream = bool(payload.get("stream", False))
    timeout_s = min(float(payload.get("timeout_s", settings.deadline_s[klass])), settings.deadline_s[klass])

    # Explicit projection: only these fields reach the engines.
    body = {
        "model": settings.served_model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        # The agent executes one tool per step; without this small models batch all three
        # calls and guess the last one's arguments before reading the evidence.
        "parallel_tool_calls": False,
        "max_tokens": max_tokens,
        "temperature": 0,
        # Qwen3 thinks out loud by default; the agent needs actions, not reasoning traces.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = Request(
        id=str(uuid.uuid4()),
        tenant=tenant,
        klass=klass,
        job_id=job_id,
        step=step,
        body=body,
        prompt_tokens=estimate_tokens(messages, TOOLS),
        max_tokens=max_tokens,
        prefix_key=PREFIX_KEY,
        client_stream=stream,
    )
    req.deadline = req.arrival + timeout_s
    return Guard(True, request=req)
