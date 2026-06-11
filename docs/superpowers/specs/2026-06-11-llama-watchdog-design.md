# Design: llama-server Watchdog

**Date:** 2026-06-11
**Status:** Approved

---

## Problem

llama-server runs via bare `nohup` on each worker. If it crashes, the process stays dead — no supervisor restarts it. The Ray Serve replica on that node remains alive but returns `ConnectError` on every request until someone manually restarts the process.

---

## Solution

`scripts/llama_watchdog.sh` — a long-running background loop on WS-11 that polls each worker's llama health and restarts llama-server if it goes down.

---

## Architecture

```
WS-11 (controller)
└── llama_watchdog.sh  [background loop]
    ├── curl health → WS-03:8080   → SSH restart if down
    ├── curl health → WS-08:8080   → SSH restart if down
    ├── curl health → WS-13:8080   → SSH restart if down
    └── curl health → WS-11:8080   → local restart if down (CONTROLLER_AS_WORKER=true only)
```

---

## Script: `scripts/llama_watchdog.sh`

### Node table

Hardcoded to match `start_linux_worker.sh`:

| Node  | IP             | Type        | Port | Model flags                             |
|-------|----------------|-------------|------|-----------------------------------------|
| WS-03 | 10.208.211.54  | text        | 8080 | Gemma 4 26B QAT, no mmproj             |
| WS-08 | 10.208.211.59  | text        | 8080 | Gemma 4 26B QAT, no mmproj             |
| WS-13 | 10.208.211.64  | multimodal  | 8080 | Qwen3-VL-8B + mmproj                   |
| WS-11 | 10.208.211.62  | text        | 8080 | Gemma 4 26B QAT (only if CONTROLLER_AS_WORKER=true) |

### Loop behaviour

1. Every 30 seconds, iterate over active nodes.
2. For each node: `curl --noproxy '*' -sf http://<ip>:8080/health`
3. If healthy → continue.
4. If unhealthy → trigger restart for that node.

### Restart procedure

1. Log timestamp + node IP + "llama-server down, restarting".
2. SSH into the node (or run locally for WS-11).
3. Kill any stale llama-server: `pkill -f llama-server || true`
4. Sleep 2s.
5. Launch llama-server with same flags as `start_linux_worker.sh`:
   - **Text nodes:** `-ngl 999 -c 131072 --parallel 1 --flash-attn auto --cache-type-k q4_0 --cache-type-v q4_0 --cont-batching --metrics`
   - **Multimodal node:** `--mmproj <path> -ngl 999 -c 32768 --parallel 2 --flash-attn auto --cache-type-k q8_0 --cache-type-v q8_0 --cont-batching --metrics`
6. Wait up to 120s for health to return (poll every 5s).
7. Log result: "recovered" or "failed to recover after 120s".

### Logging

All events written to `/tmp/llama-watchdog.log` with ISO timestamps. Format:
```
[2026-06-11T14:23:01] WS-08 (10.208.211.59) llama-server DOWN — restarting
[2026-06-11T14:23:45] WS-08 (10.208.211.59) recovered after 44s
```

---

## Integration with `startup_scripts/start_linux.sh`

After all workers are verified up (existing Step 2 completion), `start_linux.sh` launches watchdog in background:

```bash
nohup bash "$PROJECT_DIR/scripts/llama_watchdog.sh" \
    > /tmp/llama-watchdog.log 2>&1 &
echo $! > /tmp/llama-watchdog.pid
```

Prints watchdog PID in summary.

---

## Error handling

- SSH failure on restart attempt: log error, skip node, retry on next poll cycle.
- Node never recovers: log "failed to recover" every cycle until it comes back — no alert mechanism (out of scope).
- Watchdog itself crash: not self-healing — user restarts via `start_linux.sh` or manually. Watchdog loop uses `set +e` internally so a single bad iteration doesn't kill the process.

---

## Out of scope

- Restarting Ray Serve replicas (Ray recovers naturally once llama-server is back).
- Alerting / notifications.
- Self-healing watchdog (systemd or supervisord).
- Dynamic node list (hardcoded matches existing infra).
