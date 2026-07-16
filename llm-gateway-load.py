#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Send N chat completions through ECS LLM Gateway (default: Sonnet 4.6 × 100).

Prereqs: gateway-cli login, Cognito team group (e.g. Claude_dev), team budget > 0.

  python3 llm-gateway-load.py
  python3 llm-gateway-load.py --count 100 --concurrency 5
  python3 llm-gateway-load.py --model claude-sonnet-4-6 --state deployment/ecs/.state-dev.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any

DEFAULT_STATE = Path(__file__).resolve().parent / "deployment" / "ecs" / ".state-dev.json"
OIDC_PATH = Path.home() / ".gateway-cli" / "oidc-tokens.json"
DEFAULT_MODEL = "claude-sonnet-4-6"

_print_lock = Lock()


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 120,
) -> tuple[int, Any, float]:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = time.perf_counter() - t0
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype and raw:
                return resp.status, json.loads(raw), elapsed
            return resp.status, raw.decode("utf-8", errors="replace") if raw else "", elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", errors="replace")
        return e.code, payload, elapsed
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return 0, str(e), elapsed


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
            f"or --state (tried {state_path})"
        )
    return {"gateway": gw.rstrip("/"), "admin_api": api.rstrip("/")}


def exchange_vk(admin_api: str) -> str:
    if not OIDC_PATH.is_file():
        raise SystemExit(f"No OIDC tokens at {OIDC_PATH}. Run gateway-cli login first.")
    tokens = json.loads(OIDC_PATH.read_text())
    bearer = tokens.get("id_token") or tokens.get("access_token")
    status, body, _ = _http(
        "POST",
        f"{admin_api}/v1/auth/exchange",
        headers={"Authorization": f"Bearer {bearer}"},
        body={"device_name": "llm-gateway-load"},
        timeout=30,
    )
    if status != 200 or not isinstance(body, dict) or "virtual_key" not in body:
        raise SystemExit(f"VK exchange failed HTTP {status}: {body}")
    return body["virtual_key"]


def one_call(
    i: int,
    total: int,
    gw: str,
    vk: str,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    status, body, elapsed = _http(
        "POST",
        f"{gw}/v1/messages",
        headers={
            "Authorization": f"Bearer {vk}",
            "anthropic-version": "2023-06-01",
        },
        body={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": f"Reply with exactly: LOAD-{i:03d}-OK",
                }
            ],
        },
        timeout=180,
    )
    in_tok = out_tok = 0
    text = ""
    err = ""
    if status == 200 and isinstance(body, dict):
        usage = body.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        for block in body.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
    else:
        err = str(body)[:200]

    with _print_lock:
        mark = "OK" if status == 200 else "FAIL"
        print(
            f"[{i:03d}/{total}] {mark} HTTP {status}  "
            f"{elapsed:.2f}s  in={in_tok} out={out_tok}  {text[:40]!r}"
            + (f"  err={err}" if err else "")
        )
    return {
        "i": i,
        "status": status,
        "elapsed": elapsed,
        "in": in_tok,
        "out": out_tok,
        "ok": status == 200,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="LLM Gateway load traffic (Sonnet 4.6)")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--model", default=os.environ.get("GATEWAY_LOAD_MODEL", DEFAULT_MODEL))
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-tokens", type=int, default=32)
    args = p.parse_args()

    ep = load_endpoints(args.state)
    gw, api = ep["gateway"], ep["admin_api"]
    print(f"gateway={gw}")
    print(f"admin_api={api}")
    print(f"model={args.model}  count={args.count}  concurrency={args.concurrency}")

    vk = exchange_vk(api)
    print(f"vk={vk[:12]}…\n")

    t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [
            ex.submit(one_call, i, args.count, gw, vk, args.model, args.max_tokens)
            for i in range(1, args.count + 1)
        ]
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())

    wall = time.perf_counter() - t0
    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    total_in = sum(r["in"] for r in results)
    total_out = sum(r["out"] for r in results)
    latencies = sorted(r["elapsed"] for r in results if r["ok"])

    print("\n=== summary ===")
    print(f"  ok/fail:     {ok}/{fail}")
    print(f"  wall time:   {wall:.1f}s  ({ok / wall:.2f} rps ok)" if wall > 0 else "")
    print(f"  tokens:      in={total_in} out={total_out} total={total_in + total_out}")
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(f"  latency:     p50={p50:.2f}s  p95={p95:.2f}s  max={latencies[-1]:.2f}s")

    # usage/me snapshot
    status, usage, _ = _http(
        "GET",
        f"{gw}/v1/usage/me",
        headers={"Authorization": f"Bearer {vk}"},
        timeout=30,
    )
    if status == 200 and isinstance(usage, dict):
        u = usage.get("usage") or {}
        b = usage.get("budget") or {}
        print(
            f"  usage/me:    tokens={u.get('total_tokens')} "
            f"cost={u.get('total_cost_usd')} "
            f"budget={b.get('used_usd')}/{b.get('max_usd')}"
        )

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
