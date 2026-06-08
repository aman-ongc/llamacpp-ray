import itertools
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import HTTPException

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


def _build_text_proxy_urls() -> list[str]:
    """
    Ray Serve HTTP proxies for text nodes (WS-11, WS-03, WS-08).
    All on port 8001. Round-robin / affinity distributes across Gemma nodes.
    """
    serve_port = settings.ray_serve_url.split(":")[-1].rstrip("/")
    ips = [ip.strip() for ip in settings.text_node_ips.split(",") if ip.strip()]
    return [f"http://{ip}:{serve_port}" for ip in ips]


def _build_multimodal_proxy_url() -> str:
    """Single Ray Serve HTTP proxy for WS-13 (Qwen3-VL multimodal node)."""
    serve_port = settings.ray_serve_url.split(":")[-1].rstrip("/")
    return f"http://{settings.multimodal_node_ip}:{serve_port}"


_text_proxy_urls: list[str] = _build_text_proxy_urls()
_multimodal_proxy_url: str = _build_multimodal_proxy_url()

# Infinite round-robin over text nodes — GIL-safe for asyncio.
_text_proxy_cycle = itertools.cycle(_text_proxy_urls)


def _next_text_proxy_url() -> str:
    return next(_text_proxy_cycle)


def _affinity_text_proxy_url(key: str) -> str:
    """Deterministic proxy URL for a given affinity key.

    Same API key prefix always maps to the same Gemma node so llama.cpp
    can reuse its KV cache across consecutive requests from that user.
    """
    return _text_proxy_urls[hash(key) % len(_text_proxy_urls)]


def _select_text_proxy_url(affinity_key: str | None) -> str:
    if affinity_key:
        return _affinity_text_proxy_url(affinity_key)
    return _next_text_proxy_url()


def _set_no_proxy() -> None:
    env = {"NO_PROXY": settings.no_proxy, "no_proxy": settings.no_proxy}
    os.environ.update(env)


async def submit_inference(
    payload: dict[str, Any],
    affinity_key: str | None = None,
    multimodal: bool = False,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    payload = dict(payload)
    payload["stream"] = False
    _set_no_proxy()
    if multimodal:
        url = f"{_multimodal_proxy_url}/multimodal/v1/chat/completions"
    else:
        url = f"{_select_text_proxy_url(affinity_key)}/text/v1/chat/completions"
    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.is_error:
            try:
                detail = response.json()
            except Exception:
                detail = response.text or "Inference backend error"
            raise HTTPException(status_code=response.status_code, detail=detail)
        return response.json()


async def stream_inference(
    payload: dict[str, Any],
    affinity_key: str | None = None,
    multimodal: bool = False,
) -> AsyncIterator[str]:
    headers = {"Content-Type": "application/json"}
    payload = dict(payload)
    payload["stream"] = True
    _set_no_proxy()
    if multimodal:
        url = f"{_multimodal_proxy_url}/multimodal/v1/chat/completions"
    else:
        url = f"{_select_text_proxy_url(affinity_key)}/text/v1/chat/completions"
    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    yield f"{line}\n\n"
