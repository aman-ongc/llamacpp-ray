#!/usr/bin/env bash
set -euo pipefail

CONTROLLER_IP="${CONTROLLER_IP:-10.208.211.62}"
SSH_USER="${SSH_USER:-administrator}"
SSH_PASS="${SSH_PASS:-Ongc@1234}"
PROJECT_DIR="${PROJECT_DIR:-/home/administrator/projects/llm-inference-service}"

command -v sshpass >/dev/null || {
  echo "sshpass not found. Install it with: brew install hudochenkov/sshpass/sshpass" >&2
  exit 1
}

sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "$SSH_USER@$CONTROLLER_IP" \
  "cd '$PROJECT_DIR' && bash startup_scripts/stop_linux.sh"
