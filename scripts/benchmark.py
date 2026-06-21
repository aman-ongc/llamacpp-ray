#!/usr/bin/env python3
"""
ONGC LLM Platform — comprehensive benchmark suite.

Scenarios
---------
  text         Sequential short + long text (baseline latency)
  multi-turn   Multi-turn conversation with session affinity
  thinking     Thinking ON vs OFF comparison (same prompt)
  affinity     Session affinity ON vs OFF (node distribution)
  sampling     Temperature / top_p / top_k / min_p presets
  streaming    Streaming TTFT — thinking ON and OFF
  concurrent   Concurrent load at a fixed concurrency level
  queue        Queue pressure ramp (fires past --parallel 2 per node)
  throughput   Concurrency ramp 1→2→4→8 to find saturation
  routing      Same-node vs round-robin: sequential AND concurrent comparison
  image        Vision: single image, image+text, two images
  distribution Node spread check (fires concurrent requests through gateway)
  all          Run every scenario above (default)

Usage
-----
  uv run scripts/benchmark.py --api-key sk-ongc-...
  uv run scripts/benchmark.py --api-key sk-ongc-... --mode thinking
  uv run scripts/benchmark.py --api-key sk-ongc-... --mode routing
  uv run scripts/benchmark.py --api-key sk-ongc-... --mode image --image /path/to/img.jpg
  uv run scripts/benchmark.py --api-key sk-ongc-... --mode concurrent --concurrency 8 --requests 16
  uv run scripts/benchmark.py --api-key sk-ongc-... --mode queue
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import struct
import sys
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import httpx
except ImportError:
    sys.exit("Install httpx: uv add httpx")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GATEWAY_URL = "http://10.208.211.62:18000"
MODEL = "ongc-llm"
NOPROXY = {"http://": None, "https://": None}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PROMPT_SHORT = "Reply with exactly: 'pong'"

PROMPT_MEDIUM = "Explain what a GPU is in exactly 3 sentences."

PROMPT_LONG = (
    "Describe the history of oil exploration in India from 1860 to 2000, "
    "covering major milestones, key organizations, and technological advances. "
    "Be thorough and write at least 400 words."
)

PROMPT_MATH = (
    "A train travels 120 km in 1.5 hours. What is its speed in m/s? "
    "Show your full working step by step."
)

PROMPT_CREATIVE = (
    "Write the opening paragraph of a novel set on an offshore oil platform "
    "during a violent storm at night."
)

PROMPT_TECHNICAL = (
    "What is the difference between a gas-oil separator and a scrubber in "
    "petroleum processing? Explain the operating principles of each."
)

PROMPT_IMAGE = "Describe what you see in this image in detail."
PROMPT_IMAGE_MULTI = "Compare these two images and describe the key differences between them."

# Pre-scripted multi-turn conversation (fixed assistant turns so we control context length)
MULTI_TURN_SCRIPT = [
    ("user",      "What is machine learning?"),
    ("assistant", "Machine learning is a branch of AI where systems learn patterns from data without being explicitly programmed."),
    ("user",      "What are the three main types of machine learning?"),
    ("assistant", "The three main types are supervised learning, unsupervised learning, and reinforcement learning."),
    ("user",      "Give a concrete example of supervised learning used in the oil and gas industry."),
]

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RequestResult:
    ok: bool
    latency_ms: float
    ttft_ms: Optional[float]
    prompt_tokens: int
    completion_tokens: int
    node_ip: Optional[str]
    queue_ms: Optional[float]
    error: Optional[str]


@dataclass
class BenchmarkStats:
    name: str
    concurrency: int = 1
    wall_time_ms: float = 0.0
    results: list[RequestResult] = field(default_factory=list)

    @property
    def successes(self) -> list[RequestResult]:
        return [r for r in self.results if r.ok]

    @property
    def failures(self) -> list[RequestResult]:
        return [r for r in self.results if not r.ok]

    def latencies(self) -> list[float]:
        return [r.latency_ms for r in self.successes]

    def ttfts(self) -> list[float]:
        return [r.ttft_ms for r in self.successes if r.ttft_ms is not None]

    def tokens_per_sec(self) -> list[float]:
        return [
            r.completion_tokens / (r.latency_ms / 1000)
            for r in self.successes
            if r.latency_ms > 0 and r.completion_tokens > 0
        ]

    def total_completion_tokens(self) -> int:
        return sum(r.completion_tokens for r in self.successes)

    def total_tokens(self) -> int:
        return sum(r.prompt_tokens + r.completion_tokens for r in self.successes)

    def wall_rps(self) -> float:
        if self.wall_time_ms > 0:
            return len(self.successes) / (self.wall_time_ms / 1000)
        return 0.0

    def wall_tok_per_sec(self) -> float:
        if self.wall_time_ms > 0:
            return self.total_completion_tokens() / (self.wall_time_ms / 1000)
        return 0.0

    def node_distribution(self) -> dict[str, int]:
        dist: dict[str, int] = defaultdict(int)
        for r in self.successes:
            dist[r.node_ip or "unknown"] += 1
        return dict(dist)

    def summary(self) -> dict[str, Any]:
        lat = self.latencies()
        tps = self.tokens_per_sec()
        return {
            "n": len(self.results),
            "ok": len(self.successes),
            "median_latency_ms": statistics.median(lat) if lat else None,
            "p95_latency_ms": _pct(lat, 95) if len(lat) >= 2 else (lat[0] if lat else None),
            "avg_tok_per_sec": statistics.mean(tps) if tps else None,
            "wall_rps": self.wall_rps(),
            "wall_tok_per_sec": self.wall_tok_per_sec(),
        }

    def print(self) -> None:
        n = len(self.results)
        ok = len(self.successes)
        fail = len(self.failures)
        lat = self.latencies()
        tps = self.tokens_per_sec()
        ttft = self.ttfts()
        dist = self.node_distribution()

        print(f"\n{'═' * 64}")
        print(f"  {self.name}")
        print(f"{'═' * 64}")
        print(f"  Requests   : {ok}/{n} succeeded   {fail} failed   concurrency={self.concurrency}")
        if self.wall_time_ms:
            print(
                f"  Wall time  : {self.wall_time_ms:.0f} ms   "
                f"({self.wall_rps():.2f} req/s   "
                f"{self.wall_tok_per_sec():.1f} completion tok/s overall)"
            )

        if lat:
            print(f"\n  Latency per request (end-to-end)")
            print(f"    min      : {min(lat):.0f} ms")
            print(f"    median   : {statistics.median(lat):.0f} ms")
            print(f"    p95      : {_pct(lat, 95):.0f} ms")
            print(f"    p99      : {_pct(lat, 99):.0f} ms")
            print(f"    max      : {max(lat):.0f} ms")

        if ttft:
            print(f"\n  Time-to-first-token (streaming)")
            print(f"    min      : {min(ttft):.0f} ms")
            print(f"    median   : {statistics.median(ttft):.0f} ms")
            print(f"    p95      : {_pct(ttft, 95):.0f} ms")
            print(f"    max      : {max(ttft):.0f} ms")

        if tps:
            print(f"\n  Per-request throughput (tok/s)")
            print(f"    avg      : {statistics.mean(tps):.1f}")
            print(f"    min      : {min(tps):.1f}")
            print(f"    max      : {max(tps):.1f}")

        ctok = self.total_completion_tokens()
        if ctok:
            print(f"\n  Token totals")
            print(f"    completion : {ctok}")
            print(f"    all        : {self.total_tokens()}")

        if dist:
            print(f"\n  Node distribution")
            total = sum(dist.values())
            for ip, cnt in sorted(dist.items(), key=lambda x: -x[1]):
                bar = "█" * int(20 * cnt / total)
                pct = 100 * cnt / total
                print(f"    {ip:<20} {cnt:>3}  {pct:4.0f}%  {bar}")

        if self.failures:
            print(f"\n  Errors")
            seen: set[str | None] = set()
            for r in self.failures:
                if r.error not in seen:
                    print(f"    {r.error}")
                    seen.add(r.error)
        print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pct(data: list[float], p: int) -> float:
    sd = sorted(data)
    idx = min(int(len(sd) * p / 100), len(sd) - 1)
    return sd[idx]


def _print_result(idx: int, total: int, r: RequestResult, extra: str = "") -> None:
    status = "ok  " if r.ok else "FAIL"
    tps = (
        f"  {r.completion_tokens / (r.latency_ms / 1000):5.1f} tok/s"
        if r.ok and r.completion_tokens and r.latency_ms > 0
        else ""
    )
    node = r.node_ip or "?"
    print(
        f"    [{idx:>2}/{total}] {status}  {r.latency_ms:>7.0f} ms"
        f"  {r.completion_tokens:>4} tok{tps}  node={node}{extra}"
    )


def _print_comparison(
    label_a: str, stats_a: BenchmarkStats,
    label_b: str, stats_b: BenchmarkStats,
) -> None:
    print(f"\n  {'─' * 64}")
    print(f"  Comparison")
    col_w = 14
    print(f"  {'Metric':<32} {label_a:>{col_w}}   {label_b:>{col_w}}")
    print(f"  {'─' * 32} {'─' * col_w}   {'─' * col_w}")

    sa, sb = stats_a.summary(), stats_b.summary()

    def row(label: str, key: str, unit: str = "") -> None:
        va = sa.get(key)
        vb = sb.get(key)
        fa = f"{va:.1f}{unit}" if va is not None else "n/a"
        fb = f"{vb:.1f}{unit}" if vb is not None else "n/a"
        print(f"  {label:<32} {fa:>{col_w}}   {fb:>{col_w}}")

    row("Median latency", "median_latency_ms", " ms")
    row("P95 latency", "p95_latency_ms", " ms")
    row("Avg tok/s (per req)", "avg_tok_per_sec")
    row("Wall req/s", "wall_rps")
    row("Wall tok/s overall", "wall_tok_per_sec")
    print()


def _print_throughput_table(stats_list: list[BenchmarkStats]) -> None:
    print(f"\n  {'─' * 64}")
    print(f"  {'Scenario':<38} {'req/s':>6}  {'tok/s':>7}  {'p50 ms':>7}  {'p95 ms':>7}")
    print(f"  {'─' * 64}")
    for s in stats_list:
        lat = s.latencies()
        p50 = f"{statistics.median(lat):.0f}" if lat else "n/a"
        p95 = f"{_pct(lat, 95):.0f}" if len(lat) >= 2 else p50
        rps = f"{s.wall_rps():.2f}" if s.wall_time_ms else "n/a"
        tps = f"{s.wall_tok_per_sec():.1f}" if s.wall_time_ms else "n/a"
        print(f"  {s.name[:38]:<38} {rps:>6}  {tps:>7}  {p50:>7}  {p95:>7}")
    print()


# ---------------------------------------------------------------------------
# Image utilities
# ---------------------------------------------------------------------------


def _make_test_png(width: int = 64, height: int = 64) -> bytes:
    """Synthetic RGB gradient PNG — no external dependencies."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw = b"".join(
        b"\x00" + b"".join(
            bytes([int(255 * x / width), int(255 * y / height), 120])
            for x in range(width)
        )
        for y in range(height)
    )
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def _load_or_generate_image(path: Optional[str], label: str = "image") -> str:
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        print(f"  Using {label}: {path} ({len(data):,} bytes)")
    else:
        if path:
            print(f"  Path not found: {path}. Generating synthetic 64x64 PNG.")
        else:
            print(f"  No path supplied. Generating synthetic 64x64 gradient PNG.")
        data = _make_test_png()
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _make_client(timeout: float = 300.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        mounts=NOPROXY,
        timeout=httpx.Timeout(timeout, connect=5.0),
    )


