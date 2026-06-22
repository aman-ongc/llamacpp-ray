# User Guide — LLM / VLM Inference Platform

This guide covers how to call the platform once you have an API key. All
requests are plain HTTP (OpenAI-compatible), non-streaming.

---

## 1. Endpoint

```
http://10.208.211.62:18000/v1/chat/completions
```

Always bypass the corporate proxy for this internal address:

```bash
curl --noproxy '*' ...
```

> **Windows users:** all examples below use `bash`-style single quotes
> (`'...'`). If you're running these from `cmd.exe` (not Git Bash / WSL),
> single quotes are **not** string delimiters there — your JSON body will
> silently arrive empty and you'll get
> `{"detail":[{"type":"json_invalid", ..., "input":{}}]}` back. On `cmd.exe`,
> use double quotes around the whole `-d` payload and escape the inner
> double quotes with `\`:
> ```bat
> curl --noproxy "*" -s -X POST -H "Authorization: Bearer <YOUR_API_KEY>" -H "Content-Type: application/json" -d "{\"model\":\"ongc-llm\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":256}" http://10.208.211.62:18000/v1/chat/completions
> ```
> PowerShell has its own quoting quirks too — if in doubt, use Git Bash/WSL
> and the `bash` examples as-is, or save the JSON body to a file and pass
> `-d @payload.json` instead of inlining it.

## 2. Authentication

Pass your API key as a Bearer token in the `Authorization` header:

```bash
-H "Authorization: Bearer <YOUR_API_KEY>"
```

Replace `<YOUR_API_KEY>` with the key you were given (starts with `sk-ongc-`).

---

## 3. Text request (LLM)

```bash
curl --noproxy '*' -s -X POST \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "ongc-llm",
        "messages": [
          {"role": "user", "content": "Summarize the key risks in a slope stability assessment."}
        ],
        "max_tokens": 512,
        "temperature": 0.7
      }' \
  http://10.208.211.62:18000/v1/chat/completions
```

Response (trimmed):
```json
{
  "model": "ongc-llm",
  "choices": [
    {"message": {"role": "assistant", "content": "..."}}
  ],
  "usage": {"prompt_tokens": 14, "completion_tokens": 238, "total_tokens": 252}
}
```

---

## 4. Image request (VLM)

To ask a question about an image, include an `image_url` content part in the
message alongside your text. The platform automatically detects this and
routes it to the vision model — no separate endpoint or flag needed.

```bash
curl --noproxy '*' -s -X POST \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "ongc-llm",
        "messages": [
          {
            "role": "user",
            "content": [
              {"type": "text", "text": "What rock formation is visible in this image?"},
              {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<BASE64_IMAGE_DATA>"}}
            ]
          }
        ],
        "max_tokens": 512
      }' \
  http://10.208.211.62:18000/v1/chat/completions
```

`image_url.url` accepts either:
- A `data:image/...;base64,<...>` inline data URI (most common), or
- A reachable `http(s)://` URL the gateway can fetch directly.

To base64-encode a local image file:

```bash
base64 -w0 photo.jpg
```

---

## 5. Request parameters

| Field | Type | Default | Notes |
|---|---|---|---|
| `model` | string | `ongc-llm` | Fixed alias — routing to the right backend (text/vision) is automatic and transparent. Always send this value. |
| `messages` | array | required | List of `{role, content}`. `role` is `user`, `assistant`, or `system`. `content` is a string for plain text, or a list of `{type: "text"|"image_url", ...}` parts for image requests. |
| `max_tokens` | int | `256` | Maximum tokens to generate in the response. Raise this for longer answers. |
| `temperature` | float | `0.7` | Higher = more random/creative; lower (e.g. `0.1`–`0.3`) = more deterministic/focused. Use low values for factual/technical Q&A. |
| `top_p` | float | engine default | Nucleus sampling cutoff. Lower values (e.g. `0.8`) narrow the model to more likely tokens. |
| `top_k` | int | engine default | Restricts sampling to the top-K most likely tokens. Common value: `20`. |
| `presence_penalty` | float | engine default | Penalizes tokens that already appeared, reducing repetition. Typical range `0`–`2`. |
| `enable_thinking` | bool | `false` | Convenience flag for extended step-by-step reasoning before the final answer. See **Thinking mode** below before turning this on. |
| `chat_template_kwargs` | object | none | Advanced/raw passthrough to the model's chat template — e.g. `{"enable_thinking": false}`. Any key here overrides the `enable_thinking` flag above. |

Multi-turn conversation: include prior turns in `messages` in order
(`user`, `assistant`, `user`, ...) — the platform does not retain history
between requests, so the full conversation must be resent each call.

---

## 6. Thinking mode (`enable_thinking`)

Some requests benefit from the model reasoning step-by-step before answering
(e.g. multi-step calculations or logical analysis). This is **off by
default**. To turn it on:

```bash
curl --noproxy '*' -s -X POST \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "ongc-llm",
        "messages": [{"role": "user", "content": "A borehole log shows 3 fault zones at depths of 120m, 340m, and 510m. What is the average spacing between them?"}],
        "max_tokens": 1024,
        "enable_thinking": true
      }' \
  http://10.208.211.62:18000/v1/chat/completions
```

Equivalent raw form (matches the OpenAI SDK `extra_body` style, if you're
calling this via the `openai` Python client instead of curl):

```python
chat_response = client.chat.completions.create(
    model="ongc-llm",
    messages=messages,
    max_tokens=1024,
    temperature=0.7,
    top_p=0.8,
    presence_penalty=1.5,
    extra_body={
        "top_k": 20,
        "chat_template_kwargs": {"enable_thinking": False},
    },
)
```

**Important:** when thinking mode is on, set `max_tokens` generously
(`1024`+). The reasoning itself consumes tokens from your `max_tokens`
budget — if it's too low, the model can use up the entire budget reasoning
and return an empty final answer.

---

## 7. Common errors

| HTTP status | Meaning | What to do |
|---|---|---|
| `401` | Missing/invalid API key | Check the `Authorization: Bearer ...` header. |
| `429` | Rate limit exceeded | You're sending requests faster than your per-minute quota. Wait and retry (a `Retry-After` header is included). |
| `503` | Server busy (queue full) | The pool of GPU workers is fully booked. Retry after a short delay. |
| `500` | Internal error | Transient — retry. If it persists, report it. |

---

## 8. Quick checklist

- [ ] Use `--noproxy '*'` on every curl call.
- [ ] Send `Authorization: Bearer <key>` on every request.
- [ ] Always set `"model": "ongc-llm"`.
- [ ] For images, base64-encode and embed as a `data:` URI inside `content`.
- [ ] Resend full conversation history each call (no server-side memory).
- [ ] Only set `enable_thinking: true` for genuinely hard, multi-step problems, and raise `max_tokens` (1024+) when you do.
