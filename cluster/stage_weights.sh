#!/usr/bin/env bash
# Copy model weights from steward-fs to this host's local NVMe with parallel streams.
#
#   bash ~/cluster/stage_weights.sh Qwen3-30B-A3B [more models...]
#
# steward-fs reads ~74 MB/s per stream but ~630 MB/s with 16 streams (A10 test, 2026-09-30),
# so one parallel copy at boot (~1.6 min for 61 GB) beats every engine reading the network
# filesystem (~14 min each). Engines then mount LOCAL_MODELS read-only.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"
FS="$(steward_fs)"
LOCAL_MODELS="${LOCAL_MODELS:-/opt/steward/models}"
STREAMS="${STREAMS:-16}"
[[ $# -gt 0 ]] || { echo "usage: $0 MODEL [MODEL...]" >&2; exit 2; }

sudo mkdir -p "$LOCAL_MODELS" && sudo chown "$(id -u):$(id -g)" "$LOCAL_MODELS"
# Top-level files only: hf download also leaves a .cache/ folder that engines do not need.
files_bytes() { find "$1" -maxdepth 1 -type f -printf '%s\n' | awk '{s+=$1} END {print s+0}'; }

for model in "$@"; do
  src="$FS/models/$model"; dst="$LOCAL_MODELS/$model"
  [[ -d "$src" ]] || { echo "missing $src" >&2; exit 1; }
  src_bytes="$(files_bytes "$src")"
  if [[ -d "$dst" && "$(files_bytes "$dst")" == "$src_bytes" ]]; then
    echo "[stage] $model already local ($dst)"; continue
  fi
  mkdir -p "$dst"
  start=$(date +%s)
  # Small files (config, tokenizer) first, then the big shards in parallel, largest first.
  find "$src" -maxdepth 1 -type f ! -name '*.safetensors' -exec cp -p {} "$dst/" \;
  ls -S "$src"/*.safetensors | xargs -P "$STREAMS" -I{} cp -p {} "$dst/"
  secs=$(( $(date +%s) - start )); (( secs > 0 )) || secs=1
  dst_bytes="$(files_bytes "$dst")"
  [[ "$dst_bytes" == "$src_bytes" ]] || { echo "[stage] size mismatch for $model: $dst_bytes vs $src_bytes" >&2; exit 1; }
  echo "[stage] $model: $(( src_bytes / 1000000000 )) GB in ${secs} s = $(( src_bytes / 1000000 / secs )) MB/s -> $dst"
done
