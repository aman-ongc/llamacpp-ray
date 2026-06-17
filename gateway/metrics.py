from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response


REQUEST_COUNT = Counter(
    "llm_requests_total",
    "Total inference requests",
    ["model", "status_code", "streaming", "username", "node_ip"],
)
REQUEST_LATENCY_MS = Histogram(
    "llm_request_latency_ms",
    "Inference request latency in milliseconds",
    ["model", "username", "node_ip"],
    buckets=(50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000, 180000, 300000, 600000),
)
PROMPT_TOKENS = Counter(
    "llm_prompt_tokens_total",
    "Total prompt tokens processed",
    ["model", "username", "request_type"],
)
COMPLETION_TOKENS = Counter(
    "llm_completion_tokens_total",
    "Total completion tokens generated",
    ["model", "username", "request_type"],
)
TOTAL_TOKENS = Counter(
    "llm_total_tokens_total",
    "Total tokens (prompt + completion) processed",
    ["model", "username", "request_type"],
)
ACTIVE_REQUESTS = Gauge(
    "llm_active_requests",
    "Currently active inference requests",
)
HEALTHY_TEXT_NODES = Gauge(
    "llm_healthy_text_nodes",
    "Number of currently healthy text nodes",
)
HEALTHY_MULTIMODAL_NODES = Gauge(
    "llm_healthy_multimodal_nodes",
    "Number of currently healthy multimodal nodes",
)
QUEUE_REJECTED = Counter(
    "llm_queue_rejected_total",
    "Requests rejected because the Ray Serve queue was full",
    ["request_type"],
)
RATE_LIMITED = Counter(
    "llm_rate_limited_total",
    "Requests rejected by the per-user rate limiter",
    ["request_type"],
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
