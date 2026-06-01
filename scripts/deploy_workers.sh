#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKERS=("10.208.211.54" "10.208.211.59" "10.208.211.64")
SSH_PASS="Ongc@1234"
REMOTE_DIR="/home/administrator/llm-inference-service-worker"

for host in "${WORKERS[@]}"; do
  echo "Deploying worker payload to ${host}"
  sshpass -p "${SSH_PASS}" ssh -o StrictHostKeyChecking=no "administrator@${host}" "mkdir -p ${REMOTE_DIR}"
  sshpass -p "${SSH_PASS}" scp -o StrictHostKeyChecking=no -r \
    "${ROOT_DIR}/worker" "${ROOT_DIR}/gateway" "${ROOT_DIR}/requirements.txt" \
    "administrator@${host}:${REMOTE_DIR}/"
done
