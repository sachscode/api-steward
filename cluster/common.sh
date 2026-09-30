# Shared helpers for scripts that run on the Lambda hosts. Source, do not execute.

# steward-fs mount point: STEWARD_FS wins, otherwise the first mount whose path contains steward-fs.
steward_fs() {
  local fs="${STEWARD_FS:-}"
  [[ -z "$fs" ]] && fs="$(findmnt -rn -o TARGET | grep -m1 'steward-fs' || true)"
  if [[ -z "$fs" || ! -d "$fs" ]]; then
    echo "steward-fs is not mounted. Launch the host with the filesystem attached, or set STEWARD_FS." >&2
    return 1
  fi
  echo "$fs"
}

VLLM_VERSION="${VLLM_VERSION:-v0.30.0}"
IMAGE="steward/vllm-nixl:${VLLM_VERSION}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

# Run a helm command while printing the namespace's pods every 15 s, since helm --wait is silent.
helm_wait() {
  local ns="$1"; shift
  ( while true; do echo "-- $(date -u +%H:%M:%S) $ns --"; kubectl -n "$ns" get pods 2>/dev/null || true; sleep 15; done ) &
  local watch=$!
  set +e; "$@"; local rc=$?; set -e
  kill "$watch" 2>/dev/null || true; wait "$watch" 2>/dev/null || true
  return "$rc"
}
