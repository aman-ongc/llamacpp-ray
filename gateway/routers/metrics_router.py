from fastapi import APIRouter

from gateway.metrics import metrics_response


router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics():
    return metrics_response()
