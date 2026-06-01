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
    await session.refresh(user)

    response = await client.post(
        f"/admin/users/{user.id}/keys",
        headers={"X-Admin-Secret": settings.admin_secret},
        json={"label": "cli", "metadata": "CLI access for automation"},
    )
    assert response.status_code == 200
    assert response.json()["api_key"].startswith("sk-ongc-")
    assert response.json()["metadata"] == "CLI access for automation"


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
