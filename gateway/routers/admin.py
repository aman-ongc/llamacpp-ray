from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.service import generate_api_key, hash_api_key
from gateway.config import settings
from gateway.database import get_db
from gateway.models import APIKey, RequestLog, User


router = APIRouter(prefix="/admin", tags=["admin"])


class UserCreate(BaseModel):
    username: str
    email: EmailStr
    department: str


class KeyCreate(BaseModel):
    label: str = "default"
    metadata: str | None = None


async def require_admin_secret(
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> None:
    if x_admin_secret != settings.admin_secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin secret",
        )


async def _get_user_by_username(username: str, session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User '{username}' not found")
    return user


@router.get("/users", dependencies=[Depends(require_admin_secret)])
async def list_users(session: AsyncSession = Depends(get_db)) -> list[dict[str, object]]:
    result = await session.execute(select(User).order_by(User.id))
    users = result.scalars().all()
    return [
        {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "department": user.department,
            "is_active": user.is_active,
            "is_admin": user.is_admin,
        }
        for user in users
    ]


@router.post("/users", dependencies=[Depends(require_admin_secret)])
async def create_user(
    payload: UserCreate,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user = User(
        username=payload.username,
        email=payload.email,
        department=payload.department,
        is_active=True,
        is_admin=False,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with that username or email already exists",
        )
    await session.refresh(user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "department": user.department,
    }


@router.post("/users/{username}/keys", dependencies=[Depends(require_admin_secret)])
async def create_api_key(
    username: str,
    payload: KeyCreate,
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user = await _get_user_by_username(username, session)

    raw_key = generate_api_key()
    session.add(APIKey(
        user_id=user.id,
        key_hash=hash_api_key(raw_key),
        key_prefix=raw_key[:12],
        label=payload.label,
        metadata_text=payload.metadata,
        is_active=True,
    ))
    await session.commit()
    return {
        "username": user.username,
        "api_key": raw_key,
        "key_prefix": raw_key[:12],
        "label": payload.label,
        "metadata": payload.metadata,
    }


@router.get("/users/{username}/usage", dependencies=[Depends(require_admin_secret)])
async def user_usage(
    username: str,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user = await _get_user_by_username(username, session)

    logs_q = await session.execute(
        select(RequestLog).where(RequestLog.user_id == user.id)
    )
    logs = logs_q.scalars().all()

    if not logs:
        return {
            "username": user.username,
            "total_requests": 0,
            "successful_requests": 0,
            "error_requests": 0,
            "streaming_requests": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "avg_latency_ms": 0,
            "by_node": {},
            "last_request_at": None,
        }

    successful = [l for l in logs if l.status_code == 200]
    by_node: dict[str, int] = {}
    for log in logs:
        if log.node_ip:
            by_node[log.node_ip] = by_node.get(log.node_ip, 0) + 1

    return {
        "username": user.username,
        "total_requests": len(logs),
        "successful_requests": len(successful),
        "error_requests": len(logs) - len(successful),
        "streaming_requests": sum(1 for l in logs if l.streaming),
        "total_prompt_tokens": sum(l.prompt_tokens for l in logs),
        "total_completion_tokens": sum(l.completion_tokens for l in logs),
        "avg_latency_ms": int(sum(l.latency_ms for l in logs) / len(logs)),
        "by_node": by_node,
        "last_request_at": max((l.created_at for l in logs), default=None),
    }


@router.get("/keys", dependencies=[Depends(require_admin_secret)])
async def list_keys(session: AsyncSession = Depends(get_db)) -> list[dict[str, object]]:
    result = await session.execute(
        select(APIKey, User)
        .join(User, APIKey.user_id == User.id)
        .order_by(APIKey.id)
    )
    rows = result.all()
    return [
        {
            "id": key.id,
            "username": user.username,
            "key_prefix": key.key_prefix,
            "label": key.label,
            "metadata": key.metadata_text,
            "is_active": key.is_active,
        }
        for key, user in rows
    ]


@router.get("/metrics", dependencies=[Depends(require_admin_secret)])
async def admin_metrics(session: AsyncSession = Depends(get_db)) -> dict[str, int]:
    users_count = await session.scalar(select(func.count()).select_from(User))
    keys_count = await session.scalar(select(func.count()).select_from(APIKey))
    logs_count = await session.scalar(select(func.count()).select_from(RequestLog))
    return {
        "users": users_count or 0,
        "api_keys": keys_count or 0,
        "request_logs": logs_count or 0,
    }
