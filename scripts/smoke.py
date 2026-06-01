"""Internal smoke test for the gateway container."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    data = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"{method} {url} failed with {exc.code}: {body}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--admin-secret", default=os.getenv("ADMIN_SECRET", "changeme"))
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--skip-admin", action="store_true")
    parser.add_argument("--skip-chat", action="store_true")
    parser.add_argument("--basic-only", action="store_true")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    admin_headers = {"X-Admin-Secret": args.admin_secret}

    results: dict[str, object] = {}
    paths = ["/health", "/v1/models"] if args.basic_only else ["/health", "/ready", "/live", "/v1/models"]
    for path in paths:
        status, payload = request_json("GET", f"{base_url}{path}")
        results[path] = {"status": status, "payload": payload}

    if not args.skip_admin:
        try:
            status, user = request_json(
                "POST",
                f"{base_url}/admin/users",
                payload={"username": "smoke-user", "email": "smoke-user@ongc.co.in", "department": "IT"},
                headers=admin_headers,
            )
            results["create_user"] = {"status": status, "payload": user}
        except RuntimeError as exc:
            if "409" in str(exc):
                # User already exists — look up by listing users.
                _, users_list = request_json("GET", f"{base_url}/admin/users", headers=admin_headers)
                user = next((u for u in users_list if u["username"] == "smoke-user"), None)
                results["create_user"] = {"status": 409, "payload": {"note": "already exists", "id": user["id"] if user else None}}
            else:
                raise

        if results["create_user"]["payload"].get("username") or results["create_user"]["payload"].get("note"):
            status, key_payload = request_json(
                "POST",
                f"{base_url}/admin/users/smoke-user/keys",
                payload={"label": "smoke"},
                headers=admin_headers,
            )
            results["create_key"] = {"status": status, "payload": key_payload}

    if not args.skip_chat:
        status, chat_payload = request_json(
            "POST",
            f"{base_url}/v1/chat/completions",
            payload={
                "model": "qwen",
                "messages": [{"role": "user", "content": "Say hello in one sentence."}],
                "max_tokens": 32,
                "stream": False,
            },
            headers={"Authorization": f"Bearer {args.api_key}"},
        )
        results["chat"] = {"status": status, "payload": chat_payload}

    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
