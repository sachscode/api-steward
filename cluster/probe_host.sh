#!/usr/bin/env bash
# Probe one Lambda host: GPUs, NVLink topology, NICs, RDMA (InfiniBand / RoCE), container
# runtime, storage, and an intra-host NCCL all-reduce baseline.
#
# Run on the GPU host:  bash probe_host.sh
# Output:               ~/probe/<hostname>-<timestamp>.txt
# Copy back to Mac:     scp -i $LAMBDA_SSH_KEY "$LAMBDA:~/probe/*.txt" cluster/probe/
#
# Every section runs even if an earlier one fails. Nothing here changes network or GPU config.
set -uo pipefail

OUT_DIR="$HOME/probe"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).txt"
exec > >(tee "$OUT") 2>&1

section() { printf '\n===== %s =====\n' "$1"; }
run() { printf '$ %s\n' "$*"; "$@" || printf '(exit %s)\n' "$?"; }

section "host"
run hostname
run date -u
run uname -r
run lsb_release -ds
run nproc
run free -g

section "gpu"
run nvidia-smi
run nvidia-smi --query-gpu=index,name,memory.total,pci.bus_id --format=csv
run nvidia-smi topo -m
run nvidia-smi nvlink --status -i 0
run nvcc --version

section "nic hardware"
lspci | grep -i -E 'ethernet|infiniband|mellanox|connectx' || echo "(no matching PCI devices)"
run ip -br addr
run ip route show default
for dev in $(ls /sys/class/net | grep -v -E '^(lo|docker|veth|cni|flannel|br-)'); do
  echo "--- $dev"
  ethtool -i "$dev" 2>/dev/null | grep -E 'driver|version|bus-info' || echo "(ethtool -i failed)"
  ethtool "$dev" 2>/dev/null | grep -E 'Speed|Link detected' || echo "(no speed info)"
done
lsmod | grep -E 'mlx5|mlx4|ib_core|rdma_rxe|rdma_cm' || echo "(no RDMA kernel modules loaded)"

section "rdma userspace (installs rdma-core/ibverbs-utils/perftest if missing)"
if ! command -v ibv_devinfo >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq \
    && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rdma-core ibverbs-utils perftest >/dev/null \
    || echo "(apt install failed)"
fi
run ibv_devices
ibv_devinfo 2>&1 | grep -E 'hca_id|transport|link_layer|state|active_mtu|port:' || echo "(no RDMA devices)"
run rdma link show
ls /dev/infiniband 2>/dev/null || echo "(no /dev/infiniband)"

section "container runtime"
run docker --version
run nvidia-ctk --version
docker info 2>/dev/null | grep -i -E 'runtimes|default runtime' || echo "(docker info unavailable)"
command -v k3s >/dev/null && run k3s --version || echo "(k3s not installed)"

section "storage"
run df -h
mount | grep -E 'nfs|virtiofs|/lambda|persistent' || echo "(no network/persistent filesystem mounted)"

section "nccl intra-host all-reduce (all GPUs on this host)"
NCCL_TESTS="$HOME/nccl-tests"
if [[ ! -x "$NCCL_TESTS/build/all_reduce_perf" ]]; then
  git clone -q --depth 1 https://github.com/NVIDIA/nccl-tests.git "$NCCL_TESTS" \
    && make -s -C "$NCCL_TESTS" -j"$(nproc)" CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}" >/dev/null \
    || echo "(nccl-tests build failed; check CUDA_HOME and NCCL headers)"
fi
if [[ -x "$NCCL_TESTS/build/all_reduce_perf" ]]; then
  NGPU=$(nvidia-smi -L | wc -l)
  # NCCL's debug log goes to its own file so the bandwidth table prints intact.
  NCCL_LOG="$OUT_DIR/nccl-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).log"
  NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET NCCL_DEBUG_FILE="$NCCL_LOG" timeout 300 \
    "$NCCL_TESTS/build/all_reduce_perf" -b 8 -e 1G -f 2 -g "$NGPU" -c 1 -w 5 -n 20 2>&1
  echo "--- transport lines from $NCCL_LOG"
  grep -h -E 'Using network|NET/IB : Using|GPU Direct RDMA Enabled|via P2P|NVLS|nNodes' "$NCCL_LOG" \
    | sed -E 's/^[^ ]+ \[[0-9]+\] //' | sort | uniq -c | sort -rn | head -20
fi

section "done"
echo "Saved: $OUT"
