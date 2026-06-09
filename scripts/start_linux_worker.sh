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
LLAMA_SERVER="${LLAMA_SERVER:-/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server}"

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

echo "[worker] Starting llama.cpp if needed on ${WORKER_HOST}"
remote bash -s <<REMOTE
set -euo pipefail

if ! curl --noproxy '*' -sf "http://$LLAMA_HOST:$LLAMA_PORT/health" >/dev/null 2>&1; then
  if [[ -n "$MMPROJ_PATH" && -f "$MMPROJ_PATH" ]]; then
    nohup "$LLAMA_SERVER" \
      -m "$MODEL_PATH" \
      --mmproj "$MMPROJ_PATH" \
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
<<<<<<< Updated upstream
      --spec-type draft-mtp \
      --spec-draft-n-max 2 \
=======
      --metrics \
>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
      --spec-type draft-mtp \
      --spec-draft-n-max 2 \
=======
      --metrics \
>>>>>>> Stashed changes
      >/tmp/llama-server.log 2>&1 &
  fi
fi
REMOTE

echo "[worker] Attaching Ray node to ${RAY_HEAD_IP}:${RAY_PORT}"
remote bash -s <<REMOTE
set -euo pipefail
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export RAY_grpc_enable_http_proxy=0

if ! pgrep -f raylet >/dev/null 2>&1; then
  nohup env \
    no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in" \
    NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in" \
    RAY_grpc_enable_http_proxy=0 \
    RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING=0 \
    /mnt/d/VirtualEnvironments/llm-platform/bin/ray start \
    --address="$RAY_HEAD_IP:$RAY_PORT" \
    --node-ip-address="$WORKER_HOST" \
    --num-gpus=1 \
    --num-cpus=6 \
    >/tmp/ray-worker.log 2>&1 &
  sleep 8
fi
REMOTE

echo "[wait] Waiting for ${WORKER_HOST} llama health"
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

echo "[done] Worker bootstrap completed for $WORKER_HOST"
