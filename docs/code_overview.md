# Codebase Overview — ONGC LLM Inference Platform

This document explains what every component in this codebase does, why it exists, and how the pieces fit together. It is written for someone new to the project.

---

## What This System Does

This is a **distributed LLM inference platform** built for ONGC's internal intranet. It takes inference requests from internal users, routes them across multiple GPU workstations, runs the Qwen 3.6 35B model on each, and returns responses through a single unified API — all without relying on any cloud or external services.

The four physical nodes are:

| Node  | IP             | Role                 |
|-------|----------------|----------------------|
| WS-11 | 10.208.211.62  | Controller (head)    |
| WS-03 | 10.208.211.54  | Worker               |
| WS-08 | 10.208.211.59  | Worker               |
| WS-13 | 10.208.211.64  | Worker               |

Each node has 1x NVIDIA RTX A4000 (16 GB VRAM), 128 GB RAM, and runs Ubuntu 24.04 under WSL2.

---

## High-Level Architecture

```
Internal Users
      |
   NGINX (port 10080)          ← reverse proxy / ACL
      |
FastAPI Gateway (port 8000)    ← auth, logging, rate-limiting, routing
      |
Ray Serve HTTP proxies (port 8001, one per node)
      |
LlamaCppWorker replicas (one per GPU node, managed by Ray)
      |
llama-server processes (one per node, port 8080)   ← actual model inference
      |
Qwen3.6-35B GGUF model on GPU
```

Supporting services running in Docker on WS-11:

- **PostgreSQL** — stores users, API keys, request logs
- **Redis** — sliding-window rate limiting
- **Prometheus** — scrapes metrics from all nodes
- **Grafana** — dashboards over Prometheus and Postgres

---

## Repository Layout

```
llamacpp-ray/
├── gateway/           # FastAPI application — the central brain
├── worker/            # Ray Serve worker — bridges Ray to llama-server
├── infra/             # Config files for NGINX, Prometheus, Grafana
├── docker/            # Dockerfiles for gateway and worker images
├── scripts/           # Deployment, DB init, benchmarking, tooling
├── startup_scripts/   # Full platform start/stop scripts per OS
├── tests/             # Pytest unit tests for the gateway
├── docker-compose.yml # Defines all Docker services on WS-11
├── requirements.txt   # Python dependencies
└── docs/              # Documentation
```

---

## Gateway (`gateway/`)

The gateway is the platform's control layer. It is a FastAPI application that all clients talk to. It handles authentication, rate limiting, request logging, metrics, and forwards inference work to Ray Serve.

### `gateway/main.py`

Entry point. Creates the FastAPI application, registers all routers, and defines the lifespan hook. Nothing runs here except wiring.

### `gateway/config.py`

Single `Settings` class (using `pydantic-settings`) that reads all configuration from environment variables or a `.env` file. Key settings:

- **Database/Redis URLs** — where persistent services live
- **Ray Serve URL** — where to send inference requests (`http://10.208.211.62:8001`)
- **Controller/worker IPs** — used by `ray_client.py` to build the round-robin list
- **llama-server parameters** — model path, port, context size, parallelism, GPU layers
- **`enable_thinking`** — Qwen3.6 has a "thinking mode" (chain-of-thought reasoning); this flag is off by default and can be toggled globally or per-request
- **`no_proxy`** — critical for intranet environments; ensures internal traffic bypasses the corporate HTTP proxy at `10.205.122.201:8080`

A single `settings` singleton is imported everywhere. Never instantiate `Settings` again.

### `gateway/models.py`

SQLAlchemy ORM models. Three tables:

- **`User`** — one row per person/application. Fields: username, email, department, `is_active`, `is_admin`.
- **`APIKey`** — one or more keys per user. Stores only a SHA-256 hash of the raw key (the raw key is shown once at creation and never stored). `key_prefix` is the first 12 characters, used for audit logs without storing the full key.
- **`RequestLog`** — one row per inference request. Records user, model, node served, prompt tokens, completion tokens, latency, queue time, HTTP status, whether it was a streaming request, and any error message.

### `gateway/database.py`

Creates the async SQLAlchemy engine and session factory. Exposes `get_db()`, a FastAPI dependency that yields a session per request.

### `gateway/auth/service.py`

