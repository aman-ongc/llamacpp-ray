# WS-11 Node Replication Guide

Complete setup reference for replicating WS-11 (10.208.211.62) on a fresh Windows workstation.

---

## Hardware Baseline

| Component | Spec |
|-----------|------|
| GPU | NVIDIA RTX A4000 — 16 GB VRAM (Ampere, sm_86) |
| RAM | 128 GB system RAM |
| CPU | 6 cores |
| Network | 1 Gbps Ethernet, subnet 10.208.211.0/24 |
| Storage | D: drive required (Models + VirtualEnvironments) |

---

## Windows Environment

| Item | Value |
|------|-------|
| OS | Windows 11 Pro |
| Build | 10.0.26100 |
| NVIDIA Display Driver | 595.79 |
| CUDA (max supported by driver) | 13.2 |

---

## Step 1 — Install NVIDIA Driver (Windows)

1. Download driver **595.79** (or newer) for RTX A4000 from NVIDIA.
2. Install on Windows host. Do **not** install CUDA toolkit from Windows — CUDA lives inside WSL2.
3. Verify: open Device Manager → Display Adapters → NVIDIA RTX A4000.

---

## Step 2 — Enable WSL2

Run in an **elevated PowerShell**:

```powershell
# Enable WSL and Virtual Machine Platform
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart

# Reboot
Restart-Computer
```

After reboot, set WSL2 as default and update kernel:

```powershell
# Set WSL2 as default version
wsl --set-default-version 2

# Update WSL to latest (pulls kernel 6.6.87.2-microsoft-standard-WSL2)
wsl --update

# Verify
wsl --version
# Expected:
# WSL version: 2.6.3.0
# Kernel version: 6.6.87.2-1
# WSLg version: 1.0.71
```

---

## Step 3 — Install Ubuntu 24.04

```powershell
wsl --install -d Ubuntu-24.04
```

When prompted, set:
- Username: `administrator`
- Password: `Ongc@1234`

Verify distro is WSL2:

```powershell
wsl -l -v
# NAME           STATE    VERSION
# Ubuntu-24.04   Running  2
```

---

## Step 4 — WSL Configuration Files

### `C:\Users\Administrator\.wslconfig`  (Windows-side, per-user)

Create this file:

```ini
[wsl2]
networkingMode=Mirrored
```

> **Critical:** Mirrored networking makes WSL2 share the host IP. This means WSL services are directly reachable at the Windows IP (10.208.211.x) without port-proxy rules.

### `/etc/wsl.conf`  (inside Ubuntu 24.04)

```bash
wsl -d Ubuntu-24.04 -- bash -c "sudo tee /etc/wsl.conf << 'EOF'
[boot]
systemd=true

[user]
default=administrator

[interop]
appendWindowsPath = true

[network]
generateHosts = false
EOF"
```

Restart WSL to apply:

```powershell
wsl --shutdown
# Wait 8 seconds
wsl -d Ubuntu-24.04
```

### Verify Mirrored Networking Is Active

Inside WSL, check that `hostname -I` returns the actual Windows host IP (starts with `10.208.*`), **not** blank or a `172.*` NAT address:

```bash
hostname -I
# Expected: 10.208.211.XX ...
# BAD — mirrored not working: blank output
# BAD — mirrored not working: 172.x.x.x (NAT mode)
```

If result is blank or `172.*`:

1. Confirm `.wslconfig` was saved to the correct path: `C:\Users\Administrator\.wslconfig` (not `C:\Users\Public` or a wrong user).
2. Confirm content is exactly:
   ```ini
   [wsl2]
   networkingMode=Mirrored
   ```
3. Run `wsl --shutdown` again and wait at least 8 seconds before re-launching.
4. Confirm Windows build is 10.0.22621 or newer (mirrored mode requires Win 11 22H2+).
5. If still `172.*` after all above, check Windows firewall isn't blocking the WSL mirrored adapter.

Only proceed to next steps after `hostname -I` returns a `10.208.*` address.

---

## Step 5 — Proxy Configuration

