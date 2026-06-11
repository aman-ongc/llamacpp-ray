#!/usr/bin/env bash
# llama-server watchdog — runs on WS-11, monitors all worker nodes.
# Started by startup_scripts/start_linux.sh after cluster is up.
# Logs to stdout (redirected to /tmp/llama-watchdog.log by caller).
set +e  # single bad iteration must NOT kill the loop

POLL_INTERVAL=30
RESTART_WAIT=120
SSH_PASS="${SSH_PASS:-Ongc@1234}"
LLAMA_SERVER="${LLAMA_SERVER:-/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server}"
CONTROLLER_AS_WORKER="${CONTROLLER_AS_WORKER:-false}"
CONTROLLER_IP="10.208.211.62"

# Node list: "ip|type|port"
# text = Gemma 4 26B QAT   multimodal = Qwen3-VL-8B + mmproj
NODES=(
    "10.208.211.54|text|8080"
    "10.208.211.59|text|8080"
    "10.208.211.64|multimodal|8080"
)
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
    local cmd="pkill -f llama-server || true; sleep 2; nohup ${LLAMA_SERVER} \
        -m ${TEXT_MODEL} \
        -ngl 999 -c 32768 \
        --host ${ip} --port 8080 \
        --parallel 1 \
        --flash-attn auto --cache-type-k q4_0 --cache-type-v q4_0 \
        --cont-batching --no-context-shift --metrics \
        >/tmp/llama-server.log 2>&1 </dev/null &"
    if [[ "$ip" == "$CONTROLLER_IP" ]]; then
        eval "$cmd"
    else
        remote_exec "$ip" bash -c "$cmd"
    fi
}

restart_multimodal_node() {
    local ip="$1"
    remote_exec "$ip" bash -c "pkill -f llama-server || true; sleep 2; \
        nohup ${LLAMA_SERVER} \
        -m ${MULTIMODAL_MODEL} \
        --mmproj ${MULTIMODAL_MMPROJ} \
        -ngl 999 -c 32768 \
        --host ${ip} --port 8080 \
        --parallel 2 \
        --flash-attn auto --cache-type-k q8_0 --cache-type-v q8_0 \
        --cont-batching --metrics \
        >/tmp/llama-server.log 2>&1 </dev/null &"
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

log "Watchdog started. Monitoring ${#NODES[@]} nodes every ${POLL_INTERVAL}s."
# NOTE: node checks are sequential. If a node triggers a recovery wait (up to
# RESTART_WAIT=120s), checks on subsequent nodes are delayed for that duration.
# With 3 nodes, worst-case gap between checks on the last node is ~360s.
while true; do
    for node in "${NODES[@]}"; do
        IFS='|' read -r ip type port <<< "$node"
        check_and_restart "$ip" "$type" "$port"
    done
    sleep "$POLL_INTERVAL"
done
