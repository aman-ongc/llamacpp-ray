#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/administrator/projects/llm-inference-service}"
SUDO_PASS="${SUDO_PASS:-Ongc@1234}"
WORKERS="${WORKERS:-10.208.211.52 10.208.211.53 10.208.211.54 10.208.211.55 10.208.211.56 10.208.211.57 10.208.211.58 10.208.211.59 10.208.211.60 10.208.211.61 10.208.211.63 10.208.211.64 10.208.211.65 10.208.211.67}"
SSH_PASS="${SSH_PASS:-Ongc@1234}"

export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"

_ssh() {
  local host="$1"; shift
  if command -v sshpass >/dev/null 2>&1; then
    sshpass -p "$SSH_PASS" ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=5 "administrator@$host" "$@"
  else
    ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes "administrator@$host" "$@"
  fi
}

kill_llama_remote() {
  local host="$1"
  echo "[stop] Killing llama-server on $host..."
  _ssh "$host" "pkill llama-server || true; sleep 1; pkill -9 llama-server || true; echo killed" 2>&1 || echo "[warn] Could not reach $host — llama-server may still hold GPU"
}

stop_watchdog() {
  # Must run FIRST, before anything else is killed — the watchdog's job is to
  # detect dead llama-server processes and restart them. If it's still
  # running while we tear down the rest of the stack, it will race us and
  # bring processes back up mid-shutdown.
  echo "[stop] Stopping llama-server watchdog on controller..."
  if [ -f /tmp/llama-watchdog.pid ]; then
    kill -9 "$(cat /tmp/llama-watchdog.pid)" 2>/dev/null || true
    rm -f /tmp/llama-watchdog.pid
  fi
  pkill -9 -f "llama_watchdog.sh" 2>/dev/null || true
}

stop_watchdog

echo "[stop] Killing llama-server on ALL nodes (parallel)..."
# Controller (local)
pkill -f llama-server 2>/dev/null || true
sleep 1
pkill -9 -f llama-server 2>/dev/null || true
echo "[stop] llama-server killed on controller"

# Workers (parallel)
for worker in $WORKERS; do
  kill_llama_remote "$worker" &
done
wait
echo "[stop] llama-server kill issued on all nodes"

echo "[stop] Stopping Docker Compose stack"
cd "$PROJECT_DIR"
printf '%s\n' "$SUDO_PASS" | sudo -S docker compose down

echo "[stop] Platform stopped"