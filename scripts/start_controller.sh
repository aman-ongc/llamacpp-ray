#!/usr/bin/env bash
# Start Ray head node and deploy Ray Serve on the controller (WS-11).
# Idempotent — safe to run multiple times.
set -euo pipefail

VENV="/mnt/d/VirtualEnvironments/llm-platform"
PROJECT="/home/administrator/projects/llm-inference-service"
RAY_HEAD_IP="10.208.211.62"
RAY_PORT=6379
SERVE_PORT=8001

export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"
export RAY_grpc_enable_http_proxy=0

source "${VENV}/bin/activate"

# Start Ray head if not already running.
if ! ray status --address "${RAY_HEAD_IP}:${RAY_PORT}" > /dev/null 2>&1; then
  echo "[controller] Starting Ray head..."
  ray start \
    --head \
    --port="${RAY_PORT}" \
    --dashboard-host=0.0.0.0 \
    --dashboard-port=8265 \
    --num-gpus=1 \
    --num-cpus=6
  sleep 3
else
  echo "[controller] Ray head already running."
fi

# Deploy Serve app.
echo "[controller] Deploying Ray Serve..."
cd "${PROJECT}"
python scripts/deploy_serve.py
echo "[controller] Done. Serve listening on ${RAY_HEAD_IP}:${SERVE_PORT}"
