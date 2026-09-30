"""Admission: should this request enter at all?

should_shed(req, snap) -> (shed?, code, reason, retry_after_seconds)

Checks run cheapest first and stop at the first refusal:
  tenant_tokens       429  tenant used its tokens/min window (stop one tenant owning the GPU)
  tenant_concurrency  429  tenant already has max_inflight requests queued or running
  no_eligible_pod     503  no healthy decode engine to finish the request
  kv_free             503  every decode engine's KV is past the high-water mark for this class
  timeout_queue       503  the shortest predicted queue wait would eat half the deadline
429s stay local (the tenant is the problem, not capacity). 503s may overflow.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .metrics import Metrics
from .types import Request, Shed, Snapshot

WINDOW_S = 60.0


@dataclass
class FleetView:
    """The admission-time view: decode snapshots plus each engine's predicted queue wait."""

    decode: list[Snapshot]
    predicted_wait_s: dict[str, float]
    stale_s: float


def should_shed(req: Request, snap: FleetView, settings: Settings, tenants: TenantBook) -> tuple[bool, int, str, float]:
    shed = check(req, snap, settings, tenants)
    if shed is None:
        return False, 200, "admitted", 0.0
    return True, shed.status, shed.reason, shed.retry_after_s


def check(req: Request, view: FleetView, settings: Settings, tenants: TenantBook) -> Shed | None:
    policy = settings.tenant(req.tenant)
    assert policy is not None  # guardrails rejected unknown tenants
    used, window_left = tenants.window(req.tenant, req.arrival)
    if used + req.cost_tokens > policy.tokens_per_min:
        return Shed(429, "tenant_tokens", "admit", round(window_left, 1), "tenant tokens per minute exhausted")
    if tenants.inflight(req.tenant) >= policy.max_inflight:
        return Shed(429, "tenant_concurrency", "admit", 1.0, "tenant has too many requests in flight")

    live = [s for s in view.decode if s.ok and s.age_s <= view.stale_s and s.warm_state != "cold"]
    if not live:
        return Shed(503, "no_eligible_pod", "admit", 5.0, "no healthy warm decode engine")
    water = settings.kv_high_water[req.klass]
    with_kv = [s for s in live if s.kv_usage is not None]
    if with_kv and all(s.kv_usage >= water for s in with_kv):
        return Shed(503, "kv_free", "admit", 2.0, f"decode KV above {water:.0%} on every engine")
    best_wait = min(view.predicted_wait_s.get(s.engine, 0.0) for s in live)
    if best_wait > req.remaining_s() / 2:
        return Shed(503, "timeout_queue", "admit", round(best_wait, 1), "predicted queue wait exceeds the deadline")
    return None


class TenantBook:
    """Per-tenant token window and in-flight count."""

    def __init__(self, metrics: Metrics):
        self.metrics = metrics
        self._window: dict[str, tuple[float, int]] = {}
        self._inflight: dict[str, int] = {}

    def window(self, tenant: str, now: float) -> tuple[int, float]:
        start, used = self._window.get(tenant, (now, 0))
        if now - start >= WINDOW_S:
            start, used = now, 0
            self._window[tenant] = (start, used)
        return used, WINDOW_S - (now - start)

    def inflight(self, tenant: str) -> int:
        return self._inflight.get(tenant, 0)

    def charge(self, req: Request) -> None:
        start, used = self._window.get(req.tenant, (req.arrival, 0))
        if req.arrival - start >= WINDOW_S:
            start, used = req.arrival, 0
        self._window[req.tenant] = (start, used + req.cost_tokens)
        self._inflight[req.tenant] = self.inflight(req.tenant) + 1
        self.metrics.tenant_tokens.labels(req.tenant).set(used + req.cost_tokens)
        self.metrics.tenant_inflight.labels(req.tenant).set(self._inflight[req.tenant])

    def done(self, req: Request) -> None:
        self._inflight[req.tenant] = max(0, self.inflight(req.tenant) - 1)
        self.metrics.tenant_inflight.labels(req.tenant).set(self._inflight[req.tenant])
