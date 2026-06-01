# Distributed Enterprise LLM Platform — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a distributed, enterprise-grade LLM inference platform on 4 GPU workstations with centralized FastAPI gateway, Ray-based orchestration, llama.cpp workers, auth, logging, and observability.

**Architecture:** FastAPI gateway on WS-11 validates API keys, logs requests, and routes to Ray Serve head node; Ray dispatches to llama.cpp worker actors on WS-03/08/11/13; PostgreSQL stores users/keys/logs; Redis handles rate-limiting and distributed state; Prometheus+Grafana provide observability; NGINX terminates TLS and reverse-proxies externally.

**Tech Stack:** Python 3.12, FastAPI, Ray Serve 2.x, llama.cpp (llama-server binary), SQLAlchemy async + asyncpg, PostgreSQL 16, Redis 7, Prometheus, Grafana, NGINX, Docker Compose, pytest + pytest-asyncio

---

## Cluster Reference

| Node  | IP             | Role                     |
|-------|----------------|--------------------------|
| WS-11 | 10.208.211.62  | Controller, Ray Head, Gateway |
| WS-03 | 10.208.211.54  | Ray Worker               |
| WS-08 | 10.208.211.59  | Ray Worker               |
| WS-13 | 10.208.211.64  | Ray Worker               |

- SSH: `sshpass -p 'Ongc@1234' ssh administrator@<ip>`
- Models: `/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/`
- Venvs: `/mnt/d/VirtualEnvironments/`
- Proxy bypass required for all internal traffic: `--noproxy '*'` in curl; `NO_PROXY=localhost,127.0.0.1,10.0.0.0/8` in Python

---

## Scope

This plan covers **Phase 1** (core inference) and **Phase 2** (observability + Redis).
Phase 3 (NGINX TLS, LDAP) and Phase 4 (Kubernetes/KubeRay) are out of scope here.

---

## File Structure

```
llm-inference-service/
├── gateway/
│   ├── main.py                  # FastAPI app, lifespan, middleware registration
│   ├── config.py                # Pydantic Settings (env-based)
│   ├── database.py              # Async SQLAlchemy engine + session factory
│   ├── models.py                # SQLAlchemy ORM: User, APIKey, RequestLog
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── service.py           # validate_api_key(), create_key(), hash/verify
│   │   └── middleware.py        # FastAPI dependency: require_api_key
│   ├── routers/
│   │   ├── chat.py              # POST /v1/chat/completions (streaming + non-streaming)
│   │   ├── completions.py       # POST /v1/completions
│   │   ├── models_router.py     # GET /v1/models
│   │   ├── health.py            # GET /health, /ready, /live
│   │   └── admin.py             # /admin/users, /admin/keys, /admin/metrics
│   ├── logging_/
│   │   ├── __init__.py
│   │   └── request_logger.py    # async log_request() writes RequestLog row
│   ├── metrics.py               # Prometheus counters/histograms
│   └── ray_client.py            # submit_inference() — sends request to Ray
├── worker/
│   ├── ray_worker.py            # Ray Serve LlamaCppWorker deployment
│   ├── llama_process.py         # subprocess manager for llama-server binary
│   └── start_ray_worker.sh      # Shell bootstrap: start Ray, deploy worker
├── infra/
│   ├── prometheus/
│   │   └── prometheus.yml
│   ├── grafana/
│   │   ├── provisioning/
│   │   │   ├── datasources/prometheus.yml
│   │   │   └── dashboards/dashboard.yml
│   │   └── dashboards/
│   │       └── llm_platform.json
│   └── nginx/
│       └── nginx.conf
├── scripts/
│   ├── deploy_workers.sh        # SSH: copy worker/ + start_ray_worker.sh to all nodes
│   ├── init_db.py               # Create tables, seed admin user + first API key
│   └── generate_api_key.py      # CLI util: create new API key for a user
├── docker/
│   ├── gateway/
│   │   └── Dockerfile
│   └── worker/
│       └── Dockerfile
├── docker-compose.yml           # Gateway node: FastAPI + PostgreSQL + Redis + Prometheus + Grafana
├── tests/
│   ├── conftest.py              # pytest fixtures: test DB, test client, mock Ray
│   ├── test_auth.py             # API key hashing, validation, middleware
│   ├── test_chat.py             # /v1/chat/completions: auth, routing, streaming
│   ├── test_admin.py            # /admin/* CRUD
│   ├── test_logging.py          # RequestLog written after each request
│   └── test_health.py           # /health, /ready, /live
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

---

## Phase 1: Core Distributed Inference

---

### Task 1: Project Bootstrap

**Files:**
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `.env.example`
- Create: `gateway/config.py`

- [ ] **Step 1: Create requirements.txt**

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
pydantic-settings==2.4.0
sqlalchemy[asyncio]==2.0.35
asyncpg==0.29.0
alembic==1.13.3
redis[asyncio]==5.1.0
prometheus-client==0.21.0
passlib[bcrypt]==1.7.4
python-jose[cryptography]==3.3.0
ray[serve]==2.35.0
starlette==0.38.6
anyio==4.6.0
```

- [ ] **Step 2: Create requirements-dev.txt**

```
pytest==8.3.3
pytest-asyncio==0.24.0
pytest-mock==3.14.0
httpx==0.27.2
factory-boy==3.3.1
```

- [ ] **Step 3: Create .env.example**

```bash
DATABASE_URL=postgresql+asyncpg://llm:llm@localhost:5432/llm_platform
REDIS_URL=redis://localhost:6379/0
RAY_ADDRESS=ray://10.208.211.62:10001
ADMIN_SECRET=changeme-replace-in-production
NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in
LLAMA_MODEL_PATH=/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
LLAMA_MMPROJ_PATH=/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/mmproj-F16.gguf
LLAMA_PORT=8080
LLAMA_CONTEXT=65536
LLAMA_PARALLEL=2
LLAMA_NGL=999
```

- [ ] **Step 4: Create gateway/config.py**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://llm:llm@localhost:5432/llm_platform"
    redis_url: str = "redis://localhost:6379/0"
    ray_address: str = "ray://10.208.211.62:10001"
    admin_secret: str = "changeme"

    # Proxy bypass — applied to all internal httpx clients
    no_proxy: str = "localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"

    llama_model_path: str = "/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
    llama_mmproj_path: str = "/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/mmproj-F16.gguf"
    llama_port: int = 8080
    llama_context: int = 65536
    llama_parallel: int = 2
    llama_ngl: int = 999


settings = Settings()
```

- [ ] **Step 5: Install dependencies on WS-11**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  python3.12 -m venv /mnt/d/VirtualEnvironments/llm-platform &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  pip install --proxy http://10.205.122.201:8080 -r requirements.txt -r requirements-dev.txt
"
```

Expected: packages install without error.

- [ ] **Step 6: Commit**

```bash
git init
git add requirements.txt requirements-dev.txt .env.example gateway/config.py
git commit -m "feat: project bootstrap with deps and config"
```

---

### Task 2: Database Models

**Files:**
- Create: `gateway/database.py`
- Create: `gateway/models.py`
- Create: `scripts/init_db.py`
- Test: `tests/conftest.py`

