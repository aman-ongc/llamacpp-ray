#!/usr/bin/env python3
"""
Dummy metrics exporter for test-monitor.

Simulates the full ONGC LLM cluster:
  - LLM gateway metrics  (port 9101)
  - 15 stub node-exporter endpoints (ports 9201-9215)
    so Prometheus generates up{job="node-exporter"} = 15

Cluster layout:
  Controller : 10.208.211.62  (text pool, excluded from Ray by default)
  Text pool  : 10.208.211.52–.61  (10 nodes, Gemma 4 26B, --parallel 1)
  Multimodal : 10.208.211.63/.64/.65/.67  (4 nodes, Qwen3-VL-8B, --parallel 4, -c 16384)

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
def _node(ip: str, pool: str) -> dict:
    return {
        "ip": ip,
        "pool": pool,                          # "text" | "multimodal" | "controller"
        "node_inst":  f"{ip}:9100",
        "gpu_inst":   f"{ip}:9835",
        "llama_inst": f"{ip}:8080",
        # max parallel slots per node (matches llama-server --parallel)
        "max_slots": 4 if pool == "multimodal" else 1,
    }

NODES = [
    _node("10.208.211.62", "controller"),   # WS-11 — excluded from text pool by default
    _node("10.208.211.52", "text"),
    _node("10.208.211.53", "text"),
    _node("10.208.211.54", "text"),
    _node("10.208.211.55", "text"),
    _node("10.208.211.56", "text"),
    _node("10.208.211.57", "text"),
    _node("10.208.211.58", "text"),
    _node("10.208.211.59", "text"),
    _node("10.208.211.60", "text"),
    _node("10.208.211.61", "text"),
    _node("10.208.211.63", "multimodal"),
    _node("10.208.211.64", "multimodal"),
    _node("10.208.211.65", "multimodal"),
    _node("10.208.211.67", "multimodal"),
]

# Stub port mapping: each node gets one port for node/gpu/llama stub endpoints
# Ports 9201–9215 correspond to NODES[0]–NODES[14]
STUB_BASE_PORT = 9201

MODEL      = "ongc-llm"
VRAM_TOTAL = 16 * 1024 ** 3       # RTX A4000 — 16 GB
RAM_TOTAL  = 128 * 1024 ** 3      # 128 GB

USERS = [
    "rajan.kumar", "priya.sharma", "amit.singh", "kavita.nair",
    "suresh.rao",  "deepa.iyer",   "vikram.joshi", "anita.patel",
    "rohit.gupta", "mohan.verma",  "sunita.reddy", "admin",
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
PROMPT_TOKENS     = Counter("llm_prompt_tokens_total",     "Prompt tokens",              ["model", "username", "request_type"])
COMPLETION_TOKENS = Counter("llm_completion_tokens_total", "Completion tokens",           ["model", "username", "request_type"])
TOTAL_TOKENS      = Counter("llm_total_tokens_total",      "Total tokens (prompt + comp)", ["model", "username", "request_type"])
ACTIVE_REQUESTS   = Gauge("llm_active_requests", "Currently active requests")

# ── GPU metrics  (nvidia-gpu-exporter names, instance = IP:9835) ────────────
GPU_UTIL      = Gauge("nvidia_smi_utilization_gpu_ratio", "GPU utilization 0-1",   ["instance"])
GPU_MEM_USED  = Gauge("nvidia_smi_memory_used_bytes",     "VRAM used bytes",       ["instance"])
GPU_MEM_TOTAL = Gauge("nvidia_smi_memory_total_bytes",    "VRAM total bytes",      ["instance"])
GPU_TEMP      = Gauge("nvidia_smi_temperature_gpu",       "GPU temperature C",     ["instance"])

# ── Node metrics  (node-exporter names, instance = IP:9100) ─────────────────
NODE_CPU_IDLE  = Counter("node_cpu_seconds_total",         "CPU seconds by mode",   ["instance", "mode"])
NODE_MEM_AVAIL = Gauge("node_memory_MemAvailable_bytes",   "RAM available",         ["instance"])
NODE_MEM_TOTAL = Gauge("node_memory_MemTotal_bytes",       "RAM total",             ["instance"])
NODE_FS_AVAIL  = Gauge("node_filesystem_avail_bytes",      "FS available",          ["instance", "mountpoint"])
NODE_FS_SIZE   = Gauge("node_filesystem_size_bytes",       "FS size",               ["instance", "mountpoint"])
NODE_NET_RX    = Counter("node_network_receive_bytes_total",  "RX bytes",           ["instance", "device"])
NODE_NET_TX    = Counter("node_network_transmit_bytes_total", "TX bytes",           ["instance", "device"])

# ── llama.cpp metrics  (instance = IP:8080) ─────────────────────────────────
LLAMA_TOKENS     = Counter("llamacpp_tokens_predicted_total",    "Tokens predicted",    ["instance"])
LLAMA_KV         = Gauge("llamacpp_kv_cache_usage_ratio",        "KV cache 0-1",        ["instance"])
LLAMA_SLOTS      = Gauge("llamacpp_requests_processing",         "Active slots",        ["instance"])
LLAMA_PROMPT_TPS = Gauge("llamacpp_prompt_tokens_per_second",    "Prompt t/s",          ["instance"])
LLAMA_GEN_TPS    = Gauge("llamacpp_predicted_tokens_per_second", "Gen t/s",             ["instance"])


# ── Initialise static/constant values ──────────────────────────────────────
def _init_static() -> None:
    for n in NODES:
        GPU_MEM_TOTAL.labels(instance=n["gpu_inst"]).set(VRAM_TOTAL)
        NODE_MEM_TOTAL.labels(instance=n["node_inst"]).set(RAM_TOTAL)
        NODE_FS_SIZE.labels(instance=n["node_inst"], mountpoint="/").set(500 * 1024 ** 3)


# ── Simulation loop (runs every 5 s) ────────────────────────────────────────
_TICK = 5.0

def _simulate() -> None:
    phases = [random.uniform(0, 2 * math.pi) for _ in NODES]
    t = 0.0

    # Active nodes: controller excluded from Ray text pool by default
    text_nodes       = [n for n in NODES if n["pool"] == "text"]
    multimodal_nodes = [n for n in NODES if n["pool"] == "multimodal"]
    active_nodes     = text_nodes + multimodal_nodes   # 14 nodes serving requests

    while True:
        t += _TICK

        # --- LLM gateway traffic ---
        n_req = random.randint(0, 8)
        for _ in range(n_req):
            username = random.choice(USERS)
            is_multimodal = random.random() < 0.25
            node = random.choice(multimodal_nodes if is_multimodal else text_nodes)
            status = "200" if random.random() < 0.96 else "500"
            is_stream = random.random() < 0.4
            stream_str = str(is_stream).lower()
            request_type = "multimodal" if is_multimodal else "text"
            # streaming: node_ip is "stream" for text, "multimodal" for multimodal pool
            if is_stream:
                metric_node_ip = "multimodal" if is_multimodal else "stream"
            else:
                metric_node_ip = "multimodal" if is_multimodal else node["ip"]
            latency = random.lognormvariate(7.5, 0.7)
            REQUEST_COUNT.labels(
                model=MODEL, status_code=status, streaming=stream_str,
                username=username, node_ip=metric_node_ip,
            ).inc()
            REQUEST_LATENCY.labels(
                model=MODEL, username=username, node_ip=metric_node_ip,
            ).observe(latency)
            if status == "200":
                p_tok = random.randint(800, 2000) if is_multimodal else random.randint(100, 500)
                c_tok = random.randint(50, 600)
                PROMPT_TOKENS.labels(model=MODEL, username=username, request_type=request_type).inc(p_tok)
                COMPLETION_TOKENS.labels(model=MODEL, username=username, request_type=request_type).inc(c_tok)
                TOTAL_TOKENS.labels(model=MODEL, username=username, request_type=request_type).inc(p_tok + c_tok)
                LLAMA_TOKENS.labels(instance=node["llama_inst"]).inc(c_tok)

        ACTIVE_REQUESTS.set(random.randint(0, min(14, n_req + 2)))

        # --- Per-node metrics ---
        for idx, n in enumerate(NODES):
            wave = math.sin(t / 60 + phases[idx]) * 0.15

            # Controller sits mostly idle (no Ray inference traffic by default)
            is_active = n["pool"] != "controller"
            load_base = 0.72 if is_active else 0.15

            # GPU — multimodal nodes run slightly hotter (4 parallel slots, vision model)
            gpu_util = max(0.30, min(0.97, load_base + wave + random.uniform(-0.05, 0.05)))
            GPU_UTIL.labels(instance=n["gpu_inst"]).set(gpu_util)

            vram_base = 0.80 if n["pool"] == "multimodal" else 0.75
            vram_used = VRAM_TOTAL * max(0.50, min(0.97, vram_base + wave * 0.4))
            GPU_MEM_USED.labels(instance=n["gpu_inst"]).set(vram_used)

            temp_base = 70 if n["pool"] == "multimodal" else 66
            GPU_TEMP.labels(instance=n["gpu_inst"]).set(
                round(temp_base + wave * 8 + random.uniform(-2, 2), 1)
            )

            # CPU
            cpu_busy = max(0.10, min(0.85, (0.45 if is_active else 0.10) + wave + random.uniform(-0.05, 0.05)))
            idle_inc = (1.0 - cpu_busy) * 6 * _TICK
            NODE_CPU_IDLE.labels(instance=n["node_inst"], mode="idle").inc(idle_inc)
            NODE_CPU_IDLE.labels(instance=n["node_inst"], mode="user").inc(cpu_busy * 0.7 * 6 * _TICK)
            NODE_CPU_IDLE.labels(instance=n["node_inst"], mode="system").inc(cpu_busy * 0.3 * 6 * _TICK)

            # RAM
            ram_used_ratio = max(0.15, min(0.80, (0.45 if is_active else 0.20) + wave * 0.3 + random.uniform(-0.03, 0.03)))
            NODE_MEM_AVAIL.labels(instance=n["node_inst"]).set(RAM_TOTAL * (1.0 - ram_used_ratio))

            # Disk
            NODE_FS_AVAIL.labels(instance=n["node_inst"], mountpoint="/").set(
                500 * 1024 ** 3 * (0.45 + random.uniform(-0.005, 0.005))
            )

            # Network
            rx_rate = random.randint(500_000, 8_000_000) if is_active else random.randint(10_000, 200_000)
            NODE_NET_RX.labels(instance=n["node_inst"], device="eth0").inc(rx_rate)
            NODE_NET_TX.labels(instance=n["node_inst"], device="eth0").inc(int(rx_rate * 0.6))

            # llama.cpp — slot count bounded by per-node --parallel setting
            if is_active:
                kv = max(0.20, min(0.92, 0.60 + wave * 0.2 + random.uniform(-0.05, 0.05)))
                max_slots = n["max_slots"]
                active_slots = random.choices(range(max_slots + 1), weights=[1] + [3] * max_slots)[0]
            else:
                kv = 0.05
                active_slots = 0
            LLAMA_KV.labels(instance=n["llama_inst"]).set(kv)
            LLAMA_SLOTS.labels(instance=n["llama_inst"]).set(active_slots)

            # Multimodal nodes: lower gen t/s (heavier model), higher prompt t/s (vision encoder)
            if n["pool"] == "multimodal":
                LLAMA_PROMPT_TPS.labels(instance=n["llama_inst"]).set(random.uniform(400, 700))
                LLAMA_GEN_TPS.labels(instance=n["llama_inst"]).set(random.uniform(20, 35))
            elif is_active:
                LLAMA_PROMPT_TPS.labels(instance=n["llama_inst"]).set(random.uniform(200, 500))
                LLAMA_GEN_TPS.labels(instance=n["llama_inst"]).set(random.uniform(25, 45))
            else:
                LLAMA_PROMPT_TPS.labels(instance=n["llama_inst"]).set(0)
                LLAMA_GEN_TPS.labels(instance=n["llama_inst"]).set(0)

        time.sleep(_TICK)


# ── HTTP handlers ────────────────────────────────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
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
        pass


class StubHandler(BaseHTTPRequestHandler):
    """Stub node-exporter endpoint — returns empty metrics so Prometheus marks it up=1."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.end_headers()
        self.wfile.write(b"")

    def log_message(self, *_):
        pass


def _serve(handler_cls, port: int) -> None:
    HTTPServer(("0.0.0.0", port), handler_cls).serve_forever()


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_static()

    threading.Thread(target=_serve, args=(MetricsHandler, 9101), daemon=True).start()
    print("Metrics exporter  :9101/metrics", flush=True)

    for i, node in enumerate(NODES):
        port = STUB_BASE_PORT + i
        threading.Thread(target=_serve, args=(StubHandler, port), daemon=True).start()
        print(f"Stub :{port}  ({node['ip']} / {node['pool']})", flush=True)

    threading.Thread(target=_simulate, daemon=True).start()
    print(f"Simulation started (tick={_TICK}s, {len(NODES)} nodes)", flush=True)

    while True:
        time.sleep(60)
