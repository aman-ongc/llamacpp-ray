#!/usr/bin/env python3
"""
Deploy TextWorker and MultimodalWorker as separate Ray Serve applications.

- TextWorker  (route /text)       — 3 replicas pinned to text nodes (WS-11/03/08)
                                    proxies to Gemma 4 26B QAT on port 8080
- MultimodalWorker (route /multi) — 1 replica pinned to WS-13
                                    proxies to Qwen3-VL-8B on port 8080

Node pinning uses custom Ray resources:
  text nodes  : ray start --resources='{"text_node": 1}'
  WS-13       : ray start --resources='{"multimodal_node": 1}'

Run from /home/administrator/projects/llm-inference-service or wherever
the gateway package is importable.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must be set BEFORE ray.serve is imported — constants.py reads at module load.
# Keep locality routing disabled so text requests distribute across all 3 Gemma replicas.
os.environ["RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING"] = "0"
os.environ.setdefault("no_proxy", "localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in")
os.environ.setdefault("NO_PROXY", os.environ["no_proxy"])
os.environ["RAY_grpc_enable_http_proxy"] = "0"

import ray
from ray import serve

SERVE_HOST = "0.0.0.0"
SERVE_PORT = 8001

print("Connecting to local Ray cluster...", flush=True)
ray.init(address="auto", ignore_reinit_error=True)
print("Ray connected.", flush=True)

from worker.ray_worker import MultimodalWorker, TextWorker  # noqa: E402

print(f"Starting Serve on {SERVE_HOST}:{SERVE_PORT} (EveryNode proxy)", flush=True)
serve.start(
    http_options={"host": SERVE_HOST, "port": SERVE_PORT, "location": "EveryNode"},
)

# Text workers — pinned to WS-11/03/08 via "text_node" resource
serve.run(TextWorker.bind(), name="text-worker", route_prefix="/text")
print("TextWorker deployment complete (route: /text).", flush=True)

# Multimodal worker — pinned to WS-13 via "multimodal_node" resource
serve.run(MultimodalWorker.bind(), name="multimodal-worker", route_prefix="/multimodal")
print("MultimodalWorker deployment complete (route: /multimodal).", flush=True)

print(f"Text endpoint:       http://10.208.211.62:{SERVE_PORT}/text/v1/chat/completions", flush=True)
print(f"Multimodal endpoint: http://10.208.211.64:{SERVE_PORT}/multimodal/v1/chat/completions", flush=True)
