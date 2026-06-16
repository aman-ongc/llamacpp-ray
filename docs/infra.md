# Infrastructure Reference — Local LLM Cluster

> For agents and automation scripts operating on this cluster.
> Controller: WS-11 (10.208.211.62). All commands issue from here.

---

## Cluster Nodes

| Node  | IP              | Role                                  | Pool        |
|-------|-----------------|---------------------------------------|-------------|
| WS-11 | 10.208.211.62   | Controller (text optional)            | text        |
| —     | 10.208.211.52   | Text worker                           | text        |
| —     | 10.208.211.53   | Text worker                           | text        |
| WS-3  | 10.208.211.54   | Docling/dev node (text optional)      | text        |
| —     | 10.208.211.55   | Text worker                           | text        |
| —     | 10.208.211.56   | Text worker                           | text        |
| —     | 10.208.211.57   | Text worker                           | text        |
| —     | 10.208.211.58   | Text worker                           | text        |
| —     | 10.208.211.59   | Text worker                           | text        |
| —     | 10.208.211.60   | Text worker                           | text        |
| —     | 10.208.211.61   | Text worker                           | text        |
| —     | 10.208.211.63   | Multimodal worker                     | multimodal  |
| —     | 10.208.211.64   | Multimodal worker                     | multimodal  |
| —     | 10.208.211.65   | Multimodal worker                     | multimodal  |
| —     | 10.208.211.67   | Multimodal worker                     | multimodal  |

WS-11 (.62) is excluded from the text pool by default (`CONTROLLER_AS_WORKER=false`).
Set `CONTROLLER_AS_WORKER=true` to include it as an 11th text replica.

WS-3 (.54) is reserved for docling/development workloads by default (`DOCLING_NODE_AS_WORKER=false`).
Set `DOCLING_NODE_AS_WORKER=true` to include it as a text worker (only when GPU VRAM is not needed for other tasks).

---

## Hardware Per Node

| Component    | Spec                                  |
|--------------|---------------------------------------|
| GPU          | NVIDIA RTX A4000 — 16 GB VRAM (Ampere, no NVLink) |
| RAM          | 128 GB system RAM                     |
| CPU          | 6 cores                               |
| OS           | WSL2 — Ubuntu 24.04                   |
| CUDA         | Installed, same version across all nodes |
| Network      | 1 Gbps Ethernet (10.208.211.0/24)     |

---

## Corporate / Environment Constraints

### HTTP Proxy (mandatory for all outbound traffic)
```
http_proxy=http://10.205.122.201:8080
https_proxy=http://10.205.122.201:8080
```
Set in `/etc/environment` on all nodes. For tools that ignore system proxy, pass explicitly:
```bash
export http_proxy=http://10.205.122.201:8080
export https_proxy=http://10.205.122.201:8080
```
When making localhost HTTP requests (e.g. benchmarks), always bypass proxy:
```bash
curl --noproxy '*' ...
# or in Python: set NO_PROXY=localhost,127.0.0.1
```


## Paths

| Resource             | Path                                       |
|----------------------|--------------------------------------------|
| Models               | `/mnt/d/Models/`                           |
| Virtual environments | `/mnt/d/VirtualEnvironments/`              |

---

## Virtual Environments

| Framework  | Path                                    | Python  |
|------------|-----------------------------------------|---------|
| llama.cpp  | `/mnt/d/VirtualEnvironments/llamacpp/`  | 3.12    |

Identical venvs on all 4 nodes. Synced from WS-11 via cron (`administrator`, every 30 min, `scripts/sync_venvs.sh`).

---

## Working Command

./build/bin/llama-server   -m /mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf   -ngl 999   -c 65536   --host 10.208.211.62   --port 8080   --parallel 2   --no-context-shift   --flash-attn on   --cache-type-k q8_0   --cache-type-v q8_0   --cont-batching
---

## SSH Access

- User: `administrator` (password: `Ongc@1234`, same on all nodes including root)
- Non-interactive SSH: `sshpass -p 'Ongc@1234' ssh administrator@<ip>`
- SSH key auth: not yet configured (passwordless SSH not set up)
- Always use `administrator`, not `root`, for worker operations

---

## Cron Jobs

| Job             | User          | Schedule     | Script                        |
|-----------------|---------------|--------------|-------------------------------|
| Model sync      | root          | every 10 min | (do not modify)               |
| Venv sync       | administrator | every 30 min | `scripts/sync_venvs.sh`       |

---
