# Issues Resolution Plan

> Updated: 2026-06-01
> Scope: docs/current_issues.md — Issue 7 (Partial) and Issue 10 (Open)

---

## Overview

Two items remain open after the distributed Ray cluster was brought up across all four workstations:

| Issue | Severity | Status | Summary |
|-------|----------|--------|---------|
| 7 | Important | Partial | Node-exporter not persistent; GPU metrics absent |
| 10 | Enhancement | Open | No per-node worker health dashboard |

Both are addressed together because Issue 10 depends on the metric infrastructure fixed in Issue 7.

---

## Issue 7 — Node Exporter Persistence + GPU Metrics

### Problem

`start_node_exporter.sh` exists and was exercised, but:

1. No `@reboot` cron entry — process does not survive WSL2 restarts
2. No verification that all 4 nodes are currently running it
3. Standard node-exporter does not export GPU data (VRAM, utilization, temperature); the RTX A4000 on every node is completely invisible to Prometheus

### Approach

Mirror the existing `start_node_exporter.sh` pattern to add:

- **`nvidia_gpu_exporter`** (port 9835) — single Go binary, shells out to `nvidia-smi`, no DCGM daemon dependency. Chosen over DCGM-exporter because DCGM's `nv-hostengine` daemon is unreliable on WSL2. Download follows the exact same proxy + wget + idempotency pattern already proven by `start_node_exporter.sh`.
- **`@reboot` cron entries** on all 4 nodes with appropriate sleep delays (15 s for node-exporter, 20 s for gpu-exporter to give WSL2 time to initialize the NVIDIA driver).
- **`llama-cpp` Prometheus scrape job** — llama.cpp server exposes native Prometheus metrics at `/metrics` (port 8080) including `llamacpp:tokens_predicted_total` and `llamacpp:kv_cache_usage_ratio`. Adding this job now enables the dashboard panels in Issue 10 with no additional exporter.

### Files Created

| File | Purpose |
|------|---------|
| `scripts/start_gpu_exporter.sh` | Download nvidia_gpu_exporter v1.3.2, start on :9835, idempotent, gracefully skips if nvidia-smi absent |
| `scripts/install_exporters_cron.sh` | Install @reboot cron entries for both exporters on the local node; idempotent (strips old entries before inserting) |
| `scripts/deploy_exporters.sh` | Orchestrate: push scripts to all 4 nodes via sshpass+scp, run both exporters immediately, install cron, verify all 8 endpoints reachable |

### Files Modified

| File | Change |
|------|--------|
| `infra/prometheus/prometheus.yml` | Added `nvidia-gpu-exporter` job (:9835 on all 4 nodes) and `llama-cpp` job (:8080 on all 4 nodes, `metrics_path: /metrics`) |

### Execution Steps

```bash
# 1. From WS-11 — deploy exporters to all 4 nodes, start immediately, install cron
cd /home/administrator/projects/llm-inference-service
bash scripts/deploy_exporters.sh
```

Expected output: `[OK]` for all 8 endpoints (4×:9100 node-exporter, 4×:9835 gpu-exporter).

```bash
# 2. Reload Prometheus to pick up the new scrape jobs
sudo docker compose restart prometheus
```

```bash
# 3. Verify all targets UP
curl --noproxy '*' -s http://10.208.211.62:9090/api/v1/targets \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
for t in d['data']['activeTargets']:
    print(t['labels']['job'], t['labels']['instance'], t['health'])
"
```

Expected: all 4 `node-exporter` targets `up`, all 4 `nvidia-gpu-exporter` targets `up`. `llama-cpp` targets show `up` on nodes where llama.cpp is running, `down` on others — both are correct states.

```bash
# 4. Confirm GPU metric names (version check — adjust dashboard PromQL if different)
curl --noproxy '*' -s http://10.208.211.62:9835/metrics \
  | grep "^nvidia_smi_" | cut -d'{' -f1 | sort -u
```

Expected metric names (nvidia_gpu_exporter v1.3.2):
- `nvidia_smi_memory_used_bytes`
- `nvidia_smi_memory_total_bytes`
- `nvidia_smi_utilization_gpu_ratio`
- `nvidia_smi_temperature_gpu`

If names differ, update the PromQL in `infra/grafana/dashboards/worker_health.json` (11 lines total, one per gauge/stat panel that references GPU metrics).

### Risk: nvidia_gpu_exporter download blocked

If GitHub is unreachable through the corporate proxy on a worker node:

```bash
# Download binary once on WS-11 (proven proxy connectivity)
bash scripts/start_gpu_exporter.sh   # runs locally on WS-11 first

# Then deploy_exporters.sh will scp the already-extracted binary
# to each worker — the script detects the binary exists and skips wget
```

The binary is extracted to `/home/administrator/nvidia_gpu_exporter/nvidia_gpu_exporter` on WS-11. `deploy_exporters.sh` pushes the install dir contents so workers get it directly via SCP without needing outbound internet.

---

## Issue 10 — Worker Health Dashboard

### Problem

