#!/usr/bin/env bash
# Start Ray head node (with text_node resource) and deploy Ray Serve on the controller (WS-11).
# Also starts Gemma 4 26B QAT on port 8080 if not already running.
# Idempotent — safe to run multiple times.
set -euo pipefail

VENV="/mnt/d/VirtualEnvironments/llm-platform"
PROJECT="/home/administrator/projects/llm-inference-service"
LLAMA_SERVER="/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server"
RAY_HEAD_IP="10.208.211.62"
RAY_PORT=6379
SERVE_PORT=8001
TEXT_LLAMA_PORT=8080
TEXT_MODEL="/mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf"

export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"
export RAY_grpc_enable_http_proxy=0

# When true: WS-11 joins text pool (llama-server started, text_node resource registered).
# When false (default): WS-11 is head-only — no llama-server, no text requests.
CONTROLLER_AS_WORKER="${CONTROLLER_AS_WORKER:-false}"

source "${VENV}/bin/activate"

# Start Ray head if not already running.
if ! ray status --address "${RAY_HEAD_IP}:${RAY_PORT}" > /dev/null 2>&1; then
  if [[ "$CONTROLLER_AS_WORKER" == "true" ]]; then
    HEAD_RESOURCES='{"text_node": 1}'
    echo "[controller] Starting Ray head (text_node resource enabled — WS-11 is a worker)..."
  else
    HEAD_RESOURCES='{}'
    echo "[controller] Starting Ray head (head-only — WS-11 not in text pool)..."
  fi
  # CPU-only head — GPU owned exclusively by llama-server, not Ray.
  ray start \
    --head \
    --node-ip-address="${RAY_HEAD_IP}" \
    --port="${RAY_PORT}" \
    --dashboard-host=0.0.0.0 \
    --dashboard-port=8265 \
    --num-gpus=0 \
    --num-cpus=6 \
    --resources="$HEAD_RESOURCES"
  sleep 3
else
  echo "[controller] Ray head already running."
fi

# Start Gemma 4 26B QAT only when CONTROLLER_AS_WORKER=true.
if [[ "$CONTROLLER_AS_WORKER" == "true" ]]; then
  if ! curl --noproxy '*' -sf "http://${RAY_HEAD_IP}:${TEXT_LLAMA_PORT}/health" >/dev/null 2>&1; then
    echo "[controller] Starting Gemma 4 26B QAT on port ${TEXT_LLAMA_PORT}..."
    nohup "$LLAMA_SERVER" \
      -m "$TEXT_MODEL" \
      -ngl 999 -c 65536 \
      --host "$RAY_HEAD_IP" --port "$TEXT_LLAMA_PORT" \
      --parallel 1 --no-context-shift \
      --flash-attn auto --cache-type-k q4_0 --cache-type-v q4_0 \
      --cont-batching \
      >/tmp/llama-server-ws11.log 2>&1 &
    echo "[controller] Gemma launched (log: /tmp/llama-server-ws11.log)"
  else
    echo "[controller] Gemma already running on port ${TEXT_LLAMA_PORT}."
  fi
else
  echo "[controller] Controller-as-worker disabled — skipping llama-server on WS-11."
fi

# Deploy Serve apps.
echo "[controller] Deploying Ray Serve..."
cd "${PROJECT}"
python scripts/deploy_serve.py
echo "[controller] Done. Serve listening on ${RAY_HEAD_IP}:${SERVE_PORT}"
