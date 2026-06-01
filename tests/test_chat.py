import pytest

from gateway.auth.service import generate_api_key, hash_api_key
from gateway.models import APIKey, User
from gateway.routers.chat import ChatCompletionRequest, ChatMessage, _normalize_completion_response, _payload_for_inference


async def seed_user(session):
    user = User(username="chatuser", email="chat@ongc.co.in", department="IT", is_active=True)
    session.add(user)
    await session.flush()
    raw_key = generate_api_key()
    session.add(
        APIKey(
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            key_prefix=raw_key[:12],
            label="chat",
            is_active=True,
        )
    )
    await session.commit()
    return raw_key


@pytest.mark.asyncio
async def test_chat_requires_auth(client):
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "qwen", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chat_completion_success(client, session):
    raw_key = await seed_user(session)
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": raw_key},
        json={"model": "qwen", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_chat_completion_stream_success(client, session):
    raw_key = await seed_user(session)
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": raw_key},
        json={
            "model": "qwen",
            "stream": True,
            "messages": [{"role": "user", "content": "stream hello"}],
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


def test_payload_for_inference_sets_chat_template_kwargs():
    payload = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
    data = _payload_for_inference(payload)
    assert data["chat_template_kwargs"] == {"enable_thinking": False}
    # gateway-only fields must not reach Ray Serve
    assert "enable_thinking" not in data
    assert "session_affinity" not in data


def test_payload_for_inference_per_request_thinking_on():
    payload = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="hello")],
        enable_thinking=True,
    )
    data = _payload_for_inference(payload)
    assert data["chat_template_kwargs"] == {"enable_thinking": True}


def test_session_affinity_default_true():
    payload = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
    assert payload.session_affinity is True


def test_session_affinity_excluded_from_payload():
    payload = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="hello")],
        session_affinity=False,
    )
    data = _payload_for_inference(payload)
    assert "session_affinity" not in data


def test_normalize_completion_response_removes_reasoning_content():
    result = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "final answer",
                }
            }
        ]
    }
    normalized = _normalize_completion_response(result)
    message = normalized["choices"][0]["message"]
    assert message["content"] == "final answer"
    assert "reasoning_content" not in message
