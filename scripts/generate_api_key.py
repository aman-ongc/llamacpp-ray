"""CLI utility to generate a new API key for an existing user."""

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.auth.service import generate_api_key, hash_api_key
from gateway.database import AsyncSessionLocal
from gateway.models import APIKey, User


async def main(username: str, label: str, metadata: str | None) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None:
            raise SystemExit(f"User '{username}' not found")

        raw_key = generate_api_key()
        session.add(
            APIKey(
                user_id=user.id,
                key_hash=hash_api_key(raw_key),
                key_prefix=raw_key[:12],
                label=label,
                metadata_text=metadata,
                is_active=True,
            )
        )
        await session.commit()
        print(raw_key)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    parser.add_argument("--label", default="generated")
    parser.add_argument("--metadata")
    args = parser.parse_args()
    asyncio.run(main(args.username, args.label, args.metadata))
