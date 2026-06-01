#!/usr/bin/env python3
"""
Cluster benchmark: distribution verification + performance measurement.

Usage:
    python benchmark.py --api-key sk-ongc-...
    python benchmark.py --api-key sk-ongc-... --concurrency 4 --requests 20
    python benchmark.py --api-key sk-ongc-... --mode distribution   # just node spread check
    python benchmark.py --api-key sk-ongc-... --mode throughput     # concurrency ramp
    python benchmark.py --api-key sk-ongc-... --mode streaming      # TTFT measurement
    python benchmark.py --api-key sk-ongc-... --mode all            # default
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

try:
    import httpx
except ImportError:
    sys.exit("Install httpx:  pip install httpx")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GATEWAY_URL = "http://10.208.211.62:18000"
RAY_SERVE_URL = "http://10.208.211.62:8001"
MODEL = "qwen"

SHORT_PROMPT = "Reply with exactly: 'pong'"
MEDIUM_PROMPT = "Explain what a GPU is in exactly 3 sentences."
LONG_PROMPT = (
    "Describe the history of oil exploration in India from 1860 to 2000, "
    "covering major milestones, organizations, and technological advances. "
    "Be thorough and detailed."
)

NOPROXY = {"http://": None, "https://": None}  # bypass any system proxy (httpx mounts syntax)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class RequestResult:
    ok: bool
    latency_ms: float
    ttft_ms: Optional[float]       # time-to-first-token (streaming only)
    prompt_tokens: int
    completion_tokens: int
    node_ip: Optional[str]         # from x-node-ip header or response body
    error: Optional[str]


@dataclass
class BenchmarkStats:
    name: str
    results: list[RequestResult] = field(default_factory=list)

    @property
    def successes(self):
        return [r for r in self.results if r.ok]

    @property
    def failures(self):
        return [r for r in self.results if not r.ok]

    def latencies(self):
        return [r.latency_ms for r in self.successes]

    def ttfts(self):
        return [r.ttft_ms for r in self.successes if r.ttft_ms is not None]

    def tokens_per_sec(self):
        vals = []
        for r in self.successes:
            if r.latency_ms > 0 and r.completion_tokens > 0:
                vals.append(r.completion_tokens / (r.latency_ms / 1000))
        return vals

    def node_distribution(self):
        dist: dict[str, int] = defaultdict(int)
        for r in self.successes:
            dist[r.node_ip or "unknown"] += 1
        return dict(dist)

    def print(self):
        n = len(self.results)
        ok = len(self.successes)
        fail = len(self.failures)

        lat = self.latencies()
        tps = self.tokens_per_sec()
        ttft = self.ttfts()
        dist = self.node_distribution()

        print(f"\n{'═' * 60}")
        print(f"  {self.name}")
        print(f"{'═' * 60}")
        print(f"  Requests   : {ok}/{n} succeeded  ({fail} failed)")

        if lat:
            print(f"\n  Latency (end-to-end)")
            print(f"    min      : {min(lat):.0f} ms")
            print(f"    median   : {statistics.median(lat):.0f} ms")
            print(f"    p95      : {_pct(lat, 95):.0f} ms")
            print(f"    p99      : {_pct(lat, 99):.0f} ms")
            print(f"    max      : {max(lat):.0f} ms")

        if ttft:
            print(f"\n  Time-to-first-token")
            print(f"    min      : {min(ttft):.0f} ms")
            print(f"    median   : {statistics.median(ttft):.0f} ms")
            print(f"    p95      : {_pct(ttft, 95):.0f} ms")

        if tps:
            print(f"\n  Throughput")
            print(f"    tok/s    : {statistics.mean(tps):.1f} avg  "
                  f"(min {min(tps):.1f}  max {max(tps):.1f})")

        if dist:
            print(f"\n  Node distribution")
            total = sum(dist.values())
            for ip, cnt in sorted(dist.items(), key=lambda x: -x[1]):
                bar = "█" * int(20 * cnt / total)
                print(f"    {ip:<20} {cnt:>3} reqs  {bar}")

        if self.failures:
            print(f"\n  Errors")
            seen = set()
            for r in self.failures:
                if r.error not in seen:
                    print(f"    {r.error}")
                    seen.add(r.error)

        print()


def _pct(data: list[float], p: int) -> float:
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * p / 100)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _make_client(timeout: float = 300.0) -> httpx.AsyncClient:
    # httpx >=0.28 removed 'proxies' kwarg; use 'mounts' to bypass system proxy
    return httpx.AsyncClient(
        mounts=NOPROXY,
        timeout=httpx.Timeout(timeout, connect=5.0),
    )


def _chat_payload(prompt: str, max_tokens: int = 64, stream: bool = False) -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": stream,
        "temperature": 0.0,
    }


def _extract_node_ip(headers: httpx.Headers, body: dict) -> Optional[str]:
    # Gateway may forward node IP as a custom header
    ip = headers.get("x-node-ip") or headers.get("x-served-by")
    if ip:
        return ip
    # Some deployments embed it in a top-level field
    return body.get("node_ip") or body.get("worker_node")


async def _single_request(
    client: httpx.AsyncClient,
    api_key: str,
    prompt: str,
    max_tokens: int = 64,
) -> RequestResult:
    headers = {"Authorization": f"Bearer {api_key}"}
    t0 = time.perf_counter()
    try:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            json=_chat_payload(prompt, max_tokens, stream=False),
            headers=headers,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            return RequestResult(
                ok=False, latency_ms=latency_ms, ttft_ms=None,
                prompt_tokens=0, completion_tokens=0, node_ip=None,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        body = resp.json()
        usage = body.get("usage") or {}
        node_ip = _extract_node_ip(resp.headers, body)
        return RequestResult(
            ok=True,
            latency_ms=latency_ms,
            ttft_ms=None,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            node_ip=node_ip,
            error=None,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return RequestResult(
            ok=False, latency_ms=latency_ms, ttft_ms=None,
            prompt_tokens=0, completion_tokens=0, node_ip=None,
            error=str(exc)[:200],
        )


async def _streaming_request(
    client: httpx.AsyncClient,
    api_key: str,
    prompt: str,
    max_tokens: int = 64,
) -> RequestResult:
    headers = {"Authorization": f"Bearer {api_key}"}
    t0 = time.perf_counter()
    ttft_ms = None
    completion_tokens = 0
    node_ip = None

    try:
        async with client.stream(
            "POST",
            f"{GATEWAY_URL}/v1/chat/completions",
            json=_chat_payload(prompt, max_tokens, stream=True),
            headers=headers,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                latency_ms = (time.perf_counter() - t0) * 1000
                return RequestResult(
                    ok=False, latency_ms=latency_ms, ttft_ms=None,
                    prompt_tokens=0, completion_tokens=0, node_ip=None,
                    error=f"HTTP {resp.status_code}: {body.decode()[:200]}",
                )
            node_ip = _extract_node_ip(resp.headers, {})

            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content and ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000
                if content:
                    completion_tokens += 1  # approx token ≈ word chunk

                # Some servers send usage in final chunk
                usage = chunk.get("usage") or {}
                if usage.get("completion_tokens"):
                    completion_tokens = usage["completion_tokens"]
                if not node_ip:
                    node_ip = _extract_node_ip(resp.headers, chunk)

        latency_ms = (time.perf_counter() - t0) * 1000
        return RequestResult(
            ok=True, latency_ms=latency_ms, ttft_ms=ttft_ms,
            prompt_tokens=0, completion_tokens=completion_tokens,
            node_ip=node_ip, error=None,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return RequestResult(
            ok=False, latency_ms=latency_ms, ttft_ms=None,
            prompt_tokens=0, completion_tokens=0, node_ip=None,
            error=str(exc)[:200],
        )


# ---------------------------------------------------------------------------
# Distribution verification via Ray Serve health endpoint
# ---------------------------------------------------------------------------


async def check_ray_serve_distribution(n: int = 12) -> dict[str, int]:
    """
    Hit Ray Serve /health directly n times.
    Ray Serve round-robins replicas — each replica returns its own node_ip.
    """
    dist: dict[str, int] = defaultdict(int)
    async with _make_client(timeout=10.0) as client:
        tasks = [
            client.get(f"{RAY_SERVE_URL}/health")
            for _ in range(n)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            dist["error"] += 1
            continue
        try:
            body = r.json()
            ip = body.get("node_ip") or body.get("node") or "unknown"
            dist[ip] += 1
        except Exception:
            dist["parse_error"] += 1

    return dict(dist)


# ---------------------------------------------------------------------------
# Benchmark scenarios
# ---------------------------------------------------------------------------


async def run_sequential(api_key: str, n: int = 5) -> BenchmarkStats:
    """n sequential requests — baseline latency with no concurrency."""
    stats = BenchmarkStats("Sequential baseline (short prompt, no concurrency)")
    print(f"  Running {n} sequential requests...", flush=True)
    async with _make_client() as client:
        for i in range(n):
            r = await _single_request(client, api_key, SHORT_PROMPT, max_tokens=16)
            stats.results.append(r)
            status = "ok" if r.ok else "FAIL"
            print(f"    [{i+1}/{n}] {status}  {r.latency_ms:.0f}ms  node={r.node_ip or '?'}")
    return stats


async def run_concurrent(api_key: str, concurrency: int, n: int) -> BenchmarkStats:
    """n requests fired in batches of concurrency."""
    stats = BenchmarkStats(
        f"Concurrent load  (concurrency={concurrency}, n={n}, medium prompt)"
    )
    print(f"  Running {n} requests at concurrency={concurrency}...", flush=True)

    async with _make_client() as client:
        batches = [
            [_single_request(client, api_key, MEDIUM_PROMPT, max_tokens=96)
             for _ in range(min(concurrency, n - i))]
            for i in range(0, n, concurrency)
        ]
        batch_num = 0
        for batch in batches:
            batch_results = await asyncio.gather(*batch)
            for r in batch_results:
                stats.results.append(r)
                status = "ok" if r.ok else "FAIL"
                print(f"    [{len(stats.results):>2}/{n}] {status}  "
                      f"{r.latency_ms:.0f}ms  node={r.node_ip or '?'}")
            batch_num += 1

    return stats


async def run_streaming(api_key: str, n: int = 4) -> BenchmarkStats:
    """Streaming requests — measures TTFT."""
    stats = BenchmarkStats(f"Streaming TTFT  (n={n}, medium prompt)")
    print(f"  Running {n} streaming requests...", flush=True)
    async with _make_client() as client:
        tasks = [_streaming_request(client, api_key, MEDIUM_PROMPT, max_tokens=96)
                 for _ in range(n)]
        results = await asyncio.gather(*tasks)
    for i, r in enumerate(results):
        stats.results.append(r)
        status = "ok" if r.ok else "FAIL"
        ttft_str = f"  TTFT={r.ttft_ms:.0f}ms" if r.ttft_ms else ""
        print(f"    [{i+1}/{n}] {status}  {r.latency_ms:.0f}ms{ttft_str}  node={r.node_ip or '?'}")
    return stats


async def run_throughput_ramp(api_key: str) -> list[BenchmarkStats]:
    """Ramp concurrency 1→2→4 to find saturation point."""
    all_stats = []
    for c in [1, 2, 4]:
        s = await run_concurrent(api_key, concurrency=c, n=c * 3)
        all_stats.append(s)
    return all_stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> None:
    api_key = args.api_key
    mode = args.mode

    print(f"\n{'━' * 60}")
    print("  ONGC LLM Cluster Benchmark")
    print(f"  Gateway : {GATEWAY_URL}")
    print(f"  Ray     : {RAY_SERVE_URL}")
    print(f"  Mode    : {mode}")
    print(f"{'━' * 60}\n")

    # Always start with health check
    print("[ 1/4 ] Gateway health check...")
    async with _make_client(timeout=10.0) as client:
        try:
            r = await client.get(f"{GATEWAY_URL}/health")
            print(f"  Gateway /health → {r.status_code}  {r.json()}")
        except Exception as e:
            print(f"  Gateway /health FAILED: {e}")
            sys.exit(1)

    # Ray Serve distribution check
    print("\n[ 2/4 ] Ray Serve replica distribution check...")
    print("  Sending 12 requests to Ray Serve /health directly...")
    dist = await check_ray_serve_distribution(n=12)
    total = sum(dist.values())
    print(f"  Results ({total} responses):")
    if total == 0:
        print("  WARNING: Ray Serve /health returned no responses — check port 8001")
    else:
        for ip, cnt in sorted(dist.items(), key=lambda x: -x[1]):
            bar = "█" * int(20 * cnt / total)
            print(f"    {ip:<20} {cnt:>3}  {bar}")
        unique_nodes = len([k for k in dist if k not in ("error", "parse_error", "unknown")])
        if unique_nodes <= 1:
            print(f"\n  ⚠  All responses from {unique_nodes} node — replicas may be collocated")
            print("     Check: ray status  (workers must show GPU resources)")
        else:
            print(f"\n  ✓  {unique_nodes} distinct nodes responding — distribution confirmed")

    if mode == "distribution":
        return

    # Sequential baseline
    print("\n[ 3/4 ] Sequential baseline...")
    seq_stats = await run_sequential(api_key, n=args.requests // 2 or 3)
    seq_stats.print()

    if mode == "streaming" or mode == "all":
        print("\n[ 4/4 ] Streaming TTFT test...")
        stream_stats = await run_streaming(api_key, n=min(4, args.requests))
        stream_stats.print()

    if mode == "throughput" or mode == "all":
        print("\n[ 4/4 ] Throughput ramp (concurrency 1 → 2 → 4)...")
        ramp_stats = await run_throughput_ramp(api_key)
        for s in ramp_stats:
            s.print()

    if mode == "concurrent":
        print(f"\n[ 4/4 ] Concurrent test (concurrency={args.concurrency})...")
        c_stats = await run_concurrent(
            api_key, concurrency=args.concurrency, n=args.requests
        )
        c_stats.print()

    # Summary table
    print(f"{'━' * 60}")
    print("  Done.")
    print(f"{'━' * 60}\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ONGC LLM cluster benchmark")
    p.add_argument("--api-key", default=os.getenv("LLM_API_KEY"), required=False)
    p.add_argument(
        "--mode",
        choices=["all", "distribution", "sequential", "streaming", "throughput", "concurrent"],
        default="all",
        help="Benchmark mode (default: all)",
    )
    p.add_argument("--requests", type=int, default=8, help="Total requests per scenario")
    p.add_argument("--concurrency", type=int, default=4, help="Concurrent workers (mode=concurrent)")
    p.add_argument("--gateway-url", default=GATEWAY_URL)
    p.add_argument("--ray-url", default=RAY_SERVE_URL)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.api_key:
        sys.exit("Provide --api-key or set LLM_API_KEY env var")

    # Apply CLI URL overrides globally
    GATEWAY_URL = args.gateway_url
    RAY_SERVE_URL = args.ray_url

    asyncio.run(main(args))