- [ ] **Step 1: Write failing test for model creation**

Create `tests/conftest.py`:

```python
import asyncio
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from gateway.models import Base


TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as s:
        yield s
```

Create `tests/test_auth.py` (first test only):

```python
import pytest
from datetime import datetime, timezone
from gateway.models import User, APIKey


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
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_auth.py -v 2>&1 | head -30
"
```

Expected: `ModuleNotFoundError: No module named 'gateway.models'`

- [ ] **Step 3: Create gateway/database.py**

```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from gateway.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
```

- [ ] **Step 4: Create gateway/models.py**

```python
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    department: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    api_keys: Mapped[list["APIKey"]] = relationship("APIKey", back_populates="user")
    request_logs: Mapped[list["RequestLog"]] = relationship("RequestLog", back_populates="user")


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="api_keys")


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True, index=True)
    api_key_prefix: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    node_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queue_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    streaming: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[Optional["User"]] = relationship("User", back_populates="request_logs")
```

- [ ] **Step 5: Add aiosqlite for test DB**

Add to `requirements-dev.txt`:
```
aiosqlite==0.20.0
```

Install:
```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  pip install --proxy http://10.205.122.201:8080 aiosqlite==0.20.0
"
```

- [ ] **Step 6: Run tests to verify pass**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_auth.py::test_user_model_creation tests/test_auth.py::test_api_key_model_creation -v
"
```

Expected:
```
tests/test_auth.py::test_user_model_creation PASSED
tests/test_auth.py::test_api_key_model_creation PASSED
```

- [ ] **Step 7: Create scripts/init_db.py**

```python
"""Initialize database: create tables, create admin user and first API key."""
import asyncio
import secrets
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from gateway.config import settings
from gateway.models import Base, User, APIKey


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def main():
    engine = create_async_engine(settings.database_url, echo=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        admin = User(
            username="admin",
            email="admin@ongc.co.in",
            department="IT",
            is_active=True,
            is_admin=True,
        )
        session.add(admin)
        await session.flush()

        raw_key = "sk-ongc-" + secrets.token_urlsafe(32)
        key_prefix = raw_key[:12]
        api_key = APIKey(
            user_id=admin.id,
            key_hash=_hash_key(raw_key),
            key_prefix=key_prefix,
            label="admin-bootstrap",
            is_active=True,
        )
        session.add(api_key)
        await session.commit()

        print(f"\n=== Bootstrap complete ===")
        print(f"Admin API key (save this — shown once): {raw_key}")
        print(f"Key prefix: {key_prefix}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 8: Commit**

```bash
git add gateway/database.py gateway/models.py scripts/init_db.py tests/conftest.py tests/test_auth.py
git commit -m "feat: database models for users, api_keys, request_logs"
```

---

### Task 3: API Key Auth Service + Middleware

**Files:**
- Create: `gateway/auth/__init__.py`
- Create: `gateway/auth/service.py`
- Create: `gateway/auth/middleware.py`
- Test: `tests/test_auth.py` (extend)

- [ ] **Step 1: Write failing tests for auth service**

Append to `tests/test_auth.py`:

```python
import pytest
from gateway.auth.service import hash_api_key, verify_api_key, generate_api_key, validate_key_against_db
from gateway.models import User, APIKey


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
    assert result.user_id == user.id


@pytest.mark.asyncio
async def test_validate_key_against_db_invalid(session):
    result = await validate_key_against_db(session, "sk-ongc-doesnotexist")
    assert result is None


@pytest.mark.asyncio
async def test_validate_key_inactive_rejected(session):
    user = User(username="inactiveuser", department="HR", email="hr2@ongc.co.in", is_active=True)
    session.add(user)
    await session.flush()

    raw = generate_api_key()
    api_key = APIKey(
        user_id=user.id,
        key_hash=hash_api_key(raw),
        key_prefix=raw[:12],
        label="inactive",
        is_active=False,
    )
    session.add(api_key)
    await session.commit()

    result = await validate_key_against_db(session, raw)
    assert result is None
```

- [ ] **Step 2: Run to verify failure**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_auth.py -k 'not model_creation' -v 2>&1 | head -20
"
```

Expected: `ModuleNotFoundError: No module named 'gateway.auth'`

- [ ] **Step 3: Create gateway/auth/__init__.py**

```python
```
(empty)

- [ ] **Step 4: Create gateway/auth/service.py**

```python
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import APIKey


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key() -> str:
    return "sk-ongc-" + secrets.token_urlsafe(32)


async def validate_key_against_db(
    session: AsyncSession, raw_key: str
) -> Optional[APIKey]:
    key_hash = hash_api_key(raw_key)
    result = await session.execute(
        select(APIKey).where(APIKey.key_hash == key_hash, APIKey.is_active == True)
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        return None

    # Update last_used_at without full load
    api_key.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    return api_key
```

- [ ] **Step 5: Run tests to verify pass**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_auth.py -v
"
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Create gateway/auth/middleware.py**

```python
from typing import Optional
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.service import validate_key_against_db
from gateway.database import get_db
from gateway.models import APIKey, User
from sqlalchemy import select

api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


async def require_api_key(
    authorization: Optional[str] = Security(api_key_header),
    session: AsyncSession = Depends(get_db),
) -> tuple[APIKey, User]:
    """FastAPI dependency. Returns (api_key, user) or raises 401."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    # Accept both raw key and "Bearer sk-ongc-..." format
    raw_key = authorization.removeprefix("Bearer ").strip()

    api_key = await validate_key_against_db(session, raw_key)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API key",
        )

    result = await session.execute(
        select(User).where(User.id == api_key.user_id, User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account inactive",
        )

    return api_key, user
```

- [ ] **Step 7: Write middleware integration test**

Append to `tests/test_auth.py`:

```python
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch
from gateway.auth.middleware import require_api_key


def test_require_api_key_missing_header():
    """require_api_key raises 401 when header absent."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/protected")
    async def protected(auth=Depends(require_api_key)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/protected")
    assert resp.status_code == 401
```

- [ ] **Step 8: Run all auth tests**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_auth.py -v
"
```

Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
git add gateway/auth/ tests/test_auth.py
git commit -m "feat: api key auth service and middleware"
```

---

### Task 4: FastAPI Gateway Skeleton + Health Endpoints

**Files:**
- Create: `gateway/routers/health.py`
- Create: `gateway/main.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write failing health tests**

Create `tests/test_health.py`:

```python
import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
def app():
    from gateway.main import create_app
    return create_app()


@pytest.mark.asyncio
async def test_health_returns_200(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_live_returns_200(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/live")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ready_returns_200_or_503(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/ready")
    # 200 if DB reachable in test env, 503 otherwise — either is valid response shape
    assert resp.status_code in (200, 503)
    assert "status" in resp.json()
```

- [ ] **Step 2: Run to verify failure**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_health.py -v 2>&1 | head -15
"
```

Expected: `ModuleNotFoundError: No module named 'gateway.main'`

- [ ] **Step 3: Create gateway/routers/health.py**

```python
from fastapi import APIRouter
from sqlalchemy import text
from gateway.database import AsyncSessionLocal

router = APIRouter(tags=["infrastructure"])


@router.get("/health")
async def health():
    return {"status": "ok", "service": "llm-inference-gateway"}


@router.get("/live")
async def liveness():
    return {"status": "alive"}


@router.get("/ready")
async def readiness():
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail={"status": "not ready", "error": str(exc)})
```

- [ ] **Step 4: Create gateway/main.py**

```python
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.routers.health import router as health_router


def create_app() -> FastAPI:
    # Ensure internal traffic bypasses corporate proxy
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: add ray init, connection pool warmup here in later tasks
        yield
        # Shutdown: cleanup

    app = FastAPI(
        title="ONGC LLM Inference Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(health_router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway.main:app", host="0.0.0.0", port=8000, reload=False)
```

- [ ] **Step 5: Run health tests**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_health.py -v
"
```

Expected:
```
tests/test_health.py::test_health_returns_200 PASSED
tests/test_health.py::test_live_returns_200 PASSED
tests/test_health.py::test_ready_returns_200_or_503 PASSED
```

- [ ] **Step 6: Commit**

```bash
git add gateway/main.py gateway/routers/health.py tests/test_health.py
git commit -m "feat: fastapi gateway skeleton with health endpoints"
```

---

### Task 5: Ray Cluster Bootstrap

**Files:**
- Create: `worker/start_ray_worker.sh`
- Create: `scripts/deploy_workers.sh`

This task has no unit tests — it is infrastructure. Verification is via `ray status`.

- [ ] **Step 1: Create worker/start_ray_worker.sh**

```bash
#!/usr/bin/env bash
# Run on each worker node to join the Ray cluster.
# Usage: bash start_ray_worker.sh <head_node_ip>
set -euo pipefail

HEAD_IP="${1:-10.208.211.62}"
RAY_PORT=6379
DASHBOARD_PORT=8265

export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"
export RAY_grpc_enable_http_proxy=0

VENV="/mnt/d/VirtualEnvironments/llm-platform"
source "${VENV}/bin/activate"

# Stop any existing Ray process
ray stop --force 2>/dev/null || true

ray start \
  --address="${HEAD_IP}:${RAY_PORT}" \
  --num-gpus=1 \
  --num-cpus=6 \
  --block
```

- [ ] **Step 2: Create scripts/deploy_workers.sh**

```bash
#!/usr/bin/env bash
# Deploy worker files and start Ray on all worker nodes.
set -euo pipefail

HEAD_IP="10.208.211.62"
WORKERS=("10.208.211.54" "10.208.211.59" "10.208.211.64")
SSH_PASS="Ongc@1234"
REMOTE_DIR="/home/administrator/llm-worker"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

for WORKER_IP in "${WORKERS[@]}"; do
  echo "=== Deploying to ${WORKER_IP} ==="

  # Create remote dir
  sshpass -p "${SSH_PASS}" ssh -o StrictHostKeyChecking=no \
    "administrator@${WORKER_IP}" "mkdir -p ${REMOTE_DIR}/worker"

  # Copy worker files
  sshpass -p "${SSH_PASS}" scp -o StrictHostKeyChecking=no \
    "${PROJECT_DIR}/worker/start_ray_worker.sh" \
    "${PROJECT_DIR}/worker/ray_worker.py" \
    "${PROJECT_DIR}/worker/llama_process.py" \
    "administrator@${WORKER_IP}:${REMOTE_DIR}/worker/"

  # Start Ray worker in background via nohup
  sshpass -p "${SSH_PASS}" ssh -o StrictHostKeyChecking=no \
    "administrator@${WORKER_IP}" \
    "nohup bash ${REMOTE_DIR}/worker/start_ray_worker.sh ${HEAD_IP} > ${REMOTE_DIR}/ray_worker.log 2>&1 &"

  echo "  Started Ray worker on ${WORKER_IP}"
done

echo "=== All workers started. Check status with: ray status ==="
```

- [ ] **Step 3: Start Ray head node on WS-11**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'
  export NO_PROXY=\$no_proxy
  export RAY_grpc_enable_http_proxy=0
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
  ray stop --force 2>/dev/null || true
  ray start --head --num-gpus=1 --num-cpus=6 --port=6379 --dashboard-host=0.0.0.0 --dashboard-port=8265 --block &
  sleep 5
  ray status
"
```

Expected output includes: `1 node(s) with resources` and `Resources: ... GPU: 1.0`

- [ ] **Step 4: Deploy and start workers**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  bash scripts/deploy_workers.sh
"
```

- [ ] **Step 5: Verify all 4 nodes in cluster**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
  ray status
"
```

Expected: `4 node(s)` with total `GPU: 4.0`

- [ ] **Step 6: Commit**

```bash
git add worker/start_ray_worker.sh scripts/deploy_workers.sh
git commit -m "feat: ray cluster bootstrap scripts for head and workers"
```

---

### Task 6: llama.cpp Worker Actor (Ray Serve)

**Files:**
- Create: `worker/llama_process.py`
- Create: `worker/ray_worker.py`

- [ ] **Step 1: Create worker/llama_process.py**

```python
"""Manages a llama-server subprocess and provides async HTTP proxy."""
import asyncio
import os
import subprocess
import time
from typing import Optional

import httpx


LLAMA_BIN = os.path.expanduser("~/llama.cpp/build/bin/llama-server")
MODEL_PATH = os.environ.get(
    "LLAMA_MODEL_PATH",
    "/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
)
MMPROJ_PATH = os.environ.get(
    "LLAMA_MMPROJ_PATH",
    "/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/mmproj-F16.gguf",
)


class LlamaServerProcess:
    """Wraps llama-server subprocess. Start once per Ray actor lifetime."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self._proc: Optional[subprocess.Popen] = None
        # httpx client bypasses corporate proxy for localhost
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0),
            proxies={},  # no proxy for internal
        )

    def start(self) -> None:
        cmd = [
            LLAMA_BIN,
            "-m", MODEL_PATH,
            "--mmproj", MMPROJ_PATH,
            "-ngl", str(os.environ.get("LLAMA_NGL", "999")),
            "-c", str(os.environ.get("LLAMA_CONTEXT", "65536")),
            "--host", self.host,
            "--port", str(self.port),
            "--parallel", str(os.environ.get("LLAMA_PARALLEL", "2")),
            "--no-context-shift",
            "--flash-attn", "on",
            "--cache-type-k", "q8_0",
            "--cache-type-v", "q8_0",
            "--cont-batching",
            "--spec-type", "draft-mtp",
            "--spec-draft-n-max", "4",
        ]
        env = os.environ.copy()
        env["no_proxy"] = "localhost,127.0.0.1"
        env["NO_PROXY"] = "localhost,127.0.0.1"
        self._proc = subprocess.Popen(cmd, env=env)
        self._wait_for_ready()

    def _wait_for_ready(self, timeout: int = 120) -> None:
        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://{self.host}:{self.port}/health")
                return
            except Exception:
                time.sleep(2)
        raise RuntimeError(f"llama-server did not become ready within {timeout}s")

    async def chat_completions(self, payload: dict) -> httpx.Response:
        return await self._client.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
        )

    async def stream_chat_completions(self, payload: dict):
        async with self._client.stream(
            "POST", f"{self.base_url}/v1/chat/completions", json=payload
        ) as response:
            async for chunk in response.aiter_bytes():
                yield chunk

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        asyncio.get_event_loop().run_until_complete(self._client.aclose())
```

- [ ] **Step 2: Create worker/ray_worker.py**

```python
"""Ray Serve deployment wrapping llama-server subprocess."""
import os
import socket
from typing import Any

import ray
from ray import serve
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from worker.llama_process import LlamaServerProcess


@serve.deployment(
    ray_actor_options={"num_gpus": 1, "num_cpus": 2},
    num_replicas=1,
    max_ongoing_requests=4,
)
class LlamaCppWorker:
    def __init__(self):
        self._node_ip = socket.gethostbyname(socket.gethostname())
        self._llama = LlamaServerProcess(host="127.0.0.1", port=8080)
        self._llama.start()

    async def __call__(self, request: Request):
        payload = await request.json()
        streaming = payload.get("stream", False)

        if streaming:
            async def _gen():
                async for chunk in self._llama.stream_chat_completions(payload):
                    yield chunk

            return StreamingResponse(
                _gen(),
                media_type="text/event-stream",
                headers={
                    "X-Worker-Node": self._node_ip,
                    "Cache-Control": "no-cache",
                },
            )

        response = await self._llama.chat_completions(payload)
        return JSONResponse(
            content=response.json(),
            status_code=response.status_code,
            headers={"X-Worker-Node": self._node_ip},
        )


def deploy():
    """Deploy LlamaCppWorker to the connected Ray cluster."""
    serve.start(detached=True, http_options={"host": "0.0.0.0", "port": 8001})
    serve.run(LlamaCppWorker.bind(), name="llama-worker", route_prefix="/")


if __name__ == "__main__":
    ray.init(address=os.environ.get("RAY_ADDRESS", "ray://10.208.211.62:10001"))
    deploy()
    print("LlamaCppWorker deployed. Serving at :8001")
    serve.run(LlamaCppWorker.bind())
```

- [ ] **Step 3: Deploy Ray Serve worker**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
  export RAY_ADDRESS=ray://10.208.211.62:10001
  export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'
  export NO_PROXY=\$no_proxy
  export RAY_grpc_enable_http_proxy=0
  python worker/ray_worker.py
"
```

Expected: `LlamaCppWorker deployed. Serving at :8001`

- [ ] **Step 4: Smoke test Ray Serve endpoint**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  curl --noproxy '*' -s http://10.208.211.62:8001/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":5}' \
    | python3 -m json.tool
"
```

Expected: JSON response with `choices[0].message.content`

- [ ] **Step 5: Commit**

```bash
git add worker/llama_process.py worker/ray_worker.py
git commit -m "feat: ray serve deployment wrapping llama-server subprocess"
```

---

### Task 7: Gateway → Ray Routing Client

**Files:**
- Create: `gateway/ray_client.py`
- Test: `tests/test_chat.py`

- [ ] **Step 1: Write failing chat route test**

Create `tests/test_chat.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport, Response


@pytest.fixture
def app_with_mock_ray():
    """App with Ray client mocked out."""
    import gateway.ray_client as rc
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }
    mock_response.headers = {}

    with patch.object(rc, "submit_inference", new=AsyncMock(return_value=mock_response)):
        from gateway.main import create_app
        yield create_app()


@pytest.mark.asyncio
async def test_chat_completions_requires_auth(app_with_mock_ray):
    async with AsyncClient(transport=ASGITransport(app=app_with_mock_ray), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_chat_completions_with_valid_key(app_with_mock_ray, session):
    from gateway.auth.service import generate_api_key, hash_api_key
    from gateway.models import User, APIKey

    user = User(username="chatuser", department="ENG", email="chat@ongc.co.in", is_active=True)
    session.add(user)
    await session.flush()
    raw = generate_api_key()
    session.add(APIKey(user_id=user.id, key_hash=hash_api_key(raw), key_prefix=raw[:12], label="t", is_active=True))
    await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app_with_mock_ray), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": raw},
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "pong"
```

- [ ] **Step 2: Run to verify failure**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_chat.py -v 2>&1 | head -20
"
```

Expected: `ModuleNotFoundError: No module named 'gateway.ray_client'`

- [ ] **Step 3: Create gateway/ray_client.py**

```python
"""Thin async client that forwards inference requests to Ray Serve."""
import os
import httpx
from gateway.config import settings

# Ray Serve HTTP endpoint — served on head node port 8001
_RAY_SERVE_URL = os.environ.get("RAY_SERVE_URL", "http://10.208.211.62:8001")

# httpx client with proxy bypass for internal cluster traffic
_client = httpx.AsyncClient(
    base_url=_RAY_SERVE_URL,
    timeout=httpx.Timeout(300.0),
    proxies={},  # bypass all proxies for internal calls
)


async def submit_inference(path: str, payload: dict) -> httpx.Response:
    """POST payload to Ray Serve and return response."""
    return await _client.post(path, json=payload)


async def stream_inference(path: str, payload: dict):
    """Stream response from Ray Serve, yielding SSE bytes."""
    async with _client.stream("POST", path, json=payload) as response:
        async for chunk in response.aiter_bytes():
            yield chunk
```

- [ ] **Step 4: Create gateway/routers/chat.py**

```python
import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.auth.middleware import require_api_key
from gateway.models import APIKey, User
import gateway.ray_client as ray_client

router = APIRouter(prefix="/v1", tags=["inference"])


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    auth: tuple[APIKey, User] = Depends(require_api_key),
):
    api_key, user = auth
    payload = await request.json()
    streaming = payload.get("stream", False)

    if streaming:
        async def _stream():
            async for chunk in ray_client.stream_inference("/v1/chat/completions", payload):
                yield chunk

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    response = await ray_client.submit_inference("/v1/chat/completions", payload)
    return JSONResponse(content=response.json(), status_code=response.status_code)
```

- [ ] **Step 5: Register chat router in main.py**

Edit `gateway/main.py` — add after health_router import:

```python
from gateway.routers.chat import router as chat_router
```

And after `app.include_router(health_router)`:

```python
app.include_router(chat_router)
```

- [ ] **Step 6: Run chat tests**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_chat.py -v
"
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add gateway/ray_client.py gateway/routers/chat.py gateway/main.py tests/test_chat.py
git commit -m "feat: chat completions endpoint with ray serve routing"
```

---

### Task 8: Request Logging

**Files:**
- Create: `gateway/logging_/__init__.py`
- Create: `gateway/logging_/request_logger.py`
- Modify: `gateway/routers/chat.py`
- Test: `tests/test_logging.py`

- [ ] **Step 1: Write failing logging test**

Create `tests/test_logging.py`:

```python
import pytest
from datetime import datetime, timezone
from gateway.logging_.request_logger import log_request
from gateway.models import RequestLog


@pytest.mark.asyncio
async def test_log_request_writes_row(session):
    await log_request(
        session=session,
        user_id=1,
        api_key_prefix="sk-ongc-te",
        model="qwen-35b",
        node_ip="10.208.211.54",
        prompt_tokens=10,
        completion_tokens=50,
        latency_ms=1200,
        queue_ms=30,
        status_code=200,
        streaming=False,
    )

    from sqlalchemy import select
    result = await session.execute(select(RequestLog))
    logs = result.scalars().all()
    assert len(logs) == 1
    assert logs[0].prompt_tokens == 10
    assert logs[0].completion_tokens == 50
    assert logs[0].status_code == 200


@pytest.mark.asyncio
async def test_log_request_with_error(session):
    await log_request(
        session=session,
        user_id=None,
        api_key_prefix=None,
        model="qwen-35b",
        node_ip=None,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=50,
        queue_ms=0,
        status_code=500,
        streaming=False,
        error_message="upstream timeout",
    )

    from sqlalchemy import select
    result = await session.execute(select(RequestLog).where(RequestLog.status_code == 500))
    logs = result.scalars().all()
    assert len(logs) == 1
    assert logs[0].error_message == "upstream timeout"
```

- [ ] **Step 2: Run to verify failure**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_logging.py -v 2>&1 | head -15
"
```

Expected: `ModuleNotFoundError: No module named 'gateway.logging_'`

- [ ] **Step 3: Create gateway/logging_/__init__.py**

```python
```
(empty)

- [ ] **Step 4: Create gateway/logging_/request_logger.py**

```python
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from gateway.models import RequestLog


async def log_request(
    session: AsyncSession,
    user_id: Optional[int],
    api_key_prefix: Optional[str],
    model: str,
    node_ip: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    queue_ms: int,
    status_code: int,
    streaming: bool,
    error_message: Optional[str] = None,
) -> None:
    log = RequestLog(
        user_id=user_id,
        api_key_prefix=api_key_prefix,
        model=model,
        node_ip=node_ip,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        queue_ms=queue_ms,
        status_code=status_code,
        streaming=streaming,
        error_message=error_message,
    )
    session.add(log)
    await session.commit()
```

- [ ] **Step 5: Run logging tests**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_logging.py -v
"
```

Expected: both tests PASS.

- [ ] **Step 6: Wire logging into chat endpoint**

Replace `gateway/routers/chat.py` with:

```python
import time
import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.middleware import require_api_key
from gateway.database import get_db
from gateway.logging_.request_logger import log_request
from gateway.models import APIKey, User
import gateway.ray_client as ray_client

router = APIRouter(prefix="/v1", tags=["inference"])


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    auth: tuple[APIKey, User] = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
):
    api_key, user = auth
    payload = await request.json()
    model = payload.get("model", "unknown")
    streaming = payload.get("stream", False)
    t_start = time.monotonic()

    if streaming:
        async def _stream():
            async for chunk in ray_client.stream_inference("/v1/chat/completions", payload):
                yield chunk

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        response = await ray_client.submit_inference("/v1/chat/completions", payload)
        latency_ms = int((time.monotonic() - t_start) * 1000)
        node_ip = response.headers.get("X-Worker-Node")

        body = response.json()
        usage = body.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        await log_request(
            session=session,
            user_id=user.id,
            api_key_prefix=api_key.key_prefix,
            model=model,
            node_ip=node_ip,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            queue_ms=0,
            status_code=response.status_code,
            streaming=False,
        )
        return JSONResponse(content=body, status_code=response.status_code)

    except Exception as exc:
        latency_ms = int((time.monotonic() - t_start) * 1000)
        await log_request(
            session=session,
            user_id=user.id,
            api_key_prefix=api_key.key_prefix,
            model=model,
            node_ip=None,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=latency_ms,
            queue_ms=0,
            status_code=500,
            streaming=False,
            error_message=str(exc),
        )
        return JSONResponse({"error": "upstream inference failed"}, status_code=500)
```

- [ ] **Step 7: Run full test suite**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/ -v
"
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add gateway/logging_/ gateway/routers/chat.py tests/test_logging.py
git commit -m "feat: request logging middleware writes to db after each inference call"
```

---

### Task 9: Admin Endpoints

**Files:**
- Create: `gateway/routers/admin.py`
- Create: `scripts/generate_api_key.py`
- Test: `tests/test_admin.py`

- [ ] **Step 1: Write failing admin tests**

Create `tests/test_admin.py`:

```python
import pytest
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport


@pytest.fixture
def app():
    from gateway.main import create_app
    return create_app()


@pytest.mark.asyncio
async def test_admin_create_user_requires_admin_secret(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/admin/users", json={"username": "alice", "email": "a@ongc.co.in", "department": "IT"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_create_user_with_secret(app):
    from gateway.config import settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/admin/users",
            headers={"X-Admin-Secret": settings.admin_secret},
            json={"username": "bob", "email": "bob@ongc.co.in", "department": "ENG"},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "bob"
    assert "id" in data


@pytest.mark.asyncio
async def test_admin_create_api_key_for_user(app):
    from gateway.config import settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create user first
        user_resp = await client.post(
            "/admin/users",
            headers={"X-Admin-Secret": settings.admin_secret},
            json={"username": "charlie", "email": "charlie@ongc.co.in", "department": "GEO"},
        )
        user_id = user_resp.json()["id"]

        # Create API key
        key_resp = await client.post(
            f"/admin/users/{user_id}/keys",
            headers={"X-Admin-Secret": settings.admin_secret},
            json={"label": "workstation-key"},
        )
    assert key_resp.status_code == 201
    data = key_resp.json()
    assert data["key"].startswith("sk-ongc-")
    assert data["prefix"] == data["key"][:12]
```

- [ ] **Step 2: Run to verify failure**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_admin.py -v 2>&1 | head -15
"
```

Expected: 404 (routes not registered yet).

- [ ] **Step 3: Create gateway/routers/admin.py**

```python
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from gateway.auth.service import generate_api_key, hash_api_key
from gateway.config import settings
from gateway.database import get_db
from gateway.models import APIKey, User

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin secret")


class CreateUserRequest(BaseModel):
    username: str
    email: str
    department: str
    is_admin: bool = False


class CreateKeyRequest(BaseModel):
    label: str = "default"


@router.post("/users", status_code=201, dependencies=[Depends(require_admin)])
async def create_user(body: CreateUserRequest, session: AsyncSession = Depends(get_db)):
    user = User(
        username=body.username,
        email=body.email,
        department=body.department,
        is_active=True,
        is_admin=body.is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {"id": user.id, "username": user.username, "email": user.email, "department": user.department}


@router.post("/users/{user_id}/keys", status_code=201, dependencies=[Depends(require_admin)])
async def create_api_key(user_id: int, body: CreateKeyRequest, session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    raw = generate_api_key()
    api_key = APIKey(
        user_id=user.id,
        key_hash=hash_api_key(raw),
        key_prefix=raw[:12],
        label=body.label,
        is_active=True,
    )
    session.add(api_key)
    await session.commit()
    return {"key": raw, "prefix": raw[:12], "label": body.label, "user_id": user_id}


@router.get("/users", dependencies=[Depends(require_admin)])
async def list_users(session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(User).where(User.is_active == True))
    users = result.scalars().all()
    return [{"id": u.id, "username": u.username, "department": u.department, "email": u.email} for u in users]


@router.get("/metrics/summary", dependencies=[Depends(require_admin)])
async def metrics_summary(session: AsyncSession = Depends(get_db)):
    from sqlalchemy import func
    from gateway.models import RequestLog
    result = await session.execute(
        select(
            func.count(RequestLog.id).label("total_requests"),
            func.sum(RequestLog.prompt_tokens).label("total_prompt_tokens"),
            func.sum(RequestLog.completion_tokens).label("total_completion_tokens"),
            func.avg(RequestLog.latency_ms).label("avg_latency_ms"),
        )
    )
    row = result.one()
    return {
        "total_requests": row.total_requests or 0,
        "total_prompt_tokens": row.total_prompt_tokens or 0,
        "total_completion_tokens": row.total_completion_tokens or 0,
        "avg_latency_ms": round(row.avg_latency_ms or 0, 1),
    }
```

- [ ] **Step 4: Register admin router in main.py**

Add to `gateway/main.py`:

```python
from gateway.routers.admin import router as admin_router
```

And:

```python
app.include_router(admin_router)
```

- [ ] **Step 5: Run admin tests**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/test_admin.py -v
"
```

Expected: all tests PASS.

- [ ] **Step 6: Create scripts/generate_api_key.py**

```python
"""CLI utility: create API key for an existing user.
Usage: python scripts/generate_api_key.py <username> [label]
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from gateway.config import settings
from gateway.models import APIKey, User
from gateway.auth.service import generate_api_key, hash_api_key


async def main(username: str, label: str = "cli-generated"):
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if not user:
            print(f"User '{username}' not found.")
            return

        raw = generate_api_key()
        api_key = APIKey(
            user_id=user.id,
            key_hash=hash_api_key(raw),
            key_prefix=raw[:12],
            label=label,
            is_active=True,
        )
        session.add(api_key)
        await session.commit()
        print(f"API key for {username}: {raw}")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/generate_api_key.py <username> [label]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "cli-generated"))
