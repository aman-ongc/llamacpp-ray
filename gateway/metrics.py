from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response


REQUEST_COUNT = Counter(
    "llm_requests_total",
    "Total inference requests",
    ["model", "status_code", "streaming"],
)
REQUEST_LATENCY_MS = Histogram(
    "llm_request_latency_ms",
    "Inference request latency in milliseconds",
    ["model"],
    buckets=(50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)
PROMPT_TOKENS = Counter(
    "llm_prompt_tokens_total",
    "Total prompt tokens processed",
    ["model"],
)
COMPLETION_TOKENS = Counter(
    "llm_completion_tokens_total",
    "Total completion tokens generated",
    ["model"],
)
ACTIVE_REQUESTS = Gauge(
    "llm_active_requests",
    "Currently active inference requests",
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
