#!/usr/bin/env bash
set -euo pipefail

WORKER_HOST="${1:-10.208.211.54}"
WORKER_USER="${WORKER_USER:-administrator}"
SSH_PASS="${SSH_PASS:-Ongc@1234}"
REMOTE_DIR="${REMOTE_DIR:-/home/administrator/projects/llm-inference-service}"
LLAMA_SERVER="${LLAMA_SERVER:-/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server}"

# ── Node type detection ────────────────────────────────────────────────────────
# Multimodal nodes (.60/.61/.63/.64/.65/.67): Qwen3-VL-8B, --parallel 2, -c 65536
# All other nodes: Gemma 4 26B QAT, --parallel 1, -c 65536
MULTIMODAL_NODE_IPS=("10.208.211.60" "10.208.211.61" "10.208.211.63" "10.208.211.64" "10.208.211.65" "10.208.211.67")

is_multimodal() {
    local ip="$1"
    for mm in "${MULTIMODAL_NODE_IPS[@]}"; do [[ "$ip" == "$mm" ]] && return 0; done
    return 1
}

if is_multimodal "$WORKER_HOST"; then
    NODE_TYPE="multimodal"
    LLAMA_PORT=8080
    MODEL_PATH="${MODEL_PATH:-/mnt/d/Models/qwen-3-vl/Qwen3VL-8B-Instruct-Q8_0.gguf}"
    MMPROJ_PATH="${MMPROJ_PATH:-/mnt/d/Models/qwen-3-vl/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf}"
else
    NODE_TYPE="text"
    LLAMA_PORT=8080
    MODEL_PATH="${MODEL_PATH:-/mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf}"
    MMPROJ_PATH=""
fi

LLAMA_HOST="${LLAMA_HOST:-$WORKER_HOST}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

remote() {
  sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "$WORKER_USER@$WORKER_HOST" "$@"
}

echo "[sync] Copying gateway code to ${WORKER_HOST}:${REMOTE_DIR} (node type: ${NODE_TYPE})"
remote "mkdir -p '$REMOTE_DIR'"
sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no -r \
  "$ROOT_DIR/gateway" \
  "$ROOT_DIR/requirements.txt" \
  "$ROOT_DIR/scripts" \
  "$WORKER_USER@$WORKER_HOST:$REMOTE_DIR/"

echo "[worker] Starting llama.cpp on ${WORKER_HOST} (${NODE_TYPE} node, port ${LLAMA_PORT})"
remote bash -s <<REMOTE
set -euo pipefail

if ! curl --noproxy '*' -sf "http://$LLAMA_HOST:$LLAMA_PORT/health" >/dev/null 2>&1; then
  if [[ "$NODE_TYPE" == "multimodal" ]]; then
    nohup "$LLAMA_SERVER" \
      -m "$MODEL_PATH" \
      --mmproj "$MMPROJ_PATH" \
      -ngl 999 \
      -c 65536 \
      --host "$LLAMA_HOST" \
      --port "$LLAMA_PORT" \
      --parallel 2 \
      --flash-attn auto \
      --cache-type-k q8_0 \
      --cache-type-v q8_0 \
      --cont-batching \
      --metrics \
      >/tmp/llama-server.log 2>&1 </dev/null &
  else
    nohup "$LLAMA_SERVER" \
      -m "$MODEL_PATH" \
      -ngl 999 \
      -c 65536 \
      --host "$LLAMA_HOST" \
      --port "$LLAMA_PORT" \
      --parallel 1 \
      --flash-attn auto \
      --cache-type-k q4_0 \
      --cache-type-v q4_0 \
      --cont-batching \
      --no-context-shift \
      --metrics \
      >/tmp/llama-server.log 2>&1 </dev/null &
  fi
fi
REMOTE

echo "[wait] Waiting for ${WORKER_HOST} llama health (port ${LLAMA_PORT})"
for _ in {1..60}; do
  if remote "curl --noproxy '*' -sf http://${LLAMA_HOST}:${LLAMA_PORT}/health >/dev/null 2>&1"; then
    break
  fi
  sleep 2
done

echo "[verify] ${WORKER_HOST} llama health"
remote "curl --noproxy '*' -sf http://$LLAMA_HOST:$LLAMA_PORT/health"

echo "[done] Worker bootstrap completed for $WORKER_HOST (${NODE_TYPE})"