**Do this before any `apt`, `wget`, `curl`, or `pip` command.** All outbound internet traffic on the ONGC network goes through the corporate proxy `http://10.205.122.201:8080`. Without this, package downloads will fail or hang.

### 5a — `/etc/environment` (system-wide, all processes)

Primary proxy config. Applies to login shells, systemd services, and any process that reads `/etc/environment`.

```bash
sudo tee /etc/environment << 'EOF'
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"
http_proxy=http://10.205.122.201:8080
https_proxy=http://10.205.122.201:8080
HTTP_PROXY=http://10.205.122.201:8080
HTTPS_PROXY=http://10.205.122.201:8080
NO_PROXY=localhost,127.0.0.1,10.208.211.0/24
no_proxy=localhost,127.0.0.1,10.208.211.0/24
RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING=0
RAY_grpc_enable_http_proxy=0
EOF
```

> - Both `NO_PROXY` and `no_proxy` set — different tools check different case.
> - `NO_PROXY` must cover the entire cluster subnet `10.208.211.0/24`.
> - `RAY_grpc_enable_http_proxy=0` — prevents Ray gRPC traffic from being intercepted (critical for cluster communication).
> - `RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING=0` — ensures Ray Serve routes correctly across nodes.

### 5b — apt proxy (`/etc/apt/apt.conf.d/99proxy`)

apt does **not** reliably read `/etc/environment` when run non-interactively (scripts, cron, Docker builds). Set a dedicated apt proxy config:

```bash
sudo tee /etc/apt/apt.conf.d/99proxy << 'EOF'
Acquire::http::Proxy "http://10.205.122.201:8080";
Acquire::https::Proxy "http://10.205.122.201:8080";
EOF
```

### 5c — pip proxy

pip reads `http_proxy`/`https_proxy` from environment. No extra config file needed — `/etc/environment` covers it.

For one-off installs before environment is sourced:

```bash
pip install --proxy http://10.205.122.201:8080 <package>
```

### 5d — curl / wget (non-interactive scripts)

For scripts that run before `/etc/environment` is sourced, pass proxy explicitly:

```bash
curl -x http://10.205.122.201:8080 <url>
wget -e "use_proxy=yes" -e "http_proxy=http://10.205.122.201:8080" <url>
```

### 5e — Proxy bypass for internal / cluster traffic

**Always bypass proxy for localhost and cluster IPs.** The corporate proxy intercepts all traffic including LAN if not excluded:

```bash
# In bash scripts — set before any internal curl/ssh/ray calls
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
export NO_PROXY="$no_proxy"

# For curl — one-off
curl --noproxy '*' http://10.208.211.54:8080/health
```

### Proxy Configuration Summary

| Layer | Config location | Status on WS-11 |
|-------|----------------|-----------------|
| System env | `/etc/environment` | Configured |
| apt | `/etc/apt/apt.conf.d/99proxy` | Configured |
| pip | via env vars | Via `/etc/environment` |
| git | via env vars | Via `/etc/environment` |
| Docker pulls | via env vars | Via `/etc/environment` |
| Ray gRPC | `RAY_grpc_enable_http_proxy=0` | Bypassed |
| curl/wget (internal) | `--noproxy '*'` flag per call | Manual |

---

## Step 6 — Ubuntu Base Packages

Inside WSL Ubuntu:

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
    build-essential \
    cmake \
    git \
    curl \
    wget \
    openssh-server \
    openssh-client \
    sshpass \
    python3 \
    python3-pip \
    python3-venv \
    pkg-config \
    libssl-dev \
    net-tools \
    htop \
    nvtop \
    lshw
```

Versions on WS-11:
- `cmake` 3.28.3
- `git` 2.43.0
- `python3` 3.12.3
- `openssh-server` 9.6p1
- `docker.io` 28.2.2

---

## Step 7 — CUDA Toolkit 12.8 (inside WSL)

NVIDIA provides a WSL-specific CUDA repo. Do **not** install the regular Linux CUDA — use the `wsl-ubuntu` repo.

```bash
# Add CUDA keyring
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update

