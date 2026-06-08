from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://llm:llm@localhost:5432/llm_platform"
    redis_url: str = "redis://localhost:6379/0"
    ray_address: str = "ray://10.208.211.62:10001"
    ray_serve_url: str = "http://10.208.211.62:8001"
    admin_secret: str = "changeme"

    no_proxy: str = "localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8000
    # Unified model name exposed to users — routing is transparent.
    default_model: str = "ongc-llm"

    # ── Text pool: WS-11 + WS-03 + WS-08 running Gemma 4 26B QAT ─────────────
    text_node_ips: str = Field(default="10.208.211.62,10.208.211.54,10.208.211.59")
    text_llama_port: int = 8080
    text_llama_model_path: str = "/mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf"
    text_serve_replicas: int = Field(default=3)

    # ── Multimodal pool: WS-13 running Qwen3-VL-8B-Instruct ──────────────────
    multimodal_node_ip: str = Field(default="10.208.211.64")
    multimodal_llama_port: int = 8080
    multimodal_llama_model_path: str = "/mnt/d/Models/qwen-3-vl/Qwen3VL-8B-Instruct-Q8_0.gguf"
    multimodal_llama_mmproj_path: str = "/mnt/d/Models/qwen-3-vl/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"
    multimodal_serve_replicas: int = Field(default=1)

    # ── Shared llama.cpp settings ─────────────────────────────────────────────
    llama_context: int = 65536
    llama_parallel: int = 1
    llama_ngl: int = 999

    request_timeout_seconds: float = 300.0
    connect_timeout_seconds: float = 2.0

    # Kept for backward compatibility with admin/logging code that references controller IP.
    controller_node_ip: str = Field(default="10.208.211.62")
    # All worker node IPs (excluding controller) — used by admin/infra tooling.
    worker_node_ips: str = Field(default="10.208.211.54,10.208.211.59,10.208.211.64")

    # ── Controller-as-worker toggle ───────────────────────────────────────────
    # When false (default): WS-11 is excluded from text node pool and no
    # llama-server is started on it. Only WS-03 and WS-08 serve text requests.
    # When true: WS-11 joins the text pool (3 replicas, llama-server started).
    # Set via env var: CONTROLLER_AS_WORKER=true
    controller_as_worker: bool = Field(default=False)

    @model_validator(mode="after")
    def _apply_controller_worker_setting(self) -> "Settings":
        if not self.controller_as_worker:
            ips = [
                ip.strip()
                for ip in self.text_node_ips.split(",")
                if ip.strip() and ip.strip() != self.controller_node_ip
            ]
            self.text_node_ips = ",".join(ips)
            self.text_serve_replicas = len(ips)
        return self


settings = Settings()
