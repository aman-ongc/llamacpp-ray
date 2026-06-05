#!/usr/bin/env python3

"""
llama.cpp cluster sync utility

Features:
- Detect active WSL nodes
- Skip source node (WS-11)
- Auto install dependencies
- Ensure remote repo structure
- Rsync source code
- Exclude build artifacts only
- Detect source changes
- Build only when required
- Validate critical files
- Cron friendly
"""

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────

SSH_USER = os.getenv("SSH_USER", "administrator")
SSH_PASSWORD = os.getenv("SSH_PASSWORD", "Ongc@1234")

LLAMA_CPP_PATH = (
    "/home/administrator/projects/local_llm/llama.cpp"
)

SSH_TIMEOUT = 5
RSYNC_TIMEOUT = 1800
BUILD_TIMEOUT = 3600

# WS-11 is source-of-truth
SOURCE_NODE_IP = "10.208.211.62"

# WS-1 (.52) → WS-16 (.67)
ALL_NODE_IPS = [
    ip
    for ip in [
        f"10.208.211.{51 + i}"
        for i in range(1, 17)
    ]
    if ip != SOURCE_NODE_IP
]

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────

def get_logger():

    logger = logging.getLogger("llama_cpp_sync")

    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    )

    log_file = (
        LOG_DIR /
        f"sync_{datetime.now().strftime('%Y%m%d')}.log"
    )

    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger


logger = get_logger()

# ── SSH Helpers ───────────────────────────────────────────────────────

def run_ssh(ip: str, command: str, timeout=300):

    cmd = [
        "sshpass",
        f"-p{SSH_PASSWORD}",
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"ConnectTimeout={SSH_TIMEOUT}",
        f"{SSH_USER}@{ip}",
        command
    ]

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout
    )

# ── Node Discovery ────────────────────────────────────────────────────

def is_wsl_active(ip: str):

    try:

        result = run_ssh(
            ip,
            "grep -qi ubuntu /etc/os-release && echo ubuntu",
            timeout=15
        )

        return (
            result.returncode == 0
            and "ubuntu" in result.stdout.lower()
        )

    except Exception:
        return False


def get_active_nodes():

    active = []

    for ip in ALL_NODE_IPS:

        if is_wsl_active(ip):
            active.append(ip)

    logger.info(
        f"Active nodes ({len(active)}/{len(ALL_NODE_IPS)}): "
        f"{active}"
    )

    return active

# ── Dependency Installation ───────────────────────────────────────────

def ensure_dependencies(ip: str):

    logger.info(f"[{ip}] Checking dependencies")

    check_cmd = """
    command -v cmake &&
    command -v ninja &&
    command -v rsync
    """

    result = run_ssh(ip, check_cmd)

    if result.returncode == 0:

        logger.info(f"[{ip}] Dependencies already installed")
        return True

    logger.info(f"[{ip}] Installing dependencies")

    install_cmd = """
    sudo apt update &&
    sudo apt install -y \
        build-essential \
        cmake \
        ninja-build \
        rsync \
        sshpass \
        git \
        pkg-config \
        libssl-dev
    """

    try:

        result = run_ssh(
            ip,
            install_cmd,
            timeout=1800
        )

        if result.returncode == 0:

            logger.info(f"[{ip}] Dependencies installed")
            return True

        logger.error(f"[{ip}] Dependency installation failed")
        logger.error(result.stderr[-1000:])

        return False

    except Exception as e:

        logger.error(f"[{ip}] Install exception: {e}")
        return False

# ── Remote Repo Structure ─────────────────────────────────────────────

def ensure_remote_repo_structure(ip: str):

    logger.info(f"[{ip}] Ensuring repo structure")

    cmd = f"""
    mkdir -p {LLAMA_CPP_PATH}
    mkdir -p {LLAMA_CPP_PATH}/tools
    mkdir -p {LLAMA_CPP_PATH}/tools/mtmd
    mkdir -p {LLAMA_CPP_PATH}/tools/mtmd/models
    """

    result = run_ssh(ip, cmd)

    if result.returncode != 0:

        logger.error(
            f"[{ip}] Failed to create repo structure"
        )

        logger.error(result.stderr)

        return False

    return True

# ── Rsync ─────────────────────────────────────────────────────────────

