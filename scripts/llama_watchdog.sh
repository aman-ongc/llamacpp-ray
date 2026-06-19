#!/usr/bin/env bash
# llama-server + raylet watchdog — runs on WS-11, monitors all worker nodes.
# Started by startup_scripts/start_linux.sh after cluster is up.
# Logs to stdout (redirected to /tmp/llama-watchdog.log by caller).
set +e  # single bad iteration must NOT kill the loop

POLL_INTERVAL=30
RESTART_WAIT=120
SSH_PASS="${SSH_PASS:-Ongc@1234}"
LLAMA_SERVER="${LLAMA_SERVER:-/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server}"
CONTROLLER_AS_WORKER="${CONTROLLER_AS_WORKER:-false}"
CONTROLLER_IP="10.208.211.62"
RAY_HEAD_IP="${RAY_HEAD_IP:-10.208.211.62}"
RAY_PORT="${RAY_PORT:-6379}"
RAY_BIN="${RAY_BIN:-/mnt/d/VirtualEnvironments/llm-platform/bin/ray}"
# WS-3 (.54): docling/dev node — excluded from watchdog unless DOCLING_NODE_AS_WORKER=true.
DOCLING_NODE_AS_WORKER="${DOCLING_NODE_AS_WORKER:-false}"
DOCLING_NODE_IP="10.208.211.54"

# Node list: "ip|type|port"
# text = Gemma 4 26B QAT (--parallel 1, -c 65536)
# multimodal = Qwen3-VL-8B + mmproj (--parallel 4, -c 65536)
NODES=(
    "10.208.211.52|text|8080"
    "10.208.211.53|text|8080"
    "10.208.211.55|text|8080"
    "10.208.211.56|text|8080"
    "10.208.211.57|text|8080"
    "10.208.211.58|text|8080"
    # .59 excluded — display GPU (15,352 MiB VRAM vs 16,376 MiB on headless nodes; OOMs frequently)
    "10.208.211.60|text|8080"
    "10.208.211.61|text|8080"
    "10.208.211.63|multimodal|8080"
    "10.208.211.64|multimodal|8080"
    "10.208.211.65|multimodal|8080"
    "10.208.211.67|multimodal|8080"
)
if [[ "$DOCLING_NODE_AS_WORKER" == "true" ]]; then
    NODES+=("${DOCLING_NODE_IP}|text|8080")
fi
if [[ "$CONTROLLER_AS_WORKER" == "true" ]]; then
    NODES+=("${CONTROLLER_IP}|text|8080")
fi

TEXT_MODEL="/mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf"
MULTIMODAL_MODEL="/mnt/d/Models/qwen-3-vl/Qwen3VL-8B-Instruct-Q8_0.gguf"
MULTIMODAL_MMPROJ="/mnt/d/Models/qwen-3-vl/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

trap 'log "Watchdog stopping (signal received)."; exit 0' SIGTERM SIGINT

remote_exec() {
    local ip="$1"; shift
    sshpass -p "$SSH_PASS" ssh \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        "administrator@${ip}" "$@"
}

restart_text_node() {
    local ip="$1"
    local cmd="pkill llama-server || true; sleep 2; nohup ${LLAMA_SERVER} \
        -m ${TEXT_MODEL} \
        -ngl 999 -c 65536 \
        --host ${ip} --port 8080 \
        --parallel 1 \
        --flash-attn auto --cache-type-k q4_0 --cache-type-v q4_0 \
        --cont-batching --no-context-shift --metrics \
        >/tmp/llama-server.log 2>&1 </dev/null &"
    if [[ "$ip" == "$CONTROLLER_IP" ]]; then
        eval "$cmd"
    else
        # printf %q properly shell-quotes $cmd so the remote /bin/sh passes the
        # entire string as a single argument to bash -c (prevents pkill losing its
        # pattern when SSH concatenates bare arguments).
        remote_exec "$ip" "$(printf 'bash -c %q' "$cmd")"
    fi
}