def _chat_payload(
    messages: list[dict],
    max_tokens: int = 128,
    stream: bool = False,
    temperature: float = 0.0,
    **extra: Any,
) -> dict:
    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
        "temperature": temperature,
    }
    payload.update(extra)
    return payload


def _user_msg(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def _image_payload(
    image_b64: str,
    question: str,
    max_tokens: int = 256,
    system: Optional[str] = None,
    second_b64: Optional[str] = None,
) -> dict:
    """Build a multimodal chat payload (OpenAI vision format)."""
    content: list[dict] = []
    if second_b64:
        content.append({"type": "text", "text": question})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{second_b64}"}})
    else:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}})
        content.append({"type": "text", "text": question})

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})

    return {"model": MODEL, "messages": messages, "max_tokens": max_tokens}


def _extract_node_ip(headers: httpx.Headers, body: dict) -> Optional[str]:
    ip = headers.get("x-node-ip") or headers.get("x-served-by")
    if ip:
        return ip
    return body.get("node_ip") or body.get("worker_node")


async def _single_request(
    client: httpx.AsyncClient,
    api_key: str,
    payload: dict,
) -> RequestResult:
    headers = {"Authorization": f"Bearer {api_key}"}
    t0 = time.perf_counter()
    try:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            return RequestResult(
                ok=False, latency_ms=latency_ms, ttft_ms=None,
                prompt_tokens=0, completion_tokens=0, node_ip=None,
                queue_ms=None, error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        body = resp.json()
        usage = body.get("usage") or {}
        qms = body.get("queue_ms")
        return RequestResult(
            ok=True, latency_ms=latency_ms, ttft_ms=None,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            node_ip=_extract_node_ip(resp.headers, body),
            queue_ms=float(qms) if qms is not None else None,
            error=None,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return RequestResult(
            ok=False, latency_ms=latency_ms, ttft_ms=None,
            prompt_tokens=0, completion_tokens=0, node_ip=None,
            queue_ms=None, error=str(exc)[:200],
        )


async def _streaming_request(
    client: httpx.AsyncClient,
    api_key: str,
    payload: dict,
) -> RequestResult:
    headers = {"Authorization": f"Bearer {api_key}"}
    t0 = time.perf_counter()
    ttft_ms: Optional[float] = None
    completion_tokens = 0
    node_ip: Optional[str] = None

    try:
        async with client.stream(
            "POST",
            f"{GATEWAY_URL}/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                latency_ms = (time.perf_counter() - t0) * 1000
                return RequestResult(
                    ok=False, latency_ms=latency_ms, ttft_ms=None,
                    prompt_tokens=0, completion_tokens=0, node_ip=None,
                    queue_ms=None, error=f"HTTP {resp.status_code}: {body.decode()[:200]}",
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
                    completion_tokens += 1  # approx: one chunk ≈ one token

                usage = chunk.get("usage") or {}
                if usage.get("completion_tokens"):
                    completion_tokens = usage["completion_tokens"]
                if not node_ip:
                    node_ip = _extract_node_ip(resp.headers, chunk)

        latency_ms = (time.perf_counter() - t0) * 1000
        return RequestResult(
            ok=True, latency_ms=latency_ms, ttft_ms=ttft_ms,
            prompt_tokens=0, completion_tokens=completion_tokens,
            node_ip=node_ip, queue_ms=None, error=None,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return RequestResult(
            ok=False, latency_ms=latency_ms, ttft_ms=None,
            prompt_tokens=0, completion_tokens=0, node_ip=None,
            queue_ms=None, error=str(exc)[:200],
        )


# ---------------------------------------------------------------------------
# Node distribution check — fires concurrent requests through the gateway
# itself and inspects which node each landed on (gateway/router.py stamps
# node_ip on every response). Replaces the old Ray Serve /health probe.
# ---------------------------------------------------------------------------


async def check_node_distribution(api_key: str, n: int = 6) -> dict[str, int]:
    dist: dict[str, int] = defaultdict(int)
    async with _make_client(timeout=30.0) as client:
        results = await asyncio.gather(
            *[
                _single_request(client, api_key, _chat_payload(_user_msg(PROMPT_SHORT), max_tokens=8))
                for _ in range(n)
            ],
            return_exceptions=True,
        )
    for r in results:
        if isinstance(r, Exception) or not r.ok:
            dist["error"] += 1
            continue
        dist[r.node_ip or "unknown"] += 1
    return dict(dist)


def _print_distribution(dist: dict[str, int]) -> None:
    total = sum(dist.values())
    if total == 0:
        print("  WARNING: No successful responses — check gateway health")
        return
    print(f"  {total} responses:")
    for ip, cnt in sorted(dist.items(), key=lambda x: -x[1]):
        bar = "█" * int(20 * cnt / total)
        print(f"    {ip:<20} {cnt:>3}  {100 * cnt / total:4.0f}%  {bar}")
    unique = len([k for k in dist if k not in ("error", "parse_error", "unknown")])
    if unique <= 1:
        print(f"\n  WARNING: All responses from {unique} node — check gateway routing/health state")
    else:
        print(f"\n  OK: {unique} distinct nodes responding — distribution confirmed")


# ---------------------------------------------------------------------------
# Scenario: short text — sequential baseline
# ---------------------------------------------------------------------------


async def run_short_text(api_key: str, n: int = 3) -> BenchmarkStats:
    stats = BenchmarkStats("Short text — sequential baseline", concurrency=1)
    print(f"  {n} sequential requests, short prompt (max_tokens=16)...", flush=True)
    async with _make_client() as client:
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(client, api_key, _chat_payload(_user_msg(PROMPT_SHORT), max_tokens=16))
            stats.results.append(r)
            _print_result(i + 1, n, r)
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000
    return stats


# ---------------------------------------------------------------------------
# Scenario: long text — sequential
# ---------------------------------------------------------------------------


async def run_long_text(api_key: str, n: int = 2) -> BenchmarkStats:
    stats = BenchmarkStats("Long text — sequential (max_tokens=512)", concurrency=1)
    print(f"  {n} sequential requests, long prompt (max_tokens=512)...", flush=True)
    async with _make_client() as client:
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _chat_payload(_user_msg(PROMPT_LONG), max_tokens=512, temperature=0.3),
            )
            stats.results.append(r)
            _print_result(i + 1, n, r)
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000
    return stats


# ---------------------------------------------------------------------------
# Scenario: multi-turn conversation with session affinity
# ---------------------------------------------------------------------------


async def run_multi_turn(api_key: str) -> BenchmarkStats:
    """
    Three user turns with scripted intermediate assistant messages.
    session_affinity=True keeps all turns on the same GPU worker.
    Measures per-turn latency as context grows.
    """
    stats = BenchmarkStats("Multi-turn conversation (3 user turns, session_affinity=True)", concurrency=1)
    print("  3-turn conversation, session_affinity=True...", flush=True)

    history: list[dict] = []
    turn_num = 0
    async with _make_client() as client:
        t0 = time.perf_counter()
        for role, content in MULTI_TURN_SCRIPT:
            history.append({"role": role, "content": content})
            if role != "user":
                continue
            turn_num += 1
            r = await _single_request(
                client, api_key,
                _chat_payload(history, max_tokens=256, session_affinity=True),
            )
            stats.results.append(r)
            ctx_msgs = len(history)
            print(
                f"    turn {turn_num}  ({ctx_msgs} msgs in context)  "
                f"{r.latency_ms:.0f} ms  {r.completion_tokens} tok  node={r.node_ip or '?'}"
            )
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000
    return stats


# ---------------------------------------------------------------------------
# Scenario: thinking mode ON vs OFF
# ---------------------------------------------------------------------------


async def run_thinking_comparison(api_key: str, n: int = 2) -> tuple[BenchmarkStats, BenchmarkStats]:
    on = BenchmarkStats("Thinking ON  (enable_thinking=True,  max_tokens=1024)", concurrency=1)
    off = BenchmarkStats("Thinking OFF (enable_thinking=False, max_tokens=256)", concurrency=1)

    async with _make_client() as client:
        print(f"  {n} requests, enable_thinking=True, prompt: math problem...", flush=True)
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _chat_payload(_user_msg(PROMPT_MATH), max_tokens=1024, enable_thinking=True),
            )
            on.results.append(r)
            _print_result(i + 1, n, r)
        on.wall_time_ms = (time.perf_counter() - t0) * 1000

        print(f"  {n} requests, enable_thinking=False, same prompt...", flush=True)
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _chat_payload(_user_msg(PROMPT_MATH), max_tokens=256, enable_thinking=False),
            )
            off.results.append(r)
            _print_result(i + 1, n, r)
        off.wall_time_ms = (time.perf_counter() - t0) * 1000

    return on, off


# ---------------------------------------------------------------------------
# Scenario: session affinity ON vs OFF
# ---------------------------------------------------------------------------


async def run_affinity_comparison(api_key: str, n: int = 4) -> tuple[BenchmarkStats, BenchmarkStats]:
    with_a = BenchmarkStats("Session affinity ON  — sticky routing, same node", concurrency=1)
    no_a = BenchmarkStats("Session affinity OFF — spread across workers", concurrency=1)

    async with _make_client() as client:
        print(f"  {n} requests, session_affinity=True...", flush=True)
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _chat_payload(_user_msg(PROMPT_MEDIUM), max_tokens=96, session_affinity=True),
            )
            with_a.results.append(r)
            _print_result(i + 1, n, r)
        with_a.wall_time_ms = (time.perf_counter() - t0) * 1000

        print(f"  {n} requests, session_affinity=False...", flush=True)
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _chat_payload(_user_msg(PROMPT_MEDIUM), max_tokens=96, session_affinity=False),
            )
            no_a.results.append(r)
            _print_result(i + 1, n, r)
        no_a.wall_time_ms = (time.perf_counter() - t0) * 1000

    return with_a, no_a


