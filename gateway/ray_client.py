import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from gateway.config import settings

logger = logging.getLogger(__name__)

# Retry on 503 (node crashed/restarting, or Ray Serve queue backpressure),
# 500 (replica died mid-request), and 504 (worker's own call to llama-server
# timed out). Capped at 4 retries (5 attempts total) — see case B below for
# why the cap had to grow.
#
# Each worker stamps its own node_ip on every error body it produces, so we
# can tell apart three distinct situations and treat them differently
# instead of using one flat backoff for all of them:
#
#   A) node_ip present, and a different healthy node exists — evict the
#      failed node (see _reroute_after_node_failure) and reroute the retry
#      to it immediately. We already know that node is healthy, so there's
#      no recovery to wait for — just a short jittered pause to avoid
#      hammering.
#   B) node_ip present, but every node is now excluded — OR the gateway
#      pointed the retry at a different node's proxy and it *still* came
#      back from the same bad node. That second path is real: Ray Serve's
#      deployment router is cluster-wide regardless of which node's proxy
#      accepted the connection, so our "different node" URL doesn't
#      guarantee a different replica — and a node that fails instantly
#      looks idle (zero queue depth) to Ray's scheduler, so it gets picked
#      *more* often while broken, not less. We can't out-route that bias
#      from the gateway; we can only out-wait it. Observed recovery time is
#      ~56s typically, but multimodal nodes have been seen taking 100s+
#      (occasionally crossing the watchdog's own 120s restart-wait and
#      needing a second cycle), so the schedule is a cheap 1s check first
#      (don't pay for a wait if the next attempt would've failed anyway),
#      then 15s/30s/60s/120s to clear the recovery window even under that
#      bias. This tier only — case A/D's fast-reroute tier is untouched, so
#      a known-healthy reroute never pays this latency.
#   C) no node_ip, status 503 — Ray Serve's own queue backpressure
#      (max_queued_requests exceeded), rejected by the proxy before any
#      replica ran. There's no failed node to evict and no recovery for a
#      wait to buy: the queue is full because real demand exceeds real
#      capacity, not because something broke. Retrying with a guessed wait
#      doesn't fix that, so we don't retry this case at all — fail fast.
#   D) no node_ip, status 500 — the Ray actor itself died (e.g. controller
#      force-killed a replica that failed its health check), so the error
#      never reached our worker code far enough to stamp a node_ip. We
#      don't know which node it was, but we don't need to: Ray's controller
#      already evicted that replica from the deployment before surfacing
#      this error, so the same proxy/affinity URL will land on a different,
#      already-healthy replica on retry. No recovery wait needed — same
#      fast tier as (A).
#   E) httpx.ConnectError / httpx.ConnectTimeout — the TCP connection to the
#      proxy itself never came up (e.g. a node's Ray Serve proxy
#      crash-restarting, its raylet down entirely, or — for ConnectTimeout —
#      the head node's Serve HTTP proxy too backed up to accept new
#      connections within connect_timeout_seconds). Neither is an HTTP error
#      response at all — no body, no node_ip to read — so they can't go
#      through the same branch as A-D. We know which node we *tried* to
#      reach from the URL we just attempted, though, so we evict that host
#      directly and reroute like case A, instead of letting it escape
#      uncaught. ConnectTimeout used to fall through to the generic
#      ReadTimeout/TimeoutException handler below, which only retries once,
#      blind, against the same URL — fine for a slow read, useless for a
#      connection that's never going to complete. Routing it through case E
#      instead gives it the full tiered backoff, and (for multimodal, which
#      has no alternate node to reroute to) the pool-exhausted wait instead
#      of giving up after one blind retry.
_RETRYABLE_STATUS_CODES = {500, 503, 504}
# _MAX_RETRIES caps both schedules below via _backoff()'s clamp. It's sized
# to the longer one (pool-exhausted, 5 entries) — case A/D's fast tier just
# clamps to its last entry (1.5s) for the extra attempt, so this doesn't add
# latency to the fast-reroute path, only extends how long we wait out a node
# that has no healthy alternate (case B/E-no-alternate).
_MAX_RETRIES = 5
_FAST_REROUTE_BACKOFFS_SECONDS = [0.5, 1.5]
_POOL_EXHAUSTED_BACKOFFS_SECONDS = [1.0, 15.0, 30.0, 60.0, 120.0]


def _backoff(schedule: list[float], attempt: int) -> float:
    """Clamp to the schedule's last entry if attempt exceeds its length —
    lets case A/D's short fast-tier list coexist with case B's longer one
    under a single shared _MAX_RETRIES."""
    return schedule[min(attempt, len(schedule) - 1)]

# Retry once on a gateway-side read timeout (the request hung on a wedged
# replica past request_timeout_seconds). The retry goes back through the
# central proxy, which should land on a different, healthy replica.
# Non-streaming only — a streaming response may have already sent bytes to
# the client by the time it times out, so it can't be safely retried.
_TIMEOUT_MAX_RETRIES = 1


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
    """Per-node Ray Serve HTTP proxies for text nodes — used for affinity routing only."""
    serve_port = settings.ray_serve_url.split(":")[-1].rstrip("/")
    ips = [ip.strip() for ip in settings.text_node_ips.split(",") if ip.strip()]
    return [f"http://{ip}:{serve_port}" for ip in ips]


