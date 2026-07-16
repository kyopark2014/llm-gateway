# LLM Gateway 클라이언트 연결 가이드

Claude Code / Codex / Cowork 를 **현재 ECS 배포** LLM Gateway에 붙이는 방법입니다.  
최초 셋업은 약 **5분**, 이후 Virtual Key(VK) 발급·갱신은 자동입니다.

```
클라이언트 (로컬)
        │  OIDC 로그인 + VK
        │  (gateway-cli / api-key-helper)
        ▼
┌───────────────────────────────────────┐
│  ECS 배포 (installer.py)              │
│  ANTHROPIC_BASE_URL → gateway ALB     │
│  ADMIN_API_URL      → API Gateway     │
│  OIDC               → Cognito         │
└───────────────────────────────────────┘
        │
        ▼
   AWS Bedrock
```

배포·엔드포인트: [README.md](README.md) · `python3 installer.py status`

---

## 목차

1. [엔드포인트 매핑 (ECS)](#1-엔드포인트-매핑-ecs)
2. [사전 준비](#2-사전-준비)
3. [한눈에 보는 흐름](#3-한눈에-보는-흐름)
4. [공통 — gateway-cli 로그인](#4-공통--gateway-cli-로그인)
5. [Claude Code](#5-claude-code)
6. [Codex](#6-codex)
7. [Cowork](#7-cowork)
8. [연결 확인 · 일상 사용](#8-연결-확인--일상-사용)
9. [원복 / FAQ](#9-원복--faq)
10. [트러블슈팅](#10-트러블슈팅)
11. [명령어 요약](#11-명령어-요약)

---

## 1. 엔드포인트 매핑 (ECS)

installer가 만든 진입점과 클라이언트 환경변수는 **1:1로 대응**합니다.  
커스텀 도메인(`gateway.example.com`)이 아니라 **ALB DNS / API Gateway URL**을 그대로 씁니다.

| 클라이언트 변수 | installer 출력 / state 키 | 용도 |
|-----------------|---------------------------|------|
| `ANTHROPIC_BASE_URL` | `Gateway (data plane)` → `gateway_alb_dns` | Claude Code 추론 (Messages API, SSE) |
| `ADMIN_API_URL` | `Admin API (API GW REST)` → `api_gateway_endpoint` | VK 발급 (`/v1/auth/exchange`) |
| `OIDC_ISSUER_URL` | Cognito issuer → `cognito_issuer_url` | IdP |
| `OIDC_CLIENT_ID` | Cognito app client → `cognito_client_id` | OIDC PKCE |

| 쓰지 않는 URL | 이유 |
|---------------|------|
| Admin API **ALB** (`admin_api_alb_dns`) | BI chat SSE / 내부용. **VK 발급·일반 REST는 API Gateway** |
| Admin UI ALB | 브라우저 대시보드용 (Claude Code 불필요) |

### 1.1 운영자: 값 뽑는 방법

```bash
cd deployment/ecs
python3 installer.py status -c config.yaml
```

출력 예시:

```
 Gateway (data plane):  http://llm-gateway-<env>-gw-….elb.amazonaws.com
 Admin API (API GW REST): https://….execute-api.<region>.amazonaws.com

 Client env:
   ANTHROPIC_BASE_URL=http://…
   ADMIN_API_URL=https://…
```

또는 state JSON에서:

```bash
STATE=deployment/ecs/.state-dev.json   # env 이름에 맞게

jq -r '
  "export ANTHROPIC_BASE_URL=http://\(.gateway_alb_dns)",
  "export ADMIN_API_URL=\(.api_gateway_endpoint)",
  "export OIDC_ISSUER_URL=\(.cognito_issuer_url)",
  "export OIDC_CLIENT_ID=\(.cognito_client_id)"
' "$STATE"
```

Hosted UI (최초 비밀번호 변경용):

```text
https://<cognito_domain>.auth.<region>.amazoncognito.com/login
```

`cognito_domain` 은 state의 `cognito_domain` 키.

### 1.2 값 형태 예시 (플레이스홀더)

> **실제 URL·Client ID·Account ID는 문서에 넣지 마세요.** 운영자가 `installer.py status` / `.state-<env>.json` 으로 전달한 값만 사용합니다.

| 변수 | 형태 예시 |
|------|-----------|
| `OIDC_ISSUER_URL` | `https://cognito-idp.<region>.amazonaws.com/<user_pool_id>` |
| `OIDC_CLIENT_ID` | Cognito app client ID (운영자 제공) |
| `ADMIN_API_URL` | `https://<api-id>.execute-api.<region>.amazonaws.com` |
| `ANTHROPIC_BASE_URL` | `http://<gateway-alb-dns>` 또는 HTTPS 커스텀 도메인 |
| Hosted UI | `https://<cognito_domain>.auth.<region>.amazoncognito.com/login` |

> **HTTP 안내:** installer 기본 ECS 스택은 gateway/admin-ui ALB가 **HTTP**일 수 있습니다. Claude Code·gateway-cli는 HTTP ALB로도 동작합니다.  
> Cowork는 문서상 **HTTPS 필수**라, TLS(커스텀 도메인/CloudFront 등) 없이 Cowork 연동은 불가합니다.

---

## 2. 사전 준비

### 2.1 운영자에게 받을 값 (필수 4개)

위 [§1](#1-엔드포인트-매핑-ecs) 표의 네 변수. 추가로:

| 항목 | 설명 |
|------|------|
| Cognito 계정 (이메일) | Pool에 등록된 본인 계정 |
| 임시 패스워드 | 최초 Hosted UI에서 변경 |
| Hosted UI URL | §1.1 공식으로 조합, 또는 운영자 안내 |

> Cognito 콜백은 installer 기본값 `http://localhost:8090/callback`, `http://localhost:8091/callback` — `gateway-cli login` PKCE와 맞춰져 있습니다.  
> Hosted UI **도메인**이 User Pool에 있어야 합니다. 없으면 브라우저에 `BadRequest` JSON이 뜹니다 (`installer.py provision`/`deploy`가 생성).  
> **권장 로그인:** `gateway-cli login --timeout 600 --redirect-port 8091` (CLI **0.1.1+**). macOS에서 `oidc_dns_fallback` warning + `Login successful` 은 정상입니다.

### 2.2 로컬에 있어야 할 것

| 도구 | 확인 |
|------|------|
| **Claude Code** | `claude --version` (없으면 `npm install -g @anthropic-ai/claude-code`) |
| **gateway-cli** + **api-key-helper** | 아래 설치 절 |
| 브라우저 | OIDC PKCE 로그인 |
| 네트워크 | ALB·API GW·Cognito에 도달 가능 (사내망/VPN 정책 확인) |

---

## 3. 한눈에 보는 흐름

```
① 환경변수 4개 설정   ← installer status / state 에서
② gateway-cli 설치 + login   ← 세 클라이언트 공통
③ 클라이언트별 연동
   · Claude Code → gateway-cli setup → claude
   · Codex       → ~/.codex/config.toml + GATEWAY_VK
   · Cowork      → 앱 config JSON (HTTPS 필수)
```

| 단계 | 담당 | 설명 |
|------|------|------|
| SSO 로그인 | `gateway-cli login` | OIDC → `~/.gateway-cli/oidc-tokens.json` |
| VK 발급 | `api-key-helper` / exchange | `ADMIN_API_URL` (API GW) `/v1/auth/exchange` |
| API 호출 | 클라이언트 | `ANTHROPIC_BASE_URL` (gateway ALB) + Bearer VK |

---

## 4. 공통 — gateway-cli 로그인

저장소가 있고 macOS/Linux를 쓰는 경우 가장 빠릅니다.

```bash
# 운영자 제공 값 — 또는 jq로 state에서 export (§1.1)
export OIDC_ISSUER_URL="https://cognito-idp.<region>.amazonaws.com/<user_pool_id>"
export OIDC_CLIENT_ID="<cognito_app_client_id>"
export ADMIN_API_URL="https://<api-id>.execute-api.<region>.amazonaws.com"
export ANTHROPIC_BASE_URL="http://<gateway-alb-dns>"

# 리포 루트 — gateway-cli ≥ 0.1.1 권장 (Cognito DNS 폴백 포함)
uv tool install --force --from ./gateway-cli gateway-cli
gateway-cli version   # gateway-cli 0.1.1 이상

# 로그인만 먼저 (Sign up 포함 시 시간 여유)
gateway-cli login --timeout 600 --redirect-port 8091

# Claude Code 연동
gateway-cli setup \
  --gateway-url "$ANTHROPIC_BASE_URL" \
  --admin-api-url "$ADMIN_API_URL"
```

또는 원클릭 온보딩(기본 timeout 300초·포트 8090):

```bash
bash scripts/onboard-macos-linux.sh --setup-claude-code
```

Sign up·이메일 확인이 길면 온보딩 스크립트 대신 위의 `login --timeout 600` 을 쓰세요.

**Windows (관리자 PowerShell):**

```powershell
$env:OIDC_ISSUER_URL="..."
$env:OIDC_CLIENT_ID="..."
$env:ADMIN_API_URL="..."      # https://….execute-api….amazonaws.com
$env:ANTHROPIC_BASE_URL="..." # http://….elb.amazonaws.com

.\scripts\onboard-windows.ps1 -SetupClaudeCode
```

### 로그인 성공 시 모습

브라우저에서 Cognito **Sign in**(최초만 Sign up) → `localhost:8091/callback` → 터미널:

```text
Login successful.
  IDP:       https://cognito-idp.…amazonaws.com/…
  Client ID: …
  Token TTL: 3599s
  Refresh:   yes (auto-refresh enabled)
```

macOS에서 아래 **warning JSON**(`oidc_dns_fallback`)이 한 줄 나와도 정상입니다.  
`getaddrinfo` 실패 시 CLI가 `dig`로 IP를 잡아 토큰 교환한 로그입니다. **`Login successful`이면 무시해도 됩니다.**

```text
{"event": "oidc_dns_fallback", "host": "….amazoncognito.com", "ip": "…", "status": 200, ...}
```

이후:

```bash
# Claude Code 완전 종료 후
claude
```

---

## 5. Claude Code

온보딩 스크립트 대신 수동으로 할 때, 또는 §4 이후 세부 설정이 필요할 때.

### 5.1 gateway-cli 설치

**옵션 A — 운영자 패키지**

```bash
curl -L "<운영자 download URL>" -o gateway-cli.tar.gz
tar xzf gateway-cli.tar.gz
sudo mv gateway-cli api-key-helper /usr/local/bin/
```

**옵션 B — 소스 + uv (권장)**

```bash
# 리포 루트에서 — 패치 반영 시 --force
uv tool install --force --from ./gateway-cli gateway-cli
```

```bash
gateway-cli version    # 0.1.1 이상 (Cognito DNS 폴백)
which api-key-helper
```

| 바이너리 | 역할 |
|----------|------|
| `gateway-cli` | login / setup / status / disable |
| `api-key-helper` | Claude Code가 호출하는 VK 헬퍼 |

### 5.2 (최초 1회) Cognito 임시 패스워드 변경

1. Hosted UI URL 접속 (§1)  
2. 이메일 + 임시 패스워드  
3. 새 패스워드 설정  
4. 브라우저 닫기  

이미 비밀번호를 바꿨으면 건너뜁니다.

### 5.3 환경변수

```bash
# ~/.zshrc 또는 ~/.bashrc
export OIDC_ISSUER_URL="..."
export OIDC_CLIENT_ID="..."
export ADMIN_API_URL="..."        # API Gateway (https://….execute-api…)
export ANTHROPIC_BASE_URL="..."   # gateway ALB (http://….elb.amazonaws.com)

source ~/.zshrc
```

> 셸 변수는 `gateway-cli` / `api-key-helper`용.  
> Claude Code 프로세스용 값은 다음 `setup`이 settings에 넣습니다.

### 5.4 OIDC 로그인

권장 (Sign up·이메일 확인 여유 + 8090 점유 회피):

```bash
gateway-cli login --timeout 600 --redirect-port 8091
# 또는 명시적으로:
gateway-cli login \
  --issuer-url "$OIDC_ISSUER_URL" \
  --client-id "$OIDC_CLIENT_ID" \
  --timeout 600 \
  --redirect-port 8091
```

1. 브라우저에서 Cognito **Sign in** (최초만 Sign up → 이메일 확인)  
2. `http://localhost:8091/callback` 으로 돌아오면 성공  
3. 터미널에 `Login successful` (토큰 → `~/.gateway-cli/oidc-tokens.json`, 권한 `0600`)

`oidc_dns_fallback` warning JSON이 나와도 **`Login successful`이면 정상** (§4 참고).

### 5.5 Claude Code 연동 (`setup`)

```bash
gateway-cli setup \
  --gateway-url "$ANTHROPIC_BASE_URL" \
  --admin-api-url "$ADMIN_API_URL"
```

> macOS/Linux에서 `/etc/claude-code/managed-settings.d/` 기록 시 **sudo** 암호를 물을 수 있습니다.

성공 시 예시:

```
  Gateway URL:     http://llm-gateway-<env>-gw-….elb.amazonaws.com
  Admin API URL:   https://….execute-api.<region>.amazonaws.com
  API Key Helper:  /usr/local/bin/api-key-helper

  Gateway enabled: /etc/claude-code/managed-settings.d/50-gateway.json
```

| OS | 설정 경로 |
|----|-----------|
| macOS / Linux | `/etc/claude-code/managed-settings.d/50-gateway.json` (권장) 또는 `~/.config/Claude/settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.d\50-gateway.json` |

settings 핵심:

```json
{
  "apiKeyHelper": "/usr/local/bin/api-key-helper",
  "env": {
    "ANTHROPIC_BASE_URL": "http://….elb.amazonaws.com",
    "ADMIN_API_URL": "https://….execute-api….amazonaws.com",
    "OIDC_ISSUER_URL": "...",
    "OIDC_CLIENT_ID": "..."
  }
}
```

### 5.6 수동 settings (`setup` 실패 시)

```bash
mkdir -p ~/.config/Claude
cat > ~/.config/Claude/settings.json <<EOF
{
  "apiKeyHelper": "$(which api-key-helper)",
  "env": {
    "ANTHROPIC_BASE_URL": "${ANTHROPIC_BASE_URL}",
    "ADMIN_API_URL": "${ADMIN_API_URL}",
    "OIDC_ISSUER_URL": "${OIDC_ISSUER_URL}",
    "OIDC_CLIENT_ID": "${OIDC_CLIENT_ID}"
  }
}
EOF
```

> managed-settings와 개인 settings가 **둘 다** 있으면 managed가 우선합니다.

### 5.7 Claude Code 실행

```bash
# Claude Code 완전 종료 후
claude
```

첫 요청: `apiKeyHelper` → API GW에서 VK 발급 → gateway ALB로 Messages API 호출.

---

## 6. Codex

OpenAI Codex CLI는 Responses API 방언을 씁니다. `~/.codex/config.toml`:

```toml
model_provider = "gateway"

[model_providers.gateway]
base_url = "<ANTHROPIC_BASE_URL>/v1"   # 끝에 /v1
wire_api = "responses"
env_key  = "GATEWAY_VK"
```

```bash
export GATEWAY_VK="$(python3 - "$ADMIN_API_URL" <<'PY'
import json, os, sys, urllib.request
api = sys.argv[1]
tok = json.load(open(os.path.expanduser("~/.gateway-cli/oidc-tokens.json")))["id_token"]
req = urllib.request.Request(api + "/v1/auth/exchange", method="POST",
    headers={"Authorization": "Bearer " + tok})
print(json.load(urllib.request.urlopen(req))["virtual_key"])
PY
)"
# 이후 codex 실행
```

- 게이트웨이는 `originator: codex_cli_rs` 헤더로 `client=codex` 식별.
- 호스트 `~/.codex` 격리: [`gateway-clients/README.md`](gateway-clients/README.md) (`codex-box`).

---

## 7. Cowork

Claude 데스크톱 앱 config를 편집합니다 (CLI `setup` 아님).

| OS | 경로 |
|----|------|
| macOS | `~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json` |
| Windows | 개인 편집보다 MDM/.reg 배포 권장 |

앱 종료 → JSON **백업** → 아래 4키 → 앱 재시작:

```json
{
  "inferenceProvider": "gateway",
  "inferenceGatewayBaseUrl": "<ANTHROPIC_BASE_URL — HTTPS 필수>",
  "inferenceGatewayApiKey": "<VK>",
  "inferenceGatewayAuthScheme": "bearer"
}
```

VK는 §6과 같은 `/v1/auth/exchange` 스니펫으로 발급.

- UA `…claude-desktop-3p` → `client=cowork` 식별.
- **원복**: 백업 JSON 복원 후 앱 재시작.
- 현재 ECS gateway ALB가 **HTTP**이면 Cowork 연동 불가 — TLS(커스텀 도메인/CloudFront 등) 필요 (§1.2).

대량 배포: Admin UI **Export**(`.mobileconfig` / `.reg`). Claude Code는 `/etc/claude-code/managed-settings.d/` 배포가 개인 설정보다 우선.

---

## 8. 연결 확인 · 일상 사용

```bash
gateway-cli status

curl -s -o /dev/null -w "%{http_code}\n" "$ANTHROPIC_BASE_URL/health"   # → 200
curl -s -o /dev/null -w "%{http_code}\n" "$ADMIN_API_URL/health"         # → 200
```

`gateway-cli status`: `Gateway: [ON]`, Base URL = gateway ALB, apiKeyHelper 경로 일치.  
Claude Code: `/model`.

| 상황 | 동작 |
|------|------|
| 클라이언트 실행 | 캐시된 VK 사용 |
| VK 만료 (~1시간) | `api-key-helper` 자동 재발급 (Claude Code) |
| OIDC ID Token 만료 | Refresh Token 자동 갱신 |
| Refresh Token 만료 | `gateway-cli login` 재실행 |
| 팀/그룹 변경 후 | `gateway-cli login` 재실행 |

| 현상 | 의미 | 대응 |
|------|------|------|
| `429 budget_exceeded` | 월 예산 소진 (`HARD_BLOCK` 등) | 운영자에게 예산 상향 |
| `429 rate_limit_error` | RPM/TPM/CPM 초과 | `Retry-After` 후 재시도 |
| `403 model_not_allowed` | 팀 미허용 모델 | 운영자에게 모델 허용 요청 |
| `403 no_matching_team_group` | Cognito 그룹 규칙 불일치 | 그룹명 `Claude_<팀>` 등 — 운영자 확인 |

팀 예산 사용률이 높으면 운영자 설정에 따라 저가 모델로 자동 다운그레이드될 수 있습니다 (`X-Downgraded-From`). **TEAM scope** 전용이며, 개인 예산 초과는 `429`로 차단됩니다.

---

## 9. 원복 / FAQ

```bash
gateway-cli disable          # Claude Code managed-settings 제거 → 재시작
gateway-cli setup ...        # 다시 켜기
gateway-cli logout           # 토큰·VK 캐시 삭제
```

**FAQ**

- **VK란?** `vk-` 접두사 임시 토큰. Claude Code는 helper가 자동 발급/갱신. Codex/Cowork는 exchange로 직접 넣음.
- **매번 로그인?** 아니오. Refresh Token 만료(보통 수일~수십 일) 시에만 `login`.
- **여러 PC?** 가능. 단 1인 1키면 새 VK 발급 시 이전 PC VK는 만료될 수 있음.
- **모델 목록?** Claude Code `/model`. 팀별 상이.

---

## 10. 트러블슈팅

### 10.1 인증

| 증상 | 원인 | 해결 |
|------|------|------|
| `redirect port 8090 is busy` | 포트 점유 | `gateway-cli login --redirect-port 8091` |
| `not logged in` / refresh 실패 | 토큰 없음·만료 | `gateway-cli login` |
| `401 Unauthorized` | VK 만료/폐기 | `login` 후 Claude Code 재시작 |
| `403 user inactive` | 계정 비활성 | 운영자 문의 |
| exchange 실패 / Admin API 오류 | `ADMIN_API_URL`이 ALB로 잘못됨 | **API Gateway URL**로 교체 (§1) |

### 10.2 Claude Code가 게이트웨이로 안 감

1. `gateway-cli status` → Base URL / apiKeyHelper  
2. Claude Code **완전 종료 후** 재실행  
3. managed vs 개인 settings 충돌 → `disable` 후 다시 `setup`  
4. `which api-key-helper` 경로 확인  

### 10.3 Codex / Cowork

| 증상 | 해결 |
|------|------|
| Codex 401 | `GATEWAY_VK` 재발급 (§6), `base_url` 끝에 `/v1` |
| Cowork 400/연결 실패 | `inferenceGatewayBaseUrl` **HTTPS**, 4키 모두 설정, 앱 재시작 |

### 10.4 연결 / 인프라 (ECS)

| 증상 | 해결 |
|------|------|
| `Connection refused` / `502` / `503` | 운영자: `installer.py status`, ECS 서비스 Running, TG health |
| `/health` ≠ 200 | URL 오타·VPN·보안그룹. gateway는 **ALB DNS**, Admin REST는 **API GW** |
| API GW만 실패 | VPC Link / admin-api 서비스 확인 (운영자) |
| timeout | 네트워크·부하 — 잠시 후 재시도 |

### 10.5 디버그 체크리스트

```bash
gateway-cli version   # 0.1.1+
gateway-cli status
ls -la ~/.gateway-cli/
curl -s "$ANTHROPIC_BASE_URL/health"
curl -s "$ADMIN_API_URL/health"
```

### 10.6 로그인 타임아웃 / localhost / Cognito DNS

| 증상 | 원인 | 해결 |
|------|------|------|
| Cognito 후 `localhost:8090/callback` → **This site can't be reached** | 기본 **300초** 안에 콜백 전 리스너 종료 (Sign up이 길 때) | `gateway-cli login --timeout 600 --redirect-port 8091` 재실행. **Sign in**만. 옛 `?code=` URL 재사용 불가 |
| `login timed out after 300s` | 위와 동일 | 동일 |
| `redirect port 8090 is busy` | 이전 로그인 TIME_WAIT·다른 프로세스 | `--redirect-port 8091` (Cognito 콜백에 등록됨) |
| `NameResolutionError` / token exchange 실패 (amazoncognito.com) | 일부 macOS에서 Python getaddrinfo 실패 | `gateway-cli` **0.1.1+** (`uv tool install --force --from ./gateway-cli gateway-cli`). dig 폴백으로 토큰 교환 |
| `oidc_dns_fallback` warning + **`Login successful`** | 폴백이 동작한 것 | **무시해도 됨** (정상) |

> AWS/ECS 장애가 아닙니다. 콜백은 본인 PC의 `gateway-cli`가 받습니다.

---

## 11. 명령어 요약

| 명령 | 설명 |
|------|------|
| `gateway-cli version` | 설치 확인 (**0.1.1+** 권장) |
| `gateway-cli login --timeout 600 --redirect-port 8091` | Cognito OIDC 로그인 (권장) |
| `gateway-cli setup --gateway-url <ALB> --admin-api-url <API_GW>` | Claude Code 연동 |
| `gateway-cli status` | 연동 상태 |
| `gateway-cli disable` / `logout` | 원복 / 토큰 삭제 |
| `uv tool install --force --from ./gateway-cli gateway-cli` | CLI 재설치 (패치 반영) |
| `bash scripts/onboard-macos-linux.sh --setup-claude-code` | macOS/Linux 원클릭 |
| `.\scripts\onboard-windows.ps1 -SetupClaudeCode` | Windows 원클릭 |
| `python3 installer.py status -c config.yaml` | (운영자) 클라이언트에 줄 URL 확인 |

---

## 관련 문서

| 문서 | 내용 |
|------|------|
| [README.md](README.md) | 배포 마스터 (설치 Quick Guide 포함) |
| [gateway-clients/README.md](gateway-clients/README.md) | `claude-box` / `codex-box` 격리 컨테이너 |
| [deployment/ecs/installer.md](deployment/ecs/installer.md) | installer·엔드포인트 상세 |

---

## 한 줄 요약

**`gateway-cli` 0.1.1+** 로 `login --timeout 600 --redirect-port 8091` → Claude Code는 `setup` → `claude`.  
Codex/Cowork는 VK를 exchange로 넣고 각자 config. 추론은 **gateway ALB**, VK는 **API Gateway**.
