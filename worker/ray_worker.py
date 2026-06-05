import socket
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from ray.util import get_node_ip_address
from ray import serve
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from gateway.config import settings


@serve.deployment(num_replicas=settings.serve_replicas, ray_actor_options={"num_gpus": 1, "num_cpus": 1})
class LlamaCppWorker:
    def __init__(self, node_ip: str | None = None) -> None:
        self.node_ip = node_ip or get_node_ip_address() or socket.gethostbyname(socket.gethostname())
        self.base_url = f"http://{self.node_ip}:{settings.llama_port}"

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.request_timeout_seconds,
            write=settings.request_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        )

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
            if payload.get("stream"):
                return StreamingResponse(
                    self._stream_chat(payload),
                    media_type="text/event-stream",
                )
            try:
                result = await self.chat(payload)
            except httpx.HTTPStatusError as exc:
                try:
                    body = exc.response.json()
                except Exception:
                    body = {"error": exc.response.text or str(exc)}
                body["node_ip"] = self.node_ip
                return JSONResponse(body, status_code=exc.response.status_code)
            return JSONResponse(result)
        return JSONResponse({"error": "not found"}, status_code=404)

    async def health(self) -> dict[str, str]:
        return {"status": "ok", "node_ip": self.node_ip}

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        url = f"{self.base_url}/v1/chat/completions"
        transport = httpx.AsyncHTTPTransport(retries=0)
        async with httpx.AsyncClient(timeout=self._timeout(), transport=transport, trust_env=False) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        data["node_ip"] = self.node_ip
        data["queue_ms"] = int((time.perf_counter() - started) * 1000)
        return data

    async def _stream_chat(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        url = f"{self.base_url}/v1/chat/completions"
        transport = httpx.AsyncHTTPTransport(retries=0)
        async with httpx.AsyncClient(timeout=self._timeout(), transport=transport, trust_env=False) as client:
            async with client.stream("POST", url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        yield f"{line}\n\n"


app = LlamaCppWorker.bind()