Pure functions for key lifecycle:

- `generate_api_key()` — generates a `sk-ongc-<random>` token using `secrets.token_urlsafe`.
- `hash_api_key()` — SHA-256 hash. Stored in DB; the raw key is never persisted.
- `validate_key_against_db()` — looks up the hash in DB, checks the key and user are active, updates `last_used_at`, returns the `APIKey` ORM object (with user eagerly loaded).

### `gateway/auth/middleware.py`

FastAPI dependency `require_api_key`. Called on every protected route. Reads the `Authorization: Bearer <key>` header, calls `validate_key_against_db`, attaches the key prefix to `request.state`, and returns the `User`. Raises HTTP 401 if missing or invalid.

### `gateway/rate_limiter.py`

Sliding-window rate limiter backed by Redis. For each user, stores a sorted set keyed `rl:user:<id>` where each member is a timestamp. On each request:
1. Removes entries older than the window (default 60 s).
2. Adds the current timestamp.
3. Counts remaining entries.
4. Raises HTTP 429 if over limit (default 60 req/60 s).

If Redis is unavailable, the `RedisError` is caught and rate limiting is skipped (fail-open). This is intentional — Redis is not a hard dependency for correctness.

### `gateway/ray_client.py`

The connection between the gateway and Ray Serve. Two public functions:

- `submit_inference(payload, affinity_key=None)` — sends a non-streaming POST to `/v1/chat/completions` on a Ray Serve proxy and returns the JSON response.
- `stream_inference(payload, affinity_key=None)` — same but streams the response line by line as SSE.

**Load balancing — two modes:**

At module load time, `_build_proxy_urls()` constructs one URL per cluster node (controller + all workers), all on port 8001. This list is stored in `_proxy_urls` and an `itertools.cycle` iterator over it is used for round-robin.

| Mode | Function | Behaviour |
|------|----------|-----------|
| Round-robin (affinity off) | `_next_proxy_url()` | Advances the cycle; each call picks the next node in sequence |
| Session affinity (affinity on) | `_affinity_proxy_url(key)` | `_proxy_urls[hash(key) % len(_proxy_urls)]` — same key always maps to the same node |

`_select_proxy_url(affinity_key)` is the single decision point: if `affinity_key` is set, it calls `_affinity_proxy_url`; otherwise it calls `_next_proxy_url`. Both inference functions call `_select_proxy_url`.

Since Ray Serve runs an HTTP proxy on every node (EveryNode mode), and each proxy routes to its local replica, directing a request to a specific proxy URL reliably routes it to that node's GPU replica — which is where llama.cpp holds the cached KV context for that user.

**Proxy bypass:** Sets `NO_PROXY` before each request to prevent the corporate HTTP proxy from intercepting internal traffic.

### `gateway/metrics.py`

Prometheus metrics exposed at `/metrics`. Defines five metrics:

| Metric | Type | Labels |
|--------|------|--------|
| `llm_requests_total` | Counter | model, status_code, streaming |
| `llm_request_latency_ms` | Histogram | model |
| `llm_prompt_tokens_total` | Counter | model |
| `llm_completion_tokens_total` | Counter | model |
| `llm_active_requests` | Gauge | (none) |

`metrics_response()` serializes all registered metrics in Prometheus text format.

### `gateway/logging_/request_logger.py`

Single function `log_request()` that creates a `RequestLog` row and commits it. Called at the end of every inference request (both streaming and non-streaming). Records everything needed for audit, billing, and debugging.

---

## Routers (`gateway/routers/`)

Each router handles a distinct API surface. They are registered in `main.py`.

### `routers/health.py` — `/health`, `/ready`, `/live`

Three liveness/readiness probes. Return static JSON. Used by Docker healthchecks, load balancers, and the startup scripts to know when the gateway is up.

### `routers/models_router.py` — `GET /v1/models`

Returns the list of available models in OpenAI format. Currently returns a single model (`qwen`) from settings. No auth required — matches OpenAI's behavior where model listing is public.

### `routers/metrics_router.py` — `GET /metrics`

Delegates to `metrics_response()`. NGINX restricts this endpoint to the internal subnet (`10.208.211.0/24`) so it is not publicly accessible.

### `routers/completions.py` — `POST /v1/completions`

