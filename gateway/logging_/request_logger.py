from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import RequestLog, User


async def log_request(
    session: AsyncSession,
    *,
    user: User | None,
    api_key_prefix: str | None,
    model: str,
    node_ip: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    queue_ms: int,
    status_code: int,
    error_message: str | None,
    streaming: bool,
) -> RequestLog:
    entry = RequestLog(
        user_id=user.id if user else None,
        api_key_prefix=api_key_prefix,
        model=model,
        node_ip=node_ip,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        queue_ms=queue_ms,
        status_code=status_code,
        error_message=error_message,
        streaming=streaming,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry
