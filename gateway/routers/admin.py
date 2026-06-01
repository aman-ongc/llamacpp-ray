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


@router.post("/users/{user_id}/keys", dependencies=[Depends(require_admin_secret)])
async def create_api_key(
    user_id: int,
    payload: KeyCreate,
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    raw_key = generate_api_key()
    api_key = APIKey(
        user_id=user_id,
        key_hash=hash_api_key(raw_key),
        key_prefix=raw_key[:12],
        label=payload.label,
        metadata_text=payload.metadata,
        is_active=True,
    )
    session.add(api_key)
    await session.commit()
    return {
        "api_key": raw_key,
        "key_prefix": raw_key[:12],
        "label": payload.label,
        "metadata": payload.metadata,
    }


@router.get("/keys", dependencies=[Depends(require_admin_secret)])
async def list_keys(session: AsyncSession = Depends(get_db)) -> list[dict[str, object]]:
    result = await session.execute(select(APIKey).order_by(APIKey.id))
    keys = result.scalars().all()
    return [
        {
            "id": key.id,
            "user_id": key.user_id,
            "key_prefix": key.key_prefix,
            "label": key.label,
            "metadata": key.metadata_text,
            "is_active": key.is_active,
        }
        for key in keys
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