Stub for the legacy text completions API. Returns a placeholder response directing clients to use `/v1/chat/completions` instead. Requires auth. Exists for OpenAI-compatibility surface — some clients probe this endpoint.

### `routers/chat.py` — `POST /v1/chat/completions`

The main inference endpoint. Request body fields:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | str | `settings.default_model` | Model name |
| `messages` | list | required | OpenAI chat message format |
| `max_tokens` | int | 256 | Max completion length |
| `temperature` | float | 0.7 | Sampling temperature |
| `stream` | bool | False | Enable SSE streaming |
| `enable_thinking` | bool\|None | None | Per-request Qwen3.6 thinking mode override |
| `session_affinity` | bool | **True** | Route to same worker per API key for KV cache reuse |

`enable_thinking` and `session_affinity` are gateway-only fields (marked `exclude=True`) — they are stripped before the payload is forwarded to Ray Serve.

The request flow:

1. **Auth** — `require_api_key` dependency validates the Bearer token; attaches `api_key_prefix` to `request.state`.
2. **Rate limit** — `check_rate_limit` checks Redis; skips gracefully if Redis is down.
3. **Affinity key** — `affinity_key = api_key_prefix if payload.session_affinity else None`. This is passed to `submit_inference` / `stream_inference`, which use it to select the target Ray Serve proxy via `_select_proxy_url`.
4. **Payload construction** — `_payload_for_inference()` converts the request to a dict and injects `chat_template_kwargs.enable_thinking`.
5. **Inference dispatch:**
   - If `stream=True`: calls `stream_inference(payload, affinity_key)` and returns a `StreamingResponse` (SSE). On failure, falls back to a synthetic SSE stream.
   - If `stream=False`: calls `submit_inference(payload, affinity_key)`. On failure, returns `_fallback_response()`.
6. **Response normalization** — `_normalize_completion_response()` handles Qwen3.6's thinking mode: if `content` is empty but `reasoning_content` is present, promotes reasoning to content and removes the internal field.
7. **Logging** — writes a `RequestLog` row with all metrics.
8. **Prometheus** — increments counters and records latency.
9. **`ACTIVE_REQUESTS` gauge** — incremented at start, decremented in `finally` to always track in-flight requests correctly.

The fallback mechanism means the gateway never returns a 5xx to the client when Ray Serve is temporarily unavailable — it returns a synthetic answer noting the system is unavailable. This is a conscious trade-off for availability over accuracy.

### `routers/admin.py` — `POST/GET /admin/*`

Admin API protected by `X-Admin-Secret` header (shared secret set in environment). NGINX restricts this to the internal subnet.

Endpoints:
- `GET /admin/users` — list all users
- `POST /admin/users` — create a user (username, email, department)
- `POST /admin/users/{user_id}/keys` — generate an API key for a user; returns the raw key **once**
- `GET /admin/keys` — list all keys (prefixes only, never hashes)
- `GET /admin/metrics` — returns total counts of users, keys, and log rows

---

## Worker (`worker/`)

The worker layer bridges Ray Serve to the locally-running `llama-server` process on each GPU node.

### `worker/ray_worker.py`

Defines `LlamaCppWorker`, a Ray Serve deployment class. Key design points:

- Decorated with `@serve.deployment(num_replicas=settings.serve_replicas, ray_actor_options={"num_gpus": 1, "num_cpus": 1})` — Ray will place exactly one replica per GPU-capable node, ensuring each physical machine gets one actor.
- On init, discovers its own node's IP via `get_node_ip_address()` and builds its local `llama-server` URL (`http://<node_ip>:8080`).
- `__call__` is the ASGI entry point — routes requests to `health()` or `chat()`.
- `chat()` forwards the payload to the local `llama-server`'s `/v1/chat/completions` endpoint via `httpx`, then appends `node_ip` and `queue_ms` to the response so the gateway can log which node served the request.
- On `httpx` failure (llama-server down or overloaded), returns a synthetic fallback response with the error indicated in the content — the worker also fails open.

`app = LlamaCppWorker.bind()` is the Ray Serve application handle used by `deploy_serve.py`.

### `worker/llama_process.py`

`LlamaServerProcess` — a helper class that manages the `llama-server` subprocess on the local node. Builds the full CLI command from `settings`, starts the process with `subprocess.Popen`, and provides `start()` / `stop()` methods. Stdout/stderr are discarded (logs go to `/tmp/llama-server.log` when started by shell scripts).

