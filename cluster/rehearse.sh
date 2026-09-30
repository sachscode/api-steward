#!/usr/bin/env bash
# Rehearsal on the A10: deploy prefill + decode engines (tiny model) with NixlConnector,
# wait for them, check RDMA visibility inside a pod, and push one request through the hop.
# Run after k3s_up.sh rehearsal.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"
bash "$HERE/stage_weights.sh" Qwen3-0.6B

echo "== render + apply engines (rehearsal profile) =="
python3 "$HERE/render_engines.py" rehearsal --image "docker.io/$IMAGE" | kubectl apply -f -

echo "== wait for slot-0 engines (first start includes CUDA graph capture) =="
start=$(date +%s)
for d in vllm-prefill-0 vllm-decode-0; do
  kubectl rollout status deploy/"$d" --timeout=15m
done
echo "engines ready after $(( $(date +%s) - start )) s"
kubectl get pods -o wide

echo "== RDMA device inside the prefill pod =="
POD="$(kubectl get pod -l app=vllm-prefill-0 -o jsonpath='{.items[0].metadata.name}')"
kubectl exec "$POD" -- ibv_devinfo 2>&1 | grep -E 'hca_id|link_layer|state' || echo "(no RDMA device visible in the pod)"

echo "== engine startup lines worth keeping =="
kubectl logs "$POD" | grep -E -i 'KV cache|maximum concurrency|nixl|ucx|graph' | tail -15 || true

echo "== prefill -> hop -> decode =="
python3 "$HERE/pd_smoke.py" --prefill http://127.0.0.1:8000 --decode http://127.0.0.1:8001

echo
echo "== KEDA =="
kubectl get scaledobject
echo "Slot-1 engines stay at 0 replicas until pool pressure crosses the threshold."