def _build_central_text_proxy_url() -> str:
    """Single entry point for non-affinity text requests.

    Sending all requests here lets Ray's internal load balancer dispatch to
    whichever text replica is free, queuing when all are at max_ongoing_requests.
    """
    serve_port = settings.ray_serve_url.split(":")[-1].rstrip("/")
    return f"http://{settings.controller_node_ip}:{serve_port}"


def _build_multimodal_proxy_url() -> str:
    """Central Ray Serve proxy for multimodal pool (Ray dispatches to first free replica)."""
    serve_port = settings.ray_serve_url.split(":")[-1].rstrip("/")
    return f"http://{settings.controller_node_ip}:{serve_port}"


_text_node_ips: list[str] = [ip.strip() for ip in settings.text_node_ips.split(",") if ip.strip()]
_multimodal_node_ips: list[str] = [ip.strip() for ip in settings.multimodal_node_ips.split(",") if ip.strip()]
_text_proxy_urls: list[str] = _build_text_proxy_urls()
_central_text_proxy_url: str = _build_central_text_proxy_url()
_multimodal_proxy_url: str = _build_multimodal_proxy_url()

_serve_port: str = settings.ray_serve_url.split(":")[-1].rstrip("/")


def _affinity_text_proxy_url(key: str) -> str:
    """Deterministic proxy URL for a given affinity key.

    Preferred node: hash(key) % total nodes.
    If that node is currently unhealthy, fall back to the same hash applied
    against only the healthy subset — stable routing within the surviving pool.
    If no healthy nodes are known, fall through to the central Ray proxy so
    Ray's own load balancer can try.
    """
    from gateway.health_monitor import healthy_text_nodes  # avoid circular import at module load

    preferred_ip = _text_node_ips[hash(key) % len(_text_node_ips)]
    if preferred_ip in healthy_text_nodes:
        return f"http://{preferred_ip}:{_serve_port}"

    healthy = [ip for ip in _text_node_ips if ip in healthy_text_nodes]
    if healthy:
        return f"http://{healthy[hash(key) % len(healthy)]}:{_serve_port}"

    # Zero healthy nodes known — let Ray central proxy decide.
    return _central_text_proxy_url


def _select_text_proxy_url(affinity_key: str | None) -> str:
    if affinity_key:
        return _affinity_text_proxy_url(affinity_key)
    # No affinity: use central proxy so Ray queues and dispatches to the first free replica.
    return _central_text_proxy_url


def _set_no_proxy() -> None:
    env = {"NO_PROXY": settings.no_proxy, "no_proxy": settings.no_proxy}
    os.environ.update(env)


def _reroute_after_node_failure(
    detail: Any, excluded_nodes: set[str], multimodal: bool, route_suffix: str
) -> str | None:
    """Evict the node that just failed and point the next retry at a different one.

    Every worker stamps its own node_ip on every error body, so we know
    exactly which backend failed — no need to guess. Mark it unhealthy
    immediately (don't wait for the next 15s health-probe cycle in
    health_monitor) and route the retry to a node that isn't it, instead of
    resubmitting blind to the central proxy, which could easily land back on
    the same broken node.

    Returns None if there's no node_ip to act on, or no other node left to
    route to — caller should fall back to the existing URL in that case.
    """
    from gateway.health_monitor import healthy_multimodal_nodes, healthy_text_nodes

    failed_ip = detail.get("node_ip") if isinstance(detail, dict) else None
    if not failed_ip:
        return None
    excluded_nodes.add(failed_ip)

    node_ips = _multimodal_node_ips if multimodal else _text_node_ips
    healthy = healthy_multimodal_nodes if multimodal else healthy_text_nodes
    healthy.discard(failed_ip)

    candidates = [ip for ip in node_ips if ip in healthy and ip not in excluded_nodes]
    if not candidates:
        candidates = [ip for ip in node_ips if ip not in excluded_nodes]
    if not candidates:
        logger.warning(
            "reroute: %s failed, no alternate %s node available (excluded=%s) — pool exhausted",
            failed_ip, "multimodal" if multimodal else "text", sorted(excluded_nodes),
        )
        return None
    new_ip = candidates[0]
    logger.info(
        "reroute: %s failed, rerouting %s request to %s (excluded=%s)",
        failed_ip, "multimodal" if multimodal else "text", new_ip, sorted(excluded_nodes),
    )
    return f"http://{new_ip}:{_serve_port}{route_suffix}"


def _url_host(url: str) -> str | None:
    return urlparse(url).hostname


async def _reroute_after_connect_error(
    url: str, excluded_nodes: set[str], multimodal: bool, route_suffix: str, attempt: int
) -> str:
    """Case E handling: evict the host we just failed to connect to and wait
    on the same tiered backoff as A/B, since there's no node_ip-bearing body
    to drive _reroute_after_node_failure — only the URL we just tried."""
    failed_ip = _url_host(url)
    new_url = (
        _reroute_after_node_failure({"node_ip": failed_ip}, excluded_nodes, multimodal, route_suffix)
        if failed_ip
        else None
    )
    if new_url is not None:
        await asyncio.sleep(_backoff(_FAST_REROUTE_BACKOFFS_SECONDS, attempt))
        return new_url
    await asyncio.sleep(_backoff(_POOL_EXHAUSTED_BACKOFFS_SECONDS, attempt))
    return url


