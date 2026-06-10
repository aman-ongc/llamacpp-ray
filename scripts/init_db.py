"""Initialize database: create tables, create admin user and first API key."""

import asyncio
import sys
from pathlib import Path

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.auth.service import generate_api_key, hash_api_key
from gateway.config import settings
from gateway.models import APIKey, Base, User


def ensure_schema(sync_connection) -> None:
    inspector = inspect(sync_connection)
    if "api_keys" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("api_keys")}
        if "metadata" not in columns:
            sync_connection.execute(text("ALTER TABLE api_keys ADD COLUMN metadata TEXT"))
    if "request_logs" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("request_logs")}
        if "request_type" not in columns:
            sync_connection.execute(
                text("ALTER TABLE request_logs ADD COLUMN request_type VARCHAR(20) NOT NULL DEFAULT 'text'")
            )
        if "request_preview" not in columns:
            sync_connection.execute(text("ALTER TABLE request_logs ADD COLUMN request_preview TEXT"))
        if "response_preview" not in columns:
            sync_connection.execute(text("ALTER TABLE request_logs ADD COLUMN response_preview TEXT"))


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(ensure_schema)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        existing = await session.scalar(select(User).where(User.username == "admin"))
        if existing is None:
            admin = User(
                username="admin",
                email="admin@ongc.co.in",
                department="IT",
                is_active=True,
                is_admin=True,
            )
            session.add(admin)
            await session.flush()

            raw_key = generate_api_key()
            session.add(
                APIKey(
                    user_id=admin.id,
                    key_hash=hash_api_key(raw_key),
                    key_prefix=raw_key[:12],
                    label="admin-bootstrap",
                    metadata_text=None,
                    is_active=True,
                )
            )
            await session.commit()
            print("=== Bootstrap complete ===")
            print(f"Admin API key (save this - shown once): {raw_key}")
            print(f"Key prefix: {raw_key[:12]}")
        else:
            print("Admin user already exists; no new bootstrap key created.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
