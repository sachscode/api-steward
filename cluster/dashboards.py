"""Grafana dashboards for the API Steward cluster, generated from one place.

    python3 dashboards.py                       # write k8s/observability/dashboards/*.json
    python3 dashboards.py --check http://127.0.0.1:9090   # run every query against Prometheus

One dashboard per question the course asks during the demo:
  cluster        nodes, pods, GPUs (DCGM: util, memory, power, NVLink, PCIe)
  outcomes       success and failures: completed by path, shed by stage/reason, aborts, overflow
  admission      gateway + admission: sheds by reason, tenant windows, client TTFT/E2E
  router         placement: picks and why, stale snapshots, hop decisions, engine TTFT
  queues         our queue vs vLLM's queue, slots in use, queue wait by class
  vllm           engine: running/waiting, KV usage, TTFT/ITL, prefix cache, preemptions
  kv-hop         NIXL: hops, bytes, transfer time, KV consumed on decode, ghosts, RoCE NIC bytes
  scaling        KEDA scaler value vs threshold, replicas, warm state and ramp
Label conventions: gateway series carry pod="<pool>-<slot>" (the engine id); vLLM series
carry pool and slot; DCGM and node-exporter series carry node.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}
OUT = Path(__file__).resolve().parent / "k8s" / "observability" / "dashboards"
R = "[1m]"  # rate window


def q(p: float, metric: str, by: str) -> str:
    return f"histogram_quantile({p}, sum by (le, {by}) (rate({metric}_bucket{R})))"


def panel(
    title: str,
    targets: list[tuple[str, str]],
    unit: str = "short",
    kind: str = "timeseries",
    desc: str = "",
    stack: bool = False,
) -> dict:
    p = {
        "type": kind,
        "title": title,
        "description": desc,
        "datasource": PROM,
        "targets": [
            {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
            for i, (expr, legend) in enumerate(targets)
        ],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }
    if kind == "timeseries":
        p["fieldConfig"]["defaults"]["custom"] = {
            "lineWidth": 1,
            "fillOpacity": 25 if stack else 8,
            "stacking": {"mode": "normal" if stack else "none"},
        }
    return p


def dashboard(uid: str, title: str, desc: str, panels: list[dict]) -> dict:
    for i, p in enumerate(panels):
        w = 8 if p["type"] == "stat" else 12
        per_row = 24 // w
        p["id"] = i + 1
        p["gridPos"] = {"x": (i % per_row) * w, "y": (i // per_row) * 8, "w": w, "h": 8 if w == 12 else 5}
    return {
        "uid": f"steward-{uid}",
        "title": f"API Steward / {title}",
        "description": desc,
        "tags": ["api-steward"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "panels": panels,
    }


def build() -> dict[str, dict]:
    d: dict[str, dict] = {}
    d["cluster"] = dashboard(
        "cluster",
        "Cluster",
        "Nodes, pods and GPUs",
        [
            panel(
                "Nodes ready",
                [('sum(kube_node_status_condition{condition="Ready",status="true"})', "ready")],
                kind="stat",
            ),
            panel("Pods by phase", [("sum by (phase) (kube_pod_status_phase == 1)", "{{phase}}")], kind="stat"),
            panel(
                "Container restarts (15m)",
                [("sum(increase(kube_pod_container_status_restarts_total[15m]))", "restarts")],
                kind="stat",
            ),
            panel("GPU utilization by node/GPU", [("DCGM_FI_DEV_GPU_UTIL", "{{node}} gpu{{gpu}}")], "percent"),
            panel("GPU memory used", [("DCGM_FI_DEV_FB_USED * 1024 * 1024", "{{node}} gpu{{gpu}}")], "bytes"),
            panel(
                "Tensor core active",
                [("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "{{node}} gpu{{gpu}}")],
                "percentunit",
                desc="Share of cycles the tensor pipes are busy: compute actually used, not just 'GPU busy'.",
            ),
            panel(
                "NVLink bandwidth (TP all-reduce)",
                [("rate(DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL" + R + ")", "{{node}} gpu{{gpu}}")],
                "Bps",
                desc="NVLink traffic between GPUs of one host: tensor-parallel all-reduce inside an engine.",
            ),
            panel("GPU power", [("DCGM_FI_DEV_POWER_USAGE", "{{node}} gpu{{gpu}}")], "watt"),
            panel(
                "Node CPU busy",
                [('1 - avg by (node) (rate(node_cpu_seconds_total{mode="idle"}' + R + "))", "{{node}}")],
                "percentunit",
            ),
            panel("Node memory available", [("node_memory_MemAvailable_bytes", "{{node}}")], "bytes"),
        ],
    )
    d["outcomes"] = dashboard(
        "outcomes",
        "Success and failures",
        "What happened to each request",
        [
            panel(
                "Success ratio (5m)",
                [
                    (
                        "sum(increase(orch_completed_total[5m])) / clamp_min(sum(increase(orch_requests_total[5m])), 1)",
                        "served",
                    )
                ],
                "percentunit",
                kind="stat",
            ),
            panel("Requests /s", [("sum(rate(orch_requests_total" + R + "))", "in")], "reqps", kind="stat"),
            panel("Shed /s", [("sum(rate(orch_shed_total" + R + "))", "shed")], "reqps", kind="stat"),
            panel(
                "Served by path",
                [("sum by (klass, path) (rate(orch_completed_total" + R + "))", "{{klass}} via {{path}}")],
                "reqps",
                stack=True,
                desc="path: nixl = prefill on another engine + KV hop; local = decode prefilled itself; overflow = left the cluster.",
            ),
            panel(
                "Refused by stage and reason",
                [("sum by (stage, reason, code) (rate(orch_shed_total" + R + "))", "{{code}} {{stage}}/{{reason}}")],
                "reqps",
                stack=True,
            ),
            panel(
                "Client gone, by stage",
                [("sum by (stage) (rate(orch_aborted_total" + R + "))", "{{stage}}")],
                "reqps",
                desc="queue: never reached a GPU. prefill: KV built, never pulled (expires by lease). decode: stream closed, vLLM freed KV.",
            ),
            panel(
                "Overflow attempts by result",
                [
                    (
                        "sum by (result, from_reason) (rate(orch_overflow_total" + R + "))",
                        "{{result}} after {{from_reason}}",
                    )
                ],
                "reqps",
            ),
            panel(
                "Engine errors by stage",
                [("sum by (stage, code) (rate(orch_errors_total" + R + "))", "{{stage}} {{code}}")],
                "reqps",
            ),
        ],
    )
    d["admission"] = dashboard(
        "admission",
        "Gateway and admission",
        "Guardrails, admission and client latency",
        [
            panel(
                "Refused at guardrails",
                [('sum by (reason) (rate(orch_shed_total{stage="guardrails"}' + R + "))", "{{reason}}")],
                "reqps",
            ),
            panel(
                "Refused at admission",
                [('sum by (reason, code) (rate(orch_shed_total{stage="admit"}' + R + "))", "{{code}} {{reason}}")],
                "reqps",
                desc="429 tenant_tokens / tenant_concurrency stay local; 503 kv_free / timeout_queue / no_eligible_pod may overflow.",
            ),
            panel(
                "Tenant tokens in window",
                [("orch_tenant_tokens_window", "{{tenant}}")],
                "short",
                desc="Reserved at admission (prompt + max_tokens), settled to real usage on completion.",
            ),
            panel("Tenant requests in flight", [("orch_tenant_inflight", "{{tenant}}")]),
            panel(
                "Client TTFT p50 / p99 by class",
                [
                    (q(0.5, "orch_ttft_seconds", "klass"), "p50 {{klass}}"),
                    (q(0.99, "orch_ttft_seconds", "klass"), "p99 {{klass}}"),
                ],
                "s",
                desc="From gateway arrival: our queue + prefill + hop + first token. This is the SLO.",
            ),
            panel(
                "Client E2E p50 / p99 by class",
                [
                    (q(0.5, "orch_e2e_seconds", "klass"), "p50 {{klass}}"),
                    (q(0.99, "orch_e2e_seconds", "klass"), "p99 {{klass}}"),
                ],
                "s",
            ),
            panel(
                "TTFT spread p99 - p50 (interactive vs batch)",
                [(f"{q(0.99, 'orch_ttft_seconds', 'klass')} - {q(0.5, 'orch_ttft_seconds', 'klass')}", "{{klass}}")],
                "s",
                desc="p99_spread: where interactive is protected, its spread stays small while batch absorbs the wait.",
            ),
            panel(
                "Requests by tenant and class",
                [("sum by (tenant, klass) (rate(orch_requests_total" + R + "))", "{{tenant}} {{klass}}")],
                "reqps",
                stack=True,
            ),
        ],
    )
    d["router"] = dashboard(
        "router",
        "Router",
        "Placement and hop decisions",
        [
            panel(
                "Picks by engine and reason",
                [("sum by (pool, pod, why) (rate(orch_pick_total" + R + "))", "{{pod}} {{why}}")],
                "reqps",
                stack=True,
                desc="why: job_affinity (conversation cached there), prefix_affinity (shared system+tools prefix), load.",
            ),
            panel(
                "Hop decisions",
                [("sum by (decision, reason) (rate(orch_hop_decision_total" + R + "))", "{{decision}}: {{reason}}")],
                "reqps",
                stack=True,
            ),
            panel(
                "Engine TTFT p50 / p99 (excludes our queue)",
                [
                    (q(0.5, "orch_engine_ttft_seconds", "pod"), "p50 {{pod}}"),
                    (q(0.99, "orch_engine_ttft_seconds", "pod"), "p99 {{pod}}"),
                ],
                "s",
            ),
            panel(
                "Prefill call time p50 / p99",
                [
                    (q(0.5, "orch_prefill_seconds", "pod"), "p50 {{pod}}"),
                    (q(0.99, "orch_prefill_seconds", "pod"), "p99 {{pod}}"),
                ],
                "s",
            ),
            panel(
                "Placed on a stale snapshot",
                [("sum by (pool) (rate(orch_unknown_snapshot_total" + R + "))", "{{pool}}")],
                "reqps",
            ),
            panel("Snapshot age", [("orch_snapshot_age_seconds < 3600", "{{pod}}")], "s"),
            panel(
                "Affinity evictions (ghost cache avoided)",
                [("sum by (pod, why) (increase(orch_affinity_evictions_total[5m]))", "{{pod}} {{why}}")],
            ),
            panel("Engine up (last scrape)", [("orch_replica_up", "{{pod}}")]),
        ],
    )
    d["queues"] = dashboard(
        "queues",
        "Queues",
        "Our queue vs vLLM's queue, per engine",
        [
            panel(
                "Our queue depth by engine and class", [("orch_replica_queue_depth", "{{pod}} {{klass}}")], stack=True
            ),
            panel(
                "vLLM waiting by engine",
                [("vllm:num_requests_waiting", "{{pool}}-{{slot}}")],
                desc="Kept near zero by our slot caps: we decide what runs next, not the engine.",
            ),
            panel(
                "Slots in use vs cap",
                [("orch_replica_inflight", "in flight {{pod}}"), ("orch_replica_slot_cap", "cap {{pod}}")],
            ),
            panel("vLLM running by engine", [("vllm:num_requests_running", "{{pool}}-{{slot}}")]),
            panel(
                "KV tokens reserved vs budget",
                [("orch_replica_tokens_inflight", "reserved {{pod}}"), ("orch_replica_token_cap", "budget {{pod}}")],
                desc="Dispatch waits when prompt + output budget would push reserved tokens past the engine's KV budget.",
            ),
            panel(
                "Queue wait p50 / p99 by pool and class",
                [
                    (q(0.5, "orch_queue_wait_seconds", "pool, klass"), "p50 {{pool}} {{klass}}"),
                    (q(0.99, "orch_queue_wait_seconds", "pool, klass"), "p99 {{pool}} {{klass}}"),
                ],
                "s",
            ),
            panel(
                "vLLM queue time (mean)",
                [
                    (
                        "rate(vllm:request_queue_time_seconds_sum"
                        + R
                        + ") / clamp_min(rate(vllm:request_queue_time_seconds_count"
                        + R
                        + "), 1e-9)",
                        "{{pool}}-{{slot}}",
                    )
                ],
                "s",
            ),
        ],
    )
    d["vllm"] = dashboard(
        "vllm",
        "vLLM engines",
        "Engine-side view",
        [
            panel("KV cache usage", [("vllm:kv_cache_usage_perc", "{{pool}}-{{slot}}")], "percentunit"),
            panel("Preemptions /s", [("rate(vllm:num_preemptions_total" + R + ")", "{{pool}}-{{slot}}")]),
            panel(
                "TTFT (engine, mean)",
                [
                    (
                        "rate(vllm:time_to_first_token_seconds_sum"
                        + R
                        + ") / clamp_min(rate(vllm:time_to_first_token_seconds_count"
                        + R
                        + "), 1e-9)",
                        "{{pool}}-{{slot}}",
                    )
                ],
                "s",
            ),
            panel(
                "Inter-token latency (mean)",
                [
                    (
                        "rate(vllm:inter_token_latency_seconds_sum"
                        + R
                        + ") / clamp_min(rate(vllm:inter_token_latency_seconds_count"
                        + R
                        + "), 1e-9)",
                        "{{pool}}-{{slot}}",
                    )
                ],
                "s",
            ),
            panel(
                "Prefix cache hit rate",
                [
                    (
                        "rate(vllm:prefix_cache_hits_total"
                        + R
                        + ") / clamp_min(rate(vllm:prefix_cache_queries_total"
                        + R
                        + "), 1)",
                        "{{pool}}-{{slot}}",
                    )
                ],
                "percentunit",
            ),
            panel(
                "Prompt tokens by source",
                [("sum by (pool, source) (rate(vllm:prompt_tokens_by_source_total" + R + "))", "{{pool}} {{source}}")],
                "short",
                stack=True,
                desc="Computed locally vs local prefix cache vs received from another engine (external).",
            ),
            panel(
                "Tokens /s (prompt, generated)",
                [
                    ("sum by (pool) (rate(vllm:prompt_tokens_total" + R + "))", "prompt {{pool}}"),
                    ("sum by (pool) (rate(vllm:generation_tokens_total" + R + "))", "generated {{pool}}"),
                ],
            ),
            panel(
                "Tool-call parser invocations /s",
                [("sum by (pool) (rate(vllm:tool_call_parser_invocations_total" + R + "))", "{{pool}}")],
            ),
        ],
    )
    d["kv-hop"] = dashboard(
        "kv-hop",
        "KV hop (NIXL)",
        "Where KV moves between engines, and proof it was used",
        [
            panel(
                "Hops /s by route",
                [("sum by (src, dst) (rate(orch_hop_total" + R + "))", "{{src}} -> {{dst}}")],
                "reqps",
            ),
            panel(
                "KV bytes moved /s: vLLM measured vs gateway estimate",
                [
                    ("sum by (pool) (rate(vllm:nixl_bytes_transferred_sum" + R + "))", "measured ({{pool}})"),
                    ("sum(rate(orch_hop_bytes_estimated_total" + R + "))", "upper bound: all prompt blocks (gateway)"),
                ],
                "Bps",
            ),
            panel(
                "Tokens decode received instead of computing",
                [("sum by (pool, slot) (rate(vllm:external_prefix_cache_hits_total" + R + "))", "{{pool}}-{{slot}}")],
                "short",
                desc="Proof the transferred KV was consumed: external prefix-cache hits on the decode engine.",
            ),
            panel(
                "NIXL transfer time (mean)",
                [
                    (
                        "rate(vllm:nixl_xfer_time_seconds_sum"
                        + R
                        + ") / clamp_min(rate(vllm:nixl_xfer_time_seconds_count"
                        + R
                        + "), 1e-9)",
                        "{{pool}}-{{slot}}",
                    )
                ],
                "s",
            ),
            panel(
                "Ghost KV: leases expired (never pulled)",
                [("increase(vllm:nixl_num_kv_expired_reqs_total[5m])", "{{pool}}-{{slot}}")],
                desc="Prefill built KV for a remote decode that never came (client left). Freed after kv_lease_duration.",
            ),
            panel("Failed transfers", [("increase(vllm:nixl_num_failed_transfers_total[5m])", "{{pool}}-{{slot}}")]),
            panel(
                "RoCE NIC bytes /s (node-exporter)",
                [
                    ("sum by (node) (rate(node_infiniband_port_data_transmitted_bytes_total" + R + "))", "tx {{node}}"),
                    ("sum by (node) (rate(node_infiniband_port_data_received_bytes_total" + R + "))", "rx {{node}}"),
                ],
                "Bps",
                desc="Bytes on the RDMA port. Cross-host hops show here; same-host hops do not touch the wire.",
            ),
            panel(
                "Hop tokens /s", [("sum by (src, dst) (rate(orch_hop_tokens_total" + R + "))", "{{src}} -> {{dst}}")]
            ),
        ],
    )
    d["scaling"] = dashboard(
        "scaling",
        "Scaling (KEDA, replicas, warm)",
        "Which pool scales, and when it is really warm",
        [
            panel(
                "KEDA scaler value by pool",
                [("keda_scaler_metrics_value", "{{scaledObject}}")],
                desc="prefill-slot1: requests waiting on prefill. decode-slot1: decode running + waiting. Activates at the threshold.",
            ),
            panel("KEDA scaler active", [("keda_scaler_active", "{{scaledObject}}")]),
            panel(
                "Engine replicas (desired vs available)",
                [
                    ('kube_deployment_spec_replicas{deployment=~"vllm-.*"}', "desired {{deployment}}"),
                    ('kube_deployment_status_replicas_available{deployment=~"vllm-.*"}', "available {{deployment}}"),
                ],
            ),
            panel("Warm state (0 cold, 1 warming, 2 ramping, 3 warm)", [("orch_replica_warm_state", "{{pod}}")]),
            panel("Ramp weight (share of slots open)", [("orch_replica_ramp_weight", "{{pod}}")], "percentunit"),
            panel("Warm-up request TTFT", [(q(0.99, "orch_warmup_ttft_seconds", "pod"), "p99 {{pod}}")], "s"),
        ],
    )
    return d


def check(prom: str, dashboards: dict[str, dict]) -> int:
    bad = empty = 0
    for name, dash in dashboards.items():
        for p in dash["panels"]:
            for t in p["targets"]:
                url = f"{prom}/api/v1/query?" + urllib.parse.urlencode({"query": t["expr"]})
                try:
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        data = json.load(resp)
                except Exception as exc:  # noqa: BLE001 - report every failing query, keep going
                    print(f"ERROR  {name} / {p['title']}: {exc}")
                    bad += 1
                    continue
                if not data["data"]["result"]:
                    print(f"empty  {name} / {p['title']}: {t['expr'][:110]}")
                    empty += 1
    print(f"\n{bad} failing queries, {empty} with no data yet")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", metavar="PROMETHEUS_URL")
    args = ap.parse_args()
    dashboards = build()
    if args.check:
        return check(args.check.rstrip("/"), dashboards)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, dash in dashboards.items():
        (OUT / f"{name}.json").write_text(json.dumps(dash, indent=2) + "\n")
    print(f"wrote {len(dashboards)} dashboards to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
