#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-1.8.1}"
PORT="${PORT:-9100}"
INSTALL_DIR="${INSTALL_DIR:-/home/administrator/node_exporter}"
TARBALL="node_exporter-${VERSION}.linux-amd64.tar.gz"
URL="https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/${TARBALL}"

export http_proxy="${http_proxy:-http://10.205.122.201:8080}"
export https_proxy="${https_proxy:-http://10.205.122.201:8080}"
export no_proxy="${no_proxy:-localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in}"
export NO_PROXY="${NO_PROXY:-$no_proxy}"

if pgrep -f "node_exporter --web.listen-address=:${PORT}" >/dev/null 2>&1; then
  echo "node_exporter already running on :${PORT}"
  exit 0
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ ! -x "$INSTALL_DIR/node_exporter-${VERSION}.linux-amd64/node_exporter" ]; then
  if [ ! -f "$TARBALL" ]; then
    wget -q "$URL"
  fi
  tar xzf "$TARBALL"
fi

nohup "$INSTALL_DIR/node_exporter-${VERSION}.linux-amd64/node_exporter" \
  --web.listen-address=":${PORT}" \
  --collector.filesystem.mount-points-exclude='^/(dev|proc|run|run/user.*|sys|var/lib/docker/.+|var/lib/containers/storage/.+)($|/)' \
  >/tmp/node_exporter.log 2>&1 &

for _ in {1..20}; do
  if curl --noproxy '*' -sf "http://127.0.0.1:${PORT}/metrics" >/dev/null 2>&1; then
    echo "node_exporter running on :${PORT}"
    exit 0
  fi
  sleep 1
done

echo "node_exporter did not become healthy" >&2
tail -n 40 /tmp/node_exporter.log >&2 || true
exit 1
