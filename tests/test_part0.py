import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from threading import Event, Thread
from time import monotonic, sleep
from urllib import error, request

from api_steward.agent import DEFAULT_TASK, GatewayClient, run_agent
from api_steward.serving import MAX_BODY, Gateway, running_server
from api_steward.tools import SYSTEM, TOOLS, execute_tool
from api_steward.transport import STAGES, ServeError, post_json
from api_steward.workers import CompatibleWorker, ScriptedWorker


def payload(tenant="demo-a", job="job-1"):
    return {
        "model": "fixture-model",
        "tenant": tenant,
        "workload_class": "interactive",
        "job_id": job,
        "step": 1,
        "max_tokens": 512,
        "stream": False,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": DEFAULT_TASK}],
        "tools": deepcopy(TOOLS),
    }


class RecordingWorker(ScriptedWorker):
    def __init__(self):
        self.calls = []

    def complete(self, body):
        self.calls.append(deepcopy(body))
        return super().complete(body)


class BlockingWorker(RecordingWorker):
    def __init__(self):
        super().__init__()
        self.started, self.release = Event(), Event()

    def complete(self, body):
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError("test_release_timeout")
        return super().complete(body)


def wait_pending(gateway, count):
    end = monotonic() + 1
    while monotonic() < end:
        with gateway.lock:
            if sum(gateway.pending.values()) == count:
                return
        sleep(0.001)
    raise AssertionError(f"Expected {count} pending requests")


