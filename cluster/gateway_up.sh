#!/usr/bin/env bash
# Build (once) and import the gateway image, then (re)deploy orch-serve.
#
#   bash ~/api-steward/cluster/gateway_up.sh rehearsal|a100
#
# Expects the repo synced to ~/api-steward. After a code change: rsync again, then
#   kubectl rollout restart deploy/orch-serve
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"
PROFILE="${1:?rehearsal|a100}"
REPO="$(cd "$HERE/.." && pwd)"
GW_IMAGE="steward/gateway:dev"

if sudo k3s ctr images ls -q | grep -q "docker.io/$GW_IMAGE"; then
  echo "gateway image already in containerd"
else
  echo "== build $GW_IMAGE =="
  sudo docker build -q -t "$GW_IMAGE" -f "$HERE/Dockerfile.gateway" "$REPO"
  sudo docker save "$GW_IMAGE" | sudo k3s ctr images import -
fi
mkdir -p "$REPO/metrics/traces"

EXISTED=$(kubectl get deploy orch-serve -o name 2>/dev/null || true)
python3 "$HERE/render_engines.py" "$PROFILE" --gateway-only --repo "$REPO" | kubectl apply -f -
# Restart only an existing gateway, so it picks up code synced into the mounted repo.
[[ -n "$EXISTED" ]] && kubectl rollout restart deploy/orch-serve >/dev/null
kubectl rollout status deploy/orch-serve --timeout=3m
sleep 5
curl -s http://127.0.0.1:8080/healthz; echo
echo "Gateway: http://127.0.0.1:8080 on this node (hostPort) and NodePort 30080."
echo "Logs:    kubectl logs deploy/orch-serve -f"
