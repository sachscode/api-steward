#!/usr/bin/env bash
# Cross-host network evidence, run BEFORE k3s so nothing else is on the wire.
# Run the same command on both hosts, with the other host's private IP:
#
#   host B:  bash ~/cluster/two_host_net.sh server <host A private IP>
#   host A:  bash ~/cluster/two_host_net.sh client <host B private IP>
#
# 1. ib_write_bw over RoCE (rdma_cm), message sizes 2 B .. 8 MiB: raw RDMA ceiling.
# 2. NCCL all-reduce across both hosts (16 GPUs) over RoCE, then again with NCCL_IB_DISABLE=1
#    (TCP sockets), inside the engine image. Host A is the torchrun master (node rank 0).
# Output goes to steward-fs/logs so both hosts' results survive termination.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"
ROLE="${1:?server|client}"; PEER="${2:?peer private IP}"
FS="$(steward_fs)" || exit 1
LOG="$FS/logs/two_host_net-$ROLE-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).txt"
exec > >(tee "$LOG") 2>&1
ME="$(ip -4 -o addr show "$(ip route show default | awk '{print $5; exit}')" | awk '{print $4}' | cut -d/ -f1)"
echo "role=$ROLE me=$ME peer=$PEER"

command -v ib_write_bw >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq perftest ibverbs-utils >/dev/null

echo "== 1. ib_write_bw (RoCE via rdma_cm) =="
if [[ "$ROLE" == "server" ]]; then
  timeout 120 ib_write_bw -d mlx5_0 -R -a -F --report_gbits
else
  sleep 3
  timeout 120 ib_write_bw -d mlx5_0 -R -a -F --report_gbits "$PEER"
fi

echo "== 2. NCCL all-reduce across hosts =="
if ! sudo docker image inspect "$IMAGE" >/dev/null 2>&1; then
  sudo docker load -i "$FS/images/steward-vllm-nixl-${VLLM_VERSION}.tar"
fi
RANK=$([[ "$ROLE" == "client" ]] && echo 0 || echo 1)   # client = host A = master
MASTER=$([[ "$ROLE" == "client" ]] && echo "$ME" || echo "$PEER")
run_nccl() {
  local label="$1"; shift
  echo "--- $label"
  sudo docker run --rm --gpus all --network host --ipc host --privileged \
    --ulimit memlock=-1 --device /dev/infiniband -v "$HERE:/cluster:ro" \
    -e NCCL_DEBUG=INFO -e NCCL_DEBUG_SUBSYS=INIT,NET -e NCCL_SOCKET_IFNAME="$(ip route show default | awk '{print $5; exit}')" \
    "$@" --entrypoint torchrun "$IMAGE" \
    --nnodes 2 --nproc-per-node 8 --node-rank "$RANK" \
    --master-addr "$MASTER" --master-port 29500 /cluster/nccl_allreduce.py 2>&1 \
    | grep -E '^#|^\s+[0-9]|NET/IB : Using|NET/Socket : Using|Using network|GPU Direct RDMA Enabled|via NET' | sed -E 's/^[^ ]+ \[[0-9]+\] //' | awk '!seen[$0]++' | head -60
}
run_nccl "RoCE (NCCL IB transport)"
sleep 5
run_nccl "TCP sockets (NCCL_IB_DISABLE=1)" -e NCCL_IB_DISABLE=1

echo "Saved: $LOG"
