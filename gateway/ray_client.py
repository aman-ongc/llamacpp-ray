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


async def submit_inference(payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    env = {"NO_PROXY": settings.no_proxy, "no_proxy": settings.no_proxy}
    os.environ.update(env)
    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        response = await client.post(
            f"{settings.ray_serve_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()


async def stream_inference(payload: dict[str, Any]) -> AsyncIterator[str]:
    headers = {"Content-Type": "application/json"}
    payload = dict(payload)
    payload["stream"] = True
    env = {"NO_PROXY": settings.no_proxy, "no_proxy": settings.no_proxy}
    os.environ.update(env)
    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        async with client.stream(
            "POST",
            f"{settings.ray_serve_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    yield f"{line}\n\n"
