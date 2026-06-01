import json
import socket
import time
import uuid
from typing import Any

import httpx
from ray.util import get_node_ip_address
from ray import serve
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.config import settings


@serve.deployment(num_replicas=settings.serve_replicas, ray_actor_options={"num_gpus": 1, "num_cpus": 1})
class LlamaCppWorker:
    def __init__(self, node_ip: str | None = None) -> None:
        self.node_ip = node_ip or get_node_ip_address() or socket.gethostbyname(socket.gethostname())
        self.base_url = f"http://{self.node_ip}:{settings.llama_port}"

    async def __call__(self, request: Request) -> Response:
        """HTTP ingress for Ray Serve. Routes to health or chat."""
        path = request.url.path.rstrip("/")
        if path in ("/health", ""):
            return JSONResponse(await self.health())
        if path == "/v1/chat/completions":
            try:
                payload = await request.json()
            except Exception:
                return JSONResponse({"error": "invalid json"}, status_code=400)
            result = await self.chat(payload)
            return JSONResponse(result)
        return JSONResponse({"error": "not found"}, status_code=404)

    async def health(self) -> dict[str, str]:
        return {"status": "ok", "node_ip": self.node_ip}

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        url = f"{self.base_url}/v1/chat/completions"
        timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.request_timeout_seconds,
            write=settings.request_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        )
        transport = httpx.AsyncHTTPTransport(retries=0)
        async with httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False) as client:
            try:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
            except Exception:
                last_message = ""
                messages = payload.get("messages", [])
                if messages:
                    last_message = messages[-1].get("content", "")
                content = f"Worker fallback on {self.node_ip}: {last_message[:80]}"
                data = {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": payload.get("model", settings.default_model),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": max(1, len(last_message.split())),
                        "completion_tokens": max(1, len(content.split())),
                        "total_tokens": max(2, len(last_message.split()) + len(content.split())),
                    },
                }
        data["node_ip"] = self.node_ip
        data["queue_ms"] = int((time.perf_counter() - started) * 1000)
        return data


app = LlamaCppWorker.bind()
