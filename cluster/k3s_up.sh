#!/usr/bin/env bash
# Bring up the k3s control plane and cluster add-ons on this host.
#
#   bash ~/cluster/k3s_up.sh rehearsal   # A10: one node plays prefill and decode
#   bash ~/cluster/k3s_up.sh prefill     # host A: k3s server, prefill pool, gateway, monitoring
#   bash ~/cluster/k3s_up.sh single      # one 8-GPU host runs both pools (prefill GPUs 0-3, decode 4-7)
#
# Installs: k3s (nvidia default runtime), helm, HAMi (GPU device plugin + scheduler), KEDA,
# Prometheus, Grafana, DCGM exporter; imports the engine image from steward-fs.
# Host B joins later with k3s_join.sh.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"

POOL="${1:-rehearsal}"
case "$POOL" in rehearsal|prefill|single) ;; *) echo "usage: $0 rehearsal|prefill|single" >&2; exit 2 ;; esac
FS="$(steward_fs)"
TAR="$FS/images/steward-vllm-nixl-${VLLM_VERSION}.tar"
[[ -s "$TAR" ]] || { echo "Missing $TAR. Run a10_prep.sh first." >&2; exit 1; }

MODEL=$([[ "$POOL" == "rehearsal" ]] && echo Qwen3-0.6B || echo Qwen3-30B-A3B)
echo "== staging $MODEL to local NVMe in the background =="
bash "$HERE/stage_weights.sh" "$MODEL" > /tmp/stage_weights.log 2>&1 & STAGE_PID=$!

echo "== GPU =="
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader

echo "== k3s server =="
if ! command -v k3s >/dev/null 2>&1; then
  NODE_IP="$(ip -4 -o addr show "$(ip route show default | awk '{print $5; exit}')" | awk '{print $4}' | cut -d/ -f1)"
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --write-kubeconfig-mode 644 \
    --default-runtime nvidia --node-ip $NODE_IP --node-label steward/pool=$POOL" sh -
fi
until kubectl get nodes >/dev/null 2>&1; do sleep 2; done
kubectl wait --for=condition=Ready node --all --timeout=180s
NODE="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
kubectl label node "$NODE" gpu=on steward/pool="$POOL" --overwrite

echo "== engine image into containerd =="
if sudo k3s ctr images ls -q | grep -q "docker.io/${IMAGE}"; then
  echo "already imported: $IMAGE"
else
  time sudo k3s ctr images import "$TAR"
fi

if ! command -v helm >/dev/null 2>&1; then
  curl -sfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
helm repo add hami-charts https://project-hami.github.io/HAMi/ >/dev/null
helm repo add kedacore https://kedacore.github.io/charts >/dev/null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update >/dev/null

echo "== HAMi =="
# Rehearsal shares one A10 between 4 small engines (4 virtual GPUs per card). On the A100 hosts
# each engine takes 4 WHOLE GPUs for TP=4, so one virtual GPU must equal one physical GPU;
# with a split of 4, HAMi could place all four TP ranks on the same 40 GB card.
SPLIT=$([[ "$POOL" == "rehearsal" ]] && echo 4 || echo 1)
K8S_VERSION="$(kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"].split("+")[0])')"
helm_wait kube-system helm upgrade --install hami hami-charts/hami --version 2.9.0 \
  --namespace kube-system \
  --set scheduler.kubeScheduler.image.registry=registry.k8s.io \
  --set scheduler.kubeScheduler.image.repository=kube-scheduler \
  --set "scheduler.kubeScheduler.image.tag=${K8S_VERSION}" \
  --set "scheduler.kubeScheduler.imageTag=${K8S_VERSION}" \
  --set devicePlugin.deviceSplitCount="$SPLIT" \
  --wait --timeout 10m

echo "== KEDA =="
helm_wait keda helm upgrade --install keda kedacore/keda --namespace keda --create-namespace \
  --set prometheus.operator.enabled=true --set prometheus.metricServer.enabled=true \
  --wait --timeout 10m

echo "== Prometheus + Grafana + DCGM =="
helm_wait monitoring helm upgrade --install prometheus prometheus-community/prometheus \
  --namespace monitoring --create-namespace \
  --values "$HERE/k8s/observability/prometheus-values.yaml" --wait --timeout 10m
helm_wait monitoring helm upgrade --install grafana grafana/grafana --namespace monitoring \
  --values "$HERE/k8s/observability/grafana-values.yaml" --wait --timeout 10m
kubectl apply -f "$HERE/k8s/observability/dcgm-exporter.yaml"

echo "== waiting for weight staging =="
wait "$STAGE_PID" && cat /tmp/stage_weights.log || { cat /tmp/stage_weights.log; echo "staging FAILED"; exit 1; }

echo
kubectl get nodes -o wide
kubectl describe node "$NODE" | grep -E 'nvidia.com/gpu' | head -3
echo
echo "Grafana NodePort and admin password:"
kubectl -n monitoring get svc grafana -o jsonpath='{.spec.ports[0].nodePort}{"\n"}'
kubectl -n monitoring get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
if [[ "$POOL" == "prefill" ]]; then
  echo
  echo "Join token for host B (keep private):  sudo cat /var/lib/rancher/k3s/server/node-token"
  echo "This host's private IP for K3S_URL:    $(kubectl get node "$NODE" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')"
fi
echo "Next: bash ~/cluster/rehearse.sh   (rehearsal)  or  apply the A100 engine manifests"
