#!/usr/bin/env bash
# One-time prep on a cheap 1x A10 host with the steward-fs filesystem attached:
#   1. download model weights to steward-fs (big MoE + tiny rehearsal model)
#   2. build the vLLM + NIXL engine image and save it to steward-fs
#   3. measure how fast steward-fs reads (sets engine load time on the A100s)
#   4. write a manifest of what is on the filesystem
#
# Copy the cluster/ folder to the host, then:  bash ~/cluster/a10_prep.sh
# Re-runnable: finished downloads and an existing image tar are skipped.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VLLM_VERSION="${VLLM_VERSION:-v0.30.0}"
IMAGE="steward/vllm-nixl:${VLLM_VERSION}"
BIG_MODEL="${BIG_MODEL:-Qwen/Qwen3-30B-A3B}"
SMALL_MODEL="${SMALL_MODEL:-Qwen/Qwen3-0.6B}"

# --- locate steward-fs ---------------------------------------------------------------
FS="${STEWARD_FS:-}"
if [[ -z "$FS" ]]; then
  FS="$(findmnt -rn -o TARGET | grep -m1 'steward-fs' || true)"
fi
if [[ -z "$FS" || ! -d "$FS" ]]; then
  echo "steward-fs is not mounted. Launch the instance with the filesystem attached," >&2
  echo "or set STEWARD_FS=/path/to/mount. Current mounts:" >&2
  df -h >&2
  exit 1
fi
mkdir -p "$FS/models" "$FS/images" "$FS/logs"
LOG="$FS/logs/a10_prep-$(date -u +%Y%m%dT%H%M%SZ).txt"
exec > >(tee "$LOG") 2>&1
echo "steward-fs: $FS"
df -h "$FS"

# --- 1. weights (background) -------------------------------------------------------
if ! command -v hf >/dev/null 2>&1; then
  python3 -m pip install -q --user -U "huggingface_hub[hf_xet]" || python3 -m pip install -q -U "huggingface_hub[hf_xet]"
  export PATH="$HOME/.local/bin:$PATH"
fi
download() {
  local repo="$1" dest="$FS/models/$(basename "$1")"
  echo "[weights] $repo -> $dest"
  # Skip *.pth and original/ folders; vLLM loads safetensors.
  hf download "$repo" --local-dir "$dest" \
    > "$FS/logs/download-$(basename "$1").log" 2>&1 \
    && echo "[weights] done $repo ($(du -sh "$dest" | cut -f1))" \
    || echo "[weights] FAILED $repo, see $FS/logs/download-$(basename "$1").log"
}
download "$SMALL_MODEL" & PID_SMALL=$!
download "$BIG_MODEL" & PID_BIG=$!

# --- 2. engine image (foreground, while weights download) ----------------------------
TAR="$FS/images/steward-vllm-nixl-${VLLM_VERSION}.tar"
if [[ -s "$TAR" ]]; then
  echo "[image] $TAR already exists, skipping build"
else
  echo "[image] building $IMAGE"
  if sudo docker build --build-arg VLLM_VERSION="$VLLM_VERSION" -t "$IMAGE" \
       -f "$HERE/Dockerfile.vllm-nixl" "$HERE"; then
    echo "[image] saving to $TAR"
    sudo docker save "$IMAGE" -o "$TAR.partial" && mv "$TAR.partial" "$TAR"
    sudo chown "$(id -u):$(id -g)" "$TAR" 2>/dev/null || true
    ls -lh "$TAR"
  else
    echo "[image] BUILD FAILED"
  fi
fi

# --- wait for downloads ------------------------------------------------------------
echo "[weights] waiting for downloads"
wait "$PID_SMALL" "$PID_BIG"

# --- 3. filesystem read speed ------------------------------------------------------
BIG_DIR="$FS/models/$(basename "$BIG_MODEL")"
SHARD="$(ls -S "$BIG_DIR"/*.safetensors 2>/dev/null | head -1)"
if [[ -n "$SHARD" ]]; then
  echo "[fs] cold read of $(basename "$SHARD") ($(du -h "$SHARD" | cut -f1))"
  sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
  dd if="$SHARD" of=/dev/null bs=16M 2>&1 | tail -1
  TOTAL_GB="$(du -sb "$BIG_DIR" | awk '{printf "%.1f", $1/1e9}')"
  echo "[fs] full model is ${TOTAL_GB} GB; load time ≈ size ÷ the read rate above (per engine, if read in parallel they share the filesystem)"
fi

# --- 4. manifest -------------------------------------------------------------------
{
  echo "# steward-fs manifest, $(date -u)"
  echo "image: $IMAGE"
  [[ -s "$TAR" ]] && echo "image_tar: $TAR ($(du -h "$TAR" | cut -f1))"
  [[ -s "$TAR" ]] && echo "image_tar_sha256: $(sha256sum "$TAR" | cut -d' ' -f1)"
  for d in "$FS"/models/*/; do
    echo "model: $d ($(du -sh "$d" | cut -f1), $(ls "$d"/*.safetensors 2>/dev/null | wc -l) safetensors files)"
    [[ -f "$d/config.json" ]] && python3 -c "import json;c=json.load(open('$d/config.json'));print('  layers',c.get('num_hidden_layers'),'kv_heads',c.get('num_key_value_heads'),'head_dim',c.get('head_dim'),'experts',c.get('num_experts'))"
  done
} | tee "$FS/MANIFEST.txt"

echo
echo "Done. Log: $LOG"
echo "Next on this host: bash ~/cluster/k3s_up.sh rehearsal"
