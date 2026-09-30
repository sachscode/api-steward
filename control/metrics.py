"""Gateway Prometheus metrics. Names start with orch_ so dashboards can tell gateway
numbers from engine (vllm:) numbers."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LAT = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64)


class Metrics:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        self.requests = Counter("orch_requests_total", "Requests received", ["klass", "tenant"], registry=r)
        self.shed = Counter("orch_shed_total", "Requests refused", ["stage", "reason", "code"], registry=r)
        self.completed = Counter("orch_completed_total", "Requests served", ["klass", "path"], registry=r)
        self.errors = Counter("orch_errors_total", "Engine or transport errors", ["stage", "code"], registry=r)
        self.aborted = Counter("orch_aborted_total", "Client disconnects", ["stage"], registry=r)
        self.queue_depth = Gauge(
            "orch_replica_queue_depth", "Requests waiting in the gateway queue", ["pod", "pool", "klass"], registry=r
        )
        self.inflight = Gauge("orch_replica_inflight", "Requests dispatched to the engine", ["pod", "pool"], registry=r)
        self.slot_cap = Gauge("orch_replica_slot_cap", "Dispatch slots (ramp-adjusted)", ["pod", "pool"], registry=r)
        self.tokens_inflight_engine = Gauge(
            "orch_replica_tokens_inflight", "KV tokens reserved by dispatched requests", ["pod", "pool"], registry=r
        )
        self.token_cap = Gauge(
            "orch_replica_token_cap",
            "KV token budget (vLLM blocks x block size x high water)",
            ["pod", "pool"],
            registry=r,
        )
        self.queue_wait = Histogram(
            "orch_queue_wait_seconds", "Time in the gateway queue", ["pool", "klass"], buckets=LAT, registry=r
        )
        self.ttft = Histogram(
            "orch_ttft_seconds", "Client-side time to first token", ["klass", "path"], buckets=LAT, registry=r
        )
        self.engine_ttft = Histogram(
            "orch_engine_ttft_seconds", "Dispatch to first token (excludes our queue)", ["pod"], buckets=LAT, registry=r
        )
        self.e2e = Histogram("orch_e2e_seconds", "Client-side end to end", ["klass", "path"], buckets=LAT, registry=r)
        self.prefill = Histogram(
            "orch_prefill_seconds", "Remote prefill call (max_tokens=1)", ["pod"], buckets=LAT, registry=r
        )
        self.hop_decision = Counter(
            "orch_hop_decision_total", "Hop or no hop, and why", ["decision", "reason"], registry=r
        )
        self.hops = Counter("orch_hop_total", "KV hops requested", ["src", "dst", "backend"], registry=r)
        self.hop_tokens = Counter("orch_hop_tokens_total", "Prompt tokens moved by hops", ["src", "dst"], registry=r)
        self.hop_bytes = Counter(
            "orch_hop_bytes_estimated_total", "blocks x block_size x kv_bytes_per_token", ["src", "dst"], registry=r
        )
        self.picks = Counter("orch_pick_total", "Placement decisions", ["pool", "pod", "policy", "why"], registry=r)
        self.unknown = Counter("orch_unknown_snapshot_total", "Placed on a stale snapshot", ["pool"], registry=r)
        self.snapshot_age = Gauge("orch_snapshot_age_seconds", "Age of the engine snapshot", ["pod"], registry=r)
        self.engine_up = Gauge("orch_replica_up", "Last scrape succeeded", ["pod", "pool"], registry=r)
        self.warm_state = Gauge(
            "orch_replica_warm_state", "0 cold, 1 warming, 2 ramping, 3 warm", ["pod", "pool"], registry=r
        )
        self.ramp_weight = Gauge("orch_replica_ramp_weight", "Share of slots open", ["pod", "pool"], registry=r)
        self.warmup_ttft = Histogram(
            "orch_warmup_ttft_seconds", "TTFT of warmup requests", ["pod"], buckets=LAT, registry=r
        )
        self.tenant_tokens = Gauge("orch_tenant_tokens_window", "Tokens admitted in the window", ["tenant"], registry=r)
        self.tenant_inflight = Gauge("orch_tenant_inflight", "Tenant requests in flight", ["tenant"], registry=r)
        self.tokens_in_flight = Gauge(
            "orch_tokens_in_flight", "Estimated tokens queued or running", ["pool"], registry=r
        )
        self.overflow = Counter("orch_overflow_total", "Overflow attempts", ["result", "from_reason"], registry=r)
        self.affinity_evictions = Counter(
            "orch_affinity_evictions_total", "Prefix/job affinity entries dropped", ["pod", "why"], registry=r
        )
