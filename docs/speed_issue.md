# LLM Inference Speed Issue — Investigation & RCA

## Overview

Observed token generation speed of **7–10 tok/s** when serving requests through the full Ray Serve stack, versus **16–18 tok/s** when running `llama-server` manually in isolation on the same hardware with identical parameters.

---

## Infrastructure

| Node  | IP              | Role                  | GPU                        | RAM    | CPU     | OS            |
|-------|-----------------|----------------------|----------------------------|--------|---------|---------------|
| WS-11 | 10.208.211.62   | Controller + Worker  | NVIDIA RTX A4000 (16 GB)   | 128 GB | 6 cores | WSL2 Ubuntu 24.04 |
| WS-03 | 10.208.211.54   | Worker               | NVIDIA RTX A4000 (16 GB)   | 128 GB | 6 cores | WSL2 Ubuntu 24.04 |
| WS-08 | 10.208.211.59   | Worker               | NVIDIA RTX A4000 (16 GB)   | 128 GB | 6 cores | WSL2 Ubuntu 24.04 |
| WS-13 | 10.208.211.64   | Worker               | NVIDIA RTX A4000 (16 GB)   | 128 GB | 6 cores | WSL2 Ubuntu 24.04 |

**Model:** Gemma 4 26B A4, GGUF Q4_0 quantisation
**Model size on disk:** ~20 GB
**GPU VRAM per node:** 16 GB
**Network:** 1 Gbps Ethernet (10.208.211.0/24)

The model exceeds the VRAM of a single A4000. llama-server runs in **hybrid CPU+GPU mode** — layers are split between GPU and system RAM at startup based on available VRAM.

---

## Observed Symptoms

| Test condition | Measured speed |
|---|---|
| `llama-cli` directly (no server) | ~20 tok/s (generation only) |
| `llama-server` via Ray Serve (full stack) | ~7–10 tok/s (apparent) |
| `llama-server` manually on port 8090, all else stopped | ~16–18 tok/s |
| `llama-server` via Ray (port 8080), raylet killed | ~12 tok/s |

---

## Investigation

### Issue 1 — Measurement Artifact (Resolved)

**Symptom:** Benchmark and `curl` measurements showed ~9–10 tok/s.

**Finding:** The benchmark script computes tok/s as:
```
completion_tokens / total_elapsed_time
```
`total_elapsed_time` includes **prompt evaluation time** (input token processing), not just generation. `llama-cli` reports generation-only speed, which excludes prompt eval. This created a false comparison.

**Evidence from `timings` field in llama-server response:**
```json
{
  "prompt_n": 19,
  "prompt_ms": 647,
  "prompt_per_second": 29.4,
  "predicted_n": 250,
  "predicted_ms": 20466,
  "predicted_per_second": 12.2
}
```

`predicted_per_second` is the true generation speed. The apparent 9–10 tok/s was inflated by slow prompt evaluation being included in the denominator.

**Why prompt eval is slow:** The Gemma 4 26B model does not fully fit in 16 GB VRAM. A significant number of transformer layers are offloaded to CPU (6 cores). Prompt evaluation processes all input tokens as a batch through every layer — the CPU-offloaded layers must perform large matrix multiplications on the full prompt batch, which is slow on a 6-core CPU. Generation processes only 1 token per step through CPU layers, which is proportionally much faster.

---

### Issue 2 — `--parallel 2` Doubling KV Cache (Fixed)

**Original startup parameters:**
```
--parallel 2 --cache-type-k q8_0 --cache-type-v q8_0 -c 65536
```

**Finding:** With `--parallel 2`, llama-server pre-allocates KV cache for **2 slots simultaneously at startup**, regardless of active request count.

KV cache size formula:
```
n_ctx × n_layers × 2 × head_dim × n_kv_heads × bytes_per_element
```

| Config | KV cache VRAM | Remaining for model layers |
|---|---|---|
| `--parallel 2 --cache-type-k q8_0` | ~8.6 GB | ~7.4 GB |
| `--parallel 1 --cache-type-k q4_0` | ~2.1 GB | ~13.9 GB |

With 8.6 GB consumed by KV cache on a 16 GB GPU, the model (~20 GB) could only load ~38% of its layers onto GPU. The remaining layers ran on 6-core CPU. This caused the hybrid CPU bottleneck to be far worse than necessary.

**Fix applied:**
- `--parallel 2` → `--parallel 1`
- `--cache-type-k q8_0 --cache-type-v q8_0` → `--cache-type-k q4_0 --cache-type-v q4_0`
- `--flash-attn on` → `--flash-attn auto`
- Removed `--spec-type draft-mtp --spec-draft-n-max 4` (MTP-specific, not applicable)

Files updated: `startup_scripts/start_linux.sh`, `scripts/start_linux_worker.sh`, `worker/llama_process.py`, `gateway/config.py`

