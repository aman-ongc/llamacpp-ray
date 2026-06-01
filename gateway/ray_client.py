import itertools
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gateway.config import settings


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.connect_timeout_seconds,
        read=settings.request_timeout_seconds,
        write=settings.request_timeout_seconds,
        pool=settings.connect_timeout_seconds,
    )


def _transport() -> httpx.AsyncHTTPTransport:
    # Keep internal traffic off the corporate proxy path.
    return httpx.AsyncHTTPTransport(retries=0)


def _build_proxy_urls() -> list[str]:
    """
    All Ray Serve HTTP proxies — one per cluster node, all on the same port.
    Locality routing means each proxy prefers the replica on its own node,
    so round-robining across proxy URLs gives even distribution across nodes.
    """
    serve_port = settings.ray_serve_url.split(":")[-1].rstrip("/")
    all_ips = [settings.controller_node_ip] + [
        ip.strip() for ip in settings.worker_node_ips.split(",") if ip.strip()
    ]
    return [f"http://{ip}:{serve_port}" for ip in all_ips]


# Infinite round-robin iterator — thread-safe reads for asyncio (GIL protected).
_proxy_cycle = itertools.cycle(_build_proxy_urls())


def _next_proxy_url() -> str:
    return next(_proxy_cycle)


async def submit_inference(payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    env = {"NO_PROXY": settings.no_proxy, "no_proxy": settings.no_proxy}
    os.environ.update(env)
    url = f"{_next_proxy_url()}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


async def stream_inference(payload: dict[str, Any]) -> AsyncIterator[str]:
    headers = {"Content-Type": "application/json"}
    payload = dict(payload)
    payload["stream"] = True
    env = {"NO_PROXY": settings.no_proxy, "no_proxy": settings.no_proxy}
    os.environ.update(env)
    url = f"{_next_proxy_url()}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    yield f"{line}\n\n"
