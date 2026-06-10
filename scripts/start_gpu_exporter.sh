#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-1.3.2}"
PORT="${PORT:-9835}"
INSTALL_DIR="${INSTALL_DIR:-/home/administrator/nvidia_gpu_exporter}"
BINARY_NAME="nvidia_gpu_exporter"
TARBALL="${BINARY_NAME}_${VERSION}_linux_x86_64.tar.gz"
URL="https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v${VERSION}/${TARBALL}"

export http_proxy="${http_proxy:-http://10.205.122.201:8080}"
export https_proxy="${https_proxy:-http://10.205.122.201:8080}"
export no_proxy="${no_proxy:-localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in}"
export NO_PROXY="${NO_PROXY:-$no_proxy}"

# Require nvidia-smi — graceful skip if driver not present
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi not found — skipping GPU exporter on this node"
  exit 0
fi

# Idempotency guard
if pgrep -f "${BINARY_NAME} --web.listen-address=:${PORT}" >/dev/null 2>&1; then
  echo "nvidia_gpu_exporter already running on :${PORT}"
  exit 0
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ ! -x "$INSTALL_DIR/${BINARY_NAME}" ]; then
  if [ ! -f "$TARBALL" ]; then
    echo "Downloading nvidia_gpu_exporter v${VERSION}..."
    wget -q "$URL"
  fi
  tar xzf "$TARBALL"
  chmod +x "$BINARY_NAME"
fi

nohup "$INSTALL_DIR/${BINARY_NAME}" \
  --web.listen-address=":${PORT}" \
  >/tmp/nvidia_gpu_exporter.log 2>&1 </dev/null &

for _ in {1..20}; do
  if curl --noproxy '*' -sf "http://127.0.0.1:${PORT}/metrics" >/dev/null 2>&1; then
    echo "nvidia_gpu_exporter running on :${PORT}"
    exit 0
  fi
  sleep 1
done

echo "nvidia_gpu_exporter did not become healthy" >&2
tail -n 40 /tmp/nvidia_gpu_exporter.log >&2 || true
exit 1