# ---------------------------------------------------------------------------
# Scenario: sampling parameter presets
# ---------------------------------------------------------------------------

# Note: top_p, top_k, min_p, repeat_penalty are forwarded to llama.cpp only
# if the gateway's ChatCompletionRequest schema includes them. Currently only
# temperature is in the schema, so other params are silently ignored until
# the gateway is updated. Temperature comparisons are always meaningful.

SAMPLING_PRESETS = [
    {
        "label": "deterministic   temp=0.0, top_p=1.0",
        "temperature": 0.0, "top_p": 1.0,
    },
    {
        "label": "balanced        temp=0.7, top_p=0.9, top_k=40",
        "temperature": 0.7, "top_p": 0.9, "top_k": 40,
    },
    {
        "label": "creative        temp=0.9, top_p=0.95, top_k=50",
        "temperature": 0.9, "top_p": 0.95, "top_k": 50,
    },
    {
        "label": "min_p filter    temp=0.8, top_p=1.0, min_p=0.05",
        "temperature": 0.8, "top_p": 1.0, "min_p": 0.05,
    },
    {
        "label": "low repetition  temp=0.7, repeat_penalty=1.2",
        "temperature": 0.7, "repeat_penalty": 1.2,
    },
]


async def run_sampling_presets(api_key: str, n: int = 1) -> list[BenchmarkStats]:
    all_stats: list[BenchmarkStats] = []
    async with _make_client() as client:
        for preset in SAMPLING_PRESETS:
            label = preset["label"]
            params = {k: v for k, v in preset.items() if k != "label"}
            stats = BenchmarkStats(f"Sampling — {label}", concurrency=1)
            print(f"  {n} reqs  {label}...", flush=True)
            t0 = time.perf_counter()
            for i in range(n):
                r = await _single_request(
                    client, api_key,
                    _chat_payload(
                        _user_msg(PROMPT_CREATIVE),
                        max_tokens=128,
                        enable_thinking=False,
                        **params,
                    ),
                )
                stats.results.append(r)
                _print_result(i + 1, n, r)
            stats.wall_time_ms = (time.perf_counter() - t0) * 1000
            all_stats.append(stats)
    return all_stats


