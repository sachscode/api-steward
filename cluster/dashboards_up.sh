#!/usr/bin/env bash
# Generate the Grafana dashboards, check every query against Prometheus, and load them into
# Grafana through the sidecar ConfigMap (label grafana_dashboard=1).
#
#   bash ~/api-steward/cluster/dashboards_up.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"

python3 "$HERE/dashboards.py"
kubectl -n monitoring port-forward svc/prometheus-server 19090:80 >/dev/null 2>&1 & PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
sleep 3
python3 "$HERE/dashboards.py" --check http://127.0.0.1:19090 || { echo "fix failing queries first"; exit 1; }

kubectl -n monitoring create configmap steward-dashboards \
  --from-file="$HERE/k8s/observability/dashboards" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n monitoring label configmap steward-dashboards grafana_dashboard=1 --overwrite
echo
echo "Grafana NodePort: $(kubectl -n monitoring get svc grafana -o jsonpath='{.spec.ports[0].nodePort}')  (user admin)"
echo "From the Mac:  ssh -i \$LAMBDA_SSH_KEY -N -L 3000:127.0.0.1:<NodePort> \$LAMBDA_PREP   then open http://127.0.0.1:3000"