```

- [ ] **Step 7: Run full test suite**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service &&
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate &&
  NO_PROXY='*' pytest tests/ -v
"
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add gateway/routers/admin.py gateway/main.py scripts/generate_api_key.py tests/test_admin.py
git commit -m "feat: admin endpoints for user and api key management"
```

---

## Phase 2: Observability + Redis

---

### Task 10: Prometheus Metrics

**Files:**
- Create: `gateway/metrics.py`
- Create: `gateway/routers/metrics_router.py`
- Modify: `gateway/routers/chat.py`

- [ ] **Step 1: Create gateway/metrics.py**

```python
from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry

REGISTRY = CollectorRegistry()

REQUEST_COUNT = Counter(
    "llm_requests_total",
    "Total inference requests",
    ["model", "status_code", "streaming"],
    registry=REGISTRY,
)

REQUEST_LATENCY = Histogram(
    "llm_request_latency_ms",
    "Inference request latency in milliseconds",
    ["model"],
    buckets=[100, 500, 1000, 2000, 5000, 10000, 30000, 60000],
    registry=REGISTRY,
)

PROMPT_TOKENS = Counter(
    "llm_prompt_tokens_total",
    "Total prompt tokens processed",
    ["model"],
    registry=REGISTRY,
)

COMPLETION_TOKENS = Counter(
    "llm_completion_tokens_total",
    "Total completion tokens generated",
    ["model"],
    registry=REGISTRY,
)

ACTIVE_REQUESTS = Gauge(
    "llm_active_requests",
    "Currently active inference requests",
    registry=REGISTRY,
)
```

