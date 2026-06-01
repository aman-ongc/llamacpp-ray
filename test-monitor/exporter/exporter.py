#!/usr/bin/env python3
"""
Dummy metrics exporter for test-monitor.

Simulates the full ONGC LLM cluster:
  - LLM gateway metrics  (port 9101)
  - 4 stub node-exporter endpoints  (ports 9201-9204)
    so Prometheus generates up{job="node-exporter"} = 4

All per-node metrics carry the real ONGC IP:port instance labels
so the production Grafana dashboards render correctly.
"""

import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ── Cluster definition ──────────────────────────────────────────────────────
NODES = [
    {"ip": "10.208.211.62", "node_inst": "10.208.211.62:9100", "gpu_inst": "10.208.211.62:9835", "llama_inst": "10.208.211.62:8080"},
    {"ip": "10.208.211.54", "node_inst": "10.208.211.54:9100", "gpu_inst": "10.208.211.54:9835", "llama_inst": "10.208.211.54:8080"},
    {"ip": "10.208.211.59", "node_inst": "10.208.211.59:9100", "gpu_inst": "10.208.211.59:9835", "llama_inst": "10.208.211.59:8080"},
    {"ip": "10.208.211.64", "node_inst": "10.208.211.64:9100", "gpu_inst": "10.208.211.64:9835", "llama_inst": "10.208.211.64:8080"},
]
MODEL = "qwen"
VRAM_TOTAL = 16 * 1024 ** 3       # RTX A4000: 16 GB
RAM_TOTAL   = 128 * 1024 ** 3      # 128 GB

USERS = [
    "rajan.kumar", "priya.sharma", "amit.singh", "kavita.nair",
    "suresh.rao",  "deepa.iyer",   "vikram.joshi", "anita.patel",
    "rohit.gupta", "admin",
]

