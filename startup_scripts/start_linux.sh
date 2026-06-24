#!/usr/bin/env bash
# =============================================================================
# ONGC LLM Inference Platform — Startup Script (Linux / WSL2)
# Run this directly on the controller node (WS-11) inside a WSL2 Ubuntu shell.
# Idempotent — safe to run multiple times.
#
# Node layout (rebalanced 2026-06-20 — .60/.61 moved text→multimodal, parallel 4→2):
#   WS-11  (10.208.211.62) — gateway/controller; text worker only if CONTROLLER_AS_WORKER=true
#   WS-3   (10.208.211.54) — docling/dev node; text worker only if DOCLING_NODE_AS_WORKER=true
#   Text   (10.208.211.52/.53/.55-.58) — 6 nodes, Gemma 4 26B QAT, --parallel 1, -c 65536
#   Multi  (10.208.211.60/.61/.63/.64/.65/.67) — 6 nodes, Qwen3-VL-8B, --parallel 2, -c 65536
# No orchestrator — the gateway routes directly to each node's llama-server
# and handles scheduling/health/retries itself (gateway/router.py).
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
LLAMA_SERVER="/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server"

# Controller (WS-11): Gemma 4 26B QAT; joins text pool only when CONTROLLER_AS_WORKER=true
CONTROLLER_IP="10.208.211.62"
TEXT_LLAMA_PORT=8080
TEXT_MODEL="/mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf"

# Multimodal workers (.60/.61/.63/.64/.65/.67): Qwen3-VL-8B, -c 65536 --parallel 2
MULTIMODAL_NODE_IPS=("10.208.211.60" "10.208.211.61" "10.208.211.63" "10.208.211.64" "10.208.211.65" "10.208.211.67")
MULTIMODAL_LLAMA_PORT=8080
MULTIMODAL_MODEL="/mnt/d/Models/qwen-3-vl/Qwen3VL-8B-Instruct-Q8_0.gguf"
MULTIMODAL_MMPROJ="/mnt/d/Models/qwen-3-vl/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"

SUDO_PASS="Ongc@1234"

# When true: WS-11 joins text pool (llama-server started, text_node resource registered).
# When false (default): WS-11 is head-only — no llama-server, no text requests.
CONTROLLER_AS_WORKER="${CONTROLLER_AS_WORKER:-false}"

# When true: WS-3 (.54) joins text pool (llama-server started on it).
# When false (default): .54 is reserved for docling/development — no llama-server.
DOCLING_NODE_AS_WORKER="${DOCLING_NODE_AS_WORKER:-false}"
DOCLING_NODE_IP="10.208.211.54"

# Proxy bypass — critical for all internal traffic
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"

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
        for port in 8080 9090 10080 13000 18000; do
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

# ── Gateway port allowlist (10.208.211.0/24 + 10.205.136.0/24 + 10.205.137.0/24 only) ──────────
# iptables rules don't survive a reboot, so this is reapplied on every start.
# DOCKER-USER is the chain Docker preserves for user filtering of published
# ports — must allow ESTABLISHED/RELATED first, or reply packets (whose
# source is the container's internal bridge IP, not the client's subnet)
# get silently dropped, breaking every connection including from allowed IPs.
info "Applying gateway port 18000 allowlist (10.208.211.0/24, 10.205.136.0/24, 10.205.137.0/24)..."
echo "$SUDO_PASS" | sudo -S iptables -F DOCKER-USER
echo "$SUDO_PASS" | sudo -S iptables -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
echo "$SUDO_PASS" | sudo -S iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 18000 -s 10.208.211.0/24 -j ACCEPT
echo "$SUDO_PASS" | sudo -S iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 18000 -s 10.205.136.0/24 -j ACCEPT
echo "$SUDO_PASS" | sudo -S iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 18000 -s 10.205.137.0/24 -j ACCEPT
echo "$SUDO_PASS" | sudo -S iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 18000 -j DROP
ok "Gateway allowlist applied"

