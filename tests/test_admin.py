import pytest

from gateway.auth.service import generate_api_key, hash_api_key
from gateway.config import settings
from gateway.models import APIKey, User


@pytest.mark.asyncio
async def test_admin_create_user(client):
    response = await client.post(
        "/admin/users",
        headers={"X-Admin-Secret": settings.admin_secret},
        json={"username": "adminuser", "email": "adminuser@ongc.co.in", "department": "IT"},
    )
    assert response.status_code == 200
    assert response.json()["username"] == "adminuser"


@pytest.mark.asyncio
async def test_admin_create_key(client, session):
    user = User(username="key-admin", email="key@ongc.co.in", department="IT", is_active=True)
    session.add(user)
    await session.commit()

    response = await client.post(
        "/admin/users/key-admin/keys",
        headers={"X-Admin-Secret": settings.admin_secret},
        json={"label": "cli", "metadata": "CLI access for automation"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["api_key"].startswith("sk-ongc-")
    assert body["username"] == "key-admin"
    assert body["metadata"] == "CLI access for automation"


@pytest.mark.asyncio
async def test_admin_create_key_unknown_user(client):
    response = await client.post(
        "/admin/users/no-such-user/keys",
        headers={"X-Admin-Secret": settings.admin_secret},
        json={"label": "test"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_user_usage(client, session):
    from gateway.models import APIKey, RequestLog
    from gateway.auth.service import generate_api_key, hash_api_key

    user = User(username="usage-user", email="usage@ongc.co.in", department="IT", is_active=True)
    session.add(user)
    await session.flush()
    raw_key = generate_api_key()
    session.add(APIKey(user_id=user.id, key_hash=hash_api_key(raw_key),
                       key_prefix=raw_key[:12], label="test", is_active=True))
    session.add(RequestLog(user_id=user.id, api_key_prefix=raw_key[:12],
                           model="qwen", node_ip="10.208.211.62",
                           prompt_tokens=100, completion_tokens=200,
                           latency_ms=3000, queue_ms=50, status_code=200, streaming=False))
    await session.commit()

    response = await client.get(
        "/admin/users/usage-user/usage",
        headers={"X-Admin-Secret": settings.admin_secret},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "usage-user"
    assert body["total_requests"] == 1
    assert body["total_prompt_tokens"] == 100
    assert body["by_node"]["10.208.211.62"] == 1


@pytest.mark.asyncio
async def test_admin_metrics(client, session):
    user = User(username="metrics-user", email="metrics@ongc.co.in", department="Ops", is_active=True)
    session.add(user)
    await session.commit()

    response = await client.get(
        "/admin/metrics",
        headers={"X-Admin-Secret": settings.admin_secret},
    )
    assert response.status_code == 200
    assert "users" in response.json()
