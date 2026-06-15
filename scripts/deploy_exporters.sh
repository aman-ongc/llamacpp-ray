#!/usr/bin/env bash
# Deploy node-exporter and nvidia_gpu_exporter to all 15 cluster nodes.
# Uses SSH ControlMaster so all operations per host share one TCP connection,
# avoiding connection-reset errors from rapid-fire SSH sessions.
# Run from WS-11 (10.208.211.62).
set -euo pipefail

ALL_NODES=(
  "10.208.211.62"                                              # controller
  "10.208.211.52" "10.208.211.53" "10.208.211.54" "10.208.211.55"  # text pool
  "10.208.211.56" "10.208.211.57" "10.208.211.58" "10.208.211.59"
  "10.208.211.60" "10.208.211.61"
  "10.208.211.63" "10.208.211.64" "10.208.211.65" "10.208.211.67"  # multimodal pool
)
SSH_PASS="Ongc@1234"
REMOTE_DIR="/home/administrator/projects/llm-inference-service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# One temp dir for ControlMaster sockets; cleaned up on exit.
CTRL_DIR="$(mktemp -d /tmp/deploy-ssh-XXXXXX)"
trap 'rm -rf "$CTRL_DIR"' EXIT

_SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o ConnectTimeout=15
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=3
  -o BatchMode=no
  -o ControlMaster=auto
  -o "ControlPath=${CTRL_DIR}/%h"
  -o ControlPersist=120
)

remote() {
  local host="$1"; shift
  sshpass -p "$SSH_PASS" ssh "${_SSH_OPTS[@]}" -n "administrator@${host}" "$@"
}

rsync_file() {
  # scp reusing the ControlMaster socket
  local host="$1"; shift
  sshpass -p "$SSH_PASS" scp "${_SSH_OPTS[@]}" "$@" "administrator@${host}:"
}

push_scripts() {
  local host="$1"
  sshpass -p "$SSH_PASS" scp "${_SSH_OPTS[@]}" \
    "${SCRIPT_DIR}/start_node_exporter.sh" \
    "${SCRIPT_DIR}/start_gpu_exporter.sh" \
    "${SCRIPT_DIR}/install_exporters_cron.sh" \
    "administrator@${host}:${REMOTE_DIR}/scripts/"
}

NODE_EXP_VERSION="1.8.1"
GPU_EXP_VERSION="1.3.2"
NODE_EXP_TARBALL="/home/administrator/node_exporter/node_exporter-${NODE_EXP_VERSION}.linux-amd64.tar.gz"
GPU_EXP_TARBALL="/home/administrator/nvidia_gpu_exporter/nvidia_gpu_exporter_${GPU_EXP_VERSION}_linux_x86_64.tar.gz"

push_tarballs() {
  local host="$1"
  if ! remote "$host" \
      "test -f /home/administrator/node_exporter/node_exporter-${NODE_EXP_VERSION}.linux-amd64.tar.gz" 2>/dev/null; then
    echo "  Pushing node_exporter tarball to ${host}..."
    remote "$host" "mkdir -p /home/administrator/node_exporter"
    sshpass -p "$SSH_PASS" scp "${_SSH_OPTS[@]}" \
      "$NODE_EXP_TARBALL" "administrator@${host}:/home/administrator/node_exporter/"
  fi
  if ! remote "$host" \
      "test -f /home/administrator/nvidia_gpu_exporter/nvidia_gpu_exporter_${GPU_EXP_VERSION}_linux_x86_64.tar.gz" 2>/dev/null; then
    echo "  Pushing nvidia_gpu_exporter tarball to ${host}..."
    remote "$host" "mkdir -p /home/administrator/nvidia_gpu_exporter"
    sshpass -p "$SSH_PASS" scp "${_SSH_OPTS[@]}" \
      "$GPU_EXP_TARBALL" "administrator@${host}:/home/administrator/nvidia_gpu_exporter/"
  fi
}

deploy_node() {
  local host="$1"
  echo ""
  echo "=== ${host} ==="

  # Open master connection first; if this fails the node is unreachable.
  if ! sshpass -p "$SSH_PASS" ssh "${_SSH_OPTS[@]}" -fN "administrator@${host}" 2>/dev/null; then
    echo "[warn] cannot open master connection to ${host} — skipping"
    return
  fi

  remote "$host" "mkdir -p ${REMOTE_DIR}/scripts" \
    || { echo "[warn] mkdir failed on ${host} — skipping"; return; }
  push_scripts "$host" \
    || { echo "[warn] push_scripts failed on ${host} — skipping"; return; }
  push_tarballs "$host" \
    || { echo "[warn] push_tarballs failed on ${host} — skipping"; return; }
  remote "$host" "chmod +x \
    ${REMOTE_DIR}/scripts/start_node_exporter.sh \
    ${REMOTE_DIR}/scripts/start_gpu_exporter.sh \
    ${REMOTE_DIR}/scripts/install_exporters_cron.sh" || true

  remote "$host" "bash ${REMOTE_DIR}/scripts/start_node_exporter.sh" \
    || echo "[warn] node_exporter may not be healthy on ${host} — continuing"
  remote "$host" "bash ${REMOTE_DIR}/scripts/start_gpu_exporter.sh" \
    || echo "[warn] gpu_exporter may not be healthy on ${host} — continuing"
  remote "$host" "PROJECT_DIR=${REMOTE_DIR} bash ${REMOTE_DIR}/scripts/install_exporters_cron.sh" \
    || echo "[warn] cron install failed on ${host} — continuing"

  # Close master connection cleanly.
  ssh -o "ControlPath=${CTRL_DIR}/%h" -O exit "administrator@${host}" 2>/dev/null || true

  echo "--- ${host} done ---"
}

for host in "${ALL_NODES[@]}"; do
  deploy_node "$host"
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
