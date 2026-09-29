"""Local reference path: guardrails -> admit -> place -> queue -> worker.

Single process and one worker slot, intentionally not a production gateway.
"""

import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from time import perf_counter
from uuid import uuid4

from .tools import SYSTEM, TOOLS
from .transport import ServeError

MAX_BODY = 65_536
MAX_CONTEXT_CHARS = 32_000
PREFIX = hashlib.sha256(json.dumps([SYSTEM, TOOLS], sort_keys=True).encode()).hexdigest()


def validate(payload: dict, model: str, tenants: set[str]) -> dict:
    if not isinstance(payload, dict):
        raise ServeError(400, "invalid_request")
    if payload.get("model") != model:
        raise ServeError(400, "model_not_allowed")
    if not isinstance(payload.get("tenant"), str) or payload["tenant"] not in tenants:
        raise ServeError(403, "tenant_not_allowed")
    if payload.get("workload_class") not in ("interactive", "batch"):
        raise ServeError(400, "invalid_workload_class")
    if (
        not isinstance(payload.get("job_id"), str)
        or not 1 <= len(payload["job_id"]) <= 64
        or type(payload.get("step")) is not int
        or not 1 <= payload["step"] <= 8
    ):
        raise ServeError(400, "invalid_job_identity")
    tokens = payload.get("max_tokens")
    if type(tokens) is not int or not 1 <= tokens <= 512:
        raise ServeError(400, "output_budget_invalid")
    if payload.get("stream") is not False:
        raise ServeError(400, "streaming_not_supported_in_part0")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 2 <= len(messages) <= 64:
        raise ServeError(400, "invalid_messages")
    if messages[0] != {"role": "system", "content": SYSTEM} or payload.get("tools") != TOOLS:
        raise ServeError(400, "application_contract_mismatch")
    for m in messages[1:]:
        if (
            not isinstance(m, dict)
            or m.get("role") not in ("user", "assistant", "tool")
            or (m.get("content") is not None and not isinstance(m["content"], str))
        ):
            raise ServeError(400, "invalid_message")
    if len(json.dumps([messages, TOOLS], ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise ServeError(400, "context_character_limit")
    # Explicit projection: clients cannot override worker location or server policy.
    return {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": tokens,
        "temperature": 0,
        "stream": False,
    }


class Gateway:
    def __init__(
        self,
        worker,
        *,
        model="fixture-model",
        capacity=4,
        per_tenant=2,
        tenants=("demo-a", "demo-b"),
        request_timeout=40,
    ):
        if capacity < 1 or per_tenant < 1 or request_timeout <= 0:
            raise ValueError("Capacity and timeout must be positive")
        self.worker, self.model = worker, model
        self.capacity, self.per_tenant = capacity, per_tenant
        self.tenants, self.request_timeout = set(tenants), request_timeout
        self.pending = Counter()
        self.lock = Lock()
        # Admission bounds outstanding futures: at most capacity-1 waiters + 1 running.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="steward-worker")
        self.counters = Counter()

    def complete(self, payload: dict) -> dict:
        started = perf_counter()
        safe = validate(payload, self.model, self.tenants)
        tenant = payload["tenant"]
        trace = {
            "request_id": str(uuid4()),
            "tenant": tenant,
            "job_id": payload["job_id"],
            "step": payload["step"],
            "workload_class": payload["workload_class"],
            "backend": self.worker.kind,
            "stages": ["guardrails"],
            "prefix_fingerprint": PREFIX,
            "message_count": len(safe["messages"]),
            "context_chars": len(json.dumps([safe["messages"], TOOLS], ensure_ascii=False)),
            "max_output_tokens": safe["max_tokens"],
        }
        with self.lock:
            if self.pending[tenant] >= self.per_tenant:
                self.counters["tenant_rejected"] += 1
                raise ServeError(429, "tenant_concurrency_limit")
            if sum(self.pending.values()) >= self.capacity:
                self.counters["capacity_rejected"] += 1
                raise ServeError(503, "gateway_capacity_limit")
            self.pending[tenant] += 1
        trace["stages"].extend(["admit", "place", "queue"])
        trace["worker_id"] = "local-0"
        enqueued = perf_counter()

        def release(_future=None):
            with self.lock:
                self.pending[tenant] -= 1

        def run():
            trace["queue_wait_s"] = perf_counter() - enqueued
            trace["stages"].append("worker")
            try:
                body = self.worker.complete(safe)
            except ServeError:
                raise
            except Exception:  # noqa: BLE001 - redact provider diagnostics at the worker boundary.
                raise ServeError(502, "worker_failed") from None
            if not isinstance(body, dict) or not isinstance(body.get("choices"), list) or not body["choices"]:
                raise ServeError(502, "invalid_worker_completion")
            trace["gateway_e2e_s"] = perf_counter() - started
            with self.lock:
                self.counters["completed"] += 1
            return {**body, "steward": trace}

        try:
            future = self.executor.submit(run)
        except RuntimeError:
            release()
            raise ServeError(503, "gateway_stopped") from None
        future.add_done_callback(release)
        try:
            return future.result(timeout=self.request_timeout)
        except FutureTimeout:
            # Cancel queued work; active work retains its slot until backend completion.
            future.cancel()
            raise ServeError(504, "gateway_deadline_exceeded") from None

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


def make_server(gateway: Gateway, port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args):
            pass  # Do not put prompts, tenant inputs, or provider errors in access logs.

        def send_json(self, status, body):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if status in (429, 503):
                self.send_header("Retry-After", "1")
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except OSError:
                pass

        def do_GET(self):
            if self.path == "/healthz":
                self.send_json(
                    200,
                    {
                        "service": "api-steward-gateway",
                        "backend": gateway.worker.kind,
                        "model": gateway.model,
                        "scope": "local_part0",
                    },
                )
            else:
                self.send_json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_json(404, {"error": "not_found"})
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ServeError(400, "unsupported_transfer_encoding")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ServeError(400, "invalid_content_length") from None
                if not 0 < length <= MAX_BODY:
                    raise ServeError(413, "body_size_limit")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ServeError(400, "incomplete_body")
                payload = json.loads(raw)
                self.send_json(200, gateway.complete(payload))
            except ServeError as exc:
                self.send_json(exc.status, {"error": exc.code})
            except (ValueError, UnicodeDecodeError, TypeError):
                self.send_json(400, {"error": "invalid_json_request"})

    # Deliberately loopback-only: tenant labels are not authenticated identities yet.
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


@contextmanager
def running_server(gateway: Gateway, port: int = 0):
    server = make_server(gateway, port)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        gateway.close()
        thread.join()