No Grafana dashboard exists for per-node hardware visibility. Operators cannot see:
- Which node is GPU-saturated
- VRAM pressure on any individual node
- Whether llama.cpp is alive on a specific node
- Network and disk health
- KV cache usage trends

### Approach

Create `infra/grafana/dashboards/worker_health.json` (uid: `worker-health`).

The existing `dashboard.yml` provisioner already scans `/var/lib/grafana/dashboards` every 30 seconds — no Grafana restart needed. No datasource changes needed — uses the existing `prometheus` datasource uid.

**Structure:**
- **Cluster Overview row** (expanded by default) — fleet-wide summary stats
- **4 per-node rows** (WS-11, WS-03, WS-08, WS-13, collapsed by default) — per-node drill-down

### File Created

| File | Purpose |
|------|---------|
| `infra/grafana/dashboards/worker_health.json` | Full Grafana dashboard with 5 rows, ~47 panels |

### Dashboard Panels

**Cluster Overview row:**

| Panel | Type | Metric |
|-------|------|--------|
| Ray Nodes Online | stat | `count(up{job="node-exporter"} == 1)` — green=4, yellow=3, red<3 |
| All Nodes CPU % | timeseries | `100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[2m])*100)` |
| All Nodes GPU % | timeseries | `avg by(instance)(nvidia_smi_utilization_gpu_ratio*100)` |
| All Nodes VRAM Used | timeseries | `nvidia_smi_memory_used_bytes` by instance |
| Token Throughput / Node | timeseries | `rate(llamacpp:tokens_predicted_total[2m])` by instance |

**Per-node panels (×4, one collapsed row per node):**

| Panel | Type | Metric | Unit | Thresholds |
|-------|------|--------|------|-----------|
| CPU Usage % | gauge | `100 - avg(rate(node_cpu_seconds_total{mode="idle",instance="IP:9100"}[2m]))*100` | % | 0→green, 70→yellow, 90→red |
| RAM Used % | gauge | `(1-(node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes))*100` | % | 0→green, 70→yellow, 90→red |
| Disk Usage % | gauge | `(1-(node_filesystem_avail_bytes{mountpoint="/"}/node_filesystem_size_bytes{mountpoint="/"}))*100` | % | 0→green, 70→yellow, 90→red |
| GPU Util % | gauge | `nvidia_smi_utilization_gpu_ratio*100` | % | 0→green, 80→yellow, 95→red |
| VRAM Used | gauge | `nvidia_smi_memory_used_bytes` | bytes (auto GiB) | 0→green, 12.9GiB→yellow, 16.1GiB→red |
| GPU Temp | stat | `nvidia_smi_temperature_gpu` | °C | 0→green, 75→yellow, 85→red |
| Network In | timeseries | `rate(node_network_receive_bytes_total{device!="lo"}[2m])` | bytes/s | — |
| Network Out | timeseries | `rate(node_network_transmit_bytes_total{device!="lo"}[2m])` | bytes/s | — |
| llama.cpp Health | stat | `up{job="llama-cpp",instance="IP:8080"}` | — | 0→red, 1→green |
| llama.cpp Tokens/sec | timeseries | `rate(llamacpp:tokens_predicted_total{instance="IP:8080"}[2m])` | tokens/s | — |
| KV Cache Usage % | gauge | `llamacpp:kv_cache_usage_ratio{instance="IP:8080"}*100` | % | 0→green, 70→yellow, 90→red |

Node IP mapping: WS-11=10.208.211.62, WS-03=10.208.211.54, WS-08=10.208.211.59, WS-13=10.208.211.64

### Execution Steps

```bash
# Grafana auto-loads within 30 s — verify:
curl --noproxy '*' -s -u admin:ongc1234 \
  http://10.208.211.62:13000/api/dashboards/uid/worker-health \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['dashboard']['title'])"
```

Expected: `ONGC Worker Node Health`

```bash
# Verify GPU metrics are flowing (4 results expected):
curl --noproxy '*' -s \
  'http://10.208.211.62:9090/api/v1/query?query=nvidia_smi_utilization_gpu_ratio' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
for r in d['data']['result']:
    print(r['metric'].get('instance'), r['value'][1])
"
```

---

## Completion Checklist

- [ ] `bash scripts/deploy_exporters.sh` — all 8 endpoints `[OK]`
- [ ] `sudo docker compose restart prometheus`
- [ ] Prometheus targets page: node-exporter and nvidia-gpu-exporter all `up`
- [ ] GPU metric names confirmed (adjust dashboard PromQL if needed)
- [ ] Grafana dashboard `worker-health` loads with non-null panel data
- [ ] Update `docs/current_issues.md` — mark Issue 7 and Issue 10 as ✅ Resolved

---

## Issue Closure Notes

**Issue 7** closes when: all 4 node-exporter + all 4 nvidia-gpu-exporter Prometheus targets show `up`, and cron entries are verified on all nodes.

**Issue 10** closes when: "ONGC Worker Node Health" dashboard is accessible in Grafana and all gauge/timeseries panels display real data (not "No data").
