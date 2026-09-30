"""What the gateway knows about its engines: scraped snapshots, warm/ramp state, affinity.

A background task scrapes every engine's /metrics every scrape_interval_s. Placement reads
the cached Snapshot; it never waits on a scrape. Snapshots older than stale_s are "unknown".

Warm is declared by the gateway, not assumed from "the pod is Running":
  cold -> warming (warmup requests seed the shared prefix and wake the kernels)
       -> ramping (slots opened 25%, 50%, 100% while TTFT holds the SLO)
       -> warm
An engine that restarts (process_start_time changes) or fails scrapes goes back to cold, and
its affinity entries are evicted so the router does not stick to a ghost cache.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import httpx

from .config import Settings
from .engines import Engine, EngineError
from .metrics import Metrics
from .queue import EngineQueue
from .types import Snapshot

log = logging.getLogger("control.fleet")
WARM_CODE = {"cold": 0, "warming": 1, "ramping": 2, "warm": 3}
WARMUP_USER = "Warmup: say ready."


class EngineState:
    def __init__(self, engine: Engine, queue: EngineQueue):
        self.engine, self.queue = engine, queue
        self.raw: dict[str, float] = {}
        self.scraped_at = float("-inf")
        self.ok = False
        self.start_time: float | None = None
        self.warm = "cold"
        self.ramp_index = 0
        self.ramp_since = 0.0
        self.ttfts: deque[float] = deque(maxlen=50)  # recent client TTFTs served by this engine

    def snapshot(self, now: float) -> Snapshot:
        r = self.raw
        queries, hits = r.get("prefix_queries"), r.get("prefix_hits")
        return Snapshot(
            engine=self.engine.id,
            pool=self.engine.pool,
            ok=self.ok,
            age_s=now - self.scraped_at,
            kv_usage=r.get("kv_usage"),
            running=None if "running" not in r else int(r["running"]),
            waiting=None if "waiting" not in r else int(r["waiting"]),
            preemptions=r.get("preemptions"),
            prefix_hit_rate=(hits / queries) if queries else None,
            queue_depth=self.queue.depth(),
            inflight=self.queue.inflight,
            slot_cap=self.queue.cap,
            warm_state=self.warm,
            ramp_weight=self.queue.weight,
        )

    def p99_ttft(self) -> float | None:
        if len(self.ttfts) < 5:
            return None
        ordered = sorted(self.ttfts)
        return ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]


class Fleet:
    def __init__(self, settings: Settings, engines: list[Engine], metrics: Metrics, warmup_body: dict):
        self.settings, self.metrics, self.warmup_body = settings, metrics, warmup_body
        self.states: dict[str, EngineState] = {}
        for e in engines:
            q = EngineQueue(e.id, e.pool, settings.slot_cap[e.pool], metrics, settings.batch_aging_s)
            q.set_weight(0.0 if settings.warmup_requests else 1.0)
            self.states[e.id] = EngineState(e, q)
        # Affinity: which engine last held this job's conversation, and which engines have the
        # shared system+tools prefix cached. Both are hints; stale entries are evicted.
        self.job_affinity: dict[tuple[str, str], tuple[str, float]] = {}
        self.prefix_holders: dict[str, dict[str, float]] = {}
        self.job_tokens: dict[tuple[str, str], int] = {}
        self._tasks: list[asyncio.Task] = []

    # ---- lookups -----------------------------------------------------------------------
    def pool(self, name: str) -> list[EngineState]:
        return [s for s in self.states.values() if s.engine.pool == name]

    def snapshots(self, pool: str) -> list[Snapshot]:
        now = time.monotonic()
        return [s.snapshot(now) for s in self.pool(pool)]

    def remember(self, job_id: str, pool: str, engine: str, prefix_key: str, prompt_tokens: int = 0) -> None:
        now = time.monotonic()
        self.job_affinity[(job_id, pool)] = (engine, now)
        self.prefix_holders.setdefault(prefix_key, {})[engine] = now
        if prompt_tokens:
            self.job_tokens[(job_id, engine)] = prompt_tokens
        if len(self.job_affinity) > 10_000:  # bound memory; oldest jobs go first
            for key, _ in sorted(self.job_affinity.items(), key=lambda kv: kv[1][1])[:1000]:
                del self.job_affinity[key]
                self.job_tokens.pop((key[0], _[0]), None)

    def job_prompt_tokens(self, job_id: str, engine: str) -> int:
        """Prompt tokens this job last sent to this engine: the part its prefix cache may hold."""
        return self.job_tokens.get((job_id, engine), 0)

    def job_engine(self, job_id: str, pool: str, max_age_s: float = 300.0) -> str | None:
        hit = self.job_affinity.get((job_id, pool))
        if hit and time.monotonic() - hit[1] <= max_age_s:
            return hit[0]
        return None

    def holds_prefix(self, engine: str, prefix_key: str, max_age_s: float = 600.0) -> bool:
        seen = self.prefix_holders.get(prefix_key, {}).get(engine)
        return seen is not None and time.monotonic() - seen <= max_age_s

    def evict_affinity(self, engine: str, why: str) -> None:
        dropped = [k for k, (e, _) in self.job_affinity.items() if e == engine]
        for k in dropped:
            del self.job_affinity[k]
        for k in [k for k in self.job_tokens if k[1] == engine]:
            del self.job_tokens[k]
        for holders in self.prefix_holders.values():
            if holders.pop(engine, None) is not None:
                dropped.append(("prefix", engine))
        if dropped:
            self.metrics.affinity_evictions.labels(engine, why).inc(len(dropped))

    def record_ttft(self, engine: str, ttft: float) -> None:
        state = self.states.get(engine)
        if state:
            state.ttfts.append(ttft)

    # ---- background loops --------------------------------------------------------------
    def start(self) -> None:
        for state in self.states.values():
            self._tasks.append(asyncio.create_task(self._scrape_loop(state)))
        self._tasks.append(asyncio.create_task(self._ramp_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _scrape_loop(self, state: EngineState) -> None:
        eid, pool = state.engine.id, state.engine.pool
        while True:
            try:
                raw = await state.engine.scrape()
                restarted = state.start_time is not None and raw.get("start_time") not in (None, state.start_time)
                state.raw, state.scraped_at = raw, time.monotonic()
                state.start_time = raw.get("start_time", state.start_time)
                if restarted:
                    log.warning("%s restarted: back to cold", eid)
                    self._go_cold(state, "restart")
                if not state.ok:
                    state.ok = True
                    if state.warm == "cold":
                        asyncio.create_task(self._warm_up(state))
            except (httpx.HTTPError, ValueError, OSError) as exc:  # unreachable, 5xx, bad payload
                if state.ok:
                    log.warning("%s scrape failed: %s", eid, exc)
                    self._go_cold(state, "unhealthy")
                state.ok = False
            self.metrics.engine_up.labels(eid, pool).set(1 if state.ok else 0)
            self.metrics.snapshot_age.labels(eid).set(min(3600.0, time.monotonic() - state.scraped_at))
            await asyncio.sleep(self.settings.scrape_interval_s)

    def _go_cold(self, state: EngineState, why: str) -> None:
        state.warm, state.ramp_index = "cold", 0
        state.queue.set_weight(0.0 if self.settings.warmup_requests else 1.0)
        state.ttfts.clear()
        self.evict_affinity(state.engine.id, why)
        self._publish_warm(state)

    async def _warm_up(self, state: EngineState) -> None:
        """Send a few small requests carrying the real system prompt + tool schema. They
        compile/capture anything still lazy and seed the prefix cache with the shared prefix."""
        if not self.settings.warmup_requests:
            state.warm = "warm"
            self._publish_warm(state)
            return
        state.warm = "warming"
        self._publish_warm(state)
        eid = state.engine.id
        for _ in range(self.settings.warmup_requests):
            t0 = time.perf_counter()
            try:
                async for chunk in state.engine.stream(dict(self.warmup_body, max_tokens=4)):
                    if any((c.get("delta") or {}).get("content") for c in chunk.get("choices") or []):
                        break
            except (httpx.HTTPError, EngineError, ValueError) as exc:
                log.warning("%s warmup failed: %s", eid, exc)
                return  # the scrape loop will retry when the engine looks healthy again
            self.metrics.warmup_ttft.labels(eid).observe(time.perf_counter() - t0)
        self.prefix_holders.setdefault(self.warmup_prefix_key, {})[eid] = time.monotonic()
        state.warm, state.ramp_index, state.ramp_since = "ramping", 0, time.monotonic()
        state.queue.set_weight(self.settings.ramp_weights[0])
        self._publish_warm(state)
        log.info("%s warm-up done; ramping", eid)

    warmup_prefix_key = ""  # set by the gateway to guard.PREFIX_KEY

    async def _ramp_loop(self) -> None:
        """Open slots step by step while the engine's recent p99 TTFT stays within the SLO.
        If p99 breaks the SLO, hold the current step instead of slamming to 100%."""
        weights = self.settings.ramp_weights
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for state in self.states.values():
                if state.warm != "ramping" or now - state.ramp_since < self.settings.ramp_step_s:
                    continue
                p99 = state.p99_ttft()
                if p99 is not None and p99 > self.settings.ttft_slo_s:
                    state.ramp_since = now  # hold this step
                    continue
                state.ramp_index += 1
                state.ramp_since = now
                if state.ramp_index >= len(weights) - 1:
                    state.warm = "warm"
                    state.queue.set_weight(1.0)
                else:
                    state.queue.set_weight(weights[state.ramp_index])
                self._publish_warm(state)

    def _publish_warm(self, state: EngineState) -> None:
        eid, pool = state.engine.id, state.engine.pool
        self.metrics.warm_state.labels(eid, pool).set(WARM_CODE[state.warm])
        self.metrics.ramp_weight.labels(eid, pool).set(state.queue.weight)