# Install CUDA Toolkit 12.8
sudo apt install -y cuda-toolkit-12-8
```

This installs to `/usr/local/cuda-12.8/` and creates symlinks:
- `/usr/local/cuda` → `/usr/local/cuda-12.8`
- `/usr/local/cuda-12` → `/usr/local/cuda-12.8`

Also installs:
- `cuda-nvcc-12-8` (compiler)
- `cuda-cudart-12-8` (runtime)
- `cuda-libraries-12-8` (cuBLAS, cuFFT, etc.)
- `cuda-gdb-12-8`, `cuda-nsight-12-8`, etc.

Add CUDA to PATH (append to `~/.bashrc`):

```bash
echo 'export PATH=/usr/local/cuda/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```

Verify:

```bash
nvcc --version
# Cuda compilation tools, release 12.0, V12.0.140
# (note: nvcc from nvidia-cuda-toolkit ubuntu package, older; use cuda-toolkit-12-8 nvcc at /usr/local/cuda/bin/nvcc)

nvidia-smi
# Should show RTX A4000, Driver 595.79, CUDA 13.2
```

> `nvidia-smi` CUDA column shows max CUDA version supported by driver, not installed toolkit. Both are correct.

### ldconfig for CUDA libs

```bash
sudo tee /etc/ld.so.conf.d/000_cuda.conf << 'EOF'
/usr/local/cuda/targets/x86_64-linux/lib
EOF

sudo ldconfig
```

---

## Step 8 — Docker (inside WSL)

```bash
# Add Docker's official GPG key and repo
sudo apt install -y ca-certificates gnupg lsb-release
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker.io docker-compose docker-compose-plugin docker-buildx-plugin

# Add administrator to docker group
sudo usermod -aG docker administrator
newgrp docker

# Enable and start Docker
sudo systemctl enable docker
sudo systemctl start docker
```

Versions on WS-11:
- `docker.io` 28.2.2
- `docker-compose-plugin` 5.1.1
- `docker-compose` 1.29.2 (legacy V1)

Verify:

```bash
docker info | grep -E 'Version|Server'
```

> Docker daemon does **not** have a `/etc/docker/daemon.json` on WS-11. Default runtimes only (`runc`). NVIDIA container runtime is not installed — GPU is used directly by llama-server processes, not via Docker.

---

## Step 9 — SSH Server Setup

```bash
sudo systemctl enable ssh
sudo systemctl start ssh
```

Verify from another node:

```bash
sshpass -p 'Ongc@1234' ssh administrator@<new-node-ip> echo ok
```

> All nodes use password auth. SSH key auth not configured.

---

## Step 10 — D: Drive Structure

WSL mounts the Windows D: drive at `/mnt/d`. Create the required folder structure on D: before proceeding:

```
D:\
├── Models\
│   ├── gemma-4-26b-qat\
│   │   └── gemma-4-26B_q4_0-it.gguf      # 14 GB — main text model
│   ├── qwen-3-vl\
│   │   ├── Qwen3VL-8B-Instruct-Q8_0.gguf  # multimodal (WS-13 only)
│   │   └── mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf
│   └── (other models as needed)
└── VirtualEnvironments\
    ├── llm-platform\                       # Ray + FastAPI + platform deps
    ├── llamacpp\                            # llama.cpp Python bindings
    ├── vllm\                               # vLLM (optional)
    └── sglang\                             # SGLang (optional)
```

Inside WSL, verify access:

```bash
ls /mnt/d/Models/
ls /mnt/d/VirtualEnvironments/
```

---

## Step 11 — Python Virtual Environments

### llm-platform venv (required for Ray/Serve)

```bash
python3 -m venv /mnt/d/VirtualEnvironments/llm-platform
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate
pip install --upgrade pip

# Core platform deps (sync from project requirements.txt)
pip install -r /home/administrator/projects/llm-inference-service/requirements.txt

# Ray with default extras
pip install "ray[serve,default]"
```