- [ ] **Step 2: Create gateway/routers/metrics_router.py**

```python
from fastapi import APIRouter, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from gateway.metrics import REGISTRY

router = APIRouter(tags=["infrastructure"])


@router.get("/metrics")
async def prometheus_metrics():
    data = generate_latest(REGISTRY)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
```

- [ ] **Step 3: Wire metrics into chat.py**

In `gateway/routers/chat.py`, import and update the non-streaming path:

```python
from gateway.metrics import REQUEST_COUNT, REQUEST_LATENCY, PROMPT_TOKENS, COMPLETION_TOKENS, ACTIVE_REQUESTS
```

Inside the `try` block in `chat_completions`, add after computing `latency_ms`:

```python
REQUEST_COUNT.labels(model=model, status_code=str(response.status_code), streaming="false").inc()
REQUEST_LATENCY.labels(model=model).observe(latency_ms)
PROMPT_TOKENS.labels(model=model).inc(prompt_tokens)
COMPLETION_TOKENS.labels(model=model).inc(completion_tokens)
```

And in the `except` block:

```python
REQUEST_COUNT.labels(model=model, status_code="500", streaming="false").inc()
```

And wrap the main body with:

```python
ACTIVE_REQUESTS.inc()
try:
    ...  # existing code
finally:
    ACTIVE_REQUESTS.dec()
```

