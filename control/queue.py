"""The gateway's own queue, one per engine.

admit -> place -> THIS QUEUE -> engine waiting / running / preempt

Each engine gets a fixed number of dispatch slots. A request waits here until a slot frees,
so vLLM's own waiting queue stays short and the gateway, not the engine, decides what runs
next: interactive before batch, batch promoted after batch_aging_s so it is not starved, and
anything past its deadline dropped at dequeue (timeout_queue) instead of wasting GPU time.
Ramping an engine up after a restart is done by opening its slots gradually.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .metrics import Metrics
from .types import CLASS_RANK, Request


class QueueTimeout(Exception):
    """The request's deadline passed while it waited for a slot."""


@dataclass
class _Waiter:
    req: Request
    fut: asyncio.Future
    since: float = field(default_factory=time.monotonic)


class EngineQueue:
    def __init__(self, engine: str, pool: str, cap: int, metrics: Metrics, batch_aging_s: float = 15.0):
        self.engine, self.pool, self.base_cap = engine, pool, cap
        self.weight = 1.0
        self.metrics, self.batch_aging_s = metrics, batch_aging_s
        self.inflight = 0
        self.waiters: list[_Waiter] = []
        self.service_s = 1.0  # EWMA of slot hold time, for predicted wait
        self._publish()

    @property
    def cap(self) -> int:
        return max(1, round(self.base_cap * self.weight))

    def set_weight(self, weight: float) -> None:
        self.weight = weight
        self._grant()
        self._publish()

    def depth(self, klass: str | None = None) -> int:
        return sum(1 for w in self.waiters if klass is None or w.req.klass == klass)

    def predicted_wait_s(self, req: Request) -> float:
        """Rough wait if req joined now: requests that would run before it, spread over the
        slots, times the average slot hold time."""
        now = time.monotonic()
        ahead = sum(1 for w in self.waiters if self._rank(w, now) <= CLASS_RANK[req.klass])
        if self.inflight < self.cap and ahead == 0:
            return 0.0
        return (ahead + 1) / self.cap * self.service_s

    def _rank(self, w: _Waiter, now: float) -> int:
        rank = CLASS_RANK[w.req.klass]
        if rank > 0 and now - w.since >= self.batch_aging_s:
            return 0  # aged batch competes with interactive
        return rank

    def _grant(self) -> None:
        now = time.monotonic()
        while self.inflight < self.cap and self.waiters:
            best = min(self.waiters, key=lambda w: (self._rank(w, now), w.req.arrival))
            self.waiters.remove(best)
            if best.fut.done():
                continue
            if best.req.deadline <= now:
                best.fut.set_exception(QueueTimeout())
                continue
            self.inflight += 1
            best.fut.set_result(None)
        self._publish()

    def _release(self, held_s: float) -> None:
        self.inflight -= 1
        self.service_s = 0.8 * self.service_s + 0.2 * held_s
        self._grant()

    def _publish(self) -> None:
        for klass in CLASS_RANK:
            self.metrics.queue_depth.labels(self.engine, self.pool, klass).set(self.depth(klass))
        self.metrics.inflight.labels(self.engine, self.pool).set(self.inflight)
        self.metrics.slot_cap.labels(self.engine, self.pool).set(self.cap)

    async def _acquire(self, req: Request) -> float:
        t0 = time.monotonic()
        if self.inflight < self.cap and not self.waiters:
            self.inflight += 1
            self._publish()
            return 0.0
        fut = asyncio.get_running_loop().create_future()
        waiter = _Waiter(req, fut)
        self.waiters.append(waiter)
        self._publish()
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=max(0.0, req.remaining_s()))
        except TimeoutError:
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                return time.monotonic() - t0  # granted at the same instant the timer fired
            self._drop(waiter)
            raise QueueTimeout() from None
        except asyncio.CancelledError:
            # Client went away while queued. If a slot was granted in the meantime, give it back.
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                self._release(0.0)
            else:
                self._drop(waiter)
            raise
        return time.monotonic() - t0

    def _drop(self, waiter: _Waiter) -> None:
        if waiter in self.waiters:
            self.waiters.remove(waiter)
        if not waiter.fut.done():
            waiter.fut.cancel()
        self._publish()

    @asynccontextmanager
    async def slot(self, req: Request):
        waited = await self._acquire(req)
        self.metrics.queue_wait.labels(self.pool, req.klass).observe(waited)
        start = time.monotonic()
        try:
            yield waited
        finally:
            self._release(time.monotonic() - start)
