#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""llm-gateway-test.py — ECS LLM Gateway smoke test + usage check.

Prereqs:
  - gateway-cli login → ~/.gateway-cli/oidc-tokens.json
  - Cognito group matching OIDC_GROUP_PREFIX (e.g. Claude_dev)
  - deployment/ecs/.state-<env>.json  or env vars

Usage:
  python3 llm-gateway-test.py
  python3 llm-gateway-test.py --state deployment/ecs/.state-dev.json
  python3 llm-gateway-test.py --model claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_STATE = Path(__file__).resolve().parent / "deployment" / "ecs" / ".state-dev.json"
OIDC_PATH = Path.home() / ".gateway-cli" / "oidc-tokens.json"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 60,
) -> tuple[int, Any]:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype and raw:
                return resp.status, json.loads(raw)
            return resp.status, raw.decode("utf-8", errors="replace") if raw else ""
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", errors="replace")
        return e.code, payload


def load_endpoints(state_path: Path) -> dict[str, str]:
    gw = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    api = os.environ.get("ADMIN_API_URL", "").rstrip("/")
    if state_path.is_file():
        state = json.loads(state_path.read_text())
        gw = gw or f"http://{state['gateway_alb_dns']}"
        api = api or state["api_gateway_endpoint"].rstrip("/")
    if not gw or not api:
        raise SystemExit(
            "Missing endpoints. Set ANTHROPIC_BASE_URL + ADMIN_API_URL "
            f"or provide --state (tried {state_path})"
        )
    return {"gateway": gw.rstrip("/"), "admin_api": api.rstrip("/")}


def exchange_vk(admin_api: str) -> str:
    if not OIDC_PATH.is_file():
        raise SystemExit(
            f"No OIDC tokens at {OIDC_PATH}. Run: gateway-cli login "
            "--timeout 600 --redirect-port 8091"
        )
    tokens = json.loads(OIDC_PATH.read_text())
    # Prefer id_token (has email / cognito:groups for provisioning)
    bearer = tokens.get("id_token") or tokens.get("access_token")
    if not bearer:
        raise SystemExit("oidc-tokens.json has no id_token/access_token")

    status, body = _http(
        "POST",
        f"{admin_api}/v1/auth/exchange",
        headers={"Authorization": f"Bearer {bearer}"},
        body={"device_name": "llm-gateway-test"},
        timeout=30,
    )
    if status != 200 or not isinstance(body, dict) or "virtual_key" not in body:
        raise SystemExit(
            f"VK exchange failed HTTP {status}: {body}\n"
            "Hint: Cognito user needs a group like Claude_<team> "
            "(e.g. Claude_dev). Then refresh login / tokens."
        )
    return body["virtual_key"]


def print_usage(label: str, data: Any) -> None:
    print(f"\n=== usage/me ({label}) ===")
    if not isinstance(data, dict):
        print(data)
        return
    usage = data.get("usage") or {}
    budget = data.get("budget") or {}
    print(f"  period:     {data.get('period')}")
    print(f"  user_id:    {data.get('user_id')}")
    print(f"  tokens:     {usage.get('total_tokens')}")
    print(f"  cost_usd:   {usage.get('total_cost_usd')}")
    print(
        f"  budget:     used={budget.get('used_usd')} / "
        f"max={budget.get('max_usd')} ({budget.get('pct')}%) "
        f"policy={budget.get('policy')}"
    )
    models = data.get("model_breakdown") or []
    if models:
        print("  models today:")
        for m in models:
            print(
                f"    - {m.get('model')}: req={m.get('requests')} "
                f"in={m.get('input_tokens')} out={m.get('output_tokens')} "
                f"cost={m.get('cost_usd')}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM Gateway smoke + usage test")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--model", default=os.environ.get("GATEWAY_TEST_MODEL", DEFAULT_MODEL))
    parser.add_argument("--skip-messages", action="store_true", help="Only health + usage")
    args = parser.parse_args()

    ep = load_endpoints(args.state)
    gw, api = ep["gateway"], ep["admin_api"]
    print("Endpoints")
    print(f"  gateway:   {gw}")
    print(f"  admin_api: {api}")
    print(f"  model:     {args.model}")

    # 1) Health
    print("\n=== health ===")
    for name, url in (
        ("gateway", f"{gw}/health"),
        ("admin_api", f"{api}/health"),
    ):
        status, body = _http("GET", url, timeout=15)
        ok = "OK" if status == 200 else "FAIL"
        print(f"  {name}: HTTP {status} {ok}  {body!r}"[:120])
        if status != 200:
            return 1

    # 2) VK
    print("\n=== auth exchange ===")
    vk = exchange_vk(api)
    print(f"  virtual_key: {vk[:12]}…{vk[-4:]} (len={len(vk)})")

    auth = {"Authorization": f"Bearer {vk}", "anthropic-version": "2023-06-01"}

    # 3) Usage before
    status, before = _http("GET", f"{gw}/v1/usage/me", headers=auth, timeout=30)
    if status != 200:
        print(f"  usage/me before FAILED HTTP {status}: {before}")
        return 1
    print_usage("before", before)

    if args.skip_messages:
        return 0

    # 4) Messages (cheap Haiku by default)
    print("\n=== POST /v1/messages ===")
    status, msg = _http(
        "POST",
        f"{gw}/v1/messages",
        headers=auth,
        body={
            "model": args.model,
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly: GATEWAY-OK",
                }
            ],
        },
        timeout=120,
    )
    print(f"  HTTP {status}")
    if status != 200:
        print(f"  body: {msg}")
        return 1

    # Extract text
    text = ""
    if isinstance(msg, dict):
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        usage = msg.get("usage") or {}
        print(f"  reply: {text!r}")
        print(f"  usage: {usage}")
        print(f"  model: {msg.get('model')}")
    else:
        print(f"  body: {msg}")

    # 5) Usage after (Redis daily aggregate; may lag briefly / need cost-recorder)
    time.sleep(2)
    status, after = _http("GET", f"{gw}/v1/usage/me", headers=auth, timeout=30)
    if status != 200:
        print(f"  usage/me after FAILED HTTP {status}: {after}")
        return 1
    print_usage("after", after)

    before_tokens = 0
    after_tokens = 0
    if isinstance(before, dict):
        before_tokens = int((before.get("usage") or {}).get("total_tokens") or 0)
    if isinstance(after, dict):
        after_tokens = int((after.get("usage") or {}).get("total_tokens") or 0)

    print("\nPASS — gateway health + VK + /v1/messages OK")
    if after_tokens > before_tokens:
        print(f"Usage increased: {before_tokens} → {after_tokens} tokens")
    else:
        print(
            "Note: /v1/usage/me totals may still be 0 right after a call "
            "(async cost-recorder / Redis). Per-response usage above is authoritative. "
            "Admin UI also shows usage/analytics."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