def rsync_llama_cpp(ip: str):

    logger.info(f"[{ip}] Starting rsync")

    ssh_opts = (
        f"ssh "
        f"-o StrictHostKeyChecking=no "
        f"-o ConnectTimeout={SSH_TIMEOUT}"
    )

    cmd = [
        "sshpass",
        f"-p{SSH_PASSWORD}",
        "rsync",

        "-avz",

        "--delete",
        "--itemize-changes",
        "--partial",
        "--human-readable",

        f"--timeout={RSYNC_TIMEOUT}",

        "-e",
        ssh_opts,

        # Exclusions
        "--exclude=.git",
        "--exclude=build",
        "--exclude=.cache",
        "--exclude=tmp",

        "--exclude=*.o",
        "--exclude=*.so",
        "--exclude=*.a",
        "--exclude=*.pyc",

        "--exclude=*.gguf",

        "--exclude=compile_commands.json",

        f"{LLAMA_CPP_PATH}/",
        f"{SSH_USER}@{ip}:{LLAMA_CPP_PATH}/"
    ]

    try:

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RSYNC_TIMEOUT + 60
        )

        if result.returncode != 0:

            logger.error(f"[{ip}] Rsync failed")
            logger.error(result.stderr[-1000:])

            return False, False

        changed = False

        changed_extensions = (
            ".cpp",
            ".cc",
            ".c",
            ".h",
            ".hpp",
            "CMakeLists.txt"
        )

        for line in result.stdout.splitlines():

            if any(ext in line for ext in changed_extensions):
                changed = True
                break

        if changed:

            logger.info(f"[{ip}] Source changes detected")

        else:

            logger.info(f"[{ip}] No source changes detected")

        return True, changed

    except Exception as e:

        logger.error(f"[{ip}] Rsync exception: {e}")

        return False, False

# ── Validation ────────────────────────────────────────────────────────

def validate_required_files(ip: str):

    required_files = [
        f"{LLAMA_CPP_PATH}/CMakeLists.txt",
        f"{LLAMA_CPP_PATH}/tools/mtmd/models/granite-speech.cpp",
    ]

    for file in required_files:

        result = run_ssh(
            ip,
            f"test -f {file}"
        )

        if result.returncode != 0:

            logger.error(
                f"[{ip}] Missing required file: {file}"
            )

            return False

    return True

# ── Build ─────────────────────────────────────────────────────────────

def build_remote(ip: str):

    logger.info(f"[{ip}] Starting build")

    build_cmd = f"""
    cd {LLAMA_CPP_PATH} && \
    rm -rf build && \
    cmake -B build -G Ninja && \
    cmake --build build -j$(nproc)
    """

    try:

        result = run_ssh(
            ip,
            build_cmd,
            timeout=BUILD_TIMEOUT
        )

        if result.returncode == 0:

            logger.info(f"[{ip}] Build successful")
            return True

        logger.error(f"[{ip}] Build failed")
        logger.error(result.stderr[-3000:])

        return False

    except Exception as e:

        logger.error(f"[{ip}] Build exception: {e}")
        return False

# ── Main ──────────────────────────────────────────────────────────────

def main():

    logger.info("=" * 80)
    logger.info("Starting llama.cpp sync job")

    active_nodes = get_active_nodes()

    if not active_nodes:

        logger.warning("No active nodes found")
        return

    for ip in active_nodes:

        logger.info(f"[{ip}] Processing node")

        # ── Dependencies ─────────────────────────────

        deps_ok = ensure_dependencies(ip)

        if not deps_ok:

            logger.error(
                f"[{ip}] Dependency setup failed"
            )

            continue

        # ── Repo Structure ───────────────────────────

        repo_ok = ensure_remote_repo_structure(ip)

        if not repo_ok:

            continue

        # ── Sync ─────────────────────────────────────

        sync_ok, source_changed = rsync_llama_cpp(ip)

        if not sync_ok:

            logger.error(f"[{ip}] Sync failed")
            continue

        # ── Skip Build ───────────────────────────────

        if not source_changed:

            logger.info(
                f"[{ip}] Skipping build "
                f"(no source changes)"
            )

            continue

        # ── Validate ─────────────────────────────────

        valid = validate_required_files(ip)

        if not valid:

            logger.error(
                f"[{ip}] Validation failed"
            )

            continue

        # ── Build ────────────────────────────────────

        build_ok = build_remote(ip)

        if not build_ok:

            logger.error(f"[{ip}] Build failed")
            continue

        logger.info(f"[{ip}] Node updated successfully")

    logger.info("llama.cpp sync job completed")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()