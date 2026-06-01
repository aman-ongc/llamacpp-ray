from fastapi import APIRouter, Depends

from gateway.auth.middleware import require_api_key
from gateway.models import User


router = APIRouter(prefix="/v1", tags=["completions"])


@router.post("/completions")
async def completions(_: User = Depends(require_api_key)) -> dict[str, str]:
    return {
        "id": "cmpl-placeholder",
        "object": "text_completion",
        "choices": [{"text": "Use /v1/chat/completions for chat-centric traffic."}],
    }