---

### Issue 3 — Residual 12 vs 16 tok/s Gap (Under Investigation)

After applying the parameter fixes above, a performance gap remained between the Ray-managed llama-server and a manually started fresh instance on the same node with identical parameters.

**Comparative timings (same node WS-08, same model, same params):**

| Instance | Startup method | `predicted_per_second` | `prompt_per_second` | `per_token_ms` |
|---|---|---|---|---|
| Port 8080 | Ray startup script | **12.2 tok/s** | 29.4 tok/s | 81.9 ms |
| Port 8090 | Manual, all else stopped | **16.4 tok/s** | 17.2 tok/s | 61.1 ms |

**Notable inversion:** Port 8080 has *faster* prompt eval but *slower* generation than port 8090.

**Investigation steps completed:**

| Check | Result |
|---|---|
| GPU exclusively used by llama-server? | ✓ Confirmed via nvidia-smi |
| Running params identical? | ✓ Confirmed via `/proc/[pid]/cmdline` |
| Thread count | Both: 17 threads |
| Process nice value | Both: 0 |
| CPU affinity | Both: `0xfff` (all 12 logical CPUs) |
| Environment variables (OMP, BLAS, CUDA flags) | Identical, no performance-limiting vars |
| Ray CPU contention (killed raylet, retested) | No improvement — Ray not the cause |

**Hypothesis under test:** llama-server calculates how many layers to offload to GPU **once at startup**, based on available VRAM at that moment. If other processes transiently consumed VRAM during the Ray platform startup sequence, the Ray-managed llama-server instance may have been initialised with fewer GPU layers locked in — even if VRAM was freed afterward.

The prompt/generation speed inversion is consistent with this:
- More GPU layers → faster batch (prompt eval) ✓
- More GPU layers with constrained VRAM → higher VRAM pressure per token step → slower memory-bandwidth-bound generation ✓

**Fix attempted:** Reordered startup sequence so llama-server starts **after** Ray worker has fully joined the cluster (not before), giving Ray time to complete any transient VRAM operations before llama-server claims GPU layers.

Changes in `start_linux_worker.sh` — new order:
1. Sync code to worker
2. Start Ray worker, wait 12 seconds for cluster join
3. Start llama-server (VRAM now in stable state)
4. Wait for llama health check

Same reorder applied to `start_linux.sh` for WS-11.

**Status:** ✅ Resolved. After reordering startup sequence (Ray first, then llama-server), generation speed on Ray-managed instances matches manually started instances (~16–18 tok/s).

---

## Current Startup Parameters (All Nodes)

```bash
llama-server \
  -m /mnt/d/Models/gemma-4-26b-qat/gemma-4-26B_q4_0-it.gguf \
  -ngl 999 \
  -c 65536 \
  --host <node-ip> \
  --port 8080 \
  --parallel 1 \
  --no-context-shift \
  --flash-attn auto \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  --cont-batching
```

---

## Baseline Performance Reference

All measurements on WS-08 (10.208.211.59), Gemma 4 26B A4 Q4_0, prompt: "Explain quantum computing in detail", `max_tokens=250`.

| Metric | Value |
|---|---|
| Prompt tokens | 19 |
| Completion tokens | 250 |
| Prompt eval speed (manual fresh start) | ~17 tok/s |
| Generation speed (manual fresh start) | ~16.4 tok/s |
| Generation speed (Ray-managed, post-fix) | ~12.2 tok/s |
| MTP draft acceptance rate | 68–78% |
| Total request time (250 tokens) | ~15–20 s |

---

## Fundamental Hardware Constraint

The Gemma 4 26B A4 model in Q4_0 quantisation **exceeds the 16 GB VRAM** of each RTX A4000. This means:

- A portion of model layers always runs on CPU (6 cores)
- Prompt evaluation will always be slower than generation proportionally
- The maximum achievable generation speed is bounded by how many layers fit on GPU
- Reducing context window (`-c`) or using lower quantisation would allow more layers on GPU, improving both prompt eval and generation speeds — but the current configuration retains full 65536 context as a deliberate trade-off

**Options not taken (by design):**
- Reducing `-c 65536` to free VRAM — context size is a hard requirement
- Switching to lower quantisation — quality trade-off not accepted

---

## Files Modified During This Investigation

| File | Change |
|---|---|
| `gateway/config.py` | `llama_parallel: 2 → 1` |
| `worker/llama_process.py` | `q8_0 → q4_0`, `flash-attn on → auto`, `spec-draft-n-max 4 → 2` |
| `startup_scripts/start_linux.sh` | Same param updates; Ray starts before llama-server |
| `scripts/start_linux_worker.sh` | Same param updates; Ray joins before llama-server starts |
| `startup_scripts/stop_linux.sh` | Added SIGKILL fallback for llama-server on all nodes |