This class is not currently called from the Ray worker directly — the startup scripts launch `llama-server` as an independent process before Ray workers join. `LlamaServerProcess` exists as a programmatic control handle if needed.

### `worker/start_ray_worker.sh`

Shell script run on each worker node to attach it to the Ray head. Sets proxy env vars, then calls `ray start --address=<head>:<port> --num-gpus=1 --num-cpus=6 --block`. The `--block` flag keeps the script alive (Ray worker runs in foreground).

---

## Infrastructure (`infra/`)

### `infra/nginx/nginx.conf`

NGINX acts as the public-facing reverse proxy:

- `/v1/*` — proxied to the gateway, streaming enabled (`proxy_buffering off`, `chunked_transfer_encoding on`, `proxy_read_timeout 300s`).
- `/health` — proxied to the gateway, no restrictions.
- `/admin/*` — proxied to the gateway, restricted to `10.208.211.0/24`.
- `/metrics` — proxied to the gateway, restricted to `10.208.211.0/24`.

NGINX is the only service that should be exposed outside the internal subnet.

### `infra/prometheus/prometheus.yml`

Prometheus scrape configuration. Scrapes four job types:

| Job | Targets | Port |
|-----|---------|------|
| `llm-gateway` | Docker service `gateway` | 8000 (`/metrics`) |
| `node-exporter` | All 4 nodes | 9100 |
| `nvidia-gpu-exporter` | All 4 nodes | 9835 |
| `llama-cpp` | All 4 nodes | 8080 (`/metrics`, native llama.cpp metrics) |

### `infra/grafana/provisioning/`

Grafana auto-provisioning configuration:

- `datasources/prometheus.yml` — registers Prometheus (`http://prometheus:9090`) and the PostgreSQL DB as Grafana data sources automatically on startup.
- `dashboards/dashboard.yml` — tells Grafana to load dashboard JSON files from `/var/lib/grafana/dashboards`.

### `infra/grafana/dashboards/`

Pre-built Grafana dashboard JSON files:
- `llm_platform.json` — platform-level metrics (request rate, latency, token throughput, active requests).
- `worker_health.json` — per-node GPU utilization, VRAM usage, and llama-server metrics.

---

## Docker Compose (`docker-compose.yml`)

Defines all services that run on WS-11 (the controller) as containers on an isolated `llm-net` bridge network:

| Service | Image | Purpose | Exposed Port |
|---------|-------|---------|--------------|
| `postgres` | postgres:16-alpine | User/key/log storage | 15432 (host) |
| `redis` | redis:7-alpine | Rate limiter state | 16379 (host) |
| `gateway` | Built from `docker/gateway/Dockerfile` | FastAPI application | 18000 (host) |
| `prometheus` | prom/prometheus | Metrics collection | 9090 |
| `grafana` | grafana/grafana | Dashboards | 13000 (host) |
| `nginx` | nginx:alpine | Public reverse proxy | 10080 (host) |

The gateway depends on postgres being healthy and redis being started. Grafana depends on prometheus and postgres.

**Port mapping rationale:** Host ports are offset (e.g., 18000 instead of 8000) to avoid conflicts with host processes. The intended public entry point is NGINX on port 10080 (or 80 if mapped differently in production).

---

## Dockerfiles (`docker/`)

### `docker/gateway/Dockerfile`

Builds the gateway image:
- Python 3.12-slim base.
- Installs `requirements.txt` using the corporate proxy (`--proxy http://10.205.122.201:8080`).
- Copies `gateway/` and `scripts/` directories.
- Sets `no_proxy` env vars baked into the image.
- Runs `uvicorn gateway.main:app` on port 8000.

### `docker/worker/Dockerfile`

Builds a worker image (not currently used in compose — workers run on bare metal):
- Same base and proxy setup.
- Copies `gateway/` (for `config.py` and `models.py`) and `worker/`.
- Sets `RAY_grpc_enable_http_proxy=false` to prevent Ray gRPC from going through the corporate proxy.
- CMD runs `python -m worker.ray_worker` (not currently the deployment path — workers are started via shell scripts).

---

## Scripts (`scripts/`)

Operational tooling. All run from WS-11 (the controller).

### `scripts/init_db.py`

Bootstrap script. Connects to Postgres, creates all tables via SQLAlchemy metadata, then checks if an `admin` user exists. If not, creates one with an API key (printed once to stdout). Run once on first deployment.

Also handles a schema migration: if the `api_keys` table exists but lacks the `metadata` column, adds it. This avoids needing a full migration framework for a single-column addition.

### `scripts/generate_api_key.py`

CLI utility to create additional API keys for existing users. Takes a username, generates a key, stores the hash in DB, prints the raw key. Used post-bootstrap to provision keys for individual users.

Usage: `python scripts/generate_api_key.py <username> [--label <label>]`

### `scripts/deploy_serve.py`

Connects to the running Ray head node (`ray.init(address="auto")`) and deploys `LlamaCppWorker` as a Ray Serve application. Key behaviors:

- Sets `RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING=0` **before** importing Ray, which disables locality routing. Without this, the head node's HTTP proxy would send all requests to its own collocated replica, starving the worker replicas.
- Starts Serve with `location="EveryNode"` — one HTTP proxy per cluster node, all on port 8001.
- Runs `LlamaCppWorker.bind()` with 4 replicas distributed across GPU nodes.

### `scripts/deploy_workers.sh`

SSH into each worker node and rsync the `gateway/`, `worker/`, and `requirements.txt` from the controller. Ensures all workers have the latest code before Ray workers start. Uses `sshpass` for non-interactive password auth.

### `scripts/start_linux_worker.sh`

Full bootstrap for a single worker node. SSH in via `sshpass`, then:
1. Starts `llama-server` if not already running (with the production flags: `-ngl 999`, 65536 context, 2 parallel slots, flash attention, KV cache quantization, continuous batching).
2. Starts the Ray worker process (`ray start --address=<head>`).
3. Waits for llama-server to respond on `/health`.

### `scripts/deploy_exporters.sh`

Pushes `start_node_exporter.sh`, `start_gpu_exporter.sh`, and `install_exporters_cron.sh` to all 4 nodes, executes them, and verifies that the exporters are reachable. Needed once per node to get Prometheus metrics collection working.

### `scripts/smoke.py`

Internal validation script for the gateway container. Uses only stdlib (`urllib`) — no extra deps needed. Tests:
1. Health/ready/live endpoints.
2. Admin user creation and key generation.
3. A single chat completion round-trip.

Useful after deployment to verify end-to-end functionality before exposing to users.

### `scripts/benchmark.py`

Full-featured benchmark suite. Uses `httpx` with proxy bypass. Modes:
- **distribution** — hits Ray Serve `/health` 12 times directly to verify replicas are spread across multiple nodes.
- **sequential** — baseline latency with no concurrency.
- **streaming** — measures time-to-first-token (TTFT).
- **throughput** — ramps concurrency 1→2→4 to find saturation.
- **concurrent** — custom concurrency level.
- **all** — runs all of the above.

Reports min/median/p95/p99/max latency, avg tokens/sec, and per-node request distribution.

### `scripts/enable_ssh_workers.py`

One-time script run at initial cluster setup to install and start the OpenSSH Server Windows capability on worker nodes via WMI (`wmiexec.py`). Needed because the workers run WSL2 on Windows and SSH must be enabled on the Windows side for `sshpass` to work.

### `scripts/setup_docker_proxy.sh`, `scripts/setup_ws11_portproxy.ps1`, `scripts/setup_worker_windows.ps1`

Infrastructure setup scripts for the Windows host. Handle Docker proxy configuration and Windows port-proxy rules (needed in WSL2 standard networking mode to expose WSL2 services to the Windows network).

### `scripts/install_autostart.sh`

Installs cron/systemd entries to auto-start platform components on reboot.

---

## Startup Scripts (`startup_scripts/`)

High-level start/stop scripts for different operating environments.

### `startup_scripts/start_linux.sh` (primary)

The canonical full-platform startup script. Runs on WS-11 inside WSL2. Idempotent. Steps:

