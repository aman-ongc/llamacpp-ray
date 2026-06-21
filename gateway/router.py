import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import orjson
from fastapi import HTTPException

from gateway.config import settings

logger = logging.getLogger(__name__)

# Same retry taxonomy gateway/ray_client.py used, simplified: since the
# gateway now picks the node itself (instead of asking Ray's deployment
# router), every failure always has a known node_ip — there's no more
# "node_ip present vs absent" ambiguity to branch on.
#
#   - A node we just dispatched to fails (connect error, timeout, or a 5xx
#     from llama-server itself) -> evict it from the healthy set immediately
#     (don't wait for the next 15s health_monitor cycle) and retry on a
#     different node. If a healthy alternate exists, that's a fast reroute
#     (no recovery to wait for, just a short jittered pause). If every node
#     is excluded/unhealthy, fall back to the slower tiered backoff to give
#     a real recovery (llama-server restart by the watchdog) time to land.
#   - The pool itself is full (every node already at `parallel` in-flight
#     requests and the queue is at `max_queued` waiters) -> fail fast with
#     503, no retry. There's no failure to recover from; real demand exceeds
#     real capacity.
_MAX_RETRIES = 5
_FAST_REROUTE_BACKOFFS_SECONDS = [0.5, 1.5]
_POOL_EXHAUSTED_BACKOFFS_SECONDS = [1.0, 15.0, 30.0, 60.0, 120.0]

_RETRYABLE_HTTP_STATUS = {500, 502, 503, 504}


class PoolBusyError(Exception):
    """Raised when a pool's queue is already at max_queued_requests."""


def _backoff(schedule: list[float], attempt: int) -> float:
    return schedule[min(attempt, len(schedule) - 1)]


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.connect_timeout_seconds,
        read=settings.request_timeout_seconds,
        write=settings.request_timeout_seconds,
        pool=settings.connect_timeout_seconds,
    )


def _transport() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(retries=0)


class NodePool:
    """Owns scheduling for one pool of llama-server nodes: which node gets
    the next request, how many requests a node may run concurrently
    (`parallel`, mirrors Ray Serve's old max_ongoing_requests), and how many
    requests may wait for a free slot before the pool fails fast
    (`max_queued`, mirrors max_queued_requests).

    In-memory only — correct for a single gateway process (the deployment
    today runs exactly one uvicorn worker). A multi-process gateway would
    need this state in Redis instead.
    """

    def __init__(self, ips: list[str], port: int, parallel: int, max_queued: int, healthy: set[str]):
        self.ips = ips
        self.port = port
        self.parallel = parallel
        self.max_queued = max_queued
        self.healthy = healthy  # shared with gateway.health_monitor — same set object
        self.in_flight: dict[str, int] = {ip: 0 for ip in ips}
        self.waiting = 0
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)

    def _candidates(self, exclude: set[str]) -> list[str]:
        # Only fall back to treating every node as a candidate when NONE are
        # known healthy (e.g. health_monitor hasn't probed yet) — if some
        # nodes ARE known healthy but all of them are currently at capacity,
        # that must produce zero candidates (queue/wait), not a fallback to
        # an unhealthy node. Folding the health filter in before the capacity
        # filter would let a node we just evicted re-enter the running once
        # its healthy peers saturate — exactly the bias Ray's scheduler had
        # (an instantly-failing node looks "idle" and gets picked more, not
        # less, while broken).
        pool_ips = [ip for ip in self.ips if ip not in exclude]
        known_healthy = [ip for ip in pool_ips if ip in self.healthy]
        relevant = known_healthy or pool_ips
        return [ip for ip in relevant if self.in_flight[ip] < self.parallel]

    def has_alternate(self, exclude: set[str]) -> bool:
        """True if some non-excluded node could still take a request right
        now — used to pick the fast-reroute vs pool-exhausted backoff tier
        after a failure."""
        return bool(self._candidates(exclude))

    async def acquire(self, exclude: set[str], affinity_ip: str | None = None) -> str:
        """Pick a node and reserve one of its slots. Blocks (queued) if every
        node is currently at `parallel` capacity, up to `max_queued` waiters;
        beyond that, raises PoolBusyError immediately."""
        async with self._lock:
            candidates = self._candidates(exclude)
            if not candidates:
                if self.waiting >= self.max_queued:
                    raise PoolBusyError()
                self.waiting += 1
                try:
                    while not (candidates := self._candidates(exclude)):
                        await self._cond.wait()
                finally:
                    self.waiting -= 1
            if affinity_ip and affinity_ip in candidates:
                chosen = affinity_ip
            else:
                chosen = min(candidates, key=lambda ip: self.in_flight[ip])
            self.in_flight[chosen] += 1
            return chosen

    async def release(self, ip: str) -> None:
        async with self._lock:
            self.in_flight[ip] = max(0, self.in_flight[ip] - 1)
            self._cond.notify_all()

    def evict(self, ip: str) -> None:
        """Mark a node unhealthy immediately on a real failure — don't wait
        for the next health_monitor probe cycle."""
        self.healthy.discard(ip)

    def affinity_node(self, key: str) -> str | None:
        """Deterministic node for a given affinity key: hash(key) % len(ips).
        Falls back to the same hash applied to the healthy subset if the
        preferred node is currently down; returns None if nothing is known
        healthy (caller then dispatches without an affinity preference)."""
        preferred = self.ips[hash(key) % len(self.ips)]
        if preferred in self.healthy:
            return preferred
        healthy_ips = [ip for ip in self.ips if ip in self.healthy]
        if healthy_ips:
            return healthy_ips[hash(key) % len(healthy_ips)]
        return None