### llamacpp venv (for llama.cpp Python bindings if needed)

```bash
python3 -m venv /mnt/d/VirtualEnvironments/llamacpp
source /mnt/d/VirtualEnvironments/llamacpp/bin/activate
pip install --upgrade pip
# Install llama-cpp-python with CUDA support if needed:
# CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python
```

> Venvs are synced across all worker nodes via cron (`scripts/sync_venvs.sh`, every 30 min, run as `administrator`).

---

## Step 12 — Build llama.cpp with CUDA

```bash
cd /home/administrator/projects
mkdir -p local_llm && cd local_llm

git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp

# Current WS-11 commit: b9309 (062d3115a)
# For exact reproduction:
# git checkout 062d3115a

mkdir build && cd build

cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_CUDA=ON \
    -DGGML_CUDA_FA=ON \
    -DGGML_CUDA_GRAPHS=ON \
    -DGGML_CUDA_NCCL=ON

make -j$(nproc)
```

Key CMake flags used on WS-11:

| Flag | Value | Purpose |
|------|-------|---------|
| `GGML_CUDA` | ON | CUDA backend |
| `GGML_CUDA_FA` | ON | Flash Attention |
| `GGML_CUDA_GRAPHS` | ON | CUDA graph capture for performance |
| `GGML_CUDA_NCCL` | ON | Multi-GPU NCCL support |
| `CMAKE_BUILD_TYPE` | Release | Optimized build |

Binary location after build: `/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server`

Verify GPU inference works:

```bash
/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server \
    -m /mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf \
    -ngl 999 \
    -c 65536 \
    --host 0.0.0.0 \
    --port 8080 \
    --parallel 1 \
    --flash-attn auto \
    --cache-type-k q4_0 \
    --cache-type-v q4_0 \
    --cont-batching \
    --no-context-shift \
    --metrics
```

Check health:

```bash
curl --noproxy '*' http://localhost:8080/health
```

---

## Step 13 — Clone Platform Project

```bash
mkdir -p /home/administrator/projects
cd /home/administrator/projects
git clone <repo-url> llm-inference-service
cd llm-inference-service
```

> Alternatively, rsync from WS-11:

```bash
sshpass -p 'Ongc@1234' rsync -av \
    administrator@10.208.211.62:/home/administrator/projects/llm-inference-service/ \
    /home/administrator/projects/llm-inference-service/
```

---

## Step 14 — /etc/hosts (static, since generateHosts=false)

WSL does not auto-generate `/etc/hosts` because `generateHosts = false` in `/etc/wsl.conf`. Set it manually:

```bash
sudo tee /etc/hosts << 'EOF'
127.0.0.1   localhost
<new-node-ip>   <hostname>.localdomain   <hostname>

# Cluster nodes
10.208.211.62   PVLPOCC00WS011.localdomain   PVLPOCC00WS011
10.208.211.54   PVLPOCC00WS003.localdomain   PVLPOCC00WS003
10.208.211.59   PVLPOCC00WS008.localdomain   PVLPOCC00WS008
10.208.211.64   PVLPOCC00WS013.localdomain   PVLPOCC00WS013

::1     ip6-localhost ip6-loopback
fe00::0 ip6-localnet
ff00::0 ip6-mcastprefix
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF
```

---

## Step 15 — Crontab (auto-start on reboot)

```bash
crontab -e
```

Add these lines (identical to WS-11):

```cron
@reboot sleep 15 && bash /home/administrator/projects/llm-inference-service/scripts/start_node_exporter.sh >> /tmp/node_exporter_boot.log 2>&1
@reboot sleep 20 && bash /home/administrator/projects/llm-inference-service/scripts/start_gpu_exporter.sh >> /tmp/nvidia_gpu_exporter_boot.log 2>&1
```

For worker nodes (auto-join cluster on reboot), also add:

```cron
@reboot sleep 30 && bash /home/administrator/projects/llm-inference-service/startup_scripts/start_linux.sh >> /tmp/llm-startup.log 2>&1
```

Or use the provided script:

