#!/usr/bin/env bash
# One command per experiment step on the A100 host. Every run saves its own evidence under
# ~/api-steward/metrics/: the load file, a gateway scrape, /debug/state, and a meta file with
# the mode and settings, so each result can be traced to exactly how it was produced.
#
#   bash experiment.sh mode pd           prefill-0 + decode-0 (TP=4 each), KV hop over NVLink
#   bash experiment.sh mode colocated    decode-0 + decode-1, no prefill pool (same 8 GPUs, no hop)
#   bash experiment.sh mode autoscale    decode-0 only; KEDA may start decode-1 on decode pressure
#   bash experiment.sh hop 512           gateway HOP_MIN_TOKENS (0 = always hop, 1000000 = never)
#   bash experiment.sh tenants SPEC      gateway TENANTS (name:tokens_per_min:max_inflight:overflow,...)
#   bash experiment.sh run NAME PHASES [loadgen args...]
#                                        e.g. run sweep 60:2,60:8,60:20 --unique 0.5
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
source "$HERE/common.sh"
OUT="$REPO/metrics"
mkdir -p "$OUT/load"

scale() { kubectl scale deploy/"$1" --replicas="$2" >/dev/null; }
keda_hold() { kubectl annotate scaledobject "$1" autoscaling.keda.sh/paused-replicas="$2" --overwrite >/dev/null; }
keda_free() { kubectl annotate scaledobject "$1" autoscaling.keda.sh/paused-replicas- >/dev/null 2>&1 || true; }
wait_ready() {
  for d in "$@"; do
    echo "waiting for $d"
    kubectl rollout status deploy/"$d" --timeout=20m
  done
}
gateway_env() {
  kubectl set env deploy/orch-serve "$@" >/dev/null
  kubectl rollout status deploy/orch-serve --timeout=5m
  until curl -sf http://127.0.0.1:8080/healthz >/dev/null; do sleep 2; done
  sleep 5  # first scrape + warm-up check of every engine
}
state() { curl -s http://127.0.0.1:8080/debug/state; }

case "${1:-}" in
  mode)
    case "${2:?pd|colocated|autoscale}" in
      pd)
        keda_hold decode-slot1 0; keda_hold prefill-slot1 0
        scale vllm-decode-1 0; scale vllm-prefill-1 0
        scale vllm-prefill-0 1; scale vllm-decode-0 1
        wait_ready vllm-prefill-0 vllm-decode-0 ;;
      colocated)
        keda_hold prefill-slot1 0; keda_hold decode-slot1 1
        scale vllm-prefill-1 0; scale vllm-prefill-0 0
        scale vllm-decode-0 1; scale vllm-decode-1 1
        wait_ready vllm-decode-0 vllm-decode-1 ;;
      autoscale)
        keda_hold prefill-slot1 0
        scale vllm-prefill-1 0; scale vllm-prefill-0 0; scale vllm-decode-1 0
        keda_free decode-slot1
        scale vllm-decode-0 1
        wait_ready vllm-decode-0 ;;
      *) echo "unknown mode" >&2; exit 2 ;;
    esac
    # Changing MODE_NOTE rolls the gateway, which then re-scrapes every engine and warms new ones.
    gateway_env MODE_NOTE="$2"
    echo "$2" > "$OUT/.mode"
    kubectl get pods -o wide | grep -E "NAME|vllm|orch"
    ;;
  hop)
    gateway_env HOP_MIN_TOKENS="${2:?tokens}"
    echo "HOP_MIN_TOKENS=$2" ;;
  tenants)
    gateway_env TENANTS="${2:?spec}"
    echo "TENANTS=$2" ;;
  run)
    NAME="${2:?name}"; PHASES="${3:?seconds:jobs_per_s,...}"; shift 3
    TAG="a100-$NAME"
    START=$(date +%s)
    state > "$OUT/$TAG-state-before.json"
    (cd "$REPO" && python3 -m app.loadgen --phases "$PHASES" --max-workers 512 --out "metrics/load/$TAG.jsonl" "$@") \
      | tee "$OUT/$TAG-summary.txt"
    END=$(date +%s)
    curl -s http://127.0.0.1:8080/metrics > "$OUT/$TAG-gateway.prom"
    state > "$OUT/$TAG-state-after.json"
    kubectl get deploy orch-serve -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
      | grep -E "HOP_MIN_TOKENS|TENANTS|MODE_NOTE|SLOT_CAP" > "$OUT/$TAG-gateway-env.txt" || true
    python3 - "$OUT/$TAG-meta.json" "$NAME" "$PHASES" "$START" "$END" "$(cat "$OUT/.mode" 2>/dev/null || echo unknown)" "$*" <<'EOF'
import json, sys
path, name, phases, start, end, mode, extra = sys.argv[1:]
json.dump({"name": name, "phases": phases, "start": int(start), "end": int(end), "mode": mode, "loadgen_args": extra},
          open(path, "w"), indent=2)
EOF
    echo "saved: metrics/load/$TAG.jsonl and metrics/$TAG-*" ;;
  *)
    sed -n 2,14p "$0"; exit 2 ;;
esac
