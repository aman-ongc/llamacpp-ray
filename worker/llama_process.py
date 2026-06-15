import os
import subprocess
from pathlib import Path

from gateway.config import settings

_MULTIMODAL_NODE_IPS: set[str] = {
    ip.strip() for ip in settings.multimodal_node_ips.split(",") if ip.strip()
}


class LlamaServerProcess:
    """Manages a local llama-server subprocess.

    Selects model/port/flags based on whether this node is a multimodal
    node (Qwen3-VL) or a text node (Gemma 4 26B QAT).
    """

    def __init__(self, host: str, port: int | None = None) -> None:
        self.host = host
        self._is_multimodal = host in _MULTIMODAL_NODE_IPS
        if port is not None:
            self.port = port
        else:
            self.port = settings.multimodal_llama_port if self._is_multimodal else settings.text_llama_port
        self.process: subprocess.Popen[str] | None = None

    def command(self) -> list[str]:
        binary = Path("./build/bin/llama-server")
        if self._is_multimodal:
            return [
                str(binary),
                "-m", settings.multimodal_llama_model_path,
                "--mmproj", settings.multimodal_llama_mmproj_path,
                "-ngl", str(settings.llama_ngl),
                "-c", str(settings.multimodal_llama_context),
                "--host", self.host,
                "--port", str(self.port),
                "--parallel", str(settings.multimodal_llama_parallel),
                "--flash-attn", "auto",
                "--cache-type-k", "q8_0",
                "--cache-type-v", "q8_0",
                "--cont-batching",
            ]
        return [
            str(binary),
            "-m", settings.text_llama_model_path,
            "-ngl", str(settings.llama_ngl),
            "-c", str(settings.llama_context),
            "--host", self.host,
            "--port", str(self.port),
            "--parallel", str(settings.llama_parallel),
            "--flash-attn", "auto",
            "--cache-type-k", "q4_0",
            "--cache-type-v", "q4_0",
            "--cont-batching",
            "--no-context-shift",
        ]

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        env["no_proxy"] = settings.no_proxy
        env["NO_PROXY"] = settings.no_proxy
        self.process = subprocess.Popen(
            self.command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=15)