def _make_pool(ips_csv: str, port: int, parallel: int, max_queued: int, healthy: set[str]) -> NodePool:
    ips = [ip.strip() for ip in ips_csv.split(",") if ip.strip()]
    return NodePool(ips, port, parallel, max_queued, healthy)


# Imported lazily inside the factory below to avoid a circular import at
# module load (health_monitor doesn't import router, but keeping the same
# defensive pattern ray_client.py used).
from gateway.health_monitor import healthy_multimodal_nodes, healthy_text_nodes  # noqa: E402

text_pool = _make_pool(
    settings.text_node_ips, settings.text_llama_port,
    settings.llama_parallel, settings.text_max_queued_requests, healthy_text_nodes,
)
multimodal_pool = _make_pool(
    settings.multimodal_node_ips, settings.multimodal_llama_port,
    settings.multimodal_llama_parallel, settings.multimodal_max_queued_requests, healthy_multimodal_nodes,
)


def _pool_for(multimodal: bool) -> NodePool:
    return multimodal_pool if multimodal else text_pool


async def _send(client: httpx.AsyncClient, ip: str, port: int, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"http://{ip}:{port}/v1/chat/completions"
    response = await client.post(url, content=orjson.dumps(payload), headers={"Content-Type": "application/json"})
    response.raise_for_status()
    return orjson.loads(response.content)


async def submit_inference(
    payload: dict[str, Any],
    affinity_key: str | None = None,
    multimodal: bool = False,
) -> dict[str, Any]:
    pool = _pool_for(multimodal)
    payload = dict(payload)
    payload["stream"] = False
    affinity_ip = pool.affinity_node(affinity_key) if affinity_key else None
    excluded: set[str] = set()

    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        attempt = 0
        while True:
            acquire_started = time.perf_counter()
            try:
                ip = await pool.acquire(excluded, affinity_ip)
            except PoolBusyError:
                raise HTTPException(status_code=503, detail={"error": "Inference pool is full, please retry shortly"})
            queue_ms = int((time.perf_counter() - acquire_started) * 1000)
            try:
                data = await _send(client, ip, pool.port, payload)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                await pool.release(ip)
                non_retryable_http_error = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code not in _RETRYABLE_HTTP_STATUS
                )
                if non_retryable_http_error:
                    # e.g. a 400 bad request from llama-server itself — not a node
                    # failure, retrying elsewhere won't help. Surface as-is.
                    try:
                        detail = exc.response.json()
                    except Exception:
                        detail = exc.response.text or "Inference backend error"
                    raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
                if attempt >= _MAX_RETRIES:
                    raise HTTPException(status_code=503, detail={"error": f"Unable to reach inference backend: {exc}"}) from exc
                # Every httpx.TransportError variant (ConnectError, ReadError,
                # RemoteProtocolError, ConnectTimeout, ReadTimeout, ...) means
                # we picked a node and didn't get a usable response from it —
                # evict it regardless of which specific transport failure it
                # was. A wedged/dead node looks the same from here either way.
                pool.evict(ip)
                excluded.add(ip)
                has_alternate = pool.has_alternate(excluded)
                backoff = _FAST_REROUTE_BACKOFFS_SECONDS if has_alternate else _POOL_EXHAUSTED_BACKOFFS_SECONDS
                logger.warning("submit_inference: %s failed (%s), rerouting (excluded=%s)", ip, exc, sorted(excluded))
                await asyncio.sleep(_backoff(backoff, attempt))
                attempt += 1
                continue
            await pool.release(ip)
            data["node_ip"] = ip
            data["queue_ms"] = queue_ms
            return data