restart_multimodal_node() {
    local ip="$1"
    local cmd="pkill llama-server || true; sleep 2; nohup ${LLAMA_SERVER} \
        -m ${MULTIMODAL_MODEL} \
        --mmproj ${MULTIMODAL_MMPROJ} \
        -ngl 999 -c 65536 \
        --host ${ip} --port 8080 \
        --parallel 4 \
        --flash-attn auto --cache-type-k q8_0 --cache-type-v q8_0 \
        --cont-batching --metrics \
        >/tmp/llama-server.log 2>&1 </dev/null &"
    remote_exec "$ip" "$(printf 'bash -c %q' "$cmd")"
}

wait_for_health() {
    local ip="$1" port="$2" elapsed=0
    while (( elapsed < RESTART_WAIT )); do
        if curl --noproxy '*' -sf "http://${ip}:${port}/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
        (( elapsed += 5 ))
    done
    return 1
}

raylet_resource_for_type() {
    [[ "$1" == "multimodal" ]] && echo '{"multimodal_node": 1}' || echo '{"text_node": 1}'
}

raylet_alive() {
    local ip="$1"
    remote_exec "$ip" "pgrep -f raylet >/dev/null 2>&1"
}

restart_raylet() {
    local ip="$1" resource="$2"
    local cmd="export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'; \
        export NO_PROXY=\"\$no_proxy\"; export RAY_grpc_enable_http_proxy=0; \
        nohup ${RAY_BIN} start --address=${RAY_HEAD_IP}:${RAY_PORT} \
        --node-ip-address=${ip} --num-gpus=0 --num-cpus=6 \
        --resources='${resource}' >/tmp/ray-worker.log 2>&1 &"
    remote_exec "$ip" "$(printf 'bash -c %q' "$cmd")"
}

wait_for_raylet() {
    local ip="$1" elapsed=0
    while (( elapsed < RESTART_WAIT )); do
        raylet_alive "$ip" && return 0
        sleep 5
        (( elapsed += 5 ))
    done
    return 1
}

check_and_restart_raylet() {
    local ip="$1" type="$2"
    [[ "$ip" == "$CONTROLLER_IP" ]] && return 0  # head node, not a worker raylet
    if raylet_alive "$ip"; then
        return 0
    fi
    log "${ip} (${type}) raylet DOWN — restarting (rejoining Ray cluster)"
    local started
    started=$(date +%s)
    local resource
    resource="$(raylet_resource_for_type "$type")"
    restart_raylet "$ip" "$resource" || { log "${ip} raylet restart command failed"; return 1; }
    if wait_for_raylet "$ip"; then
        local elapsed=$(( $(date +%s) - started ))
        log "${ip} (${type}) raylet recovered after ${elapsed}s"
    else
        log "${ip} (${type}) raylet FAILED to recover after ${RESTART_WAIT}s — will retry next cycle"
    fi
}

check_and_restart() {
    local ip="$1" type="$2" port="$3"
    if curl --noproxy '*' -sf "http://${ip}:${port}/health" >/dev/null 2>&1; then
        return 0
    fi
    log "${ip} (${type}) llama-server DOWN — restarting"
    local started
    started=$(date +%s)
    if [[ "$type" == "multimodal" ]]; then
        restart_multimodal_node "$ip" || { log "${ip} SSH restart command failed"; return 1; }
    else
        restart_text_node "$ip" || { log "${ip} restart command failed"; return 1; }
    fi
    if wait_for_health "$ip" "$port"; then
        local elapsed=$(( $(date +%s) - started ))
        log "${ip} (${type}) recovered after ${elapsed}s"
    else
        log "${ip} (${type}) FAILED to recover after ${RESTART_WAIT}s — will retry next cycle"
    fi
}

log "Watchdog started. Monitoring ${#NODES[@]} nodes every ${POLL_INTERVAL}s (parallel checks)."
# All node checks run as background subshells in parallel.
# Worst-case cycle time is max(single node restart wait) = RESTART_WAIT = 120s
# instead of sum(all waits). Log lines from concurrent restarts may interleave
# but each line includes the node IP so they remain readable.
while true; do
    for node in "${NODES[@]}"; do
        IFS='|' read -r ip type port <<< "$node"
        check_and_restart "$ip" "$type" "$port" &
        check_and_restart_raylet "$ip" "$type" &
    done
    wait   # wait for all parallel checks before sleeping
    sleep "$POLL_INTERVAL"
done
