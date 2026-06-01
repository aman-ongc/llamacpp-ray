from fastapi import APIRouter


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "llm-inference-gateway"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready", "service": "llm-inference-gateway"}


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "live", "service": "llm-inference-gateway"}
