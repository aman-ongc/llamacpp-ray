import pytest

from gateway.auth.service import generate_api_key, hash_api_key, validate_key_against_db
from gateway.models import APIKey, User


@pytest.mark.asyncio
async def test_user_model_creation(session):
    user = User(
        username="testuser",
        department="IT",
        email="test@ongc.co.in",
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    assert user.id is not None
    assert user.username == "testuser"
    assert user.created_at is not None


@pytest.mark.asyncio
async def test_api_key_model_creation(session):
    user = User(username="keyuser", department="HR", email="hr@ongc.co.in", is_active=True)
    session.add(user)
    await session.commit()

    key = APIKey(
        user_id=user.id,
        key_hash="sha256hashvalue",
        key_prefix="sk-ong",
        label="test-key",
        is_active=True,
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)

    assert key.id is not None
    assert key.user_id == user.id


def test_hash_api_key_deterministic():
    raw = "sk-ongc-testkey123"
    assert hash_api_key(raw) == hash_api_key(raw)


def test_hash_api_key_different_inputs():
    assert hash_api_key("key1") != hash_api_key("key2")


def test_generate_api_key_format():
    key = generate_api_key()
    assert key.startswith("sk-ongc-")
    assert len(key) > 16


@pytest.mark.asyncio
async def test_validate_key_against_db_valid(session):
    user = User(username="authuser", department="ENG", email="eng@ongc.co.in", is_active=True)
    session.add(user)
    await session.flush()

    raw = generate_api_key()
    api_key = APIKey(
        user_id=user.id,
        key_hash=hash_api_key(raw),
        key_prefix=raw[:12],
        label="test",
        is_active=True,
    )
    session.add(api_key)
    await session.commit()

    result = await validate_key_against_db(session, raw)
    assert result is not None
    assert result.user.username == "authuser"
