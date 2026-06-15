#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKERS=(
  # Text pool (.52–.61)
  "10.208.211.52" "10.208.211.53" "10.208.211.54" "10.208.211.55"
  "10.208.211.56" "10.208.211.57" "10.208.211.58" "10.208.211.59"
  "10.208.211.60" "10.208.211.61"
  # Multimodal pool (.63/.64/.65/.67)
  "10.208.211.63" "10.208.211.64" "10.208.211.65" "10.208.211.67"
)
SSH_PASS="Ongc@1234"
REMOTE_DIR="/home/administrator/llm-inference-service-worker"

for host in "${WORKERS[@]}"; do
  echo "Deploying worker payload to ${host}"
  sshpass -p "${SSH_PASS}" ssh -o StrictHostKeyChecking=no "administrator@${host}" "mkdir -p ${REMOTE_DIR}"
  sshpass -p "${SSH_PASS}" scp -o StrictHostKeyChecking=no -r \
    "${ROOT_DIR}/worker" "${ROOT_DIR}/gateway" "${ROOT_DIR}/requirements.txt" \
    "administrator@${host}:${REMOTE_DIR}/"
done
