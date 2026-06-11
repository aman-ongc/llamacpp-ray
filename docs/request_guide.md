# API Request Guide

This guide covers how to send requests to the ONGC LLM platform once you have been issued an API key.

---

## Base URL and Authentication

**Base URL:** `http://10.208.211.62:18000`

Every request must include your API key in the `Authorization` header:

```
Authorization: Bearer <your-api-key>
```

**Rate limit:** 60 requests per 60-second sliding window. Exceeding this returns `429 Too Many Requests` with a `Retry-After` header.

---

## Endpoint

```
POST /v1/chat/completions
Content-Type: application/json
Authorization: Bearer <your-api-key>
```

---

## Request Parameters

| Parameter          | Type            | Default   | Description |
|--------------------|-----------------|-----------|-------------|
| `model`            | string          | `"ongc-llm"`  | Model identifier. Use `"ongc-llm"` for Gemma 4 26B. |
| `messages`         | array           | required  | Conversation turns. See examples below. |
| `max_tokens`       | integer         | `256`     | Maximum tokens to generate. |
| `temperature`      | float           | `0.7`     | Sampling temperature. `0.0` = deterministic, `1.0` = more creative. |
| `top_p`            | float           | `0.95`    | Nucleus sampling. Only tokens with cumulative probability up to this are considered. |
| `top_k`            | integer         | `40`      | Limits sampling pool to the top-k most likely tokens. |
| `min_p`            | float           | `0.05`    | Minimum probability relative to the top token. Cuts off unlikely tokens. |
| `repeat_penalty`   | float           | `1.1`     | Penalises recently used tokens. Values above `1.0` reduce repetition. |
| `stream`           | boolean         | `false`   | Set `true` to receive tokens as a server-sent event stream. |
| `enable_thinking`  | boolean or null | `null`    | Per-request thinking mode override. See Thinking Mode section. |
| `session_affinity` | boolean         | `false`   | Set `true` to pin your session to the same GPU worker for KV-cache reuse across turns. |

> `top_p`, `top_k`, `min_p`, and `repeat_penalty` are passed directly to llama.cpp.
> Leave them unset to use llama.cpp's own defaults.

---

## Examples

All examples use `curl`. Python equivalents are shown where useful.

### 1. Basic Text Request

```bash
curl --noproxy '*' http://10.208.211.62:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "ongc-llm",
    "messages": [
      {"role": "user", "content": "Explain what is a transformer model in simple terms."}
    ],
    "max_tokens": 512
  }'
```

---

### 2. Streaming Response

Add `"stream": true` to receive tokens as they are generated.

```bash
curl --noproxy '*' http://10.208.211.62:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "ongc-llm",
    "messages": [
      {"role": "user", "content": "Write a short poem about the ocean."}
    ],
    "max_tokens": 256,
    "stream": true
  }'
```

Each chunk arrives as:
```
data: {"choices": [{"delta": {"content": "token..."}, "index": 0, "finish_reason": null}]}

data: [DONE]
```

---

### 3. System Prompt

Pass a `system` role message as the first entry in `messages`.

```bash
curl --noproxy '*' http://10.208.211.62:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "ongc-llm",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant for ONGC engineers. Answer concisely and technically."},
      {"role": "user", "content": "What is the difference between a separator and a scrubber?"}
    ],
    "max_tokens": 512
  }'
```

---

### 4. Multi-Turn Conversation

Include the full conversation history in `messages` to maintain context.

```bash
curl --noproxy '*' http://10.208.211.62:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "ongc-llm",
    "messages": [
      {"role": "user", "content": "What is machine learning?"},
      {"role": "assistant", "content": "Machine learning is a branch of AI where systems learn patterns from data."},
      {"role": "user", "content": "Can you give a practical example?"}
    ],
    "max_tokens": 512
  }'
```

For long conversations where you want the model to reuse its KV cache (faster, more consistent responses), enable session affinity:

```json
"session_affinity": true
```

This pins your requests to the same GPU worker. Useful for interactive chat sessions.

---

### 5. Thinking Mode (Extended Reasoning)

Gemma 4 26B supports a thinking mode where the model reasons step-by-step before producing a final answer. This is useful for maths, logic, code, and complex analysis.

**Enable thinking for a single request:**
```bash
curl --noproxy '*' http://10.208.211.62:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "ongc-llm",
    "messages": [
      {"role": "user", "content": "A train travels 120 km in 1.5 hours. What is its speed in m/s?"}
    ],
    "max_tokens": 1024,
    "enable_thinking": true
  }'
```

**Disable thinking explicitly (standard response):**
```bash
curl --noproxy '*' http://10.208.211.62:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "ongc-llm",
    "messages": [
      {"role": "user", "content": "Summarise the following paragraph in one sentence: ..."}
    ],
    "max_tokens": 256,
    "enable_thinking": false
  }'
```

If `enable_thinking` is omitted, the server default is used (configured by the platform administrator — off by default).

> Thinking mode produces longer outputs. Set `max_tokens` high enough (1024+) when using it.

---

### 6. Image Input — Single Image

Vision (image + text) requests are handled by the dedicated multimodal node (WS-13, Qwen3-VL-8B). Send images as base64-encoded strings inside the `content` array.

