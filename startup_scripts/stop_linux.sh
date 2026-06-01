#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/administrator/projects/llm-inference-service}"
VENV_DIR="${VENV_DIR:-/mnt/d/VirtualEnvironments/llm-platform}"
SUDO_PASS="${SUDO_PASS:-Ongc@1234}"
WORKERS="${WORKERS:-10.208.211.54 10.208.211.59 10.208.211.64}"
SSH_PASS="${SSH_PASS:-Ongc@1234}"

export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"
export RAY_grpc_enable_http_proxy=0

FREE_PORTS=(8001 8080 6379 8265)

free_ports_local() {
  for port in "${FREE_PORTS[@]}"; do
    if fuser "${port}/tcp" >/dev/null 2>&1; then
      echo "[stop] Killing process on local :${port}"
      fuser -k "${port}/tcp" 2>/dev/null || true
    fi
  done
}

stop_worker() {
  local host="$1"
  if ! command -v sshpass >/dev/null 2>&1; then
    return 0
  fi
  sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    "administrator@$host" "
      pkill -f raylet 2>/dev/null || true
      pkill -f gcs_server 2>/dev/null || true
      pkill -f plasma_store 2>/dev/null || true
      pkill -f llama-server 2>/dev/null || true
      if [ -x '${VENV_DIR}/bin/ray' ]; then '${VENV_DIR}/bin/ray' stop --force 2>/dev/null || true; fi
      for port in 8001 8080 6379; do fuser -k \${port}/tcp 2>/dev/null || true; done
    " >/dev/null 2>&1 || true
}

echo "[stop] Stopping worker nodes"
for worker in $WORKERS; do
  stop_worker "$worker" &
done
wait

echo "[stop] Killing Ray processes on controller"
# Kill port holders FIRST before Ray tries to re-bind anything
free_ports_local

# Force-kill all Ray and Serve processes — skip graceful serve.delete()
# which re-initializes Ray and causes port bind errors
pkill -f "raylet" 2>/dev/null || true
pkill -f "ray::" 2>/dev/null || true
pkill -f "serve" 2>/dev/null || true
pkill -f "gcs_server" 2>/dev/null || true
pkill -f "plasma_store" 2>/dev/null || true
pkill -f "monitor.py" 2>/dev/null || true
pkill -f "dashboard" 2>/dev/null || true

if [ -d "$VENV_DIR" ]; then
  source "$VENV_DIR/bin/activate"
  ray stop --force 2>/dev/null || true
fi

echo "[stop] Stopping llama.cpp on controller"
pkill -f llama-server || true

echo "[stop] Freeing ports on controller (second pass)"
sleep 2
free_ports_local

echo "[stop] Stopping Docker Compose stack"
cd "$PROJECT_DIR"
printf '%s\n' "$SUDO_PASS" | sudo -S docker compose down

echo "[stop] Verifying ports released"
for port in 8001 8080; do
  if fuser "${port}/tcp" >/dev/null 2>&1; then
    echo "[warn] Port ${port} still in use — forcing kill"
    sudo fuser -k "${port}/tcp" 2>/dev/null || true
  fi
done

echo "[stop] Platform stopped"
