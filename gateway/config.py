from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://llm:llm@localhost:5432/llm_platform"
    redis_url: str = "redis://localhost:6379/0"
    admin_secret: str = "changeme"

    no_proxy: str = "localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8000
    # Unified model name exposed to users — routing is transparent.
    default_model: str = "ongc-llm"

    # ── Text pool: .52,.53,.55–.58 + .62 (WS-11, excluded unless controller_as_worker=true)
    #              .54 (WS-3, excluded unless docling_node_as_worker=true)
    #              .59 permanently excluded — display GPU (15,352 MiB vs 16,376 MiB on headless nodes)
    #              .60/.61 moved to the multimodal pool (2026-06-20 CPU-contention rebalance)
    text_node_ips: str = Field(
        default="10.208.211.52,10.208.211.53,10.208.211.54,10.208.211.55,"
                "10.208.211.56,10.208.211.57,10.208.211.58,10.208.211.62"
    )
    text_llama_port: int = 8080
    text_llama_model_path: str = "/mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf"
    text_max_queued_requests: int = Field(default=20)

    # ── Multimodal pool: .60, .61, .63, .64, .65, .67 running Qwen3-VL-8B-Instruct ──
    # .60/.61 added 2026-06-20, moved off the text pool to absorb capacity while
    # --parallel drops 4→2 (CPU-contention rebalance — see multimodal_llama_parallel).
    multimodal_node_ips: str = Field(
        default="10.208.211.60,10.208.211.61,10.208.211.63,10.208.211.64,"
                "10.208.211.65,10.208.211.67"
    )
    multimodal_llama_port: int = 8080
    multimodal_llama_model_path: str = "/mnt/d/Models/qwen-3-vl/Qwen3VL-8B-Instruct-Q8_0.gguf"
    multimodal_llama_mmproj_path: str = "/mnt/d/Models/qwen-3-vl/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"
    multimodal_max_queued_requests: int = Field(default=40)

    # ── Shared llama.cpp settings ─────────────────────────────────────────────
    llama_context: int = 65536
    llama_parallel: int = 1
    multimodal_llama_context: int = 32768
    multimodal_llama_parallel: int = 2
    llama_ngl: int = 999

    request_timeout_seconds: float = 900.0
    connect_timeout_seconds: float = 2.0

    # Kept for backward compatibility with admin/logging code that references controller IP.
    controller_node_ip: str = Field(default="10.208.211.62")
    # All worker node IPs (excluding controller) — used by admin/infra tooling.
    worker_node_ips: str = Field(
        default="10.208.211.52,10.208.211.53,10.208.211.54,10.208.211.55,"
                "10.208.211.56,10.208.211.57,10.208.211.58,"
                "10.208.211.60,10.208.211.61,"
                "10.208.211.63,10.208.211.64,10.208.211.65,10.208.211.67"
    )

    # ── Controller-as-worker toggle ───────────────────────────────────────────
    # When false (default): WS-11 (.62) is excluded from the text pool — does not run llama-server.
    # When true: WS-11 joins the text pool.
    # Set via env var: CONTROLLER_AS_WORKER=true
    controller_as_worker: bool = Field(default=False)

    # ── Docling-node toggle ───────────────────────────────────────────────────
    # WS-3 (.54) is reserved for docling/development workloads that need GPU VRAM.
    # When false (default): .54 is excluded from the text pool — does not run llama-server.
    # When true: .54 joins the text pool as a normal text worker.
    # Set via env var: DOCLING_NODE_AS_WORKER=true
    docling_node_ip: str = Field(default="10.208.211.54")
    docling_node_as_worker: bool = Field(default=False)

    @model_validator(mode="after")
    def _apply_node_settings(self) -> "Settings":
        excluded = set()
        if not self.controller_as_worker:
            excluded.add(self.controller_node_ip)
        if not self.docling_node_as_worker:
            excluded.add(self.docling_node_ip)
        ips = [
            ip.strip()
            for ip in self.text_node_ips.split(",")
            if ip.strip() and ip.strip() not in excluded
        ]
        self.text_node_ips = ",".join(ips)
        return self


settings = Settings()