# ---------------------------------------------------------------------------
# Scenario: streaming TTFT
# ---------------------------------------------------------------------------


async def run_streaming_ttft(api_key: str, n: int = 2, thinking: bool = False) -> BenchmarkStats:
    label = f"Streaming TTFT — thinking={'ON' if thinking else 'OFF'}  ({n} concurrent)"
    stats = BenchmarkStats(label, concurrency=n)
    prompt = PROMPT_MATH if thinking else PROMPT_MEDIUM
    max_tokens = 1024 if thinking else 128

    print(f"  {n} concurrent streaming requests, thinking={thinking}...", flush=True)
    async with _make_client() as client:
        t0 = time.perf_counter()
        tasks = [
            _streaming_request(
                client, api_key,
                _chat_payload(_user_msg(prompt), max_tokens=max_tokens, stream=True, enable_thinking=thinking),
            )
            for _ in range(n)
        ]
        results = await asyncio.gather(*tasks)
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000

    for i, r in enumerate(results):
        stats.results.append(r)
        ttft_str = f"  TTFT={r.ttft_ms:.0f} ms" if r.ttft_ms is not None else ""
        _print_result(i + 1, n, r, extra=ttft_str)
    return stats


# ---------------------------------------------------------------------------
# Scenario: concurrent batch (shared by multiple modes)
# ---------------------------------------------------------------------------


