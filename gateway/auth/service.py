import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.models import APIKey


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return f"sk-ongc-{secrets.token_urlsafe(32)}"


async def validate_key_against_db(session: AsyncSession, raw_key: str) -> APIKey | None:
    key_hash = hash_api_key(raw_key)
    stmt = (
        select(APIKey)
        .options(selectinload(APIKey.user))
        .where(APIKey.key_hash == key_hash, APIKey.is_active.is_(True))
    )
    result = await session.execute(stmt)
    api_key = result.scalar_one_or_none()
    if api_key is None or api_key.user is None or not api_key.user.is_active:
        return None

    api_key.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(api_key)
    return api_key
