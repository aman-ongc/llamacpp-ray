import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from starlette.responses import JSONResponse, StreamingResponse

from gateway.auth.middleware import require_api_key
from gateway.config import settings
from gateway.database import AsyncSessionLocal
from gateway.logging_.request_logger import log_request
from gateway.metrics import (
    ACTIVE_REQUESTS,
    COMPLETION_TOKENS,
    PROMPT_TOKENS,
    QUEUE_REJECTED,
    RATE_LIMITED,
    REQUEST_COUNT,
    REQUEST_LATENCY_MS,
    TOTAL_TOKENS,
)
from gateway.models import User
from gateway.rate_limiter import MULTIMODAL_RATE_LIMIT, TEXT_RATE_LIMIT, check_rate_limit
from gateway.router import stream_inference, submit_inference


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
    # When True, all requests from the same API key go to the same node
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


def _build_request_preview(messages: list[ChatMessage], full: bool = False) -> str:
    parts = []
    for msg in messages:
        if isinstance(msg.content, str):
            parts.append(f"[{msg.role}]: {msg.content}")
        else:
            texts = [p.get("text", "") for p in msg.content if isinstance(p, dict) and p.get("type") == "text"]
            parts.append(f"[{msg.role}]: {''.join(texts)}")
    combined = "\n".join(parts)
    return combined if full else combined[:500]


def _build_success_response_preview(result: dict) -> str:
    preview = {k: v for k, v in result.items() if k != "choices"}
    choices_preview = []
    for choice in result.get("choices", []):
        cp = {k: v for k, v in choice.items() if k != "message"}
        message = choice.get("message", {})
        content = message.get("content") or ""
        cp["message"] = {k: v for k, v in message.items() if k != "content"}
        cp["message"]["content_preview"] = content[:500]
        choices_preview.append(cp)
    preview["choices"] = choices_preview
    return json.dumps(preview)