class Part0Tests(unittest.TestCase):
    def test_both_workloads_use_http_path_and_grow_context(self):
        worker = RecordingWorker()
        gateway = Gateway(worker)
        with running_server(gateway) as url:
            runs = [run_agent(GatewayClient(url), workload_class=kind) for kind in ("interactive", "batch")]
        self.assertEqual(len(worker.calls), 8)
        for run in runs:
            self.assertEqual(run["status"], "analysis_complete")
            self.assertFalse(run["behavior_preservation_verified"])
            self.assertEqual([s["message_count"] for s in run["steps"]], [2, 4, 6, 8])
            sizes = [s["context_chars"] for s in run["steps"]]
            self.assertTrue(all(a < b for a, b in pairwise(sizes)))
            for step in run["steps"]:
                self.assertEqual(step["stages"], STAGES)
                self.assertEqual(step["job_id"], run["job_id"])
                self.assertEqual(step["workload_class"], run["workload_class"])
                self.assertEqual(step["backend"], "scripted_fixture")
                self.assertNotIn("worker_reported_usage", step)
        self.assertEqual(len({s["prefix_fingerprint"] for r in runs for s in r["steps"]}), 1)
        self.assertEqual(len({s["request_id"] for r in runs for s in r["steps"]}), 8)
        self.assertEqual(worker.calls[1]["messages"][-1]["role"], "tool")

    def test_guard_rejects_before_worker(self):
        worker = RecordingWorker()
        gateway = Gateway(worker)
        changes = [
            ({"model": "other"}, 400),
            ({"tenant": "unknown"}, 403),
            ({"tenant": []}, 403),
            ({"max_tokens": 513}, 400),
            ({"max_tokens": True}, 400),
            ({"stream": True}, 400),
            ({"tools": []}, 400),
            ({"workload_class": "urgent"}, 400),
        ]
        with running_server(gateway) as url:
            for change, status in changes:
                with self.subTest(change=change), self.assertRaises(ServeError) as caught:
                    post_json(url + "/v1/chat/completions", {**payload(), **change}, 2)
                self.assertEqual(caught.exception.status, status)
            huge = payload()
            huge["messages"][1]["content"] = "x" * 33_000
            with self.assertRaises(ServeError) as caught:
                post_json(url + "/v1/chat/completions", huge, 2)
            self.assertEqual(caught.exception.code, "context_character_limit")
        self.assertEqual(worker.calls, [])

    def test_malformed_and_oversized_http_body(self):
        worker = RecordingWorker()
        with running_server(Gateway(worker)) as url:
            for data, expected in [(b"not json", 400), (b"[1,2]", 400), (b"x" * (MAX_BODY + 1), 413)]:
                req = request.Request(url + "/v1/chat/completions", data=data)
                with self.assertRaises(error.HTTPError) as caught:
                    request.urlopen(req, timeout=2)
                self.assertEqual(caught.exception.code, expected)
                caught.exception.close()
        self.assertFalse(worker.calls)

    def test_tool_allowlist_and_arguments(self):
        self.assertEqual(execute_tool("shell", "{}")["error"], "tool_not_allowed")
        self.assertFalse(execute_tool("inspect_integration", '{"path":"/etc/passwd"}')["ok"])
        self.assertFalse(execute_tool("inspect_integration", "[]")["ok"])
        self.assertFalse(execute_tool("check_migration_plan", "broken json")["ok"])
        args = {
            "operation": "stripe.Invoice.upcoming",
            "items_parameter": "subscription_items",
            "api_version": "2025-02-24.acacia",
        }
        self.assertFalse(execute_tool("check_migration_plan", json.dumps(args))["ok"])

    def test_step_budget_is_terminal(self):
        worker = RecordingWorker()
        with running_server(Gateway(worker)) as url:
            result = run_agent(GatewayClient(url), max_steps=2)
        self.assertEqual(result["reason"], "step_budget_exhausted")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(worker.calls), 2)

    def test_failed_mapping_is_observed_and_can_be_repaired(self):
        class RepairsMapping(ScriptedWorker):
            def complete(self, body):
                observations = [m for m in body["messages"] if m["role"] == "tool"]
                if len(observations) == 3:
                    feedback = json.loads(observations[-1]["content"])
                    if not feedback["ok"]:
                        retry_body = {**body, "messages": body["messages"][:-2]}
                        out = super().complete(retry_body)
                        out["choices"][0]["message"]["tool_calls"][0]["id"] = "repair_mapping"
                        return out
                out = super().complete(body)
                if len(observations) == 2:
                    call = out["choices"][0]["message"]["tool_calls"][0]
                    args = json.loads(call["function"]["arguments"])
                    args["operation"] = "stripe.Invoice.upcoming"
                    call["function"]["arguments"] = json.dumps(args)
                return out

        with running_server(Gateway(RepairsMapping())) as url:
            result = run_agent(GatewayClient(url))
        self.assertEqual(result["status"], "analysis_complete")
        self.assertEqual(result["request_attempts"], 5)
        self.assertEqual(result["completed_model_responses"], 5)
        self.assertEqual([t["ok"] for t in result["tools"]], [True, True, False, True])
        self.assertEqual(result["checked_proposal"]["operation"], "stripe.Invoice.create_preview")
        self.assertTrue(all(s["stages"] == STAGES for s in result["steps"]))

    def test_final_answer_without_tools_is_incomplete(self):
        class EarlyAnswer(ScriptedWorker):
            def complete(self, body):
                return {
                    "choices": [
                        {"message": {"role": "assistant", "content": "Done!"}, "finish_reason": "stop"}
                    ]
                }

        with running_server(Gateway(EarlyAnswer())) as url:
            result = run_agent(GatewayClient(url))
        self.assertEqual(result["reason"], "missing_tool_evidence")
        self.assertFalse(result["behavior_preservation_verified"])

    def test_truncated_tool_call_is_not_executed(self):
        class Truncated(ScriptedWorker):
            def complete(self, body):
                out = super().complete(body)
                out["choices"][0]["finish_reason"] = "length"
                return out

        with running_server(Gateway(Truncated())) as url:
            result = run_agent(GatewayClient(url))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tools"], [])

    def test_tenant_limit_rejects_but_another_tenant_can_queue(self):
        worker = BlockingWorker()
        gateway = Gateway(worker, capacity=2, per_tenant=1)
        with ThreadPoolExecutor(max_workers=2) as callers:
            try:
                first = callers.submit(gateway.complete, payload())
                self.assertTrue(worker.started.wait(1))
                with self.assertRaises(ServeError) as caught:
                    gateway.complete(payload())
                self.assertEqual(caught.exception.status, 429)
                second = callers.submit(gateway.complete, payload("demo-b", "job-2"))
                wait_pending(gateway, 2)
                self.assertFalse(second.done())
                worker.release.set()
                self.assertEqual(first.result()["steward"]["tenant"], "demo-a")
                self.assertGreater(second.result()["steward"]["queue_wait_s"], 0)
            finally:
                worker.release.set()
                gateway.close()
        wait_pending(gateway, 0)
        self.assertEqual(len(worker.calls), 2)

    def test_global_capacity_rejection(self):
        worker = BlockingWorker()
        gateway = Gateway(worker, capacity=1, per_tenant=2)
        with ThreadPoolExecutor(max_workers=1) as callers:
            try:
                first = callers.submit(gateway.complete, payload())
                self.assertTrue(worker.started.wait(1))
                with self.assertRaises(ServeError) as caught:
                    gateway.complete(payload("demo-b"))
                self.assertEqual(caught.exception.status, 503)
                worker.release.set()
                first.result()
            finally:
                worker.release.set()
                gateway.close()
        self.assertEqual(len(worker.calls), 1)

    def test_timeout_cancels_queued_work_but_keeps_active_reservation(self):
        worker = BlockingWorker()
        gateway = Gateway(worker, capacity=2, per_tenant=2, request_timeout=0.05)
        with ThreadPoolExecutor(max_workers=1) as callers:
            try:
                first = callers.submit(gateway.complete, payload())
                self.assertTrue(worker.started.wait(1))
                with self.assertRaises(ServeError) as caught:
                    first.result()
                self.assertEqual(caught.exception.status, 504)
                wait_pending(gateway, 1)
                with self.assertRaises(ServeError):
                    gateway.complete(payload("demo-b"))
                wait_pending(gateway, 1)
                worker.release.set()
            finally:
                worker.release.set()
                gateway.close()
        self.assertEqual(len(worker.calls), 1)
        wait_pending(gateway, 0)

    def test_worker_failure_releases_slot_without_fallback(self):
        class FailsOnce(RecordingWorker):
            def complete(self, body):
                if not self.calls:
                    self.calls.append(body)
                    raise RuntimeError("private provider diagnostic")
                return super().complete(body)

        worker = FailsOnce()
        with running_server(Gateway(worker, per_tenant=1)) as url:
            first = run_agent(GatewayClient(url))
            self.assertEqual(first["reason"], "worker_failed")
            self.assertEqual(first["request_attempts"], 1)
            self.assertEqual(first["completed_model_responses"], 0)
            self.assertNotIn("private provider diagnostic", json.dumps(first))
            second = run_agent(GatewayClient(url))
            self.assertEqual(second["status"], "analysis_complete")

    def test_compatible_adapter_preserves_tools_and_client_rejects_direct_worker(self):
        worker = RecordingWorker()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                output = json.dumps(worker.complete(body)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                self.wfile.write(output)

        backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        backend_url = f"http://127.0.0.1:{backend.server_port}"
        try:
            with running_server(Gateway(CompatibleWorker(backend_url))) as url:
                result = run_agent(GatewayClient(url))
                self.assertEqual(result["status"], "analysis_complete")
                self.assertEqual(result["steps"][0]["backend"], "openai_compatible")
                self.assertTrue(all(call["tools"] == TOOLS for call in worker.calls))
                self.assertTrue(all("tenant" not in call for call in worker.calls))
                self.assertTrue(all(call["tool_choice"] == "auto" for call in worker.calls))
            bypass = run_agent(GatewayClient(backend_url))
            self.assertEqual(bypass["reason"], "missing_serve_path_evidence")
        finally:
            backend.shutdown()
            backend.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
