"""Overflow gate: stay or leave, decided after the local result.

  429 tenant_tokens / tenant_concurrency   stay: the tenant is over its share, not the fleet
  500 engine_error                         stay: a bug does not become someone else's bill
  slice_oom                                stay: an oversized request fails the same way anywhere
  503 / 529                                may leave, if the tenant allows it and the limiter has room

Target: Superlinked's OpenAI-compatible API, a named model (OVERFLOW_MODEL), limited by a
token bucket (OVERFLOW_RPS, OVERFLOW_BURST), a concurrency cap (OVERFLOW_MAX_INFLIGHT) and an
output clamp (OVERFLOW_MAX_TOKENS). Only tenants marked allow_overflow may leave: overflow
sends the prompt outside the private cluster, so private tenants get the 503 instead.
"""

from __future__ import annotations

import time

import httpx

from .config import OverflowSettings
from .metrics import Metrics
from .types import Request, Shed


class Overflow:
    def __init__(self, settings: OverflowSettings, metrics: Metrics, client: httpx.AsyncClient):
        self.s, self.metrics, self.client = settings, metrics, client
        self.tokens = float(settings.burst)
        self.refilled = time.monotonic()
        self.inflight = 0

    def _take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.s.burst, self.tokens + (now - self.refilled) * self.s.rps)
        self.refilled = now
        if self.tokens < 1 or self.inflight >= self.s.max_inflight:
            return False
        self.tokens -= 1
        return True

    async def try_send(self, req: Request, local: Shed, tenant_allows: bool) -> dict | None:
        if not local.may_leave:
            return None
        if not self.s.enabled:
            self.metrics.overflow.labels("disabled", local.reason).inc()
            return None
        if not tenant_allows:
            self.metrics.overflow.labels("tenant_private", local.reason).inc()
            return None
        if not self._take():
            self.metrics.overflow.labels("limited", local.reason).inc()
            return None
        body = {
            "model": self.s.model,
            "messages": req.body["messages"],
            "tools": req.body["tools"],
            "tool_choice": "auto",
            "max_tokens": min(req.max_tokens, self.s.max_tokens),
            "temperature": 0,
        }
        self.inflight += 1
        try:
            resp = await self.client.post(
                f"{self.s.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.s.api_key}"},
                timeout=self.s.timeout_s,
            )
        except httpx.HTTPError:
            self.metrics.overflow.labels("transport_error", local.reason).inc()
            return None
        finally:
            self.inflight -= 1
        if resp.status_code != 200:
            self.metrics.overflow.labels(f"http_{resp.status_code}", local.reason).inc()
            return None
        self.metrics.overflow.labels("ok", local.reason).inc()
        return resp.json()
