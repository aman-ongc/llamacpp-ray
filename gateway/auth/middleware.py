from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.service import validate_key_against_db
from gateway.database import get_db
from gateway.models import User


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> User:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    raw_key = authorization.removeprefix("Bearer ").strip()
    api_key = await validate_key_against_db(session, raw_key)
    if api_key is None or api_key.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    request.state.api_key_prefix = api_key.key_prefix
    return api_key.user