```bash
bash /home/administrator/projects/llm-inference-service/scripts/install_autostart.sh
```

---

## Step 16 — Venv Sync Cron (for worker nodes)

Worker nodes receive venv updates from WS-11 via cron. Add as `administrator`:

```cron
*/30 * * * * bash /home/administrator/projects/llm-inference-service/scripts/sync_venvs.sh >> /tmp/venv_sync.log 2>&1
```

---

## Installed APT Repositories Summary

| Repo | Source |
|------|--------|
| Ubuntu 24.04 (noble) | `ubuntu.sources` (standard) |
| CUDA WSL Ubuntu | `https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/` |
| Docker CE | `https://download.docker.com/linux/ubuntu` (noble stable) |
| NodeSource | `nodesource.sources` |

---

## Versions Summary (WS-11 Baseline)

| Component | Version |
|-----------|---------|
| Windows | 11 Pro 10.0.26100 |
| NVIDIA Driver (Windows) | 595.79 |
| WSL | 2.6.3.0 |
| WSL Kernel | 6.6.87.2-microsoft-standard-WSL2 |
| WSLg | 1.0.71 |
| Ubuntu | 24.04.4 LTS (noble) |
| Python | 3.12.3 |
| CUDA Toolkit (WSL) | 12.8 (`cuda-toolkit-12-8`) |
| nvcc (from nvidia-cuda-toolkit) | 12.0.140 |
| cmake | 3.28.3 |
| git | 2.43.0 |
| Docker | 28.2.2 |
| docker-compose-plugin | 5.1.1 |
| openssh-server | 9.6p1 |
| sshpass | 1.09 |
| llama.cpp | b9309 / commit `062d3115a` |

---

## User & Auth

| Item | Value |
|------|-------|
| Username | `administrator` |
| Password | `Ongc@1234` |
| Groups | `administrator adm cdrom sudo dip plugdev users` |
| SSH auth | Password (key auth not configured) |
| Docker access | Add to `docker` group: `sudo usermod -aG docker administrator` |

---

## Network Config Summary

| Setting | Value |
|---------|-------|
| WSL networking mode | Mirrored (shares Windows host IP) |
| Corporate HTTP proxy | `http://10.205.122.201:8080` |
| NO_PROXY | `localhost,127.0.0.1,10.208.211.0/24` |
| Ray gRPC proxy bypass | `RAY_grpc_enable_http_proxy=0` |
| Internal cluster subnet | `10.208.211.0/24` |
| DNS | Auto via WSL (`nameserver 10.255.255.254`) |

---

## Quick Verification Checklist

After setup, verify each layer:

```bash
# 1. WSL version
wsl --version

# 2. Mirrored networking — must show 10.208.* IP, not 172.* or blank
hostname -I

# 3. GPU visible inside WSL
nvidia-smi

# 4. CUDA compiler
/usr/local/cuda/bin/nvcc --version

# 5. Docker
docker run --rm hello-world

# 6. SSH server
systemctl status ssh

# 7. llama-server binary
/home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server --version

# 8. Python venv
source /mnt/d/VirtualEnvironments/llm-platform/bin/activate && python -c "import ray; print(ray.__version__)"

# 9. Proxy bypass works
curl --noproxy '*' http://localhost:8080/health
```

---

## Notes

- Do not install NVIDIA drivers inside WSL — the Windows host driver exposes `/usr/lib/wsl/lib/` containing `libcuda.so` automatically.
- CUDA toolkit 12.8 is installed **inside** WSL from the `wsl-ubuntu` repo, not the regular Linux repo.
- `networkingMode=Mirrored` in `.wslconfig` is essential — without it, services inside WSL are not directly reachable at the Windows IP without port-proxy rules.
- `systemd=true` in `/etc/wsl.conf` is required for `systemctl` to manage SSH and Docker.
- The D: drive must be available and mounted at `/mnt/d` — all models and venvs live there.
- All nodes use identical setup; the only difference is the assigned IP and whether the node is the Ray head (WS-11) or a worker.