async def stream_inference(
    payload: dict[str, Any],
    affinity_key: str | None = None,
    multimodal: bool = False,
) -> AsyncIterator[str]:
    pool = _pool_for(multimodal)
    payload = dict(payload)
    payload["stream"] = True
    affinity_ip = pool.affinity_node(affinity_key) if affinity_key else None
    excluded: set[str] = set()
    attempt = 0
    started_streaming = False

    async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(), trust_env=True) as client:
        while True:
            try:
                ip = await pool.acquire(excluded, affinity_ip)
            except PoolBusyError:
                raise HTTPException(status_code=503, detail={"error": "Inference pool is full, please retry shortly"})
            url = f"http://{ip}:{pool.port}/v1/chat/completions"
            try:
                async with client.stream("POST", url, content=orjson.dumps(payload), headers={"Content-Type": "application/json"}) as response:
                    if response.is_error and response.status_code in _RETRYABLE_HTTP_STATUS and attempt < _MAX_RETRIES:
                        await pool.release(ip)
                        pool.evict(ip)
                        excluded.add(ip)
                        has_alternate = pool.has_alternate(excluded)
                        backoff = _FAST_REROUTE_BACKOFFS_SECONDS if has_alternate else _POOL_EXHAUSTED_BACKOFFS_SECONDS
                        logger.warning("stream_inference: %s returned %d, rerouting (excluded=%s)", ip, response.status_code, sorted(excluded))
                        await asyncio.sleep(_backoff(backoff, attempt))
                        attempt += 1
                        continue
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            started_streaming = True
                            yield f"{line}\n\n"
                    await pool.release(ip)
                    return
            except httpx.TransportError as exc:
                await pool.release(ip)
                # A failure after bytes have already reached the client (mid-stream
                # disconnect) can't be safely retried — the client already has a
                # partial response. Only retry/reroute failures before that point
                # (connect failed, or the connection died before any content arrived).
                if started_streaming:
                    raise
                if attempt >= _MAX_RETRIES:
                    raise HTTPException(status_code=503, detail={"error": f"Unable to reach inference backend: {exc}"}) from exc
                pool.evict(ip)
                excluded.add(ip)
                has_alternate = pool.has_alternate(excluded)
                backoff = _FAST_REROUTE_BACKOFFS_SECONDS if has_alternate else _POOL_EXHAUSTED_BACKOFFS_SECONDS
                logger.warning("stream_inference: %s failed (%s), rerouting (excluded=%s)", ip, exc, sorted(excluded))
                await asyncio.sleep(_backoff(backoff, attempt))
                attempt += 1
                continue
            except Exception:
                await pool.release(ip)
                raise
