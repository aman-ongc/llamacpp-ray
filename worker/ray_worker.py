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

_MULTIMODAL_NODE_IPS: set[str] = {
    ip.strip() for ip in settings.multimodal_node_ips.split(",") if ip.strip()
}


def _resolve_node_ip() -> str:
    return get_node_ip_address() or socket.gethostbyname(socket.gethostname())


def _llama_port_for_node(node_ip: str) -> int:
    if node_ip in _MULTIMODAL_NODE_IPS:
        return settings.multimodal_llama_port
    return settings.text_llama_port


class _LlamaWorkerBase:
    """Shared HTTP proxy logic for both text and multimodal workers."""

    def __init__(self) -> None:
        self.node_ip = _resolve_node_ip()
        port = _llama_port_for_node(self.node_ip)
        self.base_url = f"http://{self.node_ip}:{port}"

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.request_timeout_seconds,
            write=settings.request_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        )

    async def __call__(self, request: Request) -> Response:
        # Ray Serve passes the FULL path including route prefix (/text/... or /multimodal/...).
        # Use endswith so both prefixed and un-prefixed calls work.
        path = request.url.path.rstrip("/")
        if path in ("/health", "") or path.endswith("/health"):
            return JSONResponse(await self.health())
        if path == "/v1/chat/completions" or path.endswith("/v1/chat/completions"):
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
            except httpx.RemoteProtocolError:
                return JSONResponse(
                    {"error": "Inference server disconnected during generation. The server may have crashed. Please retry your request.", "node_ip": self.node_ip},
                    status_code=503,
                )
            except httpx.ConnectError:
                return JSONResponse(
                    {"error": "Inference server unavailable. The server may be starting up or restarting. Please retry in a few moments.", "node_ip": self.node_ip},
                    status_code=503,
                )
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


# Replicas pinned to text nodes via custom Ray resource "text_node".
# Text nodes (WS-11, WS-03, WS-08) must be started with --resources='{"text_node": 1}'.
@serve.deployment(
    num_replicas=settings.text_serve_replicas,
    max_ongoing_requests=settings.llama_parallel,
    ray_actor_options={"num_cpus": 1, "resources": {"text_node": 0.01}},
)
class TextWorker(_LlamaWorkerBase):
    def __init__(self) -> None:
        super().__init__()


# Replica pinned to multimodal node via custom Ray resource "multimodal_node".
# WS-13 must be started with --resources='{"multimodal_node": 1}'.
@serve.deployment(
    num_replicas=settings.multimodal_serve_replicas,
    max_ongoing_requests=settings.multimodal_llama_parallel,
    ray_actor_options={"num_cpus": 1, "resources": {"multimodal_node": 0.01}},
)
class MultimodalWorker(_LlamaWorkerBase):
    def __init__(self) -> None:
        super().__init__()


text_app = TextWorker.bind()
multimodal_app = MultimodalWorker.bind()
