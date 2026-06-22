# Creating Users & API Keys

> Admin-only operations against the FastAPI gateway. Run these from WS-11 (the
> controller) or anywhere that can reach the gateway port.

---

## Prerequisites

- The gateway must be running (`sudo docker compose up -d gateway`).
- You need the admin secret, stored in `.env` as `ADMIN_SECRET`. Every admin
  request must send it as the `X-Admin-Secret` header.
- The gateway listens on container port `8000`, published on the host as
  `18000` (see `docker-compose.yml` / `docker ps`). All examples below use
  `http://localhost:18000`.
- Bypass the corporate proxy for localhost calls:
  ```bash
  curl --noproxy '*' ...
  ```

Load the admin secret into a shell variable so you don't paste it into every command:

```bash
cd /home/administrator/projects/llm-inference-service
ADMIN_SECRET=$(grep -E '^ADMIN_SECRET=' .env | cut -d= -f2-)
```

---

## 1. Create a user

Every API key belongs to a user. Users are identified by a unique
`username` and `email`; `department` is free text used for audit/grouping.

```bash
curl --noproxy '*' -s -X POST \
  -H "X-Admin-Secret: $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
        "username": "ipeot-geotech",
        "email": "ipeot-geotech@ongc.co.in",
        "department": "IPEOT-Geotech"
      }' \
  http://localhost:18000/admin/users
```

Response:
```json
{"id": 10, "username": "ipeot-geotech", "email": "ipeot-geotech@ongc.co.in", "department": "IPEOT-Geotech"}
```

If the username or email already exists, this returns `409 Conflict`.

List existing users:

```bash
curl --noproxy '*' -s -H "X-Admin-Secret: $ADMIN_SECRET" http://localhost:18000/admin/users | python3 -m json.tool
```

---

## 2. Create an API key for that user

```bash
curl --noproxy '*' -s -X POST \
  -H "X-Admin-Secret: $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"label": "ipeot-geotech"}' \
  http://localhost:18000/admin/users/ipeot-geotech/keys
```

Response:
```json
{
  "username": "ipeot-geotech",
  "api_key": "sk-ongc-LuDs3CKKmwHRj0LWdcszeHDLWnNACupB82cj5rsUcVE",
  "key_prefix": "sk-ongc-LuDs",
  "label": "ipeot-geotech",
  "metadata": null
}
```

**The `api_key` value is shown exactly once.** Only its hash is stored in
Postgres (`api_keys.key_hash`) — there is no way to retrieve or recover the
plaintext key after this response. Copy it immediately and hand it to
whoever needs it (e.g. paste into a password manager / secrets store).

`label` is just a free-text tag (e.g. team or application name) — a single
user can hold multiple keys with different labels via repeated calls to this
endpoint.

The optional `metadata` field accepts any string (e.g. a ticket number or
description):

```bash
-d '{"label": "ipeot-geotech", "metadata": "requested via INC-1234"}'
```

---

## 3. Use the key

The gateway exposes OpenAI-compatible endpoints. Pass the key as a Bearer token:

```bash
curl --noproxy '*' -s -X POST \
  -H "Authorization: Bearer sk-ongc-LuDs3CKKmwHRj0LWdcszeHDLWnNACupB82cj5rsUcVE" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "ongc-llm",
        "messages": [{"role": "user", "content": "hello"}]
      }' \
  http://10.208.211.62:18000/v1/chat/completions
```

---

## 4. Check usage for a user

```bash
curl --noproxy '*' -s -H "X-Admin-Secret: $ADMIN_SECRET" \
  http://localhost:18000/admin/users/ipeot-geotech/usage | python3 -m json.tool
```

---

## Troubleshooting

- **`403 Forbidden`** — `X-Admin-Secret` header is missing or doesn't match
  `ADMIN_SECRET` in `.env`.
- **`404 Not Found` on `/admin/users/{username}/keys`** — the user doesn't
  exist yet; create it first (step 1).
- **`500 Internal Server Error` on key creation** — this was a real bug in
  `gateway/routers/admin.py` (fixed 2026-06-22): the endpoint's response type
  didn't allow `metadata: null`, so omitting `metadata` in the request caused
  a `ResponseValidationError` *after* the key was already committed to
  Postgres — the key got created but its plaintext was never returned to the
  caller, leaving an unusable orphaned row. If you ever hit this again on an
  older gateway image, check for an orphaned row and delete it rather than
  trying to recover the key:
  ```bash
  sudo docker exec llm-postgres psql -U llm -d llm_platform -c \
    "select id, key_prefix, label from api_keys where label='<label>';"
  sudo docker exec llm-postgres psql -U llm -d llm_platform -c \
    "delete from api_keys where id=<id>;"
  ```
  Then retry key creation against a gateway build that includes the fix.
