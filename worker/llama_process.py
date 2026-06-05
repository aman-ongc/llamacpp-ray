import os
import shlex
import subprocess
from pathlib import Path

from gateway.config import settings


class LlamaServerProcess:
    def __init__(self, host: str, port: int | None = None) -> None:
        self.host = host
        self.port = port or settings.llama_port
        self.process: subprocess.Popen[str] | None = None

    def command(self) -> list[str]:
        binary = Path("./build/bin/llama-server")
        return [
            str(binary),
            "-m",
            settings.llama_model_path,
            "--mmproj",
            settings.llama_mmproj_path,
            "-ngl",
            str(settings.llama_ngl),
            "-c",
            str(settings.llama_context),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--parallel",
            str(settings.llama_parallel),
            "--no-context-shift",
            "--flash-attn",
            "auto",
            "--cache-type-k",
            "q4_0",
            "--cache-type-v",
            "q4_0",
            "--cont-batching",
            "--spec-type",
            "draft-mtp",
            "--spec-draft-n-max",
            "2",
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