1. **WSL2 mode check** — detects mirrored vs. standard networking and cleans up stale Windows portproxy rules if in mirrored mode.
2. **Docker Compose** — starts all services (`docker compose up -d --build`), waits for gateway health.
3. **llama-server** — checks if already running on port 8080; if not, launches it with production flags and waits up to 120 s for model load.
4. **Ray head** — starts `ray start --head` on WS-11 if not already running.
5. **Worker nodes** — in parallel, calls `scripts/start_linux_worker.sh` for WS-03, WS-08, and WS-13. Waits up to 120 s for all 3 workers to join the Ray cluster.
6. **Ray Serve** — runs `scripts/deploy_serve.py` to deploy `LlamaCppWorker`.
7. **Verification** — checks that NGINX responds on port 10080.
8. **Summary** — prints all service URLs and example commands.

### `startup_scripts/stop_linux.sh`

Graceful teardown. Stops workers via SSH, kills Ray and llama-server processes on the controller, runs `docker compose down`. Does two passes of port cleanup to handle processes that hold ports after signal.

### `startup_scripts/start_mac.sh`, `startup_scripts/start_windows.ps1`

Platform-specific startup variants (Mac for development, Windows PowerShell for running from the Windows host side of WS-11). Conceptually similar to `start_linux.sh` but adapted for each environment's process and path conventions.

### `startup_scripts/update_portproxy.ps1`

PowerShell script that sets up Windows `netsh portproxy` rules to forward ports from the Windows host to the WSL2 IP. Needed when WSL2 is in standard (non-mirrored) networking mode so that other nodes on the LAN can reach services running inside WSL2.

---

## Tests (`tests/`)

Unit tests for the gateway. Use an in-memory SQLite database (via `aiosqlite`) and `httpx.ASGITransport` to run the FastAPI app without a real server. The gateway's `get_db` dependency is overridden in each test via `app.dependency_overrides`.

### `tests/conftest.py`

Three fixtures:
- `engine` — session-scoped, creates the SQLite DB and all tables once per test run.
- `session` — function-scoped, truncates all tables before each test for isolation, yields a session, rolls back after.
- `client` — function-scoped, overrides `get_db` with the test session, yields an `httpx.AsyncClient` bound to the app.

### `tests/test_health.py`
Tests all three health endpoints return 200 with expected status fields.

### `tests/test_auth.py`
Tests user and API key model creation, key hash determinism, key format validation, and `validate_key_against_db` with a real DB lookup.

### `tests/test_chat.py`
Tests:
- Unauthenticated chat request returns 401.
- Authenticated chat request returns 200 with an OpenAI-shaped response.
- Streaming request returns 200 with `text/event-stream` content type.
- `_payload_for_inference` correctly injects `chat_template_kwargs`.
- `_normalize_completion_response` correctly promotes `reasoning_content` to `content`.

### `tests/test_admin.py`
Tests admin user creation, API key creation (verifying the key starts with `sk-ongc-`), and admin metrics endpoint.

### `tests/test_logging.py`
Tests that a successful chat request writes a `RequestLog` row with the correct model and API key prefix.

---

## Key Design Decisions

**Why Ray Serve instead of Kubernetes?**
Ray is AI-native. It understands GPU resources, can colocate replicas with GPUs, and handles distributed actor scheduling without requiring Kubernetes complexity. For a 4-node intranet cluster, Ray is far simpler to operate. Kubernetes remains a future option if the cluster grows.

**Why the fallback responses?**
The platform must remain available even if llama-server on one node crashes or a Ray worker is restarting. Rather than returning 5xx errors (which break client retry logic), both the gateway and worker return synthetic responses indicating the backend is temporarily unavailable. This is consistent with the priority on availability for intranet enterprise use.

**Why SHA-256 for API keys instead of bcrypt?**
API keys are high-entropy random tokens (43 characters from `token_urlsafe`). Unlike passwords, they are not guessable by dictionary attack, so bcrypt's work factor adds latency with no security benefit. SHA-256 lookup is O(1) with an indexed column.

**Why round-robin at the gateway instead of letting Ray Serve route?**
Ray Serve's default locality routing sends requests to the replica on the same node as the HTTP proxy. Since the gateway runs on WS-11 (the controller), all traffic would go to WS-11's replica, starving WS-03/WS-08/WS-13. The solution is two-part: disable locality routing (`RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING=0`) AND round-robin across all node proxy URLs in the gateway. Both are necessary.