```bash
# Encode image first
IMAGE_B64=$(base64 -w 0 /path/to/your/image.jpg)

curl --noproxy '*' http://10.208.211.62:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d "{
    \"model\": \"ongc-llm\",
    \"messages\": [
      {
        \"role\": \"user\",
        \"content\": [
          {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${IMAGE_B64}\"}},
          {\"type\": \"text\", \"text\": \"What equipment is shown in this image?\"}
        ]
      }
    ],
    \"max_tokens\": 512
  }"
```

**Python equivalent:**
```python
import base64, requests

with open("/path/to/image.jpg", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

response = requests.post(
    "http://10.208.211.62:18000/v1/chat/completions",
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    json={
        "model": "ongc-llm",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "What equipment is shown in this image?"},
                ],
            }
        ],
        "max_tokens": 512,
    },
    proxies={"http": None, "https": None},
)
print(response.json()["choices"][0]["message"]["content"])
```

---

### 7. Image + Text — Detailed Analysis

```python
import base64, requests

with open("plant_diagram.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

response = requests.post(
    "http://10.208.211.62:18000/v1/chat/completions",
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    json={
        "model": "ongc-llm",
        "messages": [
            {"role": "system", "content": "You are an expert petroleum engineer. Analyse diagrams precisely."},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "Identify all the process units in this P&ID diagram and list them."},
                ],
            },
        ],
        "max_tokens": 1024,
        "enable_thinking": true,
    },
    proxies={"http": None, "https": None},
)
```

---

### 8. Multiple Images

Pass multiple `image_url` entries in the `content` array. The model processes them in order.

```python
import base64, requests

def encode(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

b64_a = encode("before.jpg")
b64_b = encode("after.jpg")

response = requests.post(
    "http://10.208.211.62:18000/v1/chat/completions",
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    json={
        "model": "ongc-llm",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Compare these two images and describe the differences:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_a}"}},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_b}"}},
                ],
            }
        ],
        "max_tokens": 768,
    },
    proxies={"http": None, "https": None},
)
```

---

### 9. Controlling Output Style with Sampling Parameters

**Creative / varied output** — higher temperature, higher top_p:
```json
{
  "model": "ongc-llm",
  "messages": [{"role": "user", "content": "Write a creative story opening."}],
  "max_tokens": 512,
  "temperature": 0.9,
  "top_p": 0.95,
  "top_k": 50
}
```

**Deterministic / factual output** — low temperature:
```json
{
  "model": "ongc-llm",
  "messages": [{"role": "user", "content": "What is the boiling point of water at sea level?"}],
  "max_tokens": 128,
  "temperature": 0.1,
  "top_p": 1.0
}
```

**Reduce repetition** — raise repeat_penalty:
```json
{
  "model": "ongc-llm",
  "messages": [{"role": "user", "content": "Explain neural networks in detail."}],
  "max_tokens": 1024,
  "temperature": 0.7,
  "repeat_penalty": 1.15
}
```

**Conservative sampling with min_p** — filters tokens below a fraction of the top token's probability:
```json
{
  "temperature": 0.8,
  "top_p": 1.0,
  "min_p": 0.05
}
```

---

### 10. Streaming with Python

```python
import json, requests

def stream_chat(prompt: str, api_key: str):
    response = requests.post(
        "http://10.208.211.62:18000/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "ongc-llm",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "stream": True,
        },
        stream=True,
        proxies={"http": None, "https": None},
    )

    for line in response.iter_lines():
        if not line:
            continue
        line = line.decode()
        if line.startswith("data: "):
            payload = line[6:]
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            delta = chunk["choices"][0]["delta"].get("content", "")
            print(delta, end="", flush=True)
    print()

stream_chat("Describe the water injection process in oil fields.", "YOUR_API_KEY")
```

---

### 11. Using with OpenAI SDK

The platform is OpenAI-compatible, so the `openai` Python library works without modification.

```python
from openai import OpenAI

client = OpenAI(
    api_key="YOUR_API_KEY",
    base_url="http://10.208.211.62:18000/v1",
    http_client=__import__("httpx").Client(proxies={}),  # bypass corporate proxy
)

response = client.chat.completions.create(
    model="ongc-llm",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Summarise the key risks in offshore drilling."},
    ],
    max_tokens=512,
    temperature=0.7,
)

print(response.choices[0].message.content)
```

**Streaming with openai SDK:**
```python
stream = client.chat.completions.create(
    model="ongc-llm",
    messages=[{"role": "user", "content": "List ten safety protocols for a refinery."}],
    max_tokens=1024,
    stream=True,
)

for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

---

## Error Reference

| Status | Meaning |
|--------|---------|
| `401`  | Missing or invalid API key |
| `429`  | Rate limit exceeded (60 req/min). Wait and retry. |
| `500`  | Inference failed internally. Try again shortly. |

---

## Proxy Note

All requests from within the ONGC intranet should bypass the corporate proxy when calling this service directly. In `curl`, add `--noproxy '*'`. In Python with `requests`, pass `proxies={"http": None, "https": None}`. With the `openai` SDK, pass an `httpx.Client(proxies={})` as shown above.