async def _run_concurrent_batch(
    api_key: str,
    concurrency: int,
    n: int,
    payload_fn: Any,  # callable() -> dict
    label: str,
) -> BenchmarkStats:
    """Fire n requests in batches of `concurrency`."""
    stats = BenchmarkStats(label, concurrency=concurrency)
    print(f"  {n} requests, concurrency={concurrency}...", flush=True)

    async with _make_client() as client:
        t0 = time.perf_counter()
        for batch_start in range(0, n, concurrency):
            batch_size = min(concurrency, n - batch_start)
            batch = await asyncio.gather(
                *[_single_request(client, api_key, payload_fn()) for _ in range(batch_size)]
            )
            for r in batch:
                stats.results.append(r)
                _print_result(len(stats.results), n, r)
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000
    return stats


async def run_concurrent(api_key: str, concurrency: int, n: int) -> BenchmarkStats:
    return await _run_concurrent_batch(
        api_key, concurrency, n,
        payload_fn=lambda: _chat_payload(_user_msg(PROMPT_MEDIUM), max_tokens=96),
        label=f"Concurrent load — concurrency={concurrency}  n={n}  medium prompt",
    )


# ---------------------------------------------------------------------------
# Scenario: queue pressure (fires past --parallel 2 per node)
# ---------------------------------------------------------------------------


async def run_queue_pressure(api_key: str) -> list[BenchmarkStats]:
    """
    Each node has --parallel 2.  With 4 nodes: 8 total slots.
    We ramp from under-capacity to over-capacity to show queue depth effects.

      concurrency  2  →  under one node's capacity
      concurrency  4  →  at cluster sweet spot
      concurrency  8  →  exactly fills all slots
      concurrency 12  →  over-subscribed, queue depth visible in latency
    """
    all_stats: list[BenchmarkStats] = []
    for c in [2, 4, 8, 12]:
        n = c
        s = await _run_concurrent_batch(
            api_key, concurrency=c, n=n,
            payload_fn=lambda: _chat_payload(_user_msg(PROMPT_MEDIUM), max_tokens=96),
            label=f"Queue pressure — concurrency={c:>2}  n={n:>2}",
        )
        all_stats.append(s)
    return all_stats


# ---------------------------------------------------------------------------
# Scenario: throughput ramp
# ---------------------------------------------------------------------------