**Why is `no_proxy` set everywhere?**
The ONGC intranet routes all HTTP/HTTPS traffic through a corporate proxy (`10.205.122.201:8080`). Internal node-to-node communication must bypass it. If `no_proxy` is not set, Ray gRPC, httpx requests to llama-server, and Redis/Postgres connections all fail. Every service, Docker container, and shell script sets `no_proxy` and `NO_PROXY` explicitly.

**Why does session affinity use API key prefix instead of client IP?**
The ONGC intranet routes HTTP traffic through a corporate proxy (`10.205.122.201:8080`). If client IP were used, all users behind that proxy would appear as the same IP and pin to a single worker. API key prefix is per-user, is unaffected by NAT/proxy, and is already available in `request.state` from the auth middleware. It is also the correct semantic unit: KV cache reuse benefits conversations from the same user, not the same network location.

**Why does session affinity default to True?**
The primary workload is multi-turn chat conversations. Keeping a user's requests on the same GPU node lets llama.cpp reuse its KV cache for the conversation prefix, reducing time-to-first-token significantly on follow-up messages. Admins or batch-processing clients can set `session_affinity: false` when they want even load distribution (e.g., large independent requests from the same key where cache reuse is unlikely).

**Why is `enable_thinking` a per-request field excluded from the forwarded payload?**
Qwen3.6 35B supports chain-of-thought reasoning via a special chat template parameter. The flag must be injected into `chat_template_kwargs` rather than sent as a top-level OpenAI field. The gateway strips the custom field from the Pydantic model before forwarding (using `exclude=True` + `model_dump(exclude={...})`) and injects it in the correct location, so clients using standard OpenAI SDKs can opt in via a non-standard extension field without breaking compatibility.

---

## Data Flow for a Single Chat Request

```
1. Client sends POST /v1/chat/completions  (via NGINX → gateway)
2. NGINX proxies to gateway:8000
3. gateway/routers/chat.py:chat_completions
   a. require_api_key: validates Bearer token against DB
   b. check_rate_limit: Redis sliding window check
   c. ACTIVE_REQUESTS.inc()
   d. _payload_for_inference: builds payload, injects enable_thinking
   e. affinity_key = api_key_prefix if session_affinity else None
   f. ray_client.submit_inference or stream_inference:
      - session_affinity=True  → hash(api_key_prefix) % 4 → deterministic node
      - session_affinity=False → round-robin across nodes
      - POST to http://<selected_node_ip>:8001/v1/chat/completions
4. Ray Serve (on the chosen node's proxy)
   - routes to its local LlamaCppWorker replica
5. LlamaCppWorker.chat:
   - POST to http://<same_node_ip>:8080/v1/chat/completions
6. llama-server (llama.cpp)
   - runs GPU inference on the local RTX A4000
   - returns OpenAI-compatible JSON
7. LlamaCppWorker appends node_ip and queue_ms to response
8. gateway receives response
   a. _normalize_completion_response: fix thinking mode fields
   b. log_request: write RequestLog to Postgres
   c. Prometheus counters/histogram updated
   d. ACTIVE_REQUESTS.dec()
   e. Return JSONResponse to NGINX → client
```

---

## Environment Variables Reference

Key variables consumed by the gateway (from `config.py`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql+asyncpg://llm:llm@localhost:5432/llm_platform` | Postgres connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis for rate limiting |
| `RAY_SERVE_URL` | `http://10.208.211.62:8001` | Base Ray Serve URL (port extracted for multi-node round-robin) |
| `ADMIN_SECRET` | `changeme` | Shared secret for `/admin/*` endpoints |
| `CONTROLLER_NODE_IP` | `10.208.211.62` | WS-11 IP |
| `WORKER_NODE_IPS` | `10.208.211.54,10.208.211.59,10.208.211.64` | Comma-separated worker IPs |
| `SERVE_REPLICAS` | `4` | Number of Ray Serve replicas |
| `ENABLE_THINKING` | `false` | Global Qwen3.6 thinking mode toggle |
| `NO_PROXY` | `localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in` | Proxy bypass list |
| `LLAMA_MODEL_PATH` | `/mnt/d/Models/...` | Path to GGUF model file |
| `REQUEST_TIMEOUT_SECONDS` | `300.0` | Max time to wait for inference |
