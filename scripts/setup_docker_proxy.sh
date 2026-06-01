#!/usr/bin/env bash
# Configure Docker daemon proxy so it can pull images through the corporate proxy.
# Run once (or after Docker reinstall). Requires sudo.
set -euo pipefail

PROXY="http://10.205.122.201:8080"
NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
DAEMON_JSON="/etc/docker/daemon.json"

echo "[INFO] Writing Docker daemon proxy config to $DAEMON_JSON..."

sudo mkdir -p /etc/docker
sudo tee "$DAEMON_JSON" > /dev/null <<EOF
{
  "proxies": {
    "http-proxy":  "$PROXY",
    "https-proxy": "$PROXY",
    "no-proxy":    "$NO_PROXY"
  }
}
EOF

echo "[INFO] Restarting Docker daemon..."
sudo service docker restart 2>/dev/null || \
  sudo systemctl restart docker 2>/dev/null || \
  echo "[WARN] Could not restart docker — restart manually or reopen WSL2"

echo "[OK] Docker daemon proxy configured."
echo "     Test: sudo docker pull hello-world"
