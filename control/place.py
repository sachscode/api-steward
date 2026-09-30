"""Placement: which engine in a pool.

pick(req, workers, *, policy) -> Snapshot | Shed

Eligible: scraped recently, healthy, not cold, KV below the class high-water mark, and our
queue for it would not blow the deadline. Stale snapshots are used only when nothing fresh
is left (counted in orch_unknown_snapshot_total).

Scores (higher is better). Queue depth is a scorer, not only an admission input:
  prefill: +3 same job's conversation last prefilled here (its KV prefix is cached)
           +1 shared system+tools prefix seen here
           -2 * (our queue + in flight) / slots   -0.5 * vLLM waiting   +1 * KV free
  decode:  +1 same job last decoded here
           -2 * vLLM running / slots   -2 * our queue / slots   +2 * KV free
Policies:
  least_loaded      lowest (queue + in flight) / slots
  p2c               best of two random eligible engines by score
  prefix_then_load  highest score, which lets cache affinity win until load outweighs it
The request is placed once. There is no retry on another engine after dispatch (no bouncing).
"""

from __future__ import annotations

import random
from collections.abc import Callable

from .fleet import Fleet
from .types import Request, Shed, Snapshot

rng = random.Random()


def _load(s: Snapshot) -> float:
    return (s.queue_depth + s.inflight) / max(1, s.slot_cap)


def score(req: Request, s: Snapshot, fleet: Fleet) -> tuple[float, str]:
    kv_free = s.kv_free if s.kv_free is not None else 0.5
    if s.pool == "prefill":
        why = "load"
        total = -2.0 * _load(s) - 0.5 * (s.waiting or 0) + kv_free
        if fleet.job_engine(req.job_id, "prefill") == s.engine:
            total += 3.0
            why = "job_affinity"
        elif fleet.holds_prefix(s.engine, req.prefix_key):
            total += 1.0
            why = "prefix_affinity"
        return total, why
    why = "load"
    total = -2.0 * (s.running or 0) / max(1, s.slot_cap) - 2.0 * s.queue_depth / max(1, s.slot_cap) + 2.0 * kv_free
    if fleet.job_engine(req.job_id, "decode") == s.engine:
        total += 1.0
        why = "job_affinity"
    return total, why


def eligible(req: Request, snaps: list[Snapshot], fleet: Fleet) -> tuple[list[Snapshot], bool]:
    settings = fleet.settings
    water = settings.kv_high_water[req.klass]
    usable = [
        s
        for s in snaps
        if s.ok
        and s.warm_state != "cold"
        and (s.kv_usage is None or s.kv_usage < water)
        and fleet.states[s.engine].queue.predicted_wait_s(req) <= req.remaining_s() / 2
    ]
    fresh = [s for s in usable if s.age_s <= settings.stale_s]
    if fresh:
        return fresh, False
    recent = [s for s in usable if s.age_s <= 5 * settings.stale_s]
    return recent, bool(recent)


def pick(req: Request, workers: list[Snapshot], *, policy: str, fleet: Fleet) -> Snapshot | Shed:
    pool = workers[0].pool if workers else "decode"
    candidates, stale = eligible(req, workers, fleet)
    if not candidates:
        reason = (
            "kv_free"
            if any(s.ok and (s.kv_usage or 0) >= fleet.settings.kv_high_water[req.klass] for s in workers)
            else "no_eligible_pod"
        )
        return Shed(503, reason, "place", 2.0, f"no eligible {pool} engine")
    if stale:
        fleet.metrics.unknown.labels(pool).inc()

    scored = [(score(req, s, fleet), s) for s in candidates]
    chooser: dict[str, Callable[[], tuple[tuple[float, str], Snapshot]]] = {
        "least_loaded": lambda: min(scored, key=lambda t: _load(t[1])),
        "p2c": lambda: max(rng.sample(scored, 2), key=lambda t: t[0][0]) if len(scored) > 1 else scored[0],
        "prefix_then_load": lambda: max(scored, key=lambda t: t[0][0]),
    }
    (_, why), chosen = chooser.get(policy, chooser["p2c"])()
    fleet.metrics.picks.labels(pool, chosen.engine, policy, why).inc()
    return chosen