# ── Step 2: llama-server workers ──────────────────────────────────────────────
info "Step 2/3 — Starting llama-server workers (no orchestrator — gateway routes directly)..."

# Start Gemma on WS-11 only when CONTROLLER_AS_WORKER=true.
if [[ "$CONTROLLER_AS_WORKER" == "true" ]]; then
    if ! curl --noproxy '*' -sf "http://${CONTROLLER_IP}:${TEXT_LLAMA_PORT}/health" >/dev/null 2>&1; then
        info "Starting Gemma 4 26B QAT on WS-11 (port ${TEXT_LLAMA_PORT})..."
        nohup "$LLAMA_SERVER" \
            -m "$TEXT_MODEL" \
            -ngl 999 -c 65536 \
            --host "$CONTROLLER_IP" --port "$TEXT_LLAMA_PORT" \
            --parallel 1 \
            --flash-attn auto --cache-type-k q4_0 --cache-type-v q4_0 \
            --cont-batching \
            --no-context-shift \
            --metrics \
            >/tmp/llama-server-ws11.log 2>&1 </dev/null &
        ok "Gemma launched on WS-11 (log: /tmp/llama-server-ws11.log)"
    else
        ok "Gemma already running on WS-11"
    fi
else
    info "WS-11 controller-as-worker disabled — skipping llama-server on WS-11"
fi

info "Starting remote worker nodes (text pool .52/.53/.55-.58, multimodal pool .60/.61/.63/.64/.65/.67) in parallel..."
TEXT_WORKERS=(
    "10.208.211.52" "10.208.211.53" "10.208.211.55"
    "10.208.211.56" "10.208.211.57" "10.208.211.58"
    # .59 excluded — display GPU (15,352 MiB vs 16,376 MiB headless); OOMs frequently
    # .60/.61 moved to multimodal pool 2026-06-20
)
# WS-3 (.54) joins text pool only when DOCLING_NODE_AS_WORKER=true.
if [[ "$DOCLING_NODE_AS_WORKER" == "true" ]]; then
    TEXT_WORKERS+=("$DOCLING_NODE_IP")
    info "WS-3 (${DOCLING_NODE_IP}) docling-node-as-worker enabled — adding to text pool"
else
    info "WS-3 (${DOCLING_NODE_IP}) reserved for docling/dev — killing any stale llama-server on it..."
    sshpass -p "$SUDO_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        "administrator@${DOCLING_NODE_IP}" \
        'pids=$(pgrep -f llama-server 2>/dev/null); [ -n "$pids" ] && kill $pids 2>/dev/null; true' \
        && ok "WS-3 llama-server stopped (or was already down)" \
        || warn "WS-3 SSH unreachable — could not enforce llama-server shutdown"
fi
for worker in "${TEXT_WORKERS[@]}"; do
    info "  Launching text worker ${worker}..."
    MODEL_PATH="$TEXT_MODEL" \
    MMPROJ_PATH="" \
    LLAMA_SERVER="$LLAMA_SERVER" \
    bash "$PROJECT_DIR/scripts/start_linux_worker.sh" "$worker" \
        > "/tmp/worker-${worker}.log" 2>&1 &
done

for mm_ip in "${MULTIMODAL_NODE_IPS[@]}"; do
    info "  Launching multimodal worker ${mm_ip}..."
    MODEL_PATH="$MULTIMODAL_MODEL" \
    MMPROJ_PATH="$MULTIMODAL_MMPROJ" \
    LLAMA_SERVER="$LLAMA_SERVER" \
    bash "$PROJECT_DIR/scripts/start_linux_worker.sh" "$mm_ip" \
        > "/tmp/worker-${mm_ip}.log" 2>&1 &
done

info "Waiting for all worker bootstrap jobs to finish..."
wait