- [ ] **Step 4: Register metrics router in main.py**

```python
from gateway.routers.metrics_router import router as metrics_router
# ...
app.include_router(metrics_router)
```

- [ ] **Step 5: Smoke test metrics endpoint**

Start the gateway:

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
  uvicorn gateway.main:app --host 0.0.0.0 --port 8000 &
  sleep 2
  curl --noproxy '*' -s http://localhost:8000/metrics | grep llm_
"
```

Expected: lines like `# HELP llm_requests_total Total inference requests`

- [ ] **Step 6: Commit**

```bash
git add gateway/metrics.py gateway/routers/metrics_router.py gateway/routers/chat.py gateway/main.py
git commit -m "feat: prometheus metrics for request count, latency, token usage"
```

---

### Task 11: Redis Rate Limiting

**Files:**
- Create: `gateway/rate_limiter.py`
- Modify: `gateway/routers/chat.py`

- [ ] **Step 1: Create gateway/rate_limiter.py**

```python
"""Sliding window rate limiter using Redis."""
import time
from typing import Optional

import redis.asyncio as aioredis
from fastapi import HTTPException, status

from gateway.config import settings

_redis: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


DEFAULT_RATE_LIMIT = 60   # requests per window
DEFAULT_WINDOW_SEC = 60   # 1-minute window


async def check_rate_limit(
    user_id: int,
    limit: int = DEFAULT_RATE_LIMIT,
    window_sec: int = DEFAULT_WINDOW_SEC,
) -> None:
    """Raises 429 if user exceeds rate limit. Uses Redis sorted set sliding window."""
    r = get_redis()
    key = f"rl:user:{user_id}"
    now = time.time()
    window_start = now - window_sec

    pipe = r.pipeline()
    pipe.zremrangebyscore(key, "-inf", window_start)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, window_sec + 1)
    results = await pipe.execute()

    count = results[2]
    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: {limit} requests per {window_sec}s",
            headers={"Retry-After": str(window_sec)},
        )
```

