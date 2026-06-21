#!/usr/bin/env bash
# llama-server watchdog — runs on WS-11, monitors all worker nodes.
# Started by startup_scripts/start_linux.sh after cluster is up.
# Logs to stdout (redirected to /tmp/llama-watchdog.log by caller).
set +e  # single bad iteration must NOT kill the loop

POLL_INTERVAL=30
RESTART_WAIT=120
# Hard ceiling on any single SSH call (health curl or restart command). SSH's
# own ConnectTimeout only covers the TCP-connect phase — a stuck banner/key
# exchange past that point can hang indefinitely with no recovery. Wrapping
# every SSH/curl call in `timeout` bounds the damage to SSH_TIMEOUTs, so one
# wedged node can't freeze monitoring of any other node.
SSH_TIMEOUT=20
SSH_PASS="${SSH_PASS:-Ongc@1234}"
LLAMA_SERVER="${LLAMA_SERVER:-/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server}"
CONTROLLER_AS_WORKER="${CONTROLLER_AS_WORKER:-false}"
CONTROLLER_IP="10.208.211.62"
# WS-3 (.54): docling/dev node — excluded from watchdog unless DOCLING_NODE_AS_WORKER=true.
DOCLING_NODE_AS_WORKER="${DOCLING_NODE_AS_WORKER:-false}"
DOCLING_NODE_IP="10.208.211.54"

# Node list: "ip|type|port"
# text = Gemma 4 26B QAT (--parallel 1, -c 65536)
# multimodal = Qwen3-VL-8B + mmproj (--parallel 2, -c 65536)
# .60/.61 moved text→multimodal 2026-06-20 (CPU-contention rebalance).
NODES=(
    "10.208.211.52|text|8080"
    "10.208.211.53|text|8080"
    "10.208.211.55|text|8080"
    "10.208.211.56|text|8080"
    "10.208.211.57|text|8080"
    "10.208.211.58|text|8080"
    # .59 excluded — display GPU (15,352 MiB VRAM vs 16,376 MiB on headless nodes; OOMs frequently)
    "10.208.211.60|multimodal|8080"
    "10.208.211.61|multimodal|8080"
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
    timeout "$SSH_TIMEOUT" sshpass -p "$SSH_PASS" ssh \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        "administrator@${ip}" "$@"
}

restart_text_node() {
    local ip="$1"
    local cmd="pkill llama-server || true; sleep 2; \
        mv /tmp/llama-server.log /tmp/llama-server.log.prev 2>/dev/null; \
        nohup ${LLAMA_SERVER} \
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
    local cmd="pkill llama-server || true; sleep 2; \
        mv /tmp/llama-server.log /tmp/llama-server.log.prev 2>/dev/null; \
        nohup ${LLAMA_SERVER} \
        -m ${MULTIMODAL_MODEL} \
        --mmproj ${MULTIMODAL_MMPROJ} \
        -ngl 999 -c 65536 \
        --host ${ip} --port 8080 \
        --parallel 2 \
        --flash-attn auto --cache-type-k q8_0 --cache-type-v q8_0 \
        --cont-batching --metrics \
        >/tmp/llama-server.log 2>&1 </dev/null &"
    remote_exec "$ip" "$(printf 'bash -c %q' "$cmd")"
}

wait_for_health() {
    local ip="$1" port="$2" elapsed=0
    while (( elapsed < RESTART_WAIT )); do
        if curl --noproxy '*' -sf --max-time 5 "http://${ip}:${port}/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
        (( elapsed += 5 ))
    done
    return 1
}

check_and_restart() {
    local ip="$1" type="$2" port="$3"
    if curl --noproxy '*' -sf --max-time 5 "http://${ip}:${port}/health" >/dev/null 2>&1; then
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

monitor_node() {
    local ip="$1" type="$2" port="$3"
    while true; do
        check_and_restart "$ip" "$type" "$port"
        sleep "$POLL_INTERVAL"
    done
}

log "Watchdog started. Monitoring ${#NODES[@]} nodes every ${POLL_INTERVAL}s (independent per-node loops)."
# Each node gets its own long-running loop instead of "spawn all, wait for
# all, sleep, repeat" — a shared `wait` barrier meant one wedged node could
# freeze the whole cycle (and thus monitoring of every other node) for as
# long as its SSH session hung, which is exactly what happened in production
# (one stuck SSH session to a single node froze the entire watchdog for 5
# hours). With independent loops + the SSH_TIMEOUT bound above, a wedged
# node retries on its own schedule and can never block its peers.
for node in "${NODES[@]}"; do
    IFS='|' read -r ip type port <<< "$node"
    monitor_node "$ip" "$type" "$port" &
done
wait   # only returns at watchdog shutdown (all per-node loops are infinite)
