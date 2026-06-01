#!/usr/bin/env bash
# Deploy node-exporter and nvidia_gpu_exporter to all 4 cluster nodes.
# Starts both exporters immediately and installs @reboot cron entries.
# Run from WS-11 (10.208.211.62).
set -euo pipefail

ALL_NODES=("10.208.211.62" "10.208.211.54" "10.208.211.59" "10.208.211.64")
SSH_PASS="Ongc@1234"
REMOTE_DIR="/home/administrator/projects/llm-inference-service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

remote() {
  local host="$1"; shift
  sshpass -p "$SSH_PASS" ssh \
    -o StrictHostKeyChecking=no \
    -o ConnectTimeout=10 \
    "administrator@${host}" "$@"
}

push_scripts() {
  local host="$1"
  sshpass -p "$SSH_PASS" scp \
    -o StrictHostKeyChecking=no \
    "${SCRIPT_DIR}/start_node_exporter.sh" \
    "${SCRIPT_DIR}/start_gpu_exporter.sh" \
    "${SCRIPT_DIR}/install_exporters_cron.sh" \
    "administrator@${host}:${REMOTE_DIR}/scripts/"
}

for host in "${ALL_NODES[@]}"; do
  echo ""
  echo "=== ${host} ==="
  remote "$host" "mkdir -p ${REMOTE_DIR}/scripts"
  push_scripts "$host"
  remote "$host" "chmod +x ${REMOTE_DIR}/scripts/start_node_exporter.sh ${REMOTE_DIR}/scripts/start_gpu_exporter.sh ${REMOTE_DIR}/scripts/install_exporters_cron.sh"
  remote "$host" "bash ${REMOTE_DIR}/scripts/start_node_exporter.sh"
  remote "$host" "bash ${REMOTE_DIR}/scripts/start_gpu_exporter.sh"
  remote "$host" "PROJECT_DIR=${REMOTE_DIR} bash ${REMOTE_DIR}/scripts/install_exporters_cron.sh"
  echo "--- ${host} done ---"
done

echo ""
echo "=== Endpoint verification from controller ==="
all_ok=true
for host in "${ALL_NODES[@]}"; do
  if curl --noproxy '*' -sf --max-time 5 "http://${host}:9100/metrics" >/dev/null 2>&1; then
    echo "  [OK]   ${host}:9100  node-exporter"
  else
    echo "  [FAIL] ${host}:9100  node-exporter"
    all_ok=false
  fi
  if curl --noproxy '*' -sf --max-time 5 "http://${host}:9835/metrics" >/dev/null 2>&1; then
    echo "  [OK]   ${host}:9835  nvidia-gpu-exporter"
  else
    echo "  [FAIL] ${host}:9835  nvidia-gpu-exporter"
    all_ok=false
  fi
done

echo ""
if $all_ok; then
  echo "All exporters reachable. Next: sudo docker compose restart prometheus"
else
  echo "Some endpoints failed. Check logs on the affected node:"
  echo "  /tmp/node_exporter.log"
  echo "  /tmp/nvidia_gpu_exporter.log"
  exit 1
fi
