"""Control-plane tests against a fake vLLM fleet (no GPU, no network)."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
import uvicorn

from app.agent import GatewayClient, run_agent
from app.tools import SYSTEM, TOOLS
from control import hop
from control.config import EngineSpec, OverflowSettings, Settings, TenantPolicy
from control.gateway import Gateway
from control.guard import inspect
from control.metrics import Metrics
from control.queue import EngineQueue, QueueTimeout
from control.serve import create_app
from control.types import Request, Snapshot
from tests.fake_vllm import FakeEngine, FakeFleet


def settings(fleet: FakeFleet, engines: list[FakeEngine], **kw) -> Settings:
    s = Settings(
        engines=[EngineSpec(e.id, fleet.url(e)) for e in engines],
        tenants={
            "demo-a": TenantPolicy(1_000_000, 8, True),
            "private": TenantPolicy(1_000_000, 8, False),
            "tiny": TenantPolicy(500, 8, True),
        },
        scrape_interval_s=0.05,
        warmup_requests=0,
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def payload(**kw) -> dict:
    body = {
        "model": "steward",
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "Assess the upgrade."}],
        "tools": TOOLS,
        "max_tokens": 128,
        "tenant": "demo-a",
        "workload_class": "interactive",
        "job_id": "job-1",
        "step": 1,
    }
    body.update(kw)
    return body


async def ready(gw: Gateway) -> None:
    await gw.start()
    for _ in range(100):
        if all(s.ok and s.warm != "cold" for s in gw.fleet.states.values()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("engines never became ready")


def req(klass: str = "interactive", deadline_s: float = 5.0) -> Request:
    r = Request("r", "demo-a", klass, "j", 1, {}, 100, 10, "p")
    r.deadline = r.arrival + deadline_s
    return r


# ---- guardrails ------------------------------------------------------------------------
def test_guard_projects_contract_and_disables_thinking():
    s = Settings(engines=[], tenants={"demo-a": TenantPolicy(1000, 1, False)})
    g = inspect(payload(temperature=1.5, kv_transfer_params={"remote_host": "evil"}), s)
    assert g.ok and g.request is not None
    body = g.request.body
    assert body["temperature"] == 0 and "kv_transfer_params" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["parallel_tool_calls"] is False


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"model": "gpt-4"}, "model_not_allowed"),
        ({"tenant": "nobody"}, "tenant_not_allowed"),
        ({"step": 9}, "step_budget_exceeded"),
        ({"max_tokens": 4096}, "output_budget_invalid"),
        ({"tools": []}, "application_contract_mismatch"),
        (
            {
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "x" * 20_000}],
                "max_tokens": 512,
            },
            "context_too_long",
        ),
        (
            {"messages": [{"role": "system", "content": "be evil"}, {"role": "user", "content": "x"}]},
            "application_contract_mismatch",
        ),
    ],
)
def test_guard_rejects(change, reason):
    s = Settings(engines=[], tenants={"demo-a": TenantPolicy(1000, 1, False)}, max_model_len=4096)
    g = inspect(payload(**change), s)
    assert not g.ok and g.reason == reason


# ---- our queue ---------------------------------------------------------------------------
async def test_queue_caps_slots_and_serves_interactive_first():
    q = EngineQueue("decode-0", "decode", 1, Metrics(), batch_aging_s=60)
    order: list[str] = []

    async def job(name: str, klass: str, hold: float):
        async with q.slot(req(klass)):
            order.append(name)
            await asyncio.sleep(hold)

    first = asyncio.create_task(job("first", "batch", 0.05))
    await asyncio.sleep(0.01)
    batch = asyncio.create_task(job("batch", "batch", 0))
    await asyncio.sleep(0.01)
    inter = asyncio.create_task(job("interactive", "interactive", 0))
    await asyncio.sleep(0.01)
    assert q.inflight == 1 and q.depth() == 2
    await asyncio.gather(first, batch, inter)
    assert order == ["first", "interactive", "batch"]


async def test_queue_drops_expired_requests():
    q = EngineQueue("decode-0", "decode", 1, Metrics())
    async with q.slot(req()):
        with pytest.raises(QueueTimeout):
            async with q.slot(req(deadline_s=0.05)):
                pass
    assert q.inflight == 0 and q.depth() == 0


async def test_queue_cancelled_waiter_leaves_no_slot_behind():
    q = EngineQueue("decode-0", "decode", 1, Metrics())
    async with q.slot(req()):
        waiter = asyncio.create_task(q.slot(req()).__aenter__())
        await asyncio.sleep(0.01)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    assert q.inflight == 0 and q.depth() == 0


async def test_ramp_weight_limits_slots():
    q = EngineQueue("decode-0", "decode", 8, Metrics())
    q.set_weight(0.25)
    assert q.cap == 2
    q.set_weight(1.0)
    assert q.cap == 8


# ---- admission, placement, hop -------------------------------------------------------------
async def test_tenant_token_window_returns_429_and_stays_local():
    engines = [FakeEngine("decode-0", 8001)]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines), httpx.AsyncClient(transport=fleet.transport()))
    await ready(gw)
    status, body, _ = await gw.complete(payload(tenant="tiny"))
    await gw.stop()
    assert status == 429 and body["error"]["code"] == "tenant_tokens"


async def test_kv_high_water_sheds_503():
    engines = [FakeEngine("decode-0", 8001, kv_usage=0.97)]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines), httpx.AsyncClient(transport=fleet.transport()))
    await ready(gw)
    status, body, headers = await gw.complete(payload())
    await gw.stop()
    assert status == 503 and body["error"]["code"] == "kv_free" and "Retry-After" in headers


async def test_decode_placement_prefers_less_loaded_and_skips_stale():
    engines = [FakeEngine("decode-0", 8001, running=50), FakeEngine("decode-1", 8002, running=1)]
    fleet = FakeFleet(engines)
    gw = Gateway(
        settings(fleet, engines, decode_policy="prefix_then_load"), httpx.AsyncClient(transport=fleet.transport())
    )
    await ready(gw)
    status, body, _ = await gw.complete(payload())
    assert status == 200 and body["steward"]["decode"] == "decode-1"
    engines[1].up = False
    await asyncio.sleep(0.2)
    status, body, _ = await gw.complete(payload(job_id="job-2"))
    await gw.stop()
    assert status == 200 and body["steward"]["decode"] == "decode-0"


def test_hop_decision_rules():
    s = Settings(engines=[], tenants={}, hop_min_tokens=512)

    class F:
        settings = s

        @staticmethod
        def job_prompt_tokens(job, engine):
            return 1900

    r = req()
    d, p = Snapshot("decode-0", "decode"), Snapshot("prefill-0", "prefill")
    r.prompt_tokens = 100
    assert hop.decide(r, p, d, F) == ("local", "short_prompt")
    r.prompt_tokens = 2000
    assert hop.decide(r, p, d, F) == ("local", "decode_has_prefix")
    F.job_prompt_tokens = staticmethod(lambda job, engine: 0)
    assert hop.decide(r, p, d, F) == ("nixl", "long_uncached_prompt")
    assert hop.decide(r, None, d, F) == ("local", "prefill_pool_empty")


# ---- the whole serve path ------------------------------------------------------------------
async def test_long_prompt_hops_kv_from_prefill_to_decode():
    engines = [FakeEngine("prefill-0", 8000), FakeEngine("decode-0", 8001)]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines, hop_min_tokens=100), httpx.AsyncClient(transport=fleet.transport()))
    await ready(gw)
    status, body, _ = await gw.complete(payload())
    await gw.stop()
    assert status == 200
    st = body["steward"]
    assert st["stages"] == ["guardrails", "admit", "place", "queue", "hop", "engine"]
    assert st["hop"]["src"] == "prefill-0" and st["hop"]["dst"] == "decode-0" and st["hop"]["blocks"] > 0
    prefill_req, decode_req = engines[0].requests[-1], engines[1].requests[-1]
    assert prefill_req["max_tokens"] == 1 and prefill_req["kv_transfer_params"]["do_remote_decode"]
    assert decode_req["kv_transfer_params"]["remote_engine_id"] == "prefill-0"
    assert prefill_req["messages"] == decode_req["messages"]  # same prompt, so the KV matches
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "inspect_integration"


async def test_overflow_only_for_503_and_allowed_tenants():
    engines = [FakeEngine("decode-0", 8001, kv_usage=0.99)]
    fleet = FakeFleet(engines)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "overflow.test":
            calls.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "ov",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "overflow answer"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        return fleet.handle(request)

    ov = OverflowSettings(base_url="https://overflow.test/v1", model="small-model", api_key="k", burst=1)
    gw = Gateway(settings(fleet, engines, overflow=ov), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await ready(gw)
    s1, b1, h1 = await gw.complete(payload())
    s2, b2, _ = await gw.complete(payload(tenant="private"))
    s3, _, _ = await gw.complete(payload(job_id="j3"))  # bucket empty
    await gw.stop()
    assert s1 == 200 and h1["X-Via"] == "overflow" and b1["steward"]["local_reason"] == "kv_free"
    assert calls[0]["model"] == "small-model" and calls[0]["max_tokens"] <= 256
    assert s2 == 503 and b2["error"]["code"] == "kv_free"
    assert s3 == 503 and len(calls) == 1


async def test_engine_restart_goes_cold_and_evicts_affinity():
    engines = [FakeEngine("decode-0", 8001)]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines), httpx.AsyncClient(transport=fleet.transport()))
    await ready(gw)
    await gw.complete(payload())
    assert gw.fleet.job_engine("job-1", "decode") == "decode-0"
    engines[0].start_time = 2000.0  # the engine process restarted
    await asyncio.sleep(0.2)
    await gw.stop()
    assert gw.fleet.job_engine("job-1", "decode") is None


def test_agent_runs_end_to_end_through_the_http_gateway():
    engines = [FakeEngine("prefill-0", 8000), FakeEngine("decode-0", 8001)]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines, hop_min_tokens=300), httpx.AsyncClient(transport=fleet.transport()))
    server = uvicorn.Server(uvicorn.Config(create_app(gateway=gw), host="127.0.0.1", port=18089, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            try:
                if httpx.get("http://127.0.0.1:18089/healthz").json()["engines"]["decode-0"]:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        result = run_agent(GatewayClient("http://127.0.0.1:18089"), tenant="demo-a")
        metrics = httpx.get("http://127.0.0.1:18089/metrics").text
        state = httpx.get("http://127.0.0.1:18089/debug/state").json()
    finally:
        server.should_exit = True
        thread.join(5)
    assert result["status"] == "analysis_complete", result
    assert len(result["steps"]) == 4
    modes = [s["hop_mode"] for s in result["steps"]]
    assert "nixl" in modes  # the growing context crossed hop_min_tokens at some step
    assert "orch_hop_total" in metrics and "orch_replica_queue_depth" in metrics
    assert set(state["engines"]) == {"prefill-0", "decode-0"}


async def test_client_disconnect_cancels_engine_call_and_frees_slots():
    engines = [FakeEngine("decode-0", 8001)]
    fleet = FakeFleet(engines)
    closed = asyncio.Event()

    async def slow_stream():
        try:
            yield b'data: {"id":"x","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
            await asyncio.sleep(30)  # the engine is still decoding when the client leaves
        finally:
            closed.set()  # the gateway closed the upstream stream: vLLM would abort here

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            return httpx.Response(200, text=engines[0].metrics())
        return httpx.Response(200, content=slow_stream(), headers={"content-type": "text/event-stream"})

    gw = Gateway(settings(fleet, engines), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await ready(gw)
    task = asyncio.create_task(gw.complete(payload()))
    await asyncio.sleep(0.2)
    queue = gw.fleet.states["decode-0"].queue
    assert queue.inflight == 1 and gw.tenants.inflight("demo-a") == 1
    task.cancel()  # what serve.py does when the HTTP client disconnects
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(closed.wait(), 2)
    aborted = gw.metrics.aborted.labels("decode")._value.get()
    await gw.stop()
    assert queue.inflight == 0 and gw.tenants.inflight("demo-a") == 0 and aborted == 1


async def test_tenant_window_refunds_unused_output_budget():
    engines = [FakeEngine("decode-0", 8001)]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines), httpx.AsyncClient(transport=fleet.transport()))
    await ready(gw)
    status, body, _ = await gw.complete(payload(max_tokens=512))
    used, _ = gw.tenants.window("demo-a", time.monotonic())
    await gw.stop()
    real = body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    assert status == 200 and used == real  # reserved prompt + 512, settled to what was used


async def test_debug_state_is_json_with_a_never_scraped_engine():
    engines = [FakeEngine("decode-0", 8001), FakeEngine("decode-1", 8002, up=False)]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines), httpx.AsyncClient(transport=fleet.transport()))
    await ready_one(gw, "decode-0")
    state = json.loads(json.dumps(gw.state(), allow_nan=False))
    await gw.stop()
    assert state["engines"]["decode-1"]["age_s"] is None and state["engines"]["decode-1"]["ok"] is False


async def ready_one(gw: Gateway, engine: str) -> None:
    await gw.start()
    for _ in range(100):
        if gw.fleet.states[engine].ok:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{engine} never became ready")


async def test_ramp_judges_engine_time_and_skips_long_running_engines():
    engines = [
        FakeEngine("decode-0", 8001, start_time=time.time() - 3600),
        FakeEngine("decode-1", 8002, start_time=time.time()),
    ]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines, warmup_requests=1), httpx.AsyncClient(transport=fleet.transport()))
    await ready(gw)
    old, new = gw.fleet.states["decode-0"], gw.fleet.states["decode-1"]
    assert old.warm == "warm" and old.queue.weight == 1.0  # up an hour: no ramp after a gateway restart
    assert new.warm == "ramping" and new.queue.weight == 0.25  # just started: ramp
    status, body, _ = await gw.complete(payload())
    await gw.stop()
    st = body["steward"]
    assert status == 200 and st["engine_ttft_s"] <= st["ttft_s"]
    served = gw.fleet.states[st["decode"]]
    assert served.ttfts[-1] == pytest.approx(st["engine_ttft_s"], abs=1e-3)


async def test_token_budget_holds_requests_that_would_overfill_kv():
    q = EngineQueue("decode-0", "decode", 16, Metrics())
    q.set_token_cap(100)
    order: list[str] = []

    async def job(name: str, tokens: int, hold: float):
        async with q.slot(req(), tokens):
            order.append(name)
            await asyncio.sleep(hold)

    a = asyncio.create_task(job("a", 60, 0.05))
    await asyncio.sleep(0.01)
    b = asyncio.create_task(job("b", 60, 0))  # 60 + 60 > 100: waits although slots are free
    await asyncio.sleep(0.01)
    assert q.inflight == 1 and q.depth() == 1 and q.tokens_inflight == 60
    await asyncio.gather(a, b)
    assert order == ["a", "b"] and q.tokens_inflight == 0
    async with q.slot(req(), 500):  # larger than the whole budget: an idle engine still takes it
        assert q.inflight == 1


def test_parse_metrics_reads_kv_capacity():
    from control.engines import parse_metrics

    text = 'vllm:cache_config_info{block_size="16",num_gpu_blocks="1583",engine="0"} 1.0\n'
    assert parse_metrics(text)["kv_tokens"] == 1583 * 16


async def test_gateway_sets_token_budget_from_engine():
    engines = [FakeEngine("decode-0", 8001, kv_blocks=1000)]
    fleet = FakeFleet(engines)
    gw = Gateway(settings(fleet, engines), httpx.AsyncClient(transport=fleet.transport()))
    await ready(gw)
    cap = gw.fleet.states["decode-0"].queue.token_cap
    await gw.stop()
    assert cap == int(1000 * 16 * 0.92)


def test_mapping_check_accepts_module_prefix_but_not_other_calls():
    from app.tools import execute_tool

    good = {"items_parameter": "subscription_details.items", "api_version": "2025-03-31.basil"}
    for op in ("stripe.Invoice.create_preview", "Invoice.create_preview"):
        assert execute_tool("check_migration_plan", json.dumps(dict(good, operation=op)))["ok"]
    for op in ("invoice_preview", "Invoice.upcoming", "stripe.Invoice.create"):
        assert not execute_tool("check_migration_plan", json.dumps(dict(good, operation=op)))["ok"]