- [ ] **Step 2: Wire rate limiter into chat.py**

In `chat_completions`, before calling Ray, add:

```python
from gateway.rate_limiter import check_rate_limit

# Inside chat_completions, after auth:
try:
    await check_rate_limit(user.id)
except Exception:
    pass  # Redis unavailable — degrade gracefully, don't block inference
```

Replace the silent `pass` with actual raise if Redis is critical:

```python
from redis.exceptions import RedisError

try:
    await check_rate_limit(user.id)
except HTTPException:
    raise  # propagate 429
except RedisError:
    pass   # Redis down — allow through (degrade gracefully)
```

- [ ] **Step 3: Start Redis locally on WS-11 and smoke test**

Redis runs via Docker (see Task 13). For now, start directly:

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  docker run -d --name redis -p 6379:6379 redis:7-alpine
  sleep 2
  redis-cli ping
"
```

Expected: `PONG`

- [ ] **Step 4: Commit**

```bash
git add gateway/rate_limiter.py gateway/routers/chat.py
git commit -m "feat: redis sliding window rate limiting per user"
```

---

### Task 12: Docker Compose (Gateway Stack)

**Files:**
- Create: `docker/gateway/Dockerfile`
- Create: `docker-compose.yml`
- Create: `infra/prometheus/prometheus.yml`

- [ ] **Step 1: Create docker/gateway/Dockerfile**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in" \
    NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"

COPY requirements.txt .
RUN pip install --no-cache-dir --proxy http://10.205.122.201:8080 -r requirements.txt

COPY gateway/ ./gateway/

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Create infra/prometheus/prometheus.yml**

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "llm-gateway"
    static_configs:
      - targets: ["gateway:8000"]
    metrics_path: /metrics

  - job_name: "node-exporter"
    static_configs:
      - targets:
          - "10.208.211.62:9100"
          - "10.208.211.54:9100"
          - "10.208.211.59:9100"
          - "10.208.211.64:9100"
```

