#!/usr/bin/env bash
# =============================================================================
# ONGC LLM Inference Platform — Startup Script (macOS)
# Run from any Mac on the ONGC intranet.
# SSH-connects to the controller (WS-11) and starts all services remotely.
# Requires: ssh, sshpass (brew install hudochenkov/sshpass/sshpass)
# =============================================================================
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Config ────────────────────────────────────────────────────────────────────
CONTROLLER_IP="10.208.211.62"
SSH_USER="administrator"
SSH_PASS="Ongc@1234"
PROJECT_DIR="/home/administrator/projects/llm-inference-service"
VENV_DIR="/mnt/d/VirtualEnvironments/llm-platform"
LLAMA_PORT=8080
RAY_PORT=6379
SERVE_PORT=8001

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=30"

# ── Dependency checks ─────────────────────────────────────────────────────────
command -v ssh     > /dev/null || die "ssh not found"
command -v sshpass > /dev/null || die "sshpass not found. Install: brew install hudochenkov/sshpass/sshpass"
command -v curl    > /dev/null || die "curl not found"

# ── Helper: run command on controller ────────────────────────────────────────
remote() {
    sshpass -p "$SSH_PASS" ssh $SSH_OPTS "${SSH_USER}@${CONTROLLER_IP}" "$@"
}

wait_for_http() {
    local url="$1" label="$2" retries="${3:-20}"
    local i=0
    while (( i++ < retries )); do
        if curl --noproxy '*' -sf --max-time 3 "$url" > /dev/null 2>&1; then
            ok "$label is up"
            return 0
        fi
        sleep 3
    done
    die "$label did not respond at $url after $((retries * 3))s"
}

# ── Verify connectivity ───────────────────────────────────────────────────────
info "Testing SSH connectivity to controller ${CONTROLLER_IP}..."
remote "echo 'SSH OK'" || die "Cannot SSH to ${CONTROLLER_IP}. Check VPN/network."
ok "Controller reachable"

# ── Step 1: Docker Compose ────────────────────────────────────────────────────
info "Step 1/4 — Starting Docker Compose stack on controller..."
remote "
    export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'
    export NO_PROXY=\"\$no_proxy\"
    cd '${PROJECT_DIR}'
    printf '${SSH_PASS}\n' | sudo -S docker compose up -d 2>&1 | grep -E 'Started|Running|Created|healthy|error' || true
"
info "Waiting for gateway..."
wait_for_http "http://${CONTROLLER_IP}:18000/health" "Gateway" 30
ok "Docker stack ready"

# ── Step 2: llama.cpp server ──────────────────────────────────────────────────
info "Step 2/4 — Checking llama.cpp server..."
if curl --noproxy '*' -sf "http://${CONTROLLER_IP}:${LLAMA_PORT}/health" > /dev/null 2>&1; then
    ok "llama.cpp already running on port ${LLAMA_PORT}"
else
    warn "llama.cpp not running — starting on controller..."
    remote "
        export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'
        nohup /home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server \
            -m /mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
            --mmproj /mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/mmproj-F16.gguf \
            -ngl 999 -c 65536 \
            --host ${CONTROLLER_IP} --port ${LLAMA_PORT} \
            --parallel 2 --no-context-shift \
            --flash-attn on \
            --cache-type-k q8_0 --cache-type-v q8_0 \
            --cont-batching \
            --spec-type draft-mtp --spec-draft-n-max 4 \
            > /tmp/llama-server.log 2>&1 &
        echo 'llama.cpp started in background'
    "
    info "Waiting for model to load (30-90s)..."
    wait_for_http "http://${CONTROLLER_IP}:${LLAMA_PORT}/health" "llama.cpp" 60
fi

# ── Step 3: Ray head + Serve ──────────────────────────────────────────────────
info "Step 3/4 — Starting Ray head and Ray Serve on controller..."
remote "
    export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'
    export NO_PROXY=\"\$no_proxy\"
    export RAY_grpc_enable_http_proxy=0
    source '${VENV_DIR}/bin/activate'
    cd '${PROJECT_DIR}'
    bash scripts/start_controller.sh
"
wait_for_http "http://${CONTROLLER_IP}:${SERVE_PORT}/health" "Ray Serve" 20

# ── Step 4: End-to-end verify ─────────────────────────────────────────────────
info "Step 4/4 — End-to-end check through NGINX..."
wait_for_http "http://${CONTROLLER_IP}:10080/health" "NGINX → Gateway" 15

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Platform is UP${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}API endpoint${NC}    http://${CONTROLLER_IP}:10080/v1/chat/completions"
echo -e "  ${CYAN}Grafana${NC}         http://${CONTROLLER_IP}:13000   (admin / ongc1234)"
echo -e "  ${CYAN}Prometheus${NC}      http://${CONTROLLER_IP}:9090"
echo -e "  ${CYAN}Ray Dashboard${NC}   http://${CONTROLLER_IP}:8265"
echo ""
echo -e "  ${YELLOW}Generate API key (run on controller):${NC}"
echo -e "    ssh ${SSH_USER}@${CONTROLLER_IP} \"printf '${SSH_PASS}\\n' | sudo -S docker compose -f ${PROJECT_DIR}/docker-compose.yml exec -T gateway python scripts/generate_api_key.py admin\""
echo ""
