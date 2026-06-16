import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.health_monitor import health_probe_loop
from gateway.routers.admin import router as admin_router
from gateway.routers.chat import router as chat_router
from gateway.routers.completions import router as completions_router
from gateway.routers.health import router as health_router
from gateway.routers.metrics_router import router as metrics_router
from gateway.routers.models_router import router as models_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(health_probe_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="ONGC LLM Inference Gateway", lifespan=lifespan)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(models_router)
app.include_router(completions_router)
app.include_router(chat_router)
app.include_router(admin_router)
