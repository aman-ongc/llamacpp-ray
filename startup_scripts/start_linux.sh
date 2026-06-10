#!/usr/bin/env bash
# =============================================================================
# ONGC LLM Inference Platform — Startup Script (Linux / WSL2)
# Run this directly on the controller node (WS-11) inside a WSL2 Ubuntu shell.
# Idempotent — safe to run multiple times.
#
# Node layout:
#   WS-11 (10.208.211.62) — Ray head + text worker, Gemma 4 26B QAT, port 8080
#   WS-03 (10.208.211.54) — text worker, Gemma 4 26B QAT, port 8080
#   WS-08 (10.208.211.59) — text worker, Gemma 4 26B QAT, port 8080
#   WS-13 (10.208.211.64) — multimodal worker, Qwen3-VL-8B, port 8080
# =============================================================================
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_DIR="/home/administrator/projects/llm-inference-service"
VENV_DIR="/mnt/d/VirtualEnvironments/llm-platform"
LLAMA_SERVER="/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server"

# WS-11 (controller + text worker): Gemma 4 26B QAT, no mmproj
CONTROLLER_IP="10.208.211.62"
TEXT_LLAMA_PORT=8080
TEXT_MODEL="/mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf"

# WS-13 (multimodal worker): Qwen3-VL-8B with mmproj
MULTIMODAL_NODE_IP="10.208.211.64"
MULTIMODAL_LLAMA_PORT=8080
MULTIMODAL_MODEL="/mnt/d/Models/qwen-3-vl/Qwen3VL-8B-Instruct-Q8_0.gguf"
MULTIMODAL_MMPROJ="/mnt/d/Models/qwen-3-vl/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"

RAY_PORT=6379
SERVE_PORT=8001
SUDO_PASS="Ongc@1234"

# When true: WS-11 joins text pool (llama-server started, text_node resource registered).
# When false (default): WS-11 is head-only — no llama-server, no text requests.
CONTROLLER_AS_WORKER="${CONTROLLER_AS_WORKER:-false}"

# Proxy bypass — critical for all internal traffic
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"
export RAY_grpc_enable_http_proxy=0
export RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING=0

# ── Helpers ───────────────────────────────────────────────────────────────────
wait_for_http() {
    local url="$1" label="$2" retries="${3:-20}"
    local i=0
    while (( i++ < retries )); do
        if curl --noproxy '*' -sf "$url" > /dev/null 2>&1; then
            ok "$label is up"
            return 0
        fi
        sleep 2
    done
    die "$label did not respond at $url after $((retries * 2))s"
}

# ── Step 0: WSL2 network mode check + portproxy cleanup ──────────────────────
info "Step 0 — Checking WSL2 network mode..."
WSL2_NET_MODE="$(grep -i 'networkingMode' /etc/wsl.conf /mnt/c/Users/administrator/.wslconfig /mnt/c/Users/Administrator/.wslconfig 2>/dev/null | grep -i 'mirror' | head -1 || true)"
if [[ -n "$WSL2_NET_MODE" ]]; then
    ok "WSL2 mirrored networking detected — portproxy not needed, cleaning up any stale rules..."
    if command -v powershell.exe >/dev/null 2>&1; then
        for port in 8001 8080 6379 8265 9090 10080 13000 18000; do
            for addr in 0.0.0.0 127.0.0.1; do
                powershell.exe -Command "netsh interface portproxy delete v4tov4 listenport=${port} listenaddress=${addr} 2>\$null | Out-Null" 2>/dev/null || true
            done
        done
        ok "Stale portproxy rules removed. Ports are directly accessible in mirrored mode."
    fi
else
    warn "WSL2 standard networking — portproxy may be needed for browser access."
    warn "Run startup_scripts/update_portproxy.ps1 in an elevated PowerShell if browser access fails."
fi

# ── Step 1: Docker Compose ────────────────────────────────────────────────────
info "Step 1/3 — Starting Docker Compose stack..."
cd "$PROJECT_DIR"

if ! command -v docker &> /dev/null; then
    die "docker not found. Install Docker or run from WSL2 with Docker Desktop."
fi

