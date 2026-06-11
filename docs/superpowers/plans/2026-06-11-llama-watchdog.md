# llama-server Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a long-running background watchdog on WS-11 that detects crashed llama-server processes on any worker and restarts them with the correct flags.

**Architecture:** `scripts/llama_watchdog.sh` runs as a `nohup` background process on WS-11. Every 30 seconds it polls each worker's `/health` endpoint via direct `curl`. On failure, it SSHes in (or restarts locally for WS-11), kills any stale process, relaunches llama-server with the same flags as `start_linux_worker.sh`, then waits up to 120s for health to return. `startup_scripts/start_linux.sh` starts the watchdog after all nodes are verified up.

**Tech Stack:** bash, sshpass, curl, nohup — no new dependencies

---

## File Map

| Action | File | Purpose |
|--------|------|---------|
| Create | `scripts/llama_watchdog.sh` | Long-running watchdog loop |
| Modify | `startup_scripts/start_linux.sh` | Launch watchdog after cluster is up |

---

### Task 1: Write `scripts/llama_watchdog.sh`

**Files:**
- Create: `scripts/llama_watchdog.sh`

This script runs forever on WS-11. No test framework exists for infra bash — correctness is verified by code review and the integration test in Task 3.

- [ ] **Step 1: Create the script**

```bash
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
        -ngl 999 -c 131072 \
        --host ${ip} --port 8080 \
        --parallel 1 \
        --flash-attn auto --cache-type-k q4_0 --cache-type-v q4_0 \
        --cont-batching --metrics \
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
while true; do
    for node in "${NODES[@]}"; do
        IFS='|' read -r ip type port <<< "$node"
        check_and_restart "$ip" "$type" "$port"
    done
    sleep "$POLL_INTERVAL"
done
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/llama_watchdog.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/llama_watchdog.sh
git commit -m "feat: add llama-server watchdog script"
```

---

### Task 2: Wire watchdog into `startup_scripts/start_linux.sh`

**Files:**
- Modify: `startup_scripts/start_linux.sh`

Add watchdog launch after the Step 3 NGINX health check passes (around line 211). Insert the following block immediately after the `wait_for_http "http://${CONTROLLER_IP}:10080/health" ...` line and before the `echo ""` that starts the summary:

- [ ] **Step 1: Add watchdog launch block**

Find this line in `startup_scripts/start_linux.sh`:
```bash
wait_for_http "http://${CONTROLLER_IP}:10080/health" "NGINX → Gateway" 15
```

Insert immediately after it:
```bash

# ── Watchdog ──────────────────────────────────────────────────────────────────
info "Starting llama-server watchdog..."
pkill -f llama_watchdog.sh 2>/dev/null || true
sleep 1
CONTROLLER_AS_WORKER="$CONTROLLER_AS_WORKER" \
LLAMA_SERVER="$LLAMA_SERVER" \
SSH_PASS="$SUDO_PASS" \
nohup bash "$PROJECT_DIR/scripts/llama_watchdog.sh" \
    >> /tmp/llama-watchdog.log 2>&1 &
echo $! > /tmp/llama-watchdog.pid
ok "Watchdog PID $(cat /tmp/llama-watchdog.pid) — log: /tmp/llama-watchdog.log"
```

- [ ] **Step 2: Add watchdog info line to summary printout**

Find this block in the summary section:
```bash
echo -e "  ${CYAN}Gateway direct${NC}  http://${CONTROLLER_IP}:18000"
```

Add after it:
```bash
echo -e "  ${CYAN}Watchdog log${NC}    /tmp/llama-watchdog.log  (PID: $(cat /tmp/llama-watchdog.pid 2>/dev/null || echo 'not started'))"
```

- [ ] **Step 3: Commit**

```bash
git add startup_scripts/start_linux.sh
git commit -m "feat: launch llama-server watchdog from start_linux.sh"
```

---

### Task 3: Integration test

No automated test harness — verify manually on live cluster.

- [ ] **Step 1: Start watchdog in a terminal on WS-11 (without full restart)**

```bash
cd /home/administrator/projects/llm-inference-service
LLAMA_SERVER=/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server \
SSH_PASS=Ongc@1234 \
bash scripts/llama_watchdog.sh 2>&1 | tee /tmp/llama-watchdog-test.log
```

Expected first log line:
```
[2026-06-11T...Z] Watchdog started. Monitoring 3 nodes every 30s.
```

- [ ] **Step 2: Kill llama-server on WS-08 to trigger watchdog**

In a second terminal:
```bash
sshpass -p 'Ongc@1234' ssh administrator@10.208.211.59 'pkill -f llama-server'
```

- [ ] **Step 3: Verify watchdog detects and restarts**

Watch the watchdog log (first terminal). Within 30s you should see:
```
[...Z] 10.208.211.59 (text) llama-server DOWN — restarting
[...Z] 10.208.211.59 (text) recovered after <N>s
```

- [ ] **Step 4: Verify llama-server is back on WS-08**

```bash
sshpass -p 'Ongc@1234' ssh administrator@10.208.211.59 \
    'curl --noproxy "*" -sf http://10.208.211.59:8080/health'
```

Expected:
```json
{"status":"ok"}
```

- [ ] **Step 5: Stop test watchdog, commit nothing (integration test only)**

`Ctrl+C` in first terminal.

---

## Self-Review

**Spec coverage check:**
- ✅ Long-running background loop on WS-11 — Task 1 main loop
- ✅ Poll every 30s — `POLL_INTERVAL=30`
- ✅ Direct curl health check (no SSH for check) — `check_and_restart` uses `curl`
- ✅ SSH restart on failure — `remote_exec` + `restart_text_node`/`restart_multimodal_node`
- ✅ Same flags as `start_linux_worker.sh` — flags match exactly
- ✅ Wait 120s for recovery — `RESTART_WAIT=120`, `wait_for_health`
- ✅ Timestamp logs — `log()` function uses ISO8601
- ✅ WS-11 conditional on `CONTROLLER_AS_WORKER` — guarded array append
- ✅ Local restart for WS-11 (no SSH to self) — `eval "$cmd"` branch
- ✅ SSH failure logged, loop continues — `set +e` + `|| { log ...; return 1; }`
- ✅ `start_linux.sh` launches watchdog after cluster up — Task 2
- ✅ PID file written — `echo $! > /tmp/llama-watchdog.pid`
- ✅ Log path `/tmp/llama-watchdog.log` — confirmed in Task 2

**Placeholder scan:** None found.

**Type consistency:** Only bash — no type mismatches. Function names consistent across all references.
