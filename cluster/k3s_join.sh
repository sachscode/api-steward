#!/usr/bin/env bash
# Join host B to host A's k3s cluster as the decode node.
#
#   bash ~/cluster/k3s_join.sh <host A private IP> <node token>
#
# Get both values from host A after k3s_up.sh prefill. Pass the token on the command line
# only inside the SSH session; do not commit it or put it in .env.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"
SERVER="${1:?host A private IP}"; TOKEN="${2:?node token}"
FS="$(steward_fs)"
bash "$HERE/stage_weights.sh" Qwen3-30B-A3B > /tmp/stage_weights.log 2>&1 & STAGE_PID=$!

if ! command -v k3s >/dev/null 2>&1; then
  NODE_IP="$(ip -4 -o addr show "$(ip route show default | awk '{print $5; exit}')" | awk '{print $4}' | cut -d/ -f1)"
  curl -sfL https://get.k3s.io | K3S_URL="https://$SERVER:6443" K3S_TOKEN="$TOKEN" \
    INSTALL_K3S_EXEC="agent --default-runtime nvidia --node-ip $NODE_IP --node-label steward/pool=decode --node-label gpu=on" sh -
fi

echo "== engine image into containerd =="
if sudo k3s ctr images ls -q | grep -q "docker.io/${IMAGE}"; then
  echo "already imported: $IMAGE"
else
  time sudo k3s ctr images import "$FS/images/steward-vllm-nixl-${VLLM_VERSION}.tar"
fi
echo "== waiting for weight staging =="
wait "$STAGE_PID" && cat /tmp/stage_weights.log || { cat /tmp/stage_weights.log; echo "staging FAILED"; exit 1; }
echo "Joined. On host A check:  kubectl get nodes -L steward/pool"
