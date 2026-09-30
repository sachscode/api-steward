"""Bounded Track B loop; every completion goes through GatewayClient."""

import json
from time import perf_counter
from uuid import uuid4

from .tools import SYSTEM, TOOLS, execute_tool
from .transport import ServeError, endpoint, post_json, took_serve_path

DEFAULT_TASK = "Assess the Stripe invoice-preview upgrade to SDK v12 and API 2025-03-31.basil."


class GatewayClient:
    def __init__(self, base_url: str, model: str = "steward"):
        self.url, self.model = endpoint(base_url), model

    def complete(self, messages, *, tenant, workload_class, job_id, step, timeout):
        body = post_json(
            self.url,
            {
                "model": self.model,
                "messages": messages,
                "tools": TOOLS,
                "max_tokens": 512,
                "stream": False,
                "tenant": tenant,
                "workload_class": workload_class,
                "job_id": job_id,
                "step": step,
            },
            timeout,
        )
        trace = body.get("steward")
        if (
            not isinstance(trace, dict)
            or not took_serve_path(trace.get("stages"))
            or trace.get("job_id") != job_id
            or trace.get("step") != step
            or trace.get("tenant") != tenant
            or trace.get("workload_class") != workload_class
        ):
            raise ServeError(502, "missing_serve_path_evidence")
        return body


def run_agent(
    client: GatewayClient,
    *,
    question=DEFAULT_TASK,
    tenant="demo-a",
    workload_class="interactive",
    max_steps=8,
    deadline_s=120,
) -> dict:
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError("Question must contain 1-4000 characters")
    if not 1 <= max_steps <= 8 or not 0 < deadline_s <= 120:
        raise ValueError("Use 1-8 steps and a deadline of at most 120 seconds")
    started = perf_counter()
    job_id = str(uuid4())
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    result = {
        "job_id": job_id,
        "workload_class": workload_class,
        "tenant": tenant,
        "status": "incomplete",
        "reason": "step_budget_exhausted",
        "steps": [],
        "tools": [],
        "answer": None,
        "request_attempts": 0,
        "behavior_preservation_verified": False,
    }
    seen_ids = set()
    evidence = set()
    try:
        for step in range(1, max_steps + 1):
            remaining = deadline_s - (perf_counter() - started)
            if remaining <= 0:
                result["reason"] = "job_deadline_exceeded"
                break
            result["request_attempts"] += 1
            completion = client.complete(
                messages,
                tenant=tenant,
                workload_class=workload_class,
                job_id=job_id,
                step=step,
                timeout=min(45, remaining),
            )
            result["steps"].append(completion["steward"])
            if "usage" in completion:
                result["steps"][-1]["worker_reported_usage"] = completion["usage"]
            choices = completion.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ServeError(502, "invalid_completion")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ServeError(502, "invalid_assistant_message")
            calls = message.get("tool_calls")
            if calls:
                if choice.get("finish_reason") != "tool_calls" or not isinstance(calls, list) or len(calls) != 1:
                    raise ServeError(502, "expected_one_complete_tool_call")
                call = calls[0]
                if (
                    not isinstance(call, dict)
                    or call.get("type") != "function"
                    or not isinstance(call.get("id"), str)
                    or not 1 <= len(call["id"]) <= 128
                    or call["id"] in seen_ids
                    or not isinstance(call.get("function"), dict)
                ):
                    raise ServeError(502, "invalid_tool_call")
                function = call["function"]
                name, arguments = function.get("name"), function.get("arguments")
                content = message.get("content")
                if (
                    not isinstance(name, str)
                    or not isinstance(arguments, str)
                    or len(arguments) > 2048
                    or (content is not None and (not isinstance(content, str) or len(content) > 4096))
                ):
                    raise ServeError(502, "invalid_tool_arguments")
                seen_ids.add(call["id"])
                tool_started = perf_counter()
                observation = execute_tool(name, arguments)
                result["tools"].append(
                    {
                        "step": step,
                        "name": name,
                        "ok": observation["ok"],
                        "duration_s": perf_counter() - tool_started,
                        "evidence_kind": observation.get("evidence_kind"),
                        "error": observation.get("error"),
                    }
                )
                if observation["ok"]:
                    evidence.add(name)
                    if name == "check_migration_plan":
                        result["checked_proposal"] = observation["proposal"]
                else:
                    evidence.discard(name)
                    if name == "check_migration_plan":
                        result.pop("checked_proposal", None)
                # Retain returned message fields, including model-specific reasoning fields.
                messages.extend(
                    [
                        message,
                        {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(observation)},
                    ]
                )
                continue
            if (
                choice.get("finish_reason") != "stop"
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise ServeError(502, "incomplete_final_answer")
            result["answer"] = message["content"]
            required = {tool["function"]["name"] for tool in TOOLS}
            if not required.issubset(evidence):
                result["reason"] = "missing_tool_evidence"
                break
            result.update(status="analysis_complete", reason="fixture_mapping_checked")
            break
    except ServeError as exc:
        result.update(status="failed", reason=exc.code, http_status=exc.status)
    result["duration_s"] = perf_counter() - started
    result["completed_model_responses"] = len(result["steps"])
    return result
