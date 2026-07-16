# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""배포 후 검증 — installer state 기반 엔드포인트/헬스체크.

curl 을 subprocess 로 호출하되 예외를 던지지 않는다(실패는 결과값으로 표현).
rich 를 import 하지 않는다 — 렌더링은 cli.py 전담."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HEALTH_PATH = {"gateway": "/health", "admin-api": "/health", "admin-ui": "/"}
ORDER = ("gateway", "admin-api", "admin-ui")

# state JSON key → UI role
_STATE_KEYS = {
    "gateway": "gateway_alb_dns",
    "admin-ui": "admin_ui_alb_dns",
    # Prefer API Gateway public endpoint; fall back to internal ALB DNS
    "admin-api": "api_gateway_endpoint",
}
_STATE_FALLBACK = {
    "admin-api": "admin_api_alb_dns",
}


@dataclass
class Endpoint:
    role: str
    ingress_name: str  # resource label (kept for UI compatibility)
    hostname: str | None

    @property
    def url(self) -> str | None:
        if not self.hostname:
            return None
        if self.hostname.startswith("http://") or self.hostname.startswith("https://"):
            return self.hostname.rstrip("/")
        return f"http://{self.hostname}"


@dataclass
class Endpoints:
    items: list[Endpoint] = field(default_factory=list)
    error: str | None = None

    def by_role(self, role: str) -> Endpoint | None:
        return next((e for e in self.items if e.role == role), None)


@dataclass
class HealthResult:
    label: str
    state: str  # "ok" | "pending" | "check"
    detail: str


def default_state_path(env: str = "dev") -> Path:
    return Path(__file__).resolve().parents[1] / "ecs" / f".state-{env}.json"


def discover_endpoints(env: str = "dev", state_path: Path | None = None) -> Endpoints:
    """installer `.state-{env}.json` 에서 ALB/API GW DNS 를 읽는다."""
    path = state_path or default_state_path(env)
    if not path.is_file():
        return Endpoints(items=[], error=f"state 파일 없음: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return Endpoints(items=[], error=f"state 파싱 실패: {exc}")

    items: list[Endpoint] = []
    for role in ORDER:
        key = _STATE_KEYS[role]
        raw = (data.get(key) or "").strip()
        if not raw and role in _STATE_FALLBACK:
            raw = (data.get(_STATE_FALLBACK[role]) or "").strip()
        # api_gateway_endpoint may already be a full URL
        hostname = raw or None
        if hostname and hostname.startswith("https://"):
            # store full URL in hostname field; Endpoint.url handles it
            pass
        elif hostname and "://" in hostname:
            pass
        items.append(Endpoint(role=role, ingress_name=key, hostname=hostname or None))
    return Endpoints(items=items)


def _curl_status(url: str) -> int | None:
    try:
        proc = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "10", url],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    code = proc.stdout.strip()
    return int(code) if code.isdigit() and code != "000" else None


def live_healthcheck(endpoints: Endpoints) -> list[HealthResult]:
    results: list[HealthResult] = []
    for ep in endpoints.items:
        if not ep.hostname:
            results.append(HealthResult(label=ep.role, state="pending", detail="DNS 미준비"))
            continue
        path = HEALTH_PATH.get(ep.role, "/")
        code = _curl_status(f"{ep.url}{path}")
        if code is None:
            results.append(HealthResult(label=ep.role, state="pending", detail="연결 안 됨 (준비 중)"))
        elif 200 <= code < 400:
            results.append(HealthResult(label=ep.role, state="ok", detail=f"HTTP {code}"))
        else:
            results.append(HealthResult(label=ep.role, state="check", detail=f"HTTP {code}"))
    return results
