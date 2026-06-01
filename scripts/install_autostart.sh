#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/administrator/projects/llm-inference-service}"
LOG_FILE="${LOG_FILE:-/tmp/llm-startup.log}"
CRON_LINE="@reboot sleep 30 && bash ${PROJECT_DIR}/startup_scripts/start_linux.sh >> ${LOG_FILE} 2>&1"

existing="$(mktemp)"
crontab -l > "$existing" 2>/dev/null || true
if ! grep -Fq "$CRON_LINE" "$existing"; then
  {
    grep -v "startup_scripts/start_linux.sh" "$existing" || true
    echo "$CRON_LINE"
  } | crontab -
fi
rm -f "$existing"

echo "Installed reboot startup cron:"
crontab -l | grep "startup_scripts/start_linux.sh"
