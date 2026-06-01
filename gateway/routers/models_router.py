from fastapi import APIRouter

from gateway.config import settings


router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models")
async def list_models() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "id": settings.default_model,
                "object": "model",
                "owned_by": "ongc",
            }
        ],
    }
