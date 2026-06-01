#!/usr/bin/env bash
set -euo pipefail

WORKER_HOST="${1:-10.208.211.54}"
WORKER_USER="${WORKER_USER:-administrator}"
SSH_PASS="${SSH_PASS:-Ongc@1234}"
REMOTE_DIR="${REMOTE_DIR:-/home/administrator/projects/llm-inference-service}"
RAY_HEAD_IP="${RAY_HEAD_IP:-10.208.211.62}"
RAY_PORT="${RAY_PORT:-6379}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
LLAMA_HOST="${LLAMA_HOST:-$WORKER_HOST}"
MODEL_PATH="${MODEL_PATH:-/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf}"
MMPROJ_PATH="${MMPROJ_PATH:-/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/mmproj-F16.gguf}"
VENV_PATH="${VENV_PATH:-/mnt/d/VirtualEnvironments/llm-platform}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

remote() {
  sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "$WORKER_USER@$WORKER_HOST" "$@"
}

echo "[sync] Copying gateway/worker code to ${WORKER_HOST}:${REMOTE_DIR}"
remote "mkdir -p '$REMOTE_DIR'"
sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no -r \
  "$ROOT_DIR/gateway" \
  "$ROOT_DIR/worker" \
  "$ROOT_DIR/requirements.txt" \
  "$ROOT_DIR/scripts" \
  "$WORKER_USER@$WORKER_HOST:$REMOTE_DIR/"

echo "[worker] Starting llama.cpp if needed"
remote bash -s <<REMOTE
set -euo pipefail

if ! curl --noproxy '*' -sf "http://$LLAMA_HOST:$LLAMA_PORT/health" >/dev/null 2>&1; then
  if [[ -n "$MMPROJ_PATH" && -f "$MMPROJ_PATH" ]]; then
    nohup /home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server \
      -m "$MODEL_PATH" \
      --mmproj "$MMPROJ_PATH" \
      -ngl 999 \
      -c 65536 \
      --host "$LLAMA_HOST" \
      --port "$LLAMA_PORT" \
      --parallel 2 \
      --no-context-shift \
      --flash-attn on \
      --cache-type-k q8_0 \
      --cache-type-v q8_0 \
      --cont-batching \
      >/tmp/llama-server.log 2>&1 &
  else
    nohup /home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server \
      -m "$MODEL_PATH" \
      -ngl 999 \
      -c 65536 \
      --host "$LLAMA_HOST" \
      --port "$LLAMA_PORT" \
      --parallel 2 \
      --no-context-shift \
      --flash-attn on \
      --cache-type-k q8_0 \
      --cache-type-v q8_0 \
      --cont-batching \
      >/tmp/llama-server.log 2>&1 &
  fi
fi
REMOTE

echo "[worker] Attaching Ray node to ${RAY_HEAD_IP}:${RAY_PORT}"
remote bash -s <<REMOTE
set -euo pipefail

if ! pgrep -f raylet >/dev/null 2>&1; then
  nohup /mnt/d/VirtualEnvironments/llm-platform/bin/ray start \
    --address="$RAY_HEAD_IP:$RAY_PORT" \
    --num-gpus=1 \
    --num-cpus=6 \
    >/tmp/ray-worker.log 2>&1 &
  sleep 8
fi
REMOTE

echo "[wait] Waiting for WS-03 llama health"
for _ in {1..60}; do
  if remote "curl --noproxy '*' -sf http://${LLAMA_HOST}:${LLAMA_PORT}/health >/dev/null 2>&1"; then
    break
  fi
  sleep 2
done

echo "[verify] WS-03 llama health"
remote "curl --noproxy '*' -sf http://$LLAMA_HOST:$LLAMA_PORT/health"

echo "[verify] Ray cluster status from controller"
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
ray status --address "$RAY_HEAD_IP:$RAY_PORT"

echo "[done] Worker bootstrap completed for $WORKER_HOST"
