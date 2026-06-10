import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, StreamingResponse

from gateway.auth.middleware import require_api_key
from gateway.config import settings
from gateway.database import get_db
from gateway.logging_.request_logger import log_request
from gateway.metrics import (
    ACTIVE_REQUESTS,
    COMPLETION_TOKENS,
    PROMPT_TOKENS,
    REQUEST_COUNT,
    REQUEST_LATENCY_MS,
    TOTAL_TOKENS,
)
from gateway.models import User
from gateway.rate_limiter import check_rate_limit
from gateway.ray_client import stream_inference, submit_inference


router = APIRouter(prefix="/v1", tags=["chat"])


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]]


class ChatCompletionRequest(BaseModel):
    model: str = Field(default_factory=lambda: settings.default_model)
    messages: list[ChatMessage]
    max_tokens: int = 256
    temperature: float = 0.7
    stream: bool = False
    # When True, all requests from the same API key go to the same Ray worker
    # so llama.cpp can reuse its KV cache across turns.
    session_affinity: bool = Field(default=False, exclude=True)


def _is_multimodal_request(messages: list[ChatMessage]) -> bool:
    """Return True if any message contains image content parts."""
    for message in messages:
        if isinstance(message.content, list):
            for part in message.content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    return True
    return False


def _payload_for_inference(payload: ChatCompletionRequest) -> dict[str, Any]:
    return payload.model_dump(exclude={"session_affinity"})


_MODEL_ALIAS = "ongc-llm"


def _normalize_completion_response(result: dict[str, Any]) -> dict[str, Any]:
    result["model"] = _MODEL_ALIAS
    for choice in result.get("choices", []):
        message = choice.get("message")
        if isinstance(message, dict):
            message.pop("reasoning_content", None)
    return result


def _rewrite_sse_model(line: str) -> str:
    if not line.startswith("data: ") or line == "data: [DONE]":
        return line
    try:
        chunk = json.loads(line[6:])
        chunk["model"] = _MODEL_ALIAS
        return f"data: {json.dumps(chunk)}"
    except (json.JSONDecodeError, KeyError):
        return line


def _estimate_prompt_tokens(messages: list[ChatMessage]) -> int:
    total = 0
    for message in messages:
        if isinstance(message.content, str):
            total += len(message.content.split())
        else:
            for part in message.content:
                if part.get("type") == "text":
                    total += len(part.get("text", "").split())
    return max(1, total)


@router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    user: User = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
):
    api_key_prefix = getattr(request.state, "api_key_prefix", None)
    try:
        await check_rate_limit(user.id)
    except HTTPException:
        raise
    except RedisError:
        pass

    multimodal = _is_multimodal_request(payload.messages)
    request_type = "multimodal" if multimodal else "text"
    # Affinity only applies to text pool (multimodal pool is a single node).
    affinity_key = api_key_prefix if (payload.session_affinity and not multimodal) else None

    start = time.perf_counter()
    ACTIVE_REQUESTS.inc()
    try:
        if payload.stream:
            async def event_stream():
                async for chunk in stream_inference(
                    _payload_for_inference(payload), affinity_key, multimodal=multimodal
                ):
                    line = chunk.rstrip("\n")
                    yield f"{_rewrite_sse_model(line)}\n\n"

            response = StreamingResponse(event_stream(), media_type="text/event-stream")
            latency_ms = int((time.perf_counter() - start) * 1000)
            prompt_tokens = _estimate_prompt_tokens(payload.messages)
            await log_request(
                session,
                user=user,
                api_key_prefix=api_key_prefix,
                model=_MODEL_ALIAS,
                node_ip=settings.multimodal_node_ip if multimodal else settings.controller_node_ip,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                latency_ms=latency_ms,
                queue_ms=0,
                status_code=200,
                error_message=None,
                streaming=True,
                request_type=request_type,
            )
            node_ip_label = settings.multimodal_node_ip if multimodal else "stream"
            REQUEST_COUNT.labels(model=_MODEL_ALIAS, status_code="200", streaming="true", username=user.username, node_ip=node_ip_label).inc()
            REQUEST_LATENCY_MS.labels(model=_MODEL_ALIAS, username=user.username, node_ip=node_ip_label).observe(latency_ms)
            PROMPT_TOKENS.labels(model=_MODEL_ALIAS, username=user.username, request_type=request_type).inc(prompt_tokens)
            return response

        result = await submit_inference(
            _payload_for_inference(payload), affinity_key, multimodal=multimodal
        )
        result = _normalize_completion_response(result)

        usage = result.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", _estimate_prompt_tokens(payload.messages)))
        completion_tokens = int(usage.get("completion_tokens", 0))
        latency_ms = int((time.perf_counter() - start) * 1000)
        queue_ms = int(result.get("queue_ms", 0))
        node_ip = result.get("node_ip", settings.controller_node_ip)

        await log_request(
            session,
            user=user,
            api_key_prefix=api_key_prefix,
            model=_MODEL_ALIAS,
            node_ip=node_ip,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            queue_ms=queue_ms,
            status_code=200,
            error_message=None,
            streaming=False,
            request_type=request_type,
        )

        REQUEST_COUNT.labels(model=_MODEL_ALIAS, status_code="200", streaming="false", username=user.username, node_ip=node_ip).inc()
        REQUEST_LATENCY_MS.labels(model=_MODEL_ALIAS, username=user.username, node_ip=node_ip).observe(latency_ms)
        PROMPT_TOKENS.labels(model=_MODEL_ALIAS, username=user.username, request_type=request_type).inc(prompt_tokens)
        COMPLETION_TOKENS.labels(model=_MODEL_ALIAS, username=user.username, request_type=request_type).inc(completion_tokens)
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        REQUEST_COUNT.labels(model=_MODEL_ALIAS, status_code="500", streaming="false", username=user.username, node_ip="unknown").inc()
        await log_request(
            session,
            user=user,
            api_key_prefix=api_key_prefix,
            model=_MODEL_ALIAS,
            node_ip=None,
            prompt_tokens=_estimate_prompt_tokens(payload.messages),
            completion_tokens=0,
            latency_ms=latency_ms,
            queue_ms=0,
            status_code=500,
            error_message=str(exc),
            streaming=payload.stream,
        )
        raise HTTPException(status_code=500, detail="Inference request failed") from exc
    finally:
        ACTIVE_REQUESTS.dec()