# ── LLM gateway metrics (match gateway/metrics.py exactly) ─────────────────
REQUEST_COUNT = Counter(
    "llm_requests_total", "Total inference requests",
    ["model", "status_code", "streaming", "username", "node_ip"],
)
REQUEST_LATENCY = Histogram(
    "llm_request_latency_ms", "Request latency ms", ["model", "username", "node_ip"],
    buckets=(50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)
PROMPT_TOKENS     = Counter("llm_prompt_tokens_total",     "Prompt tokens",     ["model", "username"])
COMPLETION_TOKENS = Counter("llm_completion_tokens_total", "Completion tokens", ["model", "username"])
ACTIVE_REQUESTS   = Gauge("llm_active_requests", "Currently active requests")

# ── GPU metrics  (nvidia-gpu-exporter names, instance = IP:9835) ────────────
GPU_UTIL      = Gauge("nvidia_smi_utilization_gpu_ratio", "GPU utilization 0-1",   ["instance"])
GPU_MEM_USED  = Gauge("nvidia_smi_memory_used_bytes",     "VRAM used bytes",       ["instance"])
GPU_MEM_TOTAL = Gauge("nvidia_smi_memory_total_bytes",    "VRAM total bytes",      ["instance"])
GPU_TEMP      = Gauge("nvidia_smi_temperature_gpu",       "GPU temperature C",     ["instance"])

# ── Node metrics  (node-exporter names, instance = IP:9100) ─────────────────
NODE_CPU_IDLE = Counter(
    "node_cpu_seconds_total", "CPU seconds by mode",
    ["instance", "mode"],
)
NODE_MEM_AVAIL = Gauge("node_memory_MemAvailable_bytes", "RAM available", ["instance"])
NODE_MEM_TOTAL = Gauge("node_memory_MemTotal_bytes",     "RAM total",     ["instance"])
NODE_FS_AVAIL  = Gauge("node_filesystem_avail_bytes",    "FS available",  ["instance", "mountpoint"])
NODE_FS_SIZE   = Gauge("node_filesystem_size_bytes",     "FS size",       ["instance", "mountpoint"])
NODE_NET_RX    = Counter("node_network_receive_bytes_total",  "RX bytes", ["instance", "device"])
NODE_NET_TX    = Counter("node_network_transmit_bytes_total", "TX bytes", ["instance", "device"])

# ── llama.cpp metrics  (instance = IP:8080) ─────────────────────────────────
# Colon names are valid Prometheus metric names (recording-rule convention).
LLAMA_TOKENS  = Counter("llamacpp_tokens_predicted_total", "Tokens predicted",  ["instance"])
LLAMA_KV      = Gauge("llamacpp_kv_cache_usage_ratio",     "KV cache 0-1",      ["instance"])
LLAMA_SLOTS   = Gauge("llamacpp_requests_processing",      "Active slots",      ["instance"])
LLAMA_PROMPT_TPS = Gauge("llamacpp_prompt_tokens_per_second",   "Prompt t/s",  ["instance"])
LLAMA_GEN_TPS    = Gauge("llamacpp_predicted_tokens_per_second","Gen t/s",     ["instance"])


# ── Initialise static/constant values ──────────────────────────────────────
def _init_static() -> None:
    for n in NODES:
        GPU_MEM_TOTAL.labels(instance=n["gpu_inst"]).set(VRAM_TOTAL)
        NODE_MEM_TOTAL.labels(instance=n["node_inst"]).set(RAM_TOTAL)
        NODE_FS_SIZE.labels(instance=n["node_inst"], mountpoint="/").set(500 * 1024 ** 3)


# ── Simulation loop (runs every 5 s) ────────────────────────────────────────
_TICK = 5.0  # seconds between updates

def _simulate() -> None:
    # Smooth oscillation per node so each node drifts independently
    phases = [random.uniform(0, 2 * math.pi) for _ in NODES]
    t = 0.0

    while True:
        t += _TICK

        # --- LLM gateway traffic ---
        n_req = random.randint(0, 4)
        for _ in range(n_req):
            username = random.choice(USERS)
            node     = random.choice(NODES)
            status   = "200" if random.random() < 0.96 else "500"
            is_stream = random.random() < 0.4
            stream_str = str(is_stream).lower()
            # streaming requests don't carry a real node_ip (proxied before node responds)
            metric_node_ip = "stream" if is_stream else node["ip"]
            latency = random.lognormvariate(7.5, 0.7)
            REQUEST_COUNT.labels(model=MODEL, status_code=status, streaming=stream_str,
                                 username=username, node_ip=metric_node_ip).inc()
            REQUEST_LATENCY.labels(model=MODEL, username=username, node_ip=metric_node_ip).observe(latency)
            if status == "200":
                p_tok = random.randint(20, 450)
                c_tok = random.randint(40, 600)
                PROMPT_TOKENS.labels(model=MODEL, username=username).inc(p_tok)
                COMPLETION_TOKENS.labels(model=MODEL, username=username).inc(c_tok)
                LLAMA_TOKENS.labels(instance=node["llama_inst"]).inc(c_tok)

        ACTIVE_REQUESTS.set(random.randint(0, 5))

        # --- Per-node metrics ---
        for idx, n in enumerate(NODES):
            wave = math.sin(t / 60 + phases[idx]) * 0.15   # ±15% drift

            # GPU
            gpu_util = max(0.35, min(0.97, 0.72 + wave + random.uniform(-0.05, 0.05)))
            GPU_UTIL.labels(instance=n["gpu_inst"]).set(gpu_util)
            vram_used = VRAM_TOTAL * max(0.55, min(0.97, 0.78 + wave * 0.5))
            GPU_MEM_USED.labels(instance=n["gpu_inst"]).set(vram_used)
            GPU_TEMP.labels(instance=n["gpu_inst"]).set(
                round(68 + wave * 8 + random.uniform(-2, 2), 1)
            )

            # Node CPU — idle counter increments at (idle_ratio * n_cpus * tick) per tick
            cpu_busy = max(0.15, min(0.85, 0.45 + wave + random.uniform(-0.05, 0.05)))
            idle_inc = (1.0 - cpu_busy) * 6 * _TICK
            NODE_CPU_IDLE.labels(instance=n["node_inst"], mode="idle").inc(idle_inc)
            # Busy modes (user + system) to make rate() useful
            NODE_CPU_IDLE.labels(instance=n["node_inst"], mode="user").inc(
                cpu_busy * 0.7 * 6 * _TICK
            )
            NODE_CPU_IDLE.labels(instance=n["node_inst"], mode="system").inc(
                cpu_busy * 0.3 * 6 * _TICK
            )

            # Memory
            ram_used_ratio = max(0.2, min(0.8, 0.45 + wave * 0.3 + random.uniform(-0.03, 0.03)))
            NODE_MEM_AVAIL.labels(instance=n["node_inst"]).set(
                RAM_TOTAL * (1.0 - ram_used_ratio)
            )

            # Disk (slow drift — mostly static)
            disk_free = 0.45 + random.uniform(-0.005, 0.005)
            NODE_FS_AVAIL.labels(instance=n["node_inst"], mountpoint="/").set(
                500 * 1024 ** 3 * disk_free
            )

            # Network — increment by random bytes per tick
            NODE_NET_RX.labels(instance=n["node_inst"], device="eth0").inc(
                random.randint(50_000, 5_000_000)
            )
            NODE_NET_TX.labels(instance=n["node_inst"], device="eth0").inc(
                random.randint(50_000, 3_000_000)
            )

            # llama.cpp
            kv = max(0.25, min(0.92, 0.65 + wave * 0.2 + random.uniform(-0.05, 0.05)))
            LLAMA_KV.labels(instance=n["llama_inst"]).set(kv)
            LLAMA_SLOTS.labels(instance=n["llama_inst"]).set(random.choice([0, 1, 1, 2]))
            LLAMA_PROMPT_TPS.labels(instance=n["llama_inst"]).set(
                random.uniform(250, 700)
            )
            gen_tps = random.uniform(18, 42)
            LLAMA_GEN_TPS.labels(instance=n["llama_inst"]).set(gen_tps)

        time.sleep(_TICK)


# ── HTTP handlers ────────────────────────────────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    """Serves /metrics from the global prometheus registry."""
    def do_GET(self):
        if self.path == "/metrics":
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
        elif self.path in ("/health", "/"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass   # suppress access logs


class StubHandler(BaseHTTPRequestHandler):
    """Stub node-exporter endpoint — returns empty metrics so Prometheus marks it up=1."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.end_headers()
        self.wfile.write(b"")  # empty but valid Prometheus response

    def log_message(self, *_):
        pass


def _serve(handler_cls, port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), handler_cls)
    server.serve_forever()


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_static()

    # Metrics server
    threading.Thread(target=_serve, args=(MetricsHandler, 9101), daemon=True).start()
    print("Metrics exporter  :9101/metrics", flush=True)

    # 4 stub servers — one per simulated node-exporter instance
    for stub_port in (9201, 9202, 9203, 9204):
        threading.Thread(target=_serve, args=(StubHandler, stub_port), daemon=True).start()
        print(f"Stub node-exporter :{stub_port}", flush=True)

    # Start simulation in background
    threading.Thread(target=_simulate, daemon=True).start()
    print("Simulation started (tick=5s)", flush=True)

    # Keep main thread alive
    while True:
        time.sleep(60)