async def run_throughput_ramp(api_key: str) -> list[BenchmarkStats]:
    all_stats: list[BenchmarkStats] = []
    for c in [1, 2, 4, 8]:
        n = max(c * 2, 4)
        s = await _run_concurrent_batch(
            api_key, concurrency=c, n=n,
            payload_fn=lambda: _chat_payload(_user_msg(PROMPT_MEDIUM), max_tokens=96),
            label=f"Throughput ramp — concurrency={c}  n={n}",
        )
        all_stats.append(s)
    return all_stats


# ---------------------------------------------------------------------------
# Scenario: routing comparison — same node vs round-robin
# ---------------------------------------------------------------------------
#
# The cluster has 4 nodes, each with --parallel 2 (2 KV-cache slots).
# Total cluster capacity: 8 concurrent generations.
#
# session_affinity=True  → gateway pins all requests from this API key to
#                           the same node → same llama.cpp process.
# session_affinity=False → gateway load-balances across all workers.
#
# Four sub-scenarios expose different facets of this behaviour:
#
#   A) Sequential, same node   — queue builds 1-deep on one worker; KV cache
#                                 may warm up across identical prompt prefixes.
#   B) Sequential, round-robin — requests spread; KV cache cold each time;
#                                 each worker sees only 1 request at a time.
#   C) Concurrent, same node   — all requests hit one worker; only 2 run in
#                                 parallel, the rest queue inside llama.cpp.
#                                 Latency climbs sharply with queue depth.
#   D) Concurrent, round-robin — requests spread across 4 workers, 2 each;
#                                 fills every slot simultaneously, no queuing.
#                                 Maximum cluster throughput.
#
# Expected outcome:
#   Sequential  → A ≈ B in latency (same work, different node)
#   Concurrent  → D much faster wall-time than C; C shows high p95/p99


async def run_routing_comparison(api_key: str, n: int = 4) -> list[BenchmarkStats]:
    """
    Runs all four routing sub-scenarios and returns them in order
    [seq_same, seq_rr, conc_same, conc_rr] for side-by-side printing.
    """
    prompt = PROMPT_TECHNICAL   # moderate length, representative of real queries
    max_tok = 128
    conc = min(n, 8)            # concurrent requests (cap at 8 = cluster capacity)

    # ── A: Sequential, same node ──────────────────────────────────────────────
    seq_same = BenchmarkStats(
        f"Routing A — sequential  same-node  (affinity=True,  n={n})", concurrency=1
    )
    print(f"\n  A) Sequential, same-node: {n} requests, session_affinity=True...", flush=True)
    async with _make_client() as client:
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _chat_payload(_user_msg(prompt), max_tokens=max_tok, session_affinity=True),
            )
            seq_same.results.append(r)
            _print_result(i + 1, n, r)
        seq_same.wall_time_ms = (time.perf_counter() - t0) * 1000

    # ── B: Sequential, round-robin ────────────────────────────────────────────
    seq_rr = BenchmarkStats(
        f"Routing B — sequential  round-robin (affinity=False, n={n})", concurrency=1
    )
    print(f"\n  B) Sequential, round-robin: {n} requests, session_affinity=False...", flush=True)
    async with _make_client() as client:
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _chat_payload(_user_msg(prompt), max_tokens=max_tok, session_affinity=False),
            )
            seq_rr.results.append(r)
            _print_result(i + 1, n, r)
        seq_rr.wall_time_ms = (time.perf_counter() - t0) * 1000

    # ── C: Concurrent, same node ──────────────────────────────────────────────
    # All `conc` requests arrive simultaneously and fight for 2 slots on one worker.
    # Requests beyond slot 2 queue inside llama.cpp — latency reflects queue wait.
    conc_same = BenchmarkStats(
        f"Routing C — concurrent  same-node  (affinity=True,  conc={conc}  n={conc})",
        concurrency=conc,
    )
    print(
        f"\n  C) Concurrent ({conc} at once), same-node — all hit one worker's 2 slots...",
        flush=True,
    )
    async with _make_client() as client:
        t0 = time.perf_counter()
        tasks = [
            _single_request(
                client, api_key,
                _chat_payload(_user_msg(prompt), max_tokens=max_tok, session_affinity=True),
            )
            for _ in range(conc)
        ]
        results = await asyncio.gather(*tasks)
        conc_same.wall_time_ms = (time.perf_counter() - t0) * 1000
    for i, r in enumerate(results):
        conc_same.results.append(r)
        _print_result(i + 1, conc, r)

    # ── D: Concurrent, round-robin ────────────────────────────────────────────
    # Same `conc` requests, but spread across 4 workers (2 per worker).
    # Every slot on every node is used — no queuing, maximum throughput.
    conc_rr = BenchmarkStats(
        f"Routing D — concurrent  round-robin (affinity=False, conc={conc}  n={conc})",
        concurrency=conc,
    )
    print(
        f"\n  D) Concurrent ({conc} at once), round-robin — spread across all workers...",
        flush=True,
    )
    async with _make_client() as client:
        t0 = time.perf_counter()
        tasks = [
            _single_request(
                client, api_key,
                _chat_payload(_user_msg(prompt), max_tokens=max_tok, session_affinity=False),
            )
            for _ in range(conc)
        ]
        results = await asyncio.gather(*tasks)
        conc_rr.wall_time_ms = (time.perf_counter() - t0) * 1000
    for i, r in enumerate(results):
        conc_rr.results.append(r)
        _print_result(i + 1, conc, r)

    return [seq_same, seq_rr, conc_same, conc_rr]


