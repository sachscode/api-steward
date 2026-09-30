"""The serve path, in order:

  guardrails.inspect -> admit (tenant window, fleet) -> place (decode, then prefill)
  -> hop decision -> our queue -> engine /v1/chat/completions -> overflow gate

Decode capacity is reserved before prefill runs: the decode slot is acquired first, so KV
built on the prefill engine never sits waiting for a decode slot (where it would be a ghost
holding prefill blocks until NIXL's abort timeout frees them).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.tools import SYSTEM, TOOLS

from . import admit, hop, place
from .config import Settings
from .engines import Assembler, Engine, EngineError
from .fleet import Fleet
from .guard import PREFIX_KEY, inspect
from .metrics import Metrics
from .overflow import Overflow
from .queue import QueueTimeout
from .types import Request, Shed, Snapshot

log = logging.getLogger("control.gateway")


@dataclass
class Plan:
    req: Request
    decode: Snapshot
    prefill: Snapshot | None
    hop_mode: str
    hop_reason: str
    stage: str = "queue"
    trace: dict = field(default_factory=dict)

    def stages(self, via: str) -> list[str]:
        base = ["guardrails", "admit", "place", "queue"]
        if via == "overflow":
            return base + ["overflow"]
        return base + (["hop"] if self.hop_mode == "nixl" else []) + ["engine"]


class Gateway:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.metrics = Metrics()
        self.client = client or httpx.AsyncClient(limits=httpx.Limits(max_connections=1000))
        self.engines = {spec.id: Engine(spec, self.client) for spec in settings.engines}
        warmup_body = {
            "model": settings.served_model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Warmup: say ready."},
            ],
            "tools": TOOLS,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        self.fleet = Fleet(settings, list(self.engines.values()), self.metrics, warmup_body)
        self.fleet.warmup_prefix_key = PREFIX_KEY
        self.tenants = admit.TenantBook(self.metrics)
        self.overflow = Overflow(settings.overflow, self.metrics, self.client)
        self._trace = Path(settings.trace_path) if settings.trace_path else None
        if self._trace:
            self._trace.parent.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        self.fleet.start()

    async def stop(self) -> None:
        await self.fleet.stop()
        await self.client.aclose()

    # ---- decide ------------------------------------------------------------------------
    def plan(self, payload: object) -> Plan | Shed | tuple[Shed, Request]:
        """Guard, admit and place. Returns a Plan, a Shed with no request (guardrails), or a
        (Shed, Request) pair when a real request was refused later and may overflow."""
        g = inspect(payload, self.settings)
        if not g.ok:
            shed = Shed(g.status, g.reason, "guardrails", 0.0, g.message)
            self.metrics.shed.labels("guardrails", g.reason, str(g.status)).inc()
            return shed
        req = g.request
        assert req is not None
        self.metrics.requests.labels(req.klass, req.tenant).inc()

        decode_snaps = self.fleet.snapshots("decode")
        view = admit.FleetView(
            decode=decode_snaps,
            predicted_wait_s={s.engine: self.fleet.states[s.engine].queue.predicted_wait_s(req) for s in decode_snaps},
            stale_s=self.settings.stale_s,
        )
        refused = admit.check(req, view, self.settings, self.tenants)
        if refused:
            return self._shed(refused, req)

        decode = place.pick(req, decode_snaps, policy=self.settings.decode_policy, fleet=self.fleet)
        if isinstance(decode, Shed):
            return self._shed(decode, req)
        prefill_snaps = self.fleet.snapshots("prefill")
        prefill = None
        if prefill_snaps and req.prompt_tokens >= self.settings.hop_min_tokens:
            chosen = place.pick(req, prefill_snaps, policy=self.settings.prefill_policy, fleet=self.fleet)
            prefill = None if isinstance(chosen, Shed) else chosen
        mode, reason = hop.decide(req, prefill, decode, self.fleet)
        self.metrics.hop_decision.labels(mode, reason).inc()
        self.tenants.charge(req)
        return Plan(req, decode, prefill if mode == "nixl" else None, mode, reason)

    def _shed(self, shed: Shed, req: Request) -> tuple[Shed, Request]:
        self.metrics.shed.labels(shed.stage, shed.reason, str(shed.status)).inc()
        return shed, req

    # ---- run ---------------------------------------------------------------------------
    async def run(self, plan: Plan) -> AsyncIterator[dict]:
        """Execute a plan and yield the decode engine's stream chunks. Raises Shed-carrying
        exceptions (GatewayRefusal) for queue timeouts and engine refusals."""
        req = plan.req
        decode_state = self.fleet.states[plan.decode.engine]
        tr = plan.trace
        tr.update(prefill=None, decode=plan.decode.engine, hop=None, hop_mode=plan.hop_mode, hop_reason=plan.hop_reason)
        try:
            plan.stage = "queue"
            async with decode_state.queue.slot(req) as waited_d:
                tr["queue_wait_decode_s"] = round(waited_d, 4)
                kv_params = None
                if plan.hop_mode == "nixl" and plan.prefill is not None:
                    prefill_state = self.fleet.states[plan.prefill.engine]
                    async with prefill_state.queue.slot(req) as waited_p:
                        tr["queue_wait_prefill_s"] = round(waited_p, 4)
                        plan.stage = "prefill"
                        kv_params, usage, secs = await prefill_state.engine.prefill(req.body)
                    self.metrics.prefill.labels(plan.prefill.engine).observe(secs)
                    tr["prefill"], tr["prefill_s"] = plan.prefill.engine, round(secs, 4)
                    if not kv_params:
                        raise EngineError(502, "no_kv_transfer_params", "prefill engine returned no KV handle")
                    rec = hop.record(
                        req,
                        plan.prefill.engine,
                        plan.decode.engine,
                        kv_params,
                        int(usage.get("prompt_tokens") or req.prompt_tokens),
                        self.settings,
                    )
                    tr["hop"] = rec
                    self.metrics.hops.labels(rec["src"], rec["dst"], "nixl").inc()
                    self.metrics.hop_tokens.labels(rec["src"], rec["dst"]).inc(rec["tokens"])
                    self.metrics.hop_bytes.labels(rec["src"], rec["dst"]).inc(rec["bytes_estimated"])
                    self.fleet.remember(
                        req.job_id, "prefill", plan.prefill.engine, req.prefix_key, int(usage.get("prompt_tokens") or 0)
                    )
                plan.stage = "decode"
                first = True
                async for chunk in decode_state.engine.stream(req.body, kv_params):
                    if first and _has_token(chunk):
                        first = False
                        ttft = time.monotonic() - req.arrival  # from gateway arrival: includes queue, prefill, hop
                        tr["ttft_s"] = round(ttft, 4)
                        self.metrics.ttft.labels(req.klass, plan.hop_mode).observe(ttft)
                        self.fleet.record_ttft(plan.decode.engine, ttft)
                    if chunk.get("usage"):
                        tr["prompt_tokens"] = chunk["usage"].get("prompt_tokens")
                        tr["completion_tokens"] = chunk["usage"].get("completion_tokens")
                    yield chunk
            self.fleet.remember(
                req.job_id, "decode", plan.decode.engine, req.prefix_key, int(tr.get("prompt_tokens") or 0)
            )
            e2e = time.monotonic() - req.arrival
            tr["e2e_s"] = round(e2e, 4)
            self.metrics.e2e.labels(req.klass, plan.hop_mode).observe(e2e)
            self.metrics.completed.labels(req.klass, plan.hop_mode).inc()
        except QueueTimeout:
            raise GatewayRefusal(Shed(503, "timeout_queue", "queue", 2.0, "deadline passed while queued")) from None
        except EngineError as exc:
            self.metrics.errors.labels(plan.stage, str(exc.status)).inc()
            status = exc.status if exc.status in (400, 429, 500, 503, 529) else 502
            raise GatewayRefusal(Shed(status, exc.reason, plan.stage, 2.0, exc.detail[:200])) from None
        except httpx.HTTPError as exc:
            self.metrics.errors.labels(plan.stage, "transport").inc()
            raise GatewayRefusal(Shed(503, "no_eligible_pod", plan.stage, 2.0, f"engine unreachable: {exc}")) from None
        except asyncio.CancelledError:
            self.metrics.aborted.labels(plan.stage).inc()
            tr["aborted_at"] = plan.stage
            raise
        finally:
            self.tenants.done(req, tr.get("prompt_tokens"), tr.get("completion_tokens"))

    async def complete(self, payload: object) -> tuple[int, dict, dict[str, str]]:
        """Non-streaming client: stream from the engine internally (so TTFT is measured), then
        return one chat.completion with a steward trace block."""
        planned = self.plan(payload)
        if isinstance(planned, Shed):
            return planned.status, planned.body(), {}
        if isinstance(planned, tuple):
            shed, req = planned
            return await self._refused(shed, req, None)
        asm = Assembler()
        try:
            async for chunk in self.run(planned):
                asm.feed(chunk)
        except GatewayRefusal as refusal:
            return await self._refused(refusal.shed, planned.req, planned)
        body = asm.result()
        body["steward"] = self._steward(planned, "local")
        self._write_trace(planned.req, planned.trace, 200, "local")
        return 200, body, {"X-Via": "local"}

    async def _refused(self, shed: Shed, req: Request, plan: Plan | None) -> tuple[int, dict, dict[str, str]]:
        policy = self.settings.tenant(req.tenant)
        out = await self.overflow.try_send(req, shed, bool(policy and policy.allow_overflow))
        trace = plan.trace if plan else {}
        trace.update(shed_stage=shed.stage, shed_reason=shed.reason, shed_status=shed.status)
        if out is not None:
            out["steward"] = {
                "stages": ["guardrails", "admit", "place", "queue", "overflow"],
                "job_id": req.job_id,
                "step": req.step,
                "tenant": req.tenant,
                "workload_class": req.klass,
                "via": "overflow",
                "local_status": shed.status,
                "local_reason": shed.reason,
                "overflow_model": self.settings.overflow.model,
            }
            self.metrics.completed.labels(req.klass, "overflow").inc()
            self._write_trace(req, trace, 200, "overflow")
            return 200, out, {"X-Via": "overflow"}
        self._write_trace(req, trace, shed.status, "local")
        headers = {"Retry-After": str(max(1, int(shed.retry_after_s)))} if shed.status in (429, 503, 529) else {}
        return shed.status, shed.body(), headers

    def _steward(self, plan: Plan, via: str) -> dict:
        r = plan.req
        return {
            "stages": plan.stages(via),
            "job_id": r.job_id,
            "step": r.step,
            "tenant": r.tenant,
            "workload_class": r.klass,
            "via": via,
            **plan.trace,
        }

    def _write_trace(self, req: Request, trace: dict, status: int, via: str) -> None:
        if not self._trace:
            return
        event = {
            "ts": time.time(),
            "id": req.id,
            "job_id": req.job_id,
            "step": req.step,
            "tenant": req.tenant,
            "klass": req.klass,
            "prompt_tokens_est": req.prompt_tokens,
            "status": status,
            "via": via,
            **trace,
        }
        with self._trace.open("a") as f:
            f.write(json.dumps(event) + "\n")

    # ---- introspection for the Part 5 notebook -----------------------------------------
    def state(self) -> dict:
        now = time.monotonic()
        engines = {}
        for eid, st in self.fleet.states.items():
            s = st.snapshot(now)
            engines[eid] = {
                "pool": s.pool,
                "ok": s.ok,
                "age_s": round(s.age_s, 2),
                "warm": s.warm_state,
                "ramp_weight": s.ramp_weight,
                "slot_cap": s.slot_cap,
                "inflight": s.inflight,
                "our_queue": {k: st.queue.depth(k) for k in ("interactive", "batch")},
                "vllm_running": s.running,
                "vllm_waiting": s.waiting,
                "kv_usage": s.kv_usage,
                "preemptions": s.preemptions,
                "prefix_hit_rate": s.prefix_hit_rate,
                "nixl_bytes": st.raw.get("nixl_bytes"),
                "nixl_expired": st.raw.get("nixl_expired"),
                "p99_ttft_s": st.p99_ttft(),
            }
        return {"engines": engines, "tenants": {t: self.tenants.inflight(t) for t in self.settings.tenants}}


class GatewayRefusal(Exception):
    def __init__(self, shed: Shed):
        super().__init__(shed.reason)
        self.shed = shed


def _has_token(chunk: dict) -> bool:
    for c in chunk.get("choices") or []:
        d = c.get("delta") or {}
        if d.get("content") or d.get("tool_calls"):
            return True
    return False
