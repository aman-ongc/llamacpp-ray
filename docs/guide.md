# ONGC LLM Inference Platform — Operations Guide

> Audience: platform administrators and operators.
> Platform: distributed self-hosted Gemma 4 26B inference on ONGC intranet.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Node Inventory](#2-node-inventory)
3. [Starting the Platform](#3-starting-the-platform)
   - [3.1 One-command startup (recommended)](#31-one-command-startup-recommended)
   - [3.2 Manual step-by-step startup](#32-manual-step-by-step-startup)
4. [Stopping the Platform](#4-stopping-the-platform)
5. [User and API Key Management](#5-user-and-api-key-management)
6. [Making Inference Requests](#6-making-inference-requests)
7. [Monitoring and Observability](#7-monitoring-and-observability)
8. [Logs](#8-logs)
9. [Adding Worker Nodes](#9-adding-worker-nodes)
10. [Configuration Reference](#10-configuration-reference)
11. [Health Checks and Smoke Tests](#11-health-checks-and-smoke-tests)
12. [Troubleshooting](#12-troubleshooting)
13. [Backup and Recovery](#13-backup-and-recovery)

---

## 1. Architecture Overview

```
Internal Users / Applications
         |
      NGINX  (WS-11 :10080)
         |
   FastAPI Gateway  (WS-11 :18000)
     |         |
  Postgres   Redis        <-- persistence, rate limiting
         |
   Ray Serve  (WS-11 :8001)
         |
   LlamaCppWorker (Ray actor)
         |
   llama.cpp server  (WS-11 :8080)
         |
   Gemma 4 26B GGUF on RTX A4000
```

All traffic enters via NGINX. The FastAPI gateway handles auth, logging, and rate limiting. It forwards inference requests to Ray Serve, which dispatches to a `LlamaCppWorker` actor. The worker calls the locally running llama.cpp server.

---

## 2. Node Inventory

| Node  | IP              | Role             | SSH | GPU |
|-------|-----------------|------------------|-----|-----|
| WS-11 | 10.208.211.62   | Controller       | Yes | Yes |
| WS-03 | 10.208.211.54   | Worker (pending) | No  | Yes |
| WS-08 | 10.208.211.59   | Worker (pending) | No  | Yes |
| WS-13 | 10.208.211.64   | Worker (offline) | No  | Yes |

SSH into controller:
```bash
ssh administrator@10.208.211.62
# password: Ongc@1234
```

All commands below assume a WSL2 shell on WS-11 unless noted.

---

## 3. Starting the Platform

### 3.1 One-command startup (recommended)

Three startup scripts live in `startup_scripts/`. Each handles all steps in order:
Docker Compose → llama.cpp → Ray head → Ray Serve → health checks. All are idempotent.

#### Linux / WSL2 — run on WS-11

```bash
cd /home/administrator/projects/llm-inference-service
bash startup_scripts/start_linux.sh
```

Requirements: Docker, WSL2 Ubuntu shell on WS-11.

#### macOS — run from any Mac on the ONGC network

```bash
cd /path/to/llm-inference-service
bash startup_scripts/start_mac.sh
```

Requirements: `sshpass` (`brew install hudochenkov/sshpass/sshpass`), network access to `10.208.211.62`.

The script SSH-connects to WS-11 and drives all startup steps remotely.

#### Windows — run on WS-11 (Local mode)

```powershell
cd C:\path\to\llm-inference-service
.\startup_scripts\start_windows.ps1
```

Uses `wsl.exe` to run commands inside the WSL2 distro. No extra tools needed.

#### Windows — run from another Windows PC (Remote mode)

```powershell
.\startup_scripts\start_windows.ps1 -Mode Remote
```

Requires OpenSSH for Windows (Settings → Optional Features → OpenSSH Client) **or** PuTTY `plink.exe` on `PATH`.

#### What the startup scripts do

Each script runs these four steps and waits for health checks at each:

```
Step 1 — Docker Compose up   (Postgres, Redis, Gateway, NGINX, Prometheus, Grafana)
Step 2 — llama.cpp server    (skipped if already running on :8080)
Step 3 — Ray head + Serve    (skipped if Ray already running; Serve always redeployed)
Step 4 — End-to-end verify   (curl through NGINX :10080)
```

On success, the script prints all service URLs and the command to generate an API key.

---

### 3.2 Manual step-by-step startup

Use this if you need to start individual components or troubleshoot.

#### Start Docker Compose stack

```bash
cd /home/administrator/projects/llm-inference-service
printf 'Ongc@1234\n' | sudo -S docker compose up -d
```

Verify all 6 containers are up:
```bash
printf 'Ongc@1234\n' | sudo -S docker compose ps
```

Expected: all containers in state `Up` or `Up (healthy)`.

#### Start llama.cpp inference server

llama.cpp should run in a persistent session (tmux or screen):

```bash
./build/bin/llama-server \
  -m /mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf \
  -ngl 999 -c 65536 \
  --host 10.208.211.62 --port 8080 \
  --parallel 2 --no-context-shift \
  --flash-attn on \
  --cache-type-k q8_0 --cache-type-v q8_0
```

Check it is running:
```bash
curl --noproxy '*' http://10.208.211.62:8080/health
# expected: {"status":"ok"}
```

#### Start Ray head node and Ray Serve

```bash
cd /home/administrator/projects/llm-inference-service
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
bash scripts/start_controller.sh
```

Idempotent — safe to run if Ray is already up. Starts Ray head on port 6379 (if not running) and deploys the `LlamaCppWorker` Serve app on port 8001.

Verify Ray:
```bash
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
ray status
```

Verify Serve:
```bash
curl --noproxy '*' http://10.208.211.62:8001/health
# expected: {"status":"ok","node_ip":"10.208.211.62"}
```

#### Startup order summary

```
1. Docker Compose   — Postgres, Redis, Gateway, NGINX, Prometheus, Grafana
2. llama.cpp server — background session, port 8080
3. Ray + Serve      — scripts/start_controller.sh
```

---

## 4. Stopping the Platform

Stop Docker services:
```bash
cd /home/administrator/projects/llm-inference-service
printf 'Ongc@1234\n' | sudo -S docker compose down
```

Stop Ray (also stops Serve):
```bash
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
ray stop
```

Stop llama.cpp:
```bash
pkill -f llama-server
```

---

## 5. User and API Key Management

All admin calls require: `X-Admin-Secret: <ADMIN_SECRET>` header.
Default secret: `changeme`. Override with `ADMIN_SECRET` env var in docker-compose.yml.

### 5.1 Create a user

```bash
curl --noproxy '*' -X POST http://10.208.211.62:10080/admin/users \
  -H "Content-Type: application/json" \
  -H "X-Admin-Secret: changeme" \
  -d '{"username": "jdoe", "email": "jdoe@ongc.co.in", "department": "Exploration"}'
```

Response includes `"id"` — use it for key creation.

### 5.2 Create an API key for a user

```bash
curl --noproxy '*' -X POST http://10.208.211.62:10080/admin/users/1/keys \
  -H "Content-Type: application/json" \
  -H "X-Admin-Secret: changeme" \
  -d '{"label": "personal"}'
```

Response includes `"api_key"`. **Store it immediately — it is not retrievable later.**

### 5.3 Generate key via script (inside container)

```bash
printf 'Ongc@1234\n' | sudo -S docker compose exec -T gateway \
  python scripts/generate_api_key.py <username>
```

### 5.4 List users

```bash
curl --noproxy '*' http://10.208.211.62:10080/admin/users \
  -H "X-Admin-Secret: changeme"
```

### 5.5 List API keys (prefixes only, no raw keys)

```bash
curl --noproxy '*' http://10.208.211.62:10080/admin/keys \
  -H "X-Admin-Secret: changeme"
```

---

## 6. Making Inference Requests

All inference calls require: `Authorization: Bearer <api_key>`

### 6.1 Chat completions

```bash
curl --noproxy '*' -X POST http://10.208.211.62:10080/v1/chat/completions \
  -H "Authorization: Bearer sk-ongc-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ongc-llm",
    "messages": [
      {"role": "system", "content": "/no_think"},
      {"role": "user", "content": "Summarise the importance of pressure maintenance."}
    ],
    "max_tokens": 512,
    "temperature": 0.7,
    "stream": false
  }'
```

> **Thinking mode:** By default Gemma 4 26B reasons before responding.
> Add `{"role": "system", "content": "/no_think"}` to disable it.
> Without this, `content` may be empty for short responses while `reasoning_content` holds the chain-of-thought.

### 6.2 Streaming response

Set `"stream": true`. Response is SSE (`text/event-stream`), OpenAI-compatible.

### 6.3 List available models

```bash
curl --noproxy '*' http://10.208.211.62:10080/v1/models \
  -H "Authorization: Bearer sk-ongc-..."
```

### 6.4 Using OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-ongc-...",
    base_url="http://10.208.211.62:10080/v1",
)

response = client.chat.completions.create(
    model="ongc-llm",
    messages=[
        {"role": "system", "content": "/no_think"},
        {"role": "user", "content": "What is reservoir pressure?"}
    ],
    max_tokens=256
)
print(response.choices[0].message.content)
```

Set `NO_PROXY=10.208.211.62` in the environment if the corporate proxy intercepts the call.

### 6.5 Rate limits

Default: **60 requests per 60 seconds per user**. Exceeding returns HTTP 429 with `Retry-After: 60`.

---

## 7. Monitoring and Observability

### 7.1 Grafana

URL: `http://10.208.211.62:13000`
Login: `admin` / `ongc1234`

Starter dashboard includes request rate, latency histogram, token throughput, active requests, error rate.

### 7.2 Prometheus

URL: `http://10.208.211.62:9090`

Key metrics exposed by the gateway at `/metrics`:

| Metric | Description |
|--------|-------------|
| `llm_requests_total` | Total requests by model, status, streaming mode |
| `llm_request_latency_ms` | End-to-end latency histogram |
| `llm_active_requests` | Current in-flight requests (gauge) |
| `llm_prompt_tokens_total` | Cumulative prompt tokens by model |
| `llm_completion_tokens_total` | Cumulative completion tokens by model |

### 7.3 Ray Dashboard

URL: `http://10.208.211.62:8265`

Shows: cluster nodes, GPU utilization, Serve deployment health, actor logs.

### 7.4 Admin metrics

```bash
curl --noproxy '*' http://10.208.211.62:10080/admin/metrics \
  -H "X-Admin-Secret: changeme"
# {"users": N, "api_keys": N, "request_logs": N}
```

---

## 8. Logs

### 8.1 Gateway container logs

```bash
cd /home/administrator/projects/llm-inference-service
printf 'Ongc@1234\n' | sudo -S docker compose logs gateway --tail=100 -f
```

### 8.2 Query request logs from Postgres

Every request is logged with user, model, node_ip, tokens, latency, status_code.

```bash
printf 'Ongc@1234\n' | sudo -S docker compose exec -T postgres \
  psql -U llm -d llm_platform -c \
  "SELECT u.username, r.model, r.node_ip, r.latency_ms, r.status_code, r.created_at
   FROM request_logs r JOIN users u ON r.user_id = u.id
   ORDER BY r.created_at DESC LIMIT 20;"
```

Useful queries:

```sql
-- Total tokens per user today
SELECT u.username, SUM(r.prompt_tokens) AS prompt, SUM(r.completion_tokens) AS completion
FROM request_logs r JOIN users u ON r.user_id = u.id
WHERE r.created_at > NOW() - INTERVAL '1 day'
GROUP BY u.username ORDER BY completion DESC;

-- Error summary
SELECT status_code, COUNT(*) FROM request_logs
WHERE created_at > NOW() - INTERVAL '1 hour'
GROUP BY status_code;
```

### 8.3 Ray Serve replica logs

```bash
ls /tmp/ray/session_latest/logs/serve/
tail -f /tmp/ray/session_latest/logs/serve/replica_llama-worker_LlamaCppWorker_*.log
```

### 8.4 NGINX logs

```bash
printf 'Ongc@1234\n' | sudo -S docker compose logs nginx --tail=50 -f
```

---

## 9. Adding Worker Nodes

Workers join the Ray cluster and run additional llama.cpp instances for horizontal scaling.

### Step 1: Enable SSH on the Windows worker

On each Windows worker (WS-03, WS-08), open PowerShell **as Administrator**:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# Copy setup_worker_windows.ps1 to the machine, then:
.\setup_worker_windows.ps1
```

The script:
- Installs Windows OpenSSH Server
- Configures port forwarding: Windows :22 → WSL2 :22
- Starts sshd inside WSL2

### Step 2: Push worker code from controller

```bash
cd /home/administrator/projects/llm-inference-service
bash scripts/deploy_workers.sh
```

### Step 3: Start llama.cpp on the worker

```bash
ssh administrator@10.208.211.54   # replace with worker IP

./build/bin/llama-server \
  -m /mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf \
  -ngl 999 -c 65536 \
  --host 10.208.211.54 --port 8080 \
  --parallel 2 --no-context-shift \
  --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 \
  --cont-batching &
```

### Step 4: Join worker to Ray cluster

```bash
bash /home/administrator/llm-inference-service-worker/worker/start_ray_worker.sh
```

Verify from controller:
```bash
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
ray status
# Should show N+1 active nodes
```

### Step 5: Scale Serve replicas

Edit `worker/ray_worker.py`, increase `num_replicas` in `@serve.deployment(...)`, then redeploy:
```bash
bash scripts/start_controller.sh
```

---

## 10. Configuration Reference

### docker-compose.yml / .env variables

| Variable | Default | Description |
|---|---|---|
| `ADMIN_SECRET` | `changeme` | Secret for all `/admin/*` endpoints |
| `GATEWAY_HOST_PORT` | `18000` | Host port for gateway |
| `NGINX_HOST_PORT` | `10080` | Host port for NGINX |
| `GRAFANA_HOST_PORT` | `13000` | Host port for Grafana |
| `GRAFANA_PASSWORD` | `ongc1234` | Grafana admin password |
| `POSTGRES_HOST_PORT` | `15432` | Host port for Postgres |
| `REDIS_HOST_PORT` | `16379` | Host port for Redis |

Create `.env` in project root to override.

### Gateway settings (via env vars on gateway container)

| Env Var | Default | Description |
|---|---|---|
| `RAY_SERVE_URL` | `http://10.208.211.62:8001` | Ray Serve endpoint |
| `DEFAULT_MODEL` | `ongc-llm` | Model name in `/v1/models` |
| `REQUEST_TIMEOUT_SECONDS` | `300.0` | Timeout for inference calls |
| `CONNECT_TIMEOUT_SECONDS` | `2.0` | Timeout connecting to Ray Serve |
| `LLAMA_PORT` | `8080` | Port llama.cpp listens on per node |
| `LLAMA_CONTEXT` | `65536` | Context window size |
| `LLAMA_PARALLEL` | `2` | Max parallel slots in llama.cpp |

---

## 11. Health Checks and Smoke Tests

### Layer-by-layer checks

```bash
# llama.cpp
curl --noproxy '*' http://10.208.211.62:8080/health

# Ray Serve
curl --noproxy '*' http://10.208.211.62:8001/health

# Gateway (direct)
curl --noproxy '*' http://10.208.211.62:18000/health

# Through NGINX (full path)
curl --noproxy '*' http://10.208.211.62:10080/health
```

### Full smoke test (includes real inference)

```bash
printf 'Ongc@1234\n' | sudo -S docker compose exec -T gateway \
  python scripts/smoke.py --api-key sk-ongc-...
```

Tests health, ready, live, models, user+key creation, chat completions.

### Unit tests

```bash
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
cd /home/administrator/projects/llm-inference-service
NO_PROXY='*' pytest tests -q --tb=short
# expected: 16 passed
```

---

## 12. Troubleshooting

### Gateway returns fallback / synthetic responses

Ray Serve or llama.cpp is unreachable. Gateway falls back to synthetic output.

1. `curl --noproxy '*' http://10.208.211.62:8080/health` — check llama.cpp
2. `curl --noproxy '*' http://10.208.211.62:8001/health` — check Ray Serve
3. If Ray is down: `ray status` then `bash scripts/start_controller.sh`

### Gateway container fails to start

```bash
printf 'Ongc@1234\n' | sudo -S docker compose logs gateway
```
Most common cause: Postgres not ready. Restart after postgres is healthy:
```bash
printf 'Ongc@1234\n' | sudo -S docker compose restart gateway
```

### HTTP 401 Unauthorized

API key invalid or missing. Check keys:
```bash
curl --noproxy '*' http://10.208.211.62:10080/admin/keys \
  -H "X-Admin-Secret: changeme"
```
Generate a new key (Section 5.3).

### HTTP 429 Too Many Requests

Rate limit exceeded. Wait 60s or clear the Redis key:
```bash
printf 'Ongc@1234\n' | sudo -S docker compose exec -T redis \
  redis-cli DEL rl:user:<user_id>
```

### Ray workers not joining

Check connectivity from the worker:
```bash
nc -zv 10.208.211.62 6379
```
Ensure these are exported before `ray start`:
```bash
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"
export RAY_grpc_enable_http_proxy=0
```

### Empty `content` in responses

Gemma 4 26B is in thinking mode. Chain-of-thought is in `reasoning_content`; `content` is empty on short prompts.
Fix: add `{"role": "system", "content": "/no_think"}` to every request.

### Corporate proxy blocking internal calls

All internal HTTP must bypass the proxy:
```bash
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
```
Use `curl --noproxy '*'` for all testing.

---

## 13. Backup and Recovery

### Database backup

```bash
printf 'Ongc@1234\n' | sudo -S docker compose exec -T postgres \
  pg_dump -U llm llm_platform > backup_$(date +%Y%m%d_%H%M).sql
```

### Restore database

```bash
cat backup_YYYYMMDD_HHMM.sql | \
  printf 'Ongc@1234\n' | sudo -S docker compose exec -T postgres \
  psql -U llm llm_platform
```

### Data volume location

```bash
printf 'Ongc@1234\n' | sudo -S docker volume inspect llm-inference-service_postgres-data
```

### Full recovery after restart

```bash
# 1. Start Docker stack
cd /home/administrator/projects/llm-inference-service
printf 'Ongc@1234\n' | sudo -S docker compose up -d

# 2. Start llama.cpp (in tmux/screen, using command from Section 3.2)

# 3. Start Ray + Serve
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
bash scripts/start_controller.sh

# 4. Verify
curl --noproxy '*' http://10.208.211.62:10080/health
```

Postgres data, Grafana dashboards, and all API keys/users survive restarts via Docker volumes.

---

*ONGC Intranet AI Infrastructure — Gemma 4 26B + llama.cpp + Ray Serve + FastAPI*