def _print_routing_summary(stats: list[BenchmarkStats]) -> None:
    """Side-by-side table for the four routing sub-scenarios."""
    seq_same, seq_rr, conc_same, conc_rr = stats

    print(f"\n  {'─' * 72}")
    print("  Routing comparison summary")
    print(f"  {'─' * 72}")
    hdr = f"  {'Scenario':<46} {'wall ms':>8}  {'req/s':>6}  {'p50 ms':>7}  {'p95 ms':>7}"
    print(hdr)
    print(f"  {'─' * 72}")

    def row(s: BenchmarkStats) -> None:
        lat = s.latencies()
        p50 = f"{statistics.median(lat):.0f}" if lat else "n/a"
        p95 = f"{_pct(lat, 95):.0f}" if len(lat) >= 2 else p50
        wall = f"{s.wall_time_ms:.0f}" if s.wall_time_ms else "n/a"
        rps = f"{s.wall_rps():.2f}" if s.wall_time_ms else "n/a"
        print(f"  {s.name[:46]:<46} {wall:>8}  {rps:>6}  {p50:>7}  {p95:>7}")

    row(seq_same)
    row(seq_rr)
    print(f"  {'─' * 72}")
    row(conc_same)
    row(conc_rr)
    print(f"  {'─' * 72}")

    # Node distribution comparison
    print("\n  Node distribution")
    print(f"  {'─' * 50}")
    for s in stats:
        dist = s.node_distribution()
        total = sum(dist.values())
        nodes = ", ".join(
            f"{ip}({cnt})" for ip, cnt in sorted(dist.items(), key=lambda x: -x[1])
        )
        unique = len(dist)
        print(f"  {s.name[:46]:<46}  {unique} node(s): {nodes}")
    print()


# ---------------------------------------------------------------------------
# Scenario: image / vision
# ---------------------------------------------------------------------------


async def run_image_single(api_key: str, image_path: Optional[str] = None) -> BenchmarkStats:
    """Single image + question — tests vision pathway latency."""
    stats = BenchmarkStats("Vision — single image input", concurrency=1)
    b64 = _load_or_generate_image(image_path, "image")
    n = 2
    print(f"  {n} requests, single image + question...", flush=True)
    async with _make_client() as client:
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _image_payload(b64, PROMPT_IMAGE, max_tokens=256),
            )
            stats.results.append(r)
            _print_result(i + 1, n, r)
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000
    return stats


async def run_image_plus_text(api_key: str, image_path: Optional[str] = None) -> BenchmarkStats:
    """Image + detailed question + system prompt."""
    stats = BenchmarkStats("Vision — image + text + system prompt", concurrency=1)
    b64 = _load_or_generate_image(image_path, "image")
    system = "You are a visual analysis assistant. Describe images precisely and concisely."
    question = "Describe the colors, shapes, and any patterns visible in this image."
    n = 2
    print(f"  {n} requests, image + text + system prompt...", flush=True)
    async with _make_client() as client:
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _image_payload(b64, question, max_tokens=256, system=system),
            )
            stats.results.append(r)
            _print_result(i + 1, n, r)
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000
    return stats


async def run_image_multi(
    api_key: str,
    image_path: Optional[str] = None,
    image_path2: Optional[str] = None,
) -> BenchmarkStats:
    """Two images in one request — tests multi-image vision pathway."""
    stats = BenchmarkStats("Vision — two images in one request", concurrency=1)
    b64_a = _load_or_generate_image(image_path, "image 1")
    b64_b = _load_or_generate_image(image_path2, "image 2")
    n = 1
    print(f"  {n} requests, two images per request...", flush=True)
    async with _make_client() as client:
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(
                client, api_key,
                _image_payload(b64_a, PROMPT_IMAGE_MULTI, max_tokens=384, second_b64=b64_b),
            )
            stats.results.append(r)
            _print_result(i + 1, n, r)
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000
    return stats