- [ ] **Step 3: Create docker-compose.yml**

```yaml
version: "3.9"

networks:
  llm-net:
    driver: bridge

services:
  postgres:
    image: postgres:16-alpine
    container_name: llm-postgres
    environment:
      POSTGRES_USER: llm
      POSTGRES_PASSWORD: llm
      POSTGRES_DB: llm_platform
    volumes:
      - postgres-data:/var/lib/postgresql/data
    networks:
      - llm-net
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U llm"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: llm-redis
    networks:
      - llm-net
    restart: unless-stopped
    command: redis-server --save 60 1

  gateway:
    build:
      context: .
      dockerfile: docker/gateway/Dockerfile
    container_name: llm-gateway
    environment:
      DATABASE_URL: postgresql+asyncpg://llm:llm@postgres:5432/llm_platform
      REDIS_URL: redis://redis:6379/0
      RAY_SERVE_URL: http://10.208.211.62:8001
      ADMIN_SECRET: "${ADMIN_SECRET:-changeme}"
      NO_PROXY: "localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in,postgres,redis,10.208.211.62"
      no_proxy: "localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in,postgres,redis,10.208.211.62"
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    networks:
      - llm-net
    restart: unless-stopped

  prometheus:
    image: prom/prometheus:latest
    container_name: llm-prometheus
    volumes:
      - ./infra/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.retention.time=30d"
    ports:
      - "9090:9090"
    networks:
      - llm-net
    restart: unless-stopped

  grafana:
    image: grafana/grafana:latest
    container_name: llm-grafana
    environment:
      GF_SECURITY_ADMIN_PASSWORD: "${GRAFANA_PASSWORD:-ongc1234}"
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - grafana-data:/var/lib/grafana
      - ./infra/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./infra/grafana/dashboards:/var/lib/grafana/dashboards:ro
    ports:
      - "3000:3000"
    depends_on:
      - prometheus
    networks:
      - llm-net
    restart: unless-stopped

volumes:
  postgres-data:
  prometheus-data:
  grafana-data:
```

- [ ] **Step 4: Build and start stack**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  docker compose up -d --build
  sleep 10
  docker compose ps
"
```

Expected: all 5 services `running` or `healthy`.

- [ ] **Step 5: Run DB init against containerized Postgres**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
  DATABASE_URL='postgresql+asyncpg://llm:llm@localhost:5432/llm_platform' python scripts/init_db.py
"
```

Expected: outputs admin API key.

- [ ] **Step 6: End-to-end smoke test**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  API_KEY='<key-from-above>'
  curl --noproxy '*' -s http://localhost:8000/health
  curl --noproxy '*' -s http://localhost:8000/v1/chat/completions \
    -H 'Authorization: '\$API_KEY \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in 5 words\"}],\"max_tokens\":20}'
"
```

Expected: `{"status":"ok","service":"llm-inference-gateway"}` then JSON chat response.

- [ ] **Step 7: Commit**

```bash
git add docker/ docker-compose.yml infra/prometheus/prometheus.yml
git commit -m "feat: docker compose stack for gateway, postgres, redis, prometheus, grafana"
```

---

### Task 13: Grafana Dashboard

**Files:**
- Create: `infra/grafana/provisioning/datasources/prometheus.yml`
- Create: `infra/grafana/provisioning/dashboards/dashboard.yml`
- Create: `infra/grafana/dashboards/llm_platform.json`

- [ ] **Step 1: Create Grafana datasource provisioning**

Create `infra/grafana/provisioning/datasources/prometheus.yml`:

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
```

- [ ] **Step 2: Create dashboard provisioning config**

Create `infra/grafana/provisioning/dashboards/dashboard.yml`:

```yaml
apiVersion: 1

providers:
  - name: LLM Platform
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/dashboards
```

- [ ] **Step 3: Create dashboard JSON**

Create `infra/grafana/dashboards/llm_platform.json`:

```json
{
  "title": "ONGC LLM Inference Platform",
  "uid": "llm-platform",
  "schemaVersion": 37,
  "version": 1,
  "refresh": "30s",
  "panels": [
    {
      "id": 1,
      "title": "Request Rate (req/min)",
      "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 0, "y": 0},
      "targets": [{"expr": "rate(llm_requests_total[1m]) * 60", "legendFormat": "req/min"}]
    },
    {
      "id": 2,
      "title": "P95 Latency (ms)",
      "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 6, "y": 0},
      "targets": [{"expr": "histogram_quantile(0.95, rate(llm_request_latency_ms_bucket[5m]))", "legendFormat": "p95"}]
    },
    {
      "id": 3,
      "title": "Active Requests",
      "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 12, "y": 0},
      "targets": [{"expr": "llm_active_requests", "legendFormat": "active"}]
    },
    {
      "id": 4,
      "title": "Tokens/sec",
      "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 18, "y": 0},
      "targets": [{"expr": "rate(llm_completion_tokens_total[1m])", "legendFormat": "tokens/sec"}]
    },
    {
      "id": 5,
      "title": "Request Latency Distribution",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 4},
      "targets": [
        {"expr": "histogram_quantile(0.50, rate(llm_request_latency_ms_bucket[5m]))", "legendFormat": "p50"},
        {"expr": "histogram_quantile(0.95, rate(llm_request_latency_ms_bucket[5m]))", "legendFormat": "p95"},
        {"expr": "histogram_quantile(0.99, rate(llm_request_latency_ms_bucket[5m]))", "legendFormat": "p99"}
      ]
    },
    {
      "id": 6,
      "title": "Request Count by Status",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 4},
      "targets": [
        {"expr": "rate(llm_requests_total{status_code='200'}[5m])", "legendFormat": "200 OK"},
        {"expr": "rate(llm_requests_total{status_code='500'}[5m])", "legendFormat": "500 Error"},
        {"expr": "rate(llm_requests_total{status_code='429'}[5m])", "legendFormat": "429 Rate Limited"}
      ]
    },
    {
      "id": 7,
      "title": "Token Throughput",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 12},
      "targets": [
        {"expr": "rate(llm_prompt_tokens_total[5m])", "legendFormat": "prompt tokens/s"},
        {"expr": "rate(llm_completion_tokens_total[5m])", "legendFormat": "completion tokens/s"}
      ]
    }
  ]
}
```

