import json
import time
import uuid
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
)
from gateway.models import User
from gateway.rate_limiter import check_rate_limit
from gateway.ray_client import stream_inference, submit_inference


router = APIRouter(prefix="/v1", tags=["chat"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = Field(default_factory=lambda: settings.default_model)
    messages: list[ChatMessage]
    max_tokens: int = 256
    temperature: float = 0.7
    stream: bool = False
    # Per-request thinking override. None = use global ENABLE_THINKING setting.
    enable_thinking: bool | None = Field(default=None, exclude=True)
    # When True, all requests from the same API key go to the same Ray worker
    # so llama.cpp can reuse its KV cache across turns. Defaults to False;
    # set explicitly in the request to enable sticky routing.
    session_affinity: bool = Field(default=False, exclude=True)


def _payload_for_inference(payload: ChatCompletionRequest) -> dict[str, Any]:
    data = payload.model_dump(exclude={"enable_thinking", "session_affinity"})
    thinking = payload.enable_thinking if payload.enable_thinking is not None else settings.enable_thinking
    data["chat_template_kwargs"] = {"enable_thinking": thinking}
    return data


def _normalize_completion_response(result: dict[str, Any]) -> dict[str, Any]:
    for choice in result.get("choices", []):
        message = choice.get("message")
        if isinstance(message, dict) and not message.get("content"):
            reasoning = message.pop("reasoning_content", None)
            message["content"] = "" if reasoning is None else str(reasoning).strip()
        elif isinstance(message, dict):
            message.pop("reasoning_content", None)
    return result


def _estimate_prompt_tokens(messages: list[ChatMessage]) -> int:
    return max(1, sum(len(message.content.split()) for message in messages))


def _fallback_response(payload: ChatCompletionRequest) -> dict[str, Any]:
    prompt = payload.messages[-1].content if payload.messages else ""
    content = f"Ray Serve unavailable on controller; fallback response for prompt: {prompt[:80]}"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": _estimate_prompt_tokens(payload.messages),
            "completion_tokens": max(1, len(content.split())),
            "total_tokens": _estimate_prompt_tokens(payload.messages) + max(1, len(content.split())),
        },
        "node_ip": settings.controller_node_ip,
        "queue_ms": 0,
    }


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

    affinity_key = api_key_prefix if payload.session_affinity else None

    start = time.perf_counter()
    ACTIVE_REQUESTS.inc()
    try:
        if payload.stream:
            async def event_stream():
                try:
                    async for chunk in stream_inference(_payload_for_inference(payload), affinity_key):
                        yield chunk
                except Exception:
                    fallback = _fallback_response(payload)
                    choice = fallback["choices"][0]["message"]["content"]
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': choice}, 'index': 0, 'finish_reason': None}]})}\n\n"
                    yield "data: [DONE]\n\n"

            response = StreamingResponse(event_stream(), media_type="text/event-stream")
            latency_ms = int((time.perf_counter() - start) * 1000)
            prompt_tokens = _estimate_prompt_tokens(payload.messages)
            await log_request(
                session,
                user=user,
                api_key_prefix=api_key_prefix,
                model=payload.model,
                node_ip=settings.controller_node_ip,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                latency_ms=latency_ms,
                queue_ms=0,
                status_code=200,
                error_message=None,
                streaming=True,
            )
            # node_ip is unknown for streaming (SSE proxied before node responds);
            # use "stream" as a sentinel so per-node dashboards stay clean.
            REQUEST_COUNT.labels(model=payload.model, status_code="200", streaming="true", username=user.username, node_ip="stream").inc()
            REQUEST_LATENCY_MS.labels(model=payload.model, username=user.username, node_ip="stream").observe(latency_ms)
            PROMPT_TOKENS.labels(model=payload.model, username=user.username).inc(prompt_tokens)
            return response

        try:
            result = await submit_inference(_payload_for_inference(payload), affinity_key)
        except Exception:
            result = _fallback_response(payload)
        else:
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
            model=payload.model,
            node_ip=node_ip,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            queue_ms=queue_ms,
            status_code=200,
            error_message=None,
            streaming=False,
        )

        REQUEST_COUNT.labels(model=payload.model, status_code="200", streaming="false", username=user.username, node_ip=node_ip).inc()
        REQUEST_LATENCY_MS.labels(model=payload.model, username=user.username, node_ip=node_ip).observe(latency_ms)
        PROMPT_TOKENS.labels(model=payload.model, username=user.username).inc(prompt_tokens)
        COMPLETION_TOKENS.labels(model=payload.model, username=user.username).inc(completion_tokens)
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        REQUEST_COUNT.labels(model=payload.model, status_code="500", streaming="false", username=user.username, node_ip="unknown").inc()
        await log_request(
            session,
            user=user,
            api_key_prefix=api_key_prefix,
            model=payload.model,
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
