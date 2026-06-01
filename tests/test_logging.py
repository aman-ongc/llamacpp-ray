import pytest
from sqlalchemy import select

from gateway.auth.service import generate_api_key, hash_api_key
from gateway.models import APIKey, RequestLog, User


@pytest.mark.asyncio
async def test_request_log_written_after_chat(client, session):
    user = User(username="loguser", email="log@ongc.co.in", department="IT", is_active=True)
    session.add(user)
    await session.flush()
    raw_key = generate_api_key()
    session.add(
        APIKey(
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            key_prefix=raw_key[:12],
            label="log",
            is_active=True,
        )
    )
    await session.commit()

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": raw_key},
        json={"model": "qwen", "messages": [{"role": "user", "content": "log this"}]},
    )
    assert response.status_code == 200

    result = await session.execute(select(RequestLog))
    logs = result.scalars().all()
    assert logs
    assert logs[-1].model == "qwen"
    assert logs[-1].api_key_prefix == raw_key[:12]