- [ ] **Step 4: Restart Grafana and verify dashboard loads**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  docker compose restart grafana
  sleep 5
  curl --noproxy '*' -s -u admin:ongc1234 http://localhost:3000/api/dashboards/uid/llm-platform | python3 -m json.tool | grep title
"
```

Expected: `"title": "ONGC LLM Inference Platform"`

- [ ] **Step 5: Commit**

```bash
git add infra/grafana/
git commit -m "feat: grafana dashboard with latency, throughput, and request metrics"
```

---

### Task 14: NGINX Reverse Proxy

**Files:**
- Create: `infra/nginx/nginx.conf`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Create infra/nginx/nginx.conf**

```nginx
upstream llm_gateway {
    server gateway:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name _;

    # SSE / streaming requires disabling proxy buffering
    proxy_buffering off;
    proxy_cache off;

    location /v1/ {
        proxy_pass http://llm_gateway;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Required for SSE streaming
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        chunked_transfer_encoding on;
    }

    location /health {
        proxy_pass http://llm_gateway;
        proxy_set_header Host $host;
    }

    location /admin/ {
        # Restrict admin to internal subnet only
        allow 10.208.211.0/24;
        deny all;

        proxy_pass http://llm_gateway;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /metrics {
        # Metrics endpoint internal only
        allow 10.208.211.0/24;
        deny all;

        proxy_pass http://llm_gateway;
        proxy_set_header Host $host;
    }
}
```

- [ ] **Step 2: Add nginx service to docker-compose.yml**

In `docker-compose.yml`, add under `services`:

```yaml
  nginx:
    image: nginx:alpine
    container_name: llm-nginx
    volumes:
      - ./infra/nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    ports:
      - "80:80"
    depends_on:
      - gateway
    networks:
      - llm-net
    restart: unless-stopped
```

- [ ] **Step 3: Restart and test via NGINX**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  docker compose up -d nginx
  sleep 3
  curl --noproxy '*' -s http://10.208.211.62/health
"
```

Expected: `{"status":"ok","service":"llm-inference-gateway"}`

- [ ] **Step 4: Test admin endpoint blocked from outside subnet**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  curl --noproxy '*' -s -o /dev/null -w '%{http_code}' http://10.208.211.62/admin/users
"
```

Expected: `403` (NGINX denies from outside 10.208.211.0/24) or `200` (if from within subnet — both correct).

- [ ] **Step 5: Commit**

```bash
git add infra/nginx/nginx.conf docker-compose.yml
git commit -m "feat: nginx reverse proxy with streaming support and admin subnet restriction"
```

---

### Task 15: Integration Smoke Test + Final Verification

This task verifies the complete stack end-to-end.

- [ ] **Step 1: Run full unit test suite**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
  NO_PROXY='*' pytest tests/ -v --tb=short
"
```

Expected: all tests PASS, zero failures.

- [ ] **Step 2: Verify Ray cluster health**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
  ray status
"
```

Expected: 4 nodes, 4 GPUs total.

- [ ] **Step 3: Verify all Docker services healthy**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  cd /home/administrator/projects/llm-inference-service
  docker compose ps
"
```

Expected: all services `running`.

- [ ] **Step 4: Create a user + API key via admin API**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  # Create user
  USER_RESP=\$(curl --noproxy '*' -s -X POST http://10.208.211.62/admin/users \
    -H 'X-Admin-Secret: changeme' \
    -H 'Content-Type: application/json' \
    -d '{\"username\":\"testeng\",\"email\":\"eng@ongc.co.in\",\"department\":\"ENG\"}')
  echo \"User: \$USER_RESP\"
  USER_ID=\$(echo \$USER_RESP | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"id\"])')

  # Create API key
  KEY_RESP=\$(curl --noproxy '*' -s -X POST http://10.208.211.62/admin/users/\$USER_ID/keys \
    -H 'X-Admin-Secret: changeme' \
    -H 'Content-Type: application/json' \
    -d '{\"label\":\"test-key\"}')
  echo \"Key: \$KEY_RESP\"
"
```

- [ ] **Step 5: Send inference request through NGINX**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  API_KEY='<key-from-above>'
  curl --noproxy '*' -s http://10.208.211.62/v1/chat/completions \
    -H 'Authorization: '\$API_KEY \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"What is 2+2?\"}],\"max_tokens\":20}' \
    | python3 -m json.tool
"
```

Expected: JSON with `choices[0].message.content` containing the answer.

- [ ] **Step 6: Verify request logged in DB**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  docker exec llm-postgres psql -U llm -d llm_platform -c 'SELECT user_id, model, prompt_tokens, completion_tokens, latency_ms, status_code FROM request_logs ORDER BY created_at DESC LIMIT 5;'
"
```

Expected: rows with filled-in token counts and latency.

- [ ] **Step 7: Verify Prometheus scraping**

```bash
wsl -d Ubuntu-24.04 --exec bash -c "
  curl --noproxy '*' -s 'http://localhost:9090/api/v1/query?query=llm_requests_total' | python3 -m json.tool | grep value
"
```

Expected: metric with non-zero value.

- [ ] **Step 8: Final commit + tag**

```bash
git add .
git commit -m "feat: complete phase 1+2 integration — distributed llm inference platform"
git tag v0.1.0
```

---

## Known Constraints and Notes

| Concern | Handling |
|---------|----------|
| Corporate proxy `10.205.122.201:8080` | Set `no_proxy`/`NO_PROXY` everywhere; `RAY_grpc_enable_http_proxy=0` for Ray |
| llama.cpp already running on WS-11 | Ray Serve worker starts its own process; stop existing before deploying |
| WSL2 path for models | `/mnt/d/Models/` — same path on all nodes via `D:` drive |
| SSH password auth | Use `sshpass` in scripts; set up key auth in Phase 3 |
| RTX A4000 has no NVLink | Single-GPU inference per node; no tensor parallelism across GPUs |
| Concurrency per node | Start with `--parallel 2` per llama.cpp instance; tune based on VRAM |

---

## What Phase 3 Will Add

- NGINX with self-signed TLS (internal CA)
- SSH key-based auth across nodes
- LDAP / Active Directory integration for user auth
- RBAC (admin / user / readonly roles)
- Audit log dashboard in Grafana
- Alembic migrations (replace `create_all`)
- Worker auto-restart on failure

---

## What Phase 4 Will Add

- KubeRay + GPU Operator
- Multi-model routing (embeddings, reranker)
- Token-aware load balancing
- Batch inference endpoint
- RAG pipeline integration
