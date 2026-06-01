#!/usr/bin/env python3
"""
Deploy LlamaCppWorker as a Ray Serve application on the controller.
Run from /home/administrator/projects/llm-inference-service or wherever
the gateway package is importable.
"""
import os
import sys

# Ensure gateway package importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must be set BEFORE ray.serve is imported — constants.py reads this at module load time.
# Disables locality-aware routing so requests are distributed across all replicas,
# not preferentially sent to the replica collocated with the head node proxy.
os.environ["RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING"] = "0"
os.environ.setdefault("no_proxy", "localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in")
os.environ.setdefault("NO_PROXY", os.environ["no_proxy"])
os.environ["RAY_grpc_enable_http_proxy"] = "0"

import ray
from ray import serve

SERVE_HOST = "0.0.0.0"
SERVE_PORT = 8001

# Connect to the already-running local Ray head node directly
# (not via client protocol, which gRPC-proxies block).
print("Connecting to local Ray cluster...", flush=True)
ray.init(address="auto", ignore_reinit_error=True)
print("Ray connected.", flush=True)

from worker.ray_worker import LlamaCppWorker  # noqa: E402

print(f"Starting Serve on {SERVE_HOST}:{SERVE_PORT}", flush=True)
serve.start(http_options={"host": SERVE_HOST, "port": SERVE_PORT})
handle = serve.run(LlamaCppWorker.bind(), name="llama-worker", route_prefix="/")
print("Ray Serve deployment complete.", flush=True)
print(f"Endpoint: http://10.208.211.62:{SERVE_PORT}/v1/chat/completions", flush=True)