def _err_text(exc: Exception) -> str:
    return str(exc) or repr(exc)


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
):
    api_key_prefix = getattr(request.state, "api_key_prefix", None)
    multimodal = _is_multimodal_request(payload.messages)
    request_type = "multimodal" if multimodal else "text"
    start = time.perf_counter()
    try:
        await check_rate_limit(
            user.id,
            pool=request_type,
            limit=MULTIMODAL_RATE_LIMIT if multimodal else TEXT_RATE_LIMIT,
        )
    except HTTPException as exc:
        RATE_LIMITED.labels(request_type=request_type).inc()
        latency_ms = int((time.perf_counter() - start) * 1000)
        error_str = json.dumps(exc.detail) if isinstance(exc.detail, (dict, list)) else str(exc.detail)
        REQUEST_COUNT.labels(model=_MODEL_ALIAS, status_code=str(exc.status_code), streaming=str(payload.stream).lower(), username=user.username, node_ip="unknown").inc()
        try:
            async with AsyncSessionLocal() as session:
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
                    status_code=exc.status_code,
                    error_message=error_str,
                    streaming=payload.stream,
                    request_type=request_type,
                    request_preview=_build_request_preview(payload.messages, full=True),
                    response_preview=error_str,
                )
        except Exception:
            logger.exception("Failed to log rate-limited request (status=%d)", exc.status_code)
        raise
    except RedisError:
        pass
    # Affinity only applies to text pool (multimodal pool is a single node).
    affinity_key = api_key_prefix if (payload.session_affinity and not multimodal) else None

    ACTIVE_REQUESTS.inc()
    try:
        if payload.stream:
            prompt_tokens = _estimate_prompt_tokens(payload.messages)
            node_ip_label = "multimodal" if multimodal else "stream"

            async def event_stream():
                stream_error: Exception | None = None
                try:
                    async for chunk in stream_inference(
                        _payload_for_inference(payload), affinity_key, multimodal=multimodal
                    ):
                        line = chunk.rstrip("\n")
                        yield f"{_rewrite_sse_model(line)}\n\n"
                except Exception as exc:
                    stream_error = exc
                    raise
                finally:
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    status_code = 500 if stream_error is not None else 200
                    error_str = _err_text(stream_error) if stream_error is not None else None
                    if isinstance(stream_error, HTTPException) and stream_error.status_code == 503:
                        QUEUE_REJECTED.labels(request_type=request_type).inc()
                    async with AsyncSessionLocal() as session:
                        await log_request(
                            session,
                            user=user,
                            api_key_prefix=api_key_prefix,
                            model=_MODEL_ALIAS,
                            node_ip="multimodal" if multimodal else settings.controller_node_ip,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=0,
                            latency_ms=latency_ms,
                            queue_ms=0,
                            status_code=status_code,
                            error_message=error_str,
                            streaming=True,
                            request_type=request_type,
                            request_preview=_build_request_preview(payload.messages),
                            response_preview="[streaming]" if stream_error is None else error_str,
                        )
                    REQUEST_COUNT.labels(model=_MODEL_ALIAS, status_code=str(status_code), streaming="true", username=user.username, node_ip=node_ip_label).inc()
                    REQUEST_LATENCY_MS.labels(model=_MODEL_ALIAS, username=user.username, node_ip=node_ip_label).observe(latency_ms)
                    PROMPT_TOKENS.labels(model=_MODEL_ALIAS, username=user.username, request_type=request_type).inc(prompt_tokens)

            return StreamingResponse(event_stream(), media_type="text/event-stream")

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

        try:
            async with AsyncSessionLocal() as session:
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
                    request_preview=_build_request_preview(payload.messages),
                    response_preview=_build_success_response_preview(result),
                )
        except Exception:
            logger.exception("Failed to log successful inference for user=%s", user.username)

        REQUEST_COUNT.labels(model=_MODEL_ALIAS, status_code="200", streaming="false", username=user.username, node_ip=node_ip).inc()
        REQUEST_LATENCY_MS.labels(model=_MODEL_ALIAS, username=user.username, node_ip=node_ip).observe(latency_ms)
        PROMPT_TOKENS.labels(model=_MODEL_ALIAS, username=user.username, request_type=request_type).inc(prompt_tokens)
        COMPLETION_TOKENS.labels(model=_MODEL_ALIAS, username=user.username, request_type=request_type).inc(completion_tokens)
        return JSONResponse(result)
    except HTTPException as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        error_str = json.dumps(exc.detail) if isinstance(exc.detail, (dict, list)) else str(exc.detail)
        if exc.status_code == 503:
            QUEUE_REJECTED.labels(request_type=request_type).inc()
        REQUEST_COUNT.labels(model=_MODEL_ALIAS, status_code=str(exc.status_code), streaming="false", username=user.username, node_ip="unknown").inc()
        try:
            async with AsyncSessionLocal() as session:
                await log_request(
                    session,
                    user=user,
                    api_key_prefix=api_key_prefix,
                    model=_MODEL_ALIAS,
                    node_ip="multimodal" if multimodal else None,
                    prompt_tokens=_estimate_prompt_tokens(payload.messages),
                    completion_tokens=0,
                    latency_ms=latency_ms,
                    queue_ms=0,
                    status_code=exc.status_code,
                    error_message=error_str,
                    streaming=payload.stream,
                    request_type=request_type,
                    request_preview=_build_request_preview(payload.messages, full=True),
                    response_preview=error_str,
                )
        except Exception:
            logger.exception("Failed to log HTTPException request (status=%d)", exc.status_code)
        raise
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        REQUEST_COUNT.labels(model=_MODEL_ALIAS, status_code="500", streaming="false", username=user.username, node_ip="unknown").inc()
        try:
            async with AsyncSessionLocal() as session:
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
                    error_message=_err_text(exc),
                    streaming=payload.stream,
                    request_type=request_type,
                    request_preview=_build_request_preview(payload.messages, full=True),
                    response_preview=_err_text(exc),
                )
        except Exception:
            logger.exception("Failed to log inference exception: %s", exc)
        raise HTTPException(status_code=500, detail="Inference request failed") from exc
    finally:
        ACTIVE_REQUESTS.dec()