echo "$SUDO_PASS" | sudo -S docker compose up -d --build 2>&1 | grep -E "Started|Running|Created|Built|healthy|error" || true

info "Waiting for gateway container..."
wait_for_http "http://${CONTROLLER_IP}:18000/health" "Gateway" 30
ok "Docker stack ready"

# ── Step 2: Ray head + Serve ──────────────────────────────────────────────────
info "Step 2/3 — Starting Ray head (WS-11: controller + text inference worker)..."
if [[ ! -d "$VENV_DIR" ]]; then
    die "Python venv not found at: $VENV_DIR"
fi
source "${VENV_DIR}/bin/activate"

info "Ensuring Ray/Serve ports are free before starting..."
pkill -f "raylet" 2>/dev/null || true
pkill -f "ray::" 2>/dev/null || true
pkill -f "ray::Serve" 2>/dev/null || true
pkill -f "gcs_server" 2>/dev/null || true
pkill -f "plasma_store" 2>/dev/null || true
sleep 1
for port in 8001 "${RAY_PORT}"; do
    if fuser "${port}/tcp" >/dev/null 2>&1; then
        warn "Port ${port} in use — killing..."
        fuser -k "${port}/tcp" 2>/dev/null || sudo fuser -k "${port}/tcp" 2>/dev/null || true
        sleep 1
    fi
    if fuser "${port}/tcp" >/dev/null 2>&1; then
        die "Port ${port} still blocked after kill — likely a Windows portproxy reservation.\nRun this in an ELEVATED PowerShell on Windows:\n  foreach (\$addr in @('0.0.0.0','127.0.0.1')) { foreach (\$p in @(8001,6379,8265)) { netsh interface portproxy delete v4tov4 listenport=\$p listenaddress=\$addr 2>\$null } }\nThen re-run this script."
    fi
done

# Head node: CPU-only Ray orchestrator. GPU owned exclusively by llama-server.
# Register "text_node" resource only when WS-11 is enabled as a worker.
if ray status --address "${CONTROLLER_IP}:${RAY_PORT}" > /dev/null 2>&1; then
    ok "Ray head already running"
else
    if [[ "$CONTROLLER_AS_WORKER" == "true" ]]; then
        HEAD_RESOURCES='{"text_node": 1}'
        info "Starting Ray head node (text_node resource enabled — WS-11 is a worker)..."
    else
        HEAD_RESOURCES='{}'
        info "Starting Ray head node (head-only — WS-11 not in text pool)..."
    fi
    ray start \
        --head \
        --node-ip-address="${CONTROLLER_IP}" \
        --port="${RAY_PORT}" \
        --dashboard-host=0.0.0.0 \
        --dashboard-port=8265 \
        --num-gpus=0 \
        --num-cpus=6 \
        --resources="$HEAD_RESOURCES"
    sleep 4
fi

# Start Gemma on WS-11 only when CONTROLLER_AS_WORKER=true.
if [[ "$CONTROLLER_AS_WORKER" == "true" ]]; then
    if ! curl --noproxy '*' -sf "http://${CONTROLLER_IP}:${TEXT_LLAMA_PORT}/health" >/dev/null 2>&1; then
        info "Starting Gemma 4 26B QAT on WS-11 (port ${TEXT_LLAMA_PORT})..."
        nohup "$LLAMA_SERVER" \
            -m "$TEXT_MODEL" \
            -ngl 999 -c 65536 \
            --host "$CONTROLLER_IP" --port "$TEXT_LLAMA_PORT" \
            --parallel 1 --no-context-shift \
            --flash-attn auto --cache-type-k q4_0 --cache-type-v q4_0 \
            --cont-batching \
            >/tmp/llama-server-ws11.log 2>&1 &
        ok "Gemma launched on WS-11 (log: /tmp/llama-server-ws11.log)"
    else
        ok "Gemma already running on WS-11"
    fi
else
    info "WS-11 controller-as-worker disabled — skipping llama-server on WS-11"
fi

