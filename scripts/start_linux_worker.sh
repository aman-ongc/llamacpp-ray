#!/usr/bin/env bash
set -euo pipefail

WORKER_HOST="${1:-10.208.211.54}"
WORKER_USER="${WORKER_USER:-administrator}"
SSH_PASS="${SSH_PASS:-Ongc@1234}"
REMOTE_DIR="${REMOTE_DIR:-/home/administrator/projects/llm-inference-service}"
RAY_HEAD_IP="${RAY_HEAD_IP:-10.208.211.62}"
RAY_PORT="${RAY_PORT:-6379}"
VENV_PATH="${VENV_PATH:-/mnt/d/VirtualEnvironments/llm-platform}"
LLAMA_SERVER="${LLAMA_SERVER:-/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server}"

# ── Node type detection ────────────────────────────────────────────────────────
# WS-13 (10.208.211.64) = multimodal node: Qwen3-VL-8B, port 8080, with mmproj
# All other nodes      = text nodes: Gemma 4 26B QAT, port 8080, no mmproj
MULTIMODAL_NODE_IP="10.208.211.64"

if [[ "$WORKER_HOST" == "$MULTIMODAL_NODE_IP" ]]; then
    NODE_TYPE="multimodal"
    LLAMA_PORT=8080
    MODEL_PATH="${MODEL_PATH:-/mnt/d/Models/qwen-3-vl/Qwen3VL-8B-Instruct-Q8_0.gguf}"
    MMPROJ_PATH="${MMPROJ_PATH:-/mnt/d/Models/qwen-3-vl/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf}"
    RAY_RESOURCE='{"multimodal_node": 1}'
else
    NODE_TYPE="text"
    LLAMA_PORT=8080
    MODEL_PATH="${MODEL_PATH:-/mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf}"
    MMPROJ_PATH=""
    RAY_RESOURCE='{"text_node": 1}'
fi

LLAMA_HOST="${LLAMA_HOST:-$WORKER_HOST}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

remote() {
  sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "$WORKER_USER@$WORKER_HOST" "$@"
}

echo "[sync] Copying gateway/worker code to ${WORKER_HOST}:${REMOTE_DIR} (node type: ${NODE_TYPE})"
remote "mkdir -p '$REMOTE_DIR'"
sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no -r \
  "$ROOT_DIR/gateway" \
  "$ROOT_DIR/worker" \
  "$ROOT_DIR/requirements.txt" \
  "$ROOT_DIR/scripts" \
  "$WORKER_USER@$WORKER_HOST:$REMOTE_DIR/"

echo "[worker] Attaching Ray node to ${RAY_HEAD_IP}:${RAY_PORT} (resources: ${RAY_RESOURCE})"
remote bash -s <<REMOTE
set -euo pipefail
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export RAY_grpc_enable_http_proxy=0

if ! pgrep -f raylet >/dev/null 2>&1; then
  # CPU-only Ray worker — GPU is owned exclusively by llama-server on this node.
  # Custom resource tag pins the correct Serve replicas to this node type.
  nohup env \
    no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in" \
    NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in" \
    RAY_grpc_enable_http_proxy=0 \
    RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING=0 \
    /mnt/d/VirtualEnvironments/llm-platform/bin/ray start \
    --address="$RAY_HEAD_IP:$RAY_PORT" \
    --node-ip-address="$WORKER_HOST" \
    --num-gpus=0 \
    --num-cpus=6 \
    --resources='$RAY_RESOURCE' \
    >/tmp/ray-worker.log 2>&1 &
  sleep 12
fi
REMOTE

echo "[worker] Starting llama.cpp on ${WORKER_HOST} (${NODE_TYPE} node, port ${LLAMA_PORT})"
remote bash -s <<REMOTE
set -euo pipefail

if ! curl --noproxy '*' -sf "http://$LLAMA_HOST:$LLAMA_PORT/health" >/dev/null 2>&1; then
  if [[ "$NODE_TYPE" == "multimodal" ]]; then
    nohup "$LLAMA_SERVER" \
      -m "$MODEL_PATH" \
      --mmproj "$MMPROJ_PATH" \
      -ngl 999 \
      -c 32768 \
      --host "$LLAMA_HOST" \
      --port "$LLAMA_PORT" \
      --parallel 2 \
      --flash-attn auto \
      --cache-type-k q8_0 \
      --cache-type-v q8_0 \
      --cont-batching \
      >/tmp/llama-server.log 2>&1 &
  else
    nohup "$LLAMA_SERVER" \
      -m "$MODEL_PATH" \
      -ngl 999 \
      -c 65536 \
      --host "$LLAMA_HOST" \
      --port "$LLAMA_PORT" \
      --parallel 1 \
      --no-context-shift \
      --flash-attn auto \
      --cache-type-k q4_0 \
      --cache-type-v q4_0 \
      --cont-batching \
      >/tmp/llama-server.log 2>&1 &
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

echo "[verify] Ray cluster status from controller"
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
ray status --address "$RAY_HEAD_IP:$RAY_PORT"

echo "[done] Worker bootstrap completed for $WORKER_HOST (${NODE_TYPE})"