async def submit_inference(
    payload: dict[str, Any],
    affinity_key: str | None = None,
    multimodal: bool = False,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    payload = dict(payload)
    payload["stream"] = False
    _set_no_proxy()
    route_suffix = "/multimodal/v1/chat/completions" if multimodal else "/text/v1/chat/completions"
    if multimodal:
        url = f"{_multimodal_proxy_url}{route_suffix}"
    else:
        url = f"{_select_text_proxy_url(affinity_key)}{route_suffix}"
    excluded_nodes: set[str] = set()
    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        for timeout_attempt in range(_TIMEOUT_MAX_RETRIES + 1):
            try:
                attempt = 0
                while True:
                    try:
                        response = await client.post(url, json=payload, headers=headers)
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        if attempt >= _MAX_RETRIES:
                            raise HTTPException(
                                status_code=503,
                                detail={"error": f"Unable to reach inference backend: {exc}"},
                            ) from exc
                        url = await _reroute_after_connect_error(url, excluded_nodes, multimodal, route_suffix, attempt)
                        attempt += 1
                        continue
                    if response.is_error:
                        try:
                            detail = response.json()
                        except Exception:
                            detail = response.text or "Inference backend error"
                        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                            failed_ip = detail.get("node_ip") if isinstance(detail, dict) else None
                            if failed_ip:
                                new_url = _reroute_after_node_failure(detail, excluded_nodes, multimodal, route_suffix)
                                if new_url is not None:
                                    url = new_url
                                    await asyncio.sleep(_backoff(_FAST_REROUTE_BACKOFFS_SECONDS, attempt))
                                else:
                                    await asyncio.sleep(_backoff(_POOL_EXHAUSTED_BACKOFFS_SECONDS, attempt))
                                attempt += 1
                                continue
                            if response.status_code == 500:
                                # No node_ip, status 500: Ray actor died — controller already
                                # evicted it, so retrying the same URL lands on a different,
                                # healthy replica. No node to exclude, no wait to buy.
                                await asyncio.sleep(_backoff(_FAST_REROUTE_BACKOFFS_SECONDS, attempt))
                                attempt += 1
                                continue
                            # No node_ip, status 503: Ray queue backpressure, not a node
                            # failure. No fix for a retry to buy — fail fast.
                        raise HTTPException(status_code=response.status_code, detail=detail)
                    return response.json()
            except (httpx.ReadTimeout, httpx.TimeoutException):
                if timeout_attempt < _TIMEOUT_MAX_RETRIES:
                    continue
                raise


async def stream_inference(
    payload: dict[str, Any],
    affinity_key: str | None = None,
    multimodal: bool = False,
) -> AsyncIterator[str]:
    headers = {"Content-Type": "application/json"}
    payload = dict(payload)
    payload["stream"] = True
    _set_no_proxy()
    route_suffix = "/multimodal/v1/chat/completions" if multimodal else "/text/v1/chat/completions"
    if multimodal:
        url = f"{_multimodal_proxy_url}{route_suffix}"
    else:
        url = f"{_select_text_proxy_url(affinity_key)}{route_suffix}"
    excluded_nodes: set[str] = set()
    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        attempt = 0
        while True:
            try:
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.is_error and response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                        try:
                            await response.aread()
                            detail = response.json()
                        except Exception:
                            detail = None
                        failed_ip = detail.get("node_ip") if isinstance(detail, dict) else None
                        if failed_ip:
                            new_url = _reroute_after_node_failure(detail, excluded_nodes, multimodal, route_suffix)
                            if new_url is not None:
                                url = new_url
                                await asyncio.sleep(_backoff(_FAST_REROUTE_BACKOFFS_SECONDS, attempt))
                            else:
                                await asyncio.sleep(_backoff(_POOL_EXHAUSTED_BACKOFFS_SECONDS, attempt))
                            attempt += 1
                            continue
                        if response.status_code == 500:
                            # No node_ip, status 500: Ray actor died — controller already
                            # evicted it, retry the same URL to land on a healthy replica.
                            await asyncio.sleep(_backoff(_FAST_REROUTE_BACKOFFS_SECONDS, attempt))
                            attempt += 1
                            continue
                        # No node_ip, status 503: Ray queue backpressure — fail fast, no retry.
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
                    return
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Connection never established — no bytes can have reached the
                # client yet, so this is always safe to retry/reroute, unlike a
                # mid-stream failure.
                if attempt >= _MAX_RETRIES:
                    raise HTTPException(
                        status_code=503,
                        detail={"error": f"Unable to reach inference backend: {exc}"},
                    ) from exc
                url = await _reroute_after_connect_error(url, excluded_nodes, multimodal, route_suffix, attempt)
                attempt += 1
                continue
