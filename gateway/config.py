from pydantic import Field
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
    default_model: str = "qwen"

    llama_model_path: str = (
        "/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
    )
    llama_mmproj_path: str = "/mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/mmproj-F16.gguf"
    llama_port: int = 8080
    llama_context: int = 65536
    llama_parallel: int = 1
    llama_ngl: int = 999

    # Qwen3.6 thinking mode — off by default; set ENABLE_THINKING=true to enable
    enable_thinking: bool = False

    request_timeout_seconds: float = 300.0
    connect_timeout_seconds: float = 2.0

    controller_node_ip: str = Field(default="10.208.211.62")
    worker_node_ips: str = Field(default="10.208.211.54,10.208.211.59,10.208.211.64")
    serve_replicas: int = Field(default=4)


settings = Settings()
