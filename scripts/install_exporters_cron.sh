#!/usr/bin/env bash
# Install @reboot cron entries for node-exporter and nvidia_gpu_exporter.
# Idempotent — strips existing exporter entries before inserting fresh ones.
# Run on each target node (locally or via ssh).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/administrator/projects/llm-inference-service}"

NODE_EXP_LINE="@reboot sleep 15 && bash ${PROJECT_DIR}/scripts/start_node_exporter.sh >> /tmp/node_exporter_boot.log 2>&1"
GPU_EXP_LINE="@reboot sleep 20 && bash ${PROJECT_DIR}/scripts/start_gpu_exporter.sh >> /tmp/nvidia_gpu_exporter_boot.log 2>&1"
NODE_WATCH_LINE="*/5 * * * * bash ${PROJECT_DIR}/scripts/start_node_exporter.sh >> /tmp/node_exporter_watchdog.log 2>&1"
GPU_WATCH_LINE="*/5 * * * * bash ${PROJECT_DIR}/scripts/start_gpu_exporter.sh >> /tmp/nvidia_gpu_exporter_watchdog.log 2>&1"

tmpfile="$(mktemp)"
# Dump current crontab (ignore error if empty)
crontab -l 2>/dev/null > "$tmpfile" || true

# Strip any existing exporter entries, then append fresh ones
{
  grep -v "start_node_exporter.sh" "$tmpfile" | grep -v "start_gpu_exporter.sh" || true
  echo "$NODE_EXP_LINE"
  echo "$GPU_EXP_LINE"
  echo "$NODE_WATCH_LINE"
  echo "$GPU_WATCH_LINE"
} | crontab -

rm -f "$tmpfile"

echo "Cron entries installed on $(hostname):"
crontab -l | grep -E "node_exporter|gpu_exporter"