info "Starting remote worker nodes (WS-03, WS-08, WS-13) in parallel..."
# WS-03 and WS-08: text nodes (Gemma, port 8080, text_node resource)
# WS-13: multimodal node (Qwen3-VL, port 8080, multimodal_node resource)
TEXT_WORKERS=("10.208.211.54" "10.208.211.59")
for worker in "${TEXT_WORKERS[@]}"; do
    info "  Launching text worker ${worker}..."
    MODEL_PATH="$TEXT_MODEL" \
    MMPROJ_PATH="" \
    LLAMA_SERVER="$LLAMA_SERVER" \
    RAY_HEAD_IP="$CONTROLLER_IP" \
    bash "$PROJECT_DIR/scripts/start_linux_worker.sh" "$worker" \
        > "/tmp/worker-${worker}.log" 2>&1 &
done

info "  Launching multimodal worker ${MULTIMODAL_NODE_IP}..."
MODEL_PATH="$MULTIMODAL_MODEL" \
MMPROJ_PATH="$MULTIMODAL_MMPROJ" \
LLAMA_SERVER="$LLAMA_SERVER" \
RAY_HEAD_IP="$CONTROLLER_IP" \
bash "$PROJECT_DIR/scripts/start_linux_worker.sh" "$MULTIMODAL_NODE_IP" \
    > "/tmp/worker-${MULTIMODAL_NODE_IP}.log" 2>&1 &

info "Waiting for all 4 nodes to join cluster (up to 120s)..."
for i in {1..24}; do
    node_count=$(ray status --address "${CONTROLLER_IP}:${RAY_PORT}" 2>/dev/null \
        | grep -cE "node_[0-9a-f]+" || true)
    if [[ "$node_count" -ge 4 ]]; then
        ok "All 4 nodes active: WS-11 (head+text) + WS-03 (text) + WS-08 (text) + WS-13 (multimodal)"
        break
    fi
    sleep 5
done
ray status --address "${CONTROLLER_IP}:${RAY_PORT}" || true

info "Deploying Ray Serve (TextWorker + MultimodalWorker)..."
cd "$PROJECT_DIR"
python scripts/deploy_serve.py
sleep 10  # allow replicas on remote nodes to fully initialize before health check

wait_for_http "http://${CONTROLLER_IP}:${SERVE_PORT}/text/health" "Ray Serve (text)" 60

# ── Step 3: Verify end-to-end ─────────────────────────────────────────────────
info "Step 3/3 — End-to-end verification..."
wait_for_http "http://${CONTROLLER_IP}:10080/health" "NGINX → Gateway" 15

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Platform is UP${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}API endpoint${NC}    http://${CONTROLLER_IP}:10080/v1/chat/completions"
echo -e "  ${CYAN}Model name${NC}      ongc-llm  (text → Gemma pool, image → Qwen3-VL)"
echo -e "  ${CYAN}Grafana${NC}         http://${CONTROLLER_IP}:13000   (admin / ongc1234)"
echo -e "  ${CYAN}Prometheus${NC}      http://${CONTROLLER_IP}:9090"
echo -e "  ${CYAN}Ray Dashboard${NC}   http://${CONTROLLER_IP}:8265"
echo -e "  ${CYAN}Gateway direct${NC}  http://${CONTROLLER_IP}:18000"
echo ""
if [[ "$CONTROLLER_AS_WORKER" == "true" ]]; then
    echo -e "  ${CYAN}Text nodes${NC}      WS-11/03/08 → Gemma 4 26B QAT (port 8080)"
else
    echo -e "  ${CYAN}Text nodes${NC}      WS-03/08 → Gemma 4 26B QAT (port 8080)  [WS-11 head-only]"
fi
echo -e "  ${CYAN}Multimodal${NC}      WS-13       → Qwen3-VL-8B     (port 8080)"
echo ""
echo -e "  ${YELLOW}Manage users/keys:${NC}"
echo -e "    curl --noproxy '*' http://${CONTROLLER_IP}:10080/admin/users -H 'X-Admin-Secret: changeme'"
echo ""
echo -e "  ${YELLOW}Generate API key:${NC}"
echo -e "    printf '${SUDO_PASS}\\n' | sudo -S docker compose exec -T gateway python scripts/generate_api_key.py admin"
echo ""