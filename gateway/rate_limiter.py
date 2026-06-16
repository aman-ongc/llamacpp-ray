"""Sliding window rate limiter using Redis."""

import time

import redis.asyncio as aioredis
from fastapi import HTTPException, status

from gateway.config import settings


_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            retry_on_timeout=False,
        )
    return _redis


TEXT_RATE_LIMIT = 60
MULTIMODAL_RATE_LIMIT = 500
DEFAULT_WINDOW_SEC = 60


async def check_rate_limit(
    user_id: int,
    limit: int = TEXT_RATE_LIMIT,
    window_sec: int = DEFAULT_WINDOW_SEC,
) -> None:
    redis_client = get_redis()
    key = f"rl:user:{user_id}"
    now = time.time()
    window_start = now - window_sec

    pipe = redis_client.pipeline()
    pipe.zremrangebyscore(key, "-inf", window_start)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, window_sec + 1)
    results = await pipe.execute()

    count = results[2]
    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: {limit} requests per {window_sec}s",
            headers={"Retry-After": str(window_sec)},
        )