async def run_image_thinking(api_key: str, image_path: Optional[str] = None) -> BenchmarkStats:
    """Image + question with thinking mode ON — vision + reasoning combined."""
    stats = BenchmarkStats("Vision — image + thinking ON (enable_thinking=True)", concurrency=1)
    b64 = _load_or_generate_image(image_path, "image")
    question = "Look at this image carefully. What objects, colors, and patterns do you see? Think step by step."
    n = 1
    print(f"  {n} requests, image + enable_thinking=True...", flush=True)

    payload = _image_payload(b64, question, max_tokens=1024)
    payload["enable_thinking"] = True

    async with _make_client() as client:
        t0 = time.perf_counter()
        for i in range(n):
            r = await _single_request(client, api_key, payload)
            stats.results.append(r)
            _print_result(i + 1, n, r)
        stats.wall_time_ms = (time.perf_counter() - t0) * 1000
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> None:
    global GATEWAY_URL, MODEL
    GATEWAY_URL = args.gateway_url
    MODEL = args.model

    mode = args.mode
    n = args.requests
    run_all = mode == "all"

    print(f"\n{'━' * 64}")
    print("  ONGC LLM Platform — Benchmark Suite")
    print(f"  Gateway : {GATEWAY_URL}")
    print(f"  Model   : {MODEL}")
    print(f"  Mode    : {mode}")
    print(f"{'━' * 64}\n")

    # Health check — always runs
    print("[ health ] Gateway health check...")
    async with _make_client(timeout=10.0) as client:
        try:
            r = await client.get(f"{GATEWAY_URL}/health")
            print(f"  /health → {r.status_code}  {r.json()}")
        except Exception as e:
            print(f"  /health FAILED: {e}")
            sys.exit(1)

    # Node distribution
    if run_all or mode == "distribution":
        print("\n[ distribution ] Node spread across the pool...")
        dist = await check_node_distribution(args.api_key, n=6)
        _print_distribution(dist)
        if mode == "distribution":
            return

    # Text: short + long
    if run_all or mode == "text":
        print("\n[ text ] Short text — sequential baseline...")
        s = await run_short_text(args.api_key, n=max(n, 3))
        s.print()

        print("\n[ text ] Long text — sequential...")
        s = await run_long_text(args.api_key, n=2)
        s.print()

    # Multi-turn
    if run_all or mode == "multi-turn":
        print("\n[ multi-turn ] Multi-turn conversation with session affinity...")
        s = await run_multi_turn(args.api_key)
        s.print()

    # Thinking
    if run_all or mode == "thinking":
        print("\n[ thinking ] Thinking ON vs OFF on the same prompt...")
        on, off = await run_thinking_comparison(args.api_key, n=max(n // 2, 1))
        on.print()
        off.print()
        _print_comparison("thinking=ON", on, "thinking=OFF", off)

    # Session affinity
    if run_all or mode == "affinity":
        print("\n[ affinity ] Session affinity ON vs OFF...")
        with_a, no_a = await run_affinity_comparison(args.api_key, n=max(n, 4))
        with_a.print()
        no_a.print()
        _print_comparison("affinity=ON", with_a, "affinity=OFF", no_a)

    # Sampling
    if run_all or mode == "sampling":
        print("\n[ sampling ] Sampling parameter presets...")
        print("  Note: top_p/top_k/min_p/repeat_penalty require gateway schema update to take effect.")
        print("        temperature comparisons are always meaningful.\n")
        sampling_stats = await run_sampling_presets(args.api_key, n=1)
        for s in sampling_stats:
            s.print()
        _print_throughput_table(sampling_stats)

    # Streaming
    if run_all or mode == "streaming":
        print("\n[ streaming ] Streaming TTFT — thinking OFF...")
        s_off = await run_streaming_ttft(args.api_key, n=min(2, n), thinking=False)
        s_off.print()

        print("\n[ streaming ] Streaming TTFT — thinking ON...")
        s_on = await run_streaming_ttft(args.api_key, n=min(2, n), thinking=True)
        s_on.print()

        _print_comparison("stream thinking=OFF", s_off, "stream thinking=ON", s_on)

    # Concurrent
    if mode == "concurrent":
        print(f"\n[ concurrent ] Concurrent load test (concurrency={args.concurrency})...")
        s = await run_concurrent(args.api_key, concurrency=args.concurrency, n=n)
        s.print()

    # Routing comparison
    if run_all or mode == "routing":
        print("\n[ routing ] Same-node vs round-robin — sequential and concurrent...")
        print("  Cluster: 4 nodes x 2 parallel slots = 8 total concurrent slots.")
        print("  affinity=True  → all requests pin to one worker (2 slots, rest queue).")
        print("  affinity=False → load-balanced across all workers (8 slots available).\n")
        routing_stats = await run_routing_comparison(args.api_key, n=max(n, 4))
        for s in routing_stats:
            s.print()
        _print_routing_summary(routing_stats)

    # Queue pressure
    if run_all or mode == "queue":
        print("\n[ queue ] Queue pressure — ramping past --parallel 2 per node...")
        print("  Cluster has 4 nodes x 2 parallel = 8 total slots.")
        print("  concurrency >8 forces requests to queue inside llama.cpp.\n")
        queue_stats = await run_queue_pressure(args.api_key)
        for s in queue_stats:
            s.print()
        _print_throughput_table(queue_stats)

    # Throughput ramp
    if run_all or mode == "throughput":
        print("\n[ throughput ] Throughput ramp (concurrency 1 → 2 → 4 → 8)...")
        ramp = await run_throughput_ramp(args.api_key)
        for s in ramp:
            s.print()
        _print_throughput_table(ramp)

    # Image / vision
    if run_all or mode == "image":
        print("\n[ image ] Vision scenarios...")
        print("  Note: requires gateway ChatCompletionRequest to accept multimodal content arrays.")
        print("        A 422 error means vision is not yet enabled at the gateway schema level.\n")

        s = await run_image_single(args.api_key, args.image)
        s.print()

        s = await run_image_plus_text(args.api_key, args.image)
        s.print()

        s = await run_image_multi(args.api_key, args.image, args.image2)
        s.print()

        s = await run_image_thinking(args.api_key, args.image)
        s.print()

    print(f"\n{'━' * 64}")
    print("  Benchmark complete.")
    print(f"{'━' * 64}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ONGC LLM platform benchmark suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--api-key", default=os.getenv("LLM_API_KEY"), help="Bearer API key")
    p.add_argument(
        "--mode",
        choices=[
            "all", "distribution", "text", "multi-turn", "thinking",
            "affinity", "sampling", "streaming", "concurrent",
            "queue", "throughput", "routing", "image",
        ],
        default="all",
        help="Benchmark mode (default: all)",
    )
    p.add_argument("--requests",    type=int,   default=4,           help="Requests per scenario where applicable")
    p.add_argument("--concurrency", type=int,   default=4,           help="Concurrency for --mode concurrent")
    p.add_argument("--image",       default=None,                    help="Image file path for vision tests")
    p.add_argument("--image2",      default=None,                    help="Second image path for multi-image test")
    p.add_argument("--gateway-url", default=GATEWAY_URL)
    p.add_argument("--model",       default=MODEL)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.api_key:
        sys.exit("Provide --api-key or set LLM_API_KEY env var")
    asyncio.run(main(args))