info "Verifying llama-server health on all worker nodes..."
UNHEALTHY=()
for ip in "${TEXT_WORKERS[@]}"; do
    if curl --noproxy '*' -sf "http://${ip}:${TEXT_LLAMA_PORT}/health" >/dev/null 2>&1; then
        ok "  ${ip} healthy"
    else
        warn "  ${ip} NOT responding on port ${TEXT_LLAMA_PORT} — check /tmp/worker-${ip}.log"
        UNHEALTHY+=("$ip")
    fi
done
for ip in "${MULTIMODAL_NODE_IPS[@]}"; do
    if curl --noproxy '*' -sf "http://${ip}:${MULTIMODAL_LLAMA_PORT}/health" >/dev/null 2>&1; then
        ok "  ${ip} healthy"
    else
        warn "  ${ip} NOT responding on port ${MULTIMODAL_LLAMA_PORT} — check /tmp/worker-${ip}.log"
        UNHEALTHY+=("$ip")
    fi
done
if (( ${#UNHEALTHY[@]} > 0 )); then
    warn "${#UNHEALTHY[@]} node(s) unhealthy at startup: ${UNHEALTHY[*]} — watchdog will keep retrying."
fi

# ── Step 3: Verify end-to-end ─────────────────────────────────────────────────
info "Step 3/3 — End-to-end verification..."
wait_for_http "http://${CONTROLLER_IP}:10080/health" "NGINX → Gateway" 15

# ── Watchdog ──────────────────────────────────────────────────────────────────
info "Starting llama-server watchdog..."
pkill -f llama_watchdog.sh 2>/dev/null || true
sleep 1
CONTROLLER_AS_WORKER="$CONTROLLER_AS_WORKER" \
DOCLING_NODE_AS_WORKER="$DOCLING_NODE_AS_WORKER" \
LLAMA_SERVER="$LLAMA_SERVER" \
SSH_PASS="$SUDO_PASS" \
nohup bash "$PROJECT_DIR/scripts/llama_watchdog.sh" \
    >> /tmp/llama-watchdog.log 2>&1 &
echo "$!" > /tmp/llama-watchdog.pid
ok "Watchdog PID $(cat /tmp/llama-watchdog.pid) — log: /tmp/llama-watchdog.log"

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
echo -e "  ${CYAN}Gateway direct${NC}  http://${CONTROLLER_IP}:18000"
echo -e "  ${CYAN}Watchdog log${NC}    /tmp/llama-watchdog.log  (PID: $(cat /tmp/llama-watchdog.pid 2>/dev/null || echo 'not started'))"
echo ""
TEXT_NODE_COUNT=${#TEXT_WORKERS[@]}
[[ "$CONTROLLER_AS_WORKER" == "true" ]] && (( TEXT_NODE_COUNT += 1 ))
TEXT_NODE_NOTES=""
[[ "$CONTROLLER_AS_WORKER" != "true" ]] && TEXT_NODE_NOTES+=" [WS-11 head-only]"
[[ "$DOCLING_NODE_AS_WORKER" != "true" ]] && TEXT_NODE_NOTES+=" [WS-3 docling/dev]"
echo -e "  ${CYAN}Text nodes${NC}      ${TEXT_NODE_COUNT} active → Gemma 4 26B QAT --parallel 1${TEXT_NODE_NOTES}"
echo -e "  ${CYAN}Multimodal${NC}      .60/.61/.63/.64/.65/.67 (6 nodes) → Qwen3-VL-8B --parallel 2 -c 65536"
echo ""
echo -e "  ${YELLOW}Manage users/keys:${NC}"
echo -e "    curl --noproxy '*' http://${CONTROLLER_IP}:10080/admin/users -H 'X-Admin-Secret: changeme'"
echo ""
echo -e "  ${YELLOW}Generate API key:${NC}"
echo -e "    printf '${SUDO_PASS}\\n' | sudo -S docker compose exec -T gateway python scripts/generate_api_key.py admin"
echo ""