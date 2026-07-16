# LLM Gateway — 설치되는 AWS 인프라 리스트

`deployment/ecs/installer.py`로 배포되는 모든 AWS 리소스의 상세 목록입니다.

네이밍 prefix: `{project}-{environment}` (예: `llm-gateway-dev`)  
기본값: `project=llm-gateway`, `environment=dev`, `region=ap-northeast-2`, `vpcCidr=10.50.0.0/16`

## 총 리소스 개수

| 영역 | 개수 |
|------|------|
| **네트워킹 (VPC)** | VPC 1 + IGW 1 + NAT 1 + EIP 1 + Subnet 8 + RT 2 |
| **보안 (Security Groups)** | 4개 (ALB, Tasks, Aurora, ElastiCache) |
| **데이터베이스 (Aurora)** | Cluster 1 + Instance 1 + DB Subnet Group 1 |
| **캐시 (ElastiCache Valkey)** | Replication Group 1 + Subnet Group 1 |
| **인증 (Cognito)** | User Pool 1 + App Client 1 + Domain 1 + Group 1+ |
| **시크릿 관리 (Secrets Manager)** | 3~5개 (app / db / redis / Aurora master / 선택 chat-reader) |
| **IAM 역할** | 4개 (ECS) + 선택 시 chat-agent 2개 |
| **ECR** | 7개 리포 (+ 선택 admin-chat-agent) |
| **로드 밸런서 (ELBv2)** | ALB 3 + Target Group 3 + Listener 3 |
| **API Gateway** | HTTP API 1 + VPC Link 1 + Integration 1 + Routes 5 |
| **서비스 디스커버리** | Cloud Map Namespace 1 + Service 1 |
| **로깅** | CloudWatch Log Group 1 |
| **ECS** | Cluster 1 + Service 6 + Task Definition 7 (migration 포함) |
| **선택: Chat Agent** | AgentCore Runtime 1 + Lambda 2 + S3 1 + IAM 2 |
| **합계** | **~50개** 주요 리소스 (chat-agent 제외 시 ~45개) |

---

## 단계별 생성 리소스

`deploy` 실행 순서 (`deployment/ecs/installer.md` 기준, 모두 idempotent):

| # | 단계 | 비고 |
|---|------|------|
| 0 | dataplane | VPC → Aurora → Valkey → Cognito |
| 0b | secret URL refresh | DB/Redis JSON에 접속 URL 갱신 |
| 1 | app secret | `/{project}/{env}/app` |
| 2 | IAM roles ×4 | |
| 3 | ECR repos | 없으면 생성 (이미지는 별도 push) |
| 4 | log group / cluster / SG / Cloud Map | |
| 5 | ALB ×3 | |
| 6 | API GW + VPC Link | VPC Link AVAILABLE 대기 후 integration |
| 7 | Task Def + Services | |
| 8 | migration RunTask | `--skip-migration` 시 생략 |
| 9 | (선택) chat-agent | `enableChatAgent: true` |
| 10 | wait | pull 실패 시 조기 감지 |

---

### 1단계: 네트워킹 (`dataplane/vpc.py` — `provision`)

#### VPC

| 항목 | 값 |
|-----|-----|
| **이름 (Name 태그)** | `{project}-{environment}` |
| **CIDR** | `10.50.0.0/16` (기본) / `vpcCidr`로 변경 |
| **DNS Support / Hostnames** | 활성화 |
| **NACL** | 기본 VPC NACL만 사용 (커스텀 미생성) |

#### Internet Gateway / NAT

| 리소스 | 이름 | 설명 |
|--------|------|------|
| IGW | `{prefix}-igw` | VPC attach |
| EIP | `{prefix}-nat` | VPC domain |
| NAT Gateway | `{prefix}-nat` | **단일 NAT**, 첫 public subnet (dev) |

#### Subnets (2 AZ × 4 tier = 8개)

VPC CIDR에서 `/24` 8개 슬라이스:

| Index | Tier | Name 패턴 | MapPublicIp | 추가 태그 |
|-------|------|-----------|-------------|-----------|
| 0–1 | public | `{prefix}-public-{az}` | Yes | `kubernetes.io/role/elb=1` |
| 2–3 | private | `{prefix}-private-{az}` | No | `kubernetes.io/role/internal-elb=1` |
| 4–5 | database | `{prefix}-database-{az}` | No | `Tier=database` |
| 6–7 | elasticache | `{prefix}-elasticache-{az}` | No | `Tier=elasticache` |

#### Route Tables

| 이름 | 라우트 | Associate |
|------|--------|-----------|
| `{prefix}-public` | `0.0.0.0/0 → IGW` | public subnets |
| `{prefix}-private` | `0.0.0.0/0 → NAT` | private + database + elasticache |

#### 아웃바운드 흐름

```
인터넷 ──:80──► ALB SG ──:8000/3000/8080──► Tasks SG
Tasks SG ──:5432──► Aurora SG (VPC CIDR)
Tasks SG ──:6379──► ElastiCache SG (VPC CIDR)
Tasks SG ──NAT──► 인터넷 (ECR / Bedrock / Cognito 등)
```

---

### 2단계: 데이터베이스 (`dataplane/aurora.py`)

#### Aurora PostgreSQL Serverless v2

| 항목 | 값 |
|-----|-----|
| **클러스터 ID** | `{project}-{environment}` |
| **인스턴스** | `{cluster_id}-instance-1` (`db.serverless`) |
| **엔진** | `aurora-postgresql` **16.11** (기본) |
| **용량** | Min **0.5** ACU / Max **4.0** ACU |
| **DB 이름** | `gateway` |
| **앱 사용자** | `gateway` |
| **마스터 사용자** | `postgres_admin` (`ManageMasterUserPassword=True`) |

#### Aurora 설정

| 설정 | 값 |
|-----|-----|
| **VPC 보안 그룹** | `{cluster_id}-aurora` — TCP 5432 ← VPC CIDR |
| **DB Subnet Group** | `{cluster_id}-aurora` (database tier) |
| **공개 액세스** | 비활성화 |
| **백업 보존 기간** | 7일 |
| **스토리지 암호화** | 활성화 |
| **포트** | 5432 |

**대기 시간**: 생성에 약 5–10분 소요

---

### 3단계: 캐시 (`dataplane/redis.py`)

#### ElastiCache Valkey

| 항목 | 값 |
|-----|-----|
| **Replication Group** | `{project}-{environment}` |
| **엔진** | `valkey` |
| **노드 타입** | `cache.t4g.small` |
| **클러스터 수** | 1 (`NumCacheClusters=1`) |
| **포트** | 6379 |
| **TLS** | Transit + At-rest encryption 활성화 |
| **AUTH** | AuthToken (64자) |
| **HA** | Failover/Multi-AZ **비활성** (dev) |
| **Subnet Group** | `{rg_id}-redis` (elasticache tier) |
| **보안 그룹** | `{rg_id}-elasticache` — TCP 6379 ← VPC CIDR |

---

### 4단계: 인증 (`dataplane/cognito.py`)

#### Cognito User Pool

| 항목 | 값 |
|-----|-----|
| **User Pool** | `{project}-{environment}-userpool` |
| **Username / Auto-verify** | email |
| **비밀번호 정책** | 최소 12자, 대소문자·숫자 (기호 불필요) |
| **스키마** | `email` (필수), `name` (선택) |
| **Domain** | `{project[:12]}-{environment}-auth-{account_id}` |
| **App Client** | `{project}-{environment}-cli` (시크릿 없음) |
| **Auth flows** | `ALLOW_USER_SRP_AUTH`, `ALLOW_REFRESH_TOKEN_AUTH` |
| **OAuth** | authorization code; scopes `openid email profile` |
| **Groups** | 기본 `ClaudeAdmin` |
| **Issuer** | `https://cognito-idp.{region}.amazonaws.com/{pool_id}` |

---

### 5단계: 시크릿 관리

#### Secrets Manager

| 이름 | 형식 | 용도 / ECS 주입 키 |
|-----|------|-------------------|
| `/{project}/{env}/app` | JSON | `virtual_key_encryption_key`, `nextauth_secret`, `jwt_jwks_cache_key` |
| `/{project}/{env}/db` | JSON | `database_url`, `db_url`, `database_url_sync` (+ username/password) |
| `/{project}/{env}/redis/auth_token` | JSON | `redis_url` (+ `auth_token`) |
| `rds!cluster-…` | RDS 관리 | migration의 `DB_MASTER_PASSWORD` |
| `/{project}/{env}/chat-reader` | JSON | (선택) BI chat DB reader |

**특징**:
- 앱은 URL에 비밀번호가 포함된 형태만 읽음 (`DATABASE_URL` / `DB_URL` / `REDIS_URL`)
- deploy마다 host 확정 후 URL 키를 refresh
- 상세: [`deployment/docs/secrets-contract.md`](deployment/docs/secrets-contract.md)

---

### 6단계: IAM 역할 (`iam.py`)

신뢰 정책 (ECS 역할 공통):
```json
{
  "Effect": "Allow",
  "Principal": { "Service": "ecs-tasks.amazonaws.com" },
  "Action": "sts:AssumeRole"
}
```

| Role | 이름 | 용도 / 정책 |
|------|------|-------------|
| Execution | `{prefix}-ecs-execution` | ECR pull, Logs, Secrets read (`/{project}/{env}/*`, `rds!cluster-*`) + `AmazonECSTaskExecutionRolePolicy` |
| Gateway | `{prefix}-ecs-gateway-proxy` | Bedrock Invoke/List, Mantle, AgentCore `InvokeGateway` |
| Admin API | `{prefix}-ecs-admin-api` | Cognito admin, Pricing, (선택) AgentCore Runtime / S3 staging |
| Worker | `{prefix}-ecs-worker` | admin-ui / workers / migration — **기본 inline 정책 없음** |

---

### 7단계: 플랫폼 (`platform.py` + ECR)

#### ECR Repositories

| 리포 이름 | 비고 |
|-----------|------|
| `{project}/gateway-proxy` | scanOnPush, MUTABLE |
| `{project}/admin-api` | |
| `{project}/admin-ui` | |
| `{project}/cost-recorder-worker` | |
| `{project}/notification-worker` | |
| `{project}/migration` | |
| `{project}/scheduler` | (예약; 실제 scheduler는 admin-api 이미지 사용) |
| `{project}/admin-chat-agent` | (선택) chat-agent |

> installer는 **리포만 생성**합니다. 이미지는 별도 `docker build/push` 필요.

#### CloudWatch Log Group

| 항목 | 값 |
|-----|-----|
| **이름** | `/ecs/{prefix}-ecs` |
| **보존 기간** | dev **7일** / 그 외 **30일** |
| **Stream prefix** | `gateway-proxy`, `admin-api`, `admin-ui`, `scheduler`, `cost-recorder`, `notification-worker`, `migration` |

#### Security Groups (컴퓨트)

| 이름 | 설명 | 인바운드 규칙 |
|-----|------|-------------|
| `{prefix}-ecs-alb` | ALB용 | 포트 80 ← `0.0.0.0/0` |
| `{prefix}-ecs-tasks` | ECS Task용 | ALB SG 전체 TCP + self (UI→API Cloud Map) |

#### Cloud Map

| 항목 | 값 |
|-----|-----|
| **Namespace** | `{project}.local` (Private DNS, VPC-scoped) |
| **Service** | `admin-api` → DNS `admin-api.{project}.local:8080` |
| **라우팅** | MULTIVALUE A, TTL 10 |

#### ECS Cluster

| 항목 | 값 |
|-----|-----|
| **이름** | `{prefix}-ecs` |
| **Capacity Provider** | FARGATE (+ FARGATE_SPOT), 기본 FARGATE weight 1 |
| **Container Insights** | 활성화 |

---

### 8단계: 로드 밸런서 (`alb.py`)

공통: internet-facing, public subnets, ALB SG, IPv4, HTTP `:80` → TG (`target-type: ip`)

헬스 체크 공통: interval 30s / timeout 5s / healthy 2 / unhealthy 3 / matcher 200 / deregistration 30s

| short | ALB / TG 이름 | Target 포트 | Health path | Idle timeout |
|-------|---------------|-------------|-------------|--------------|
| `gw` | `{prefix}-gw` | **8000** | `/health` | **600s** (SSE) |
| `ui` | `{prefix}-ui` | **3000** | `/api/health` | **120s** |
| `api` | `{prefix}-api` | **8080** | `/health` | **600s** (SSE) |

---

### 9단계: API Gateway (`apigw.py`)

| 항목 | 값 |
|-----|-----|
| **HTTP API** | `{prefix}-admin-api` |
| **VPC Link** | `{api_name}-vpc-link` (private subnets + tasks SG) |
| **Integration** | HTTP_PROXY → admin-api ALB listener, timeout **29s** |
| **Stage** | `$default` (AutoDeploy) |
| **CORS** | Origins `*`; Methods GET/POST/PUT/PATCH/DELETE/OPTIONS |

**Routes** (REST만 — SSE는 ALB 직접):

| Method | Path |
|--------|------|
| POST | `/v1/auth/exchange` |
| GET | `/v1/usage/me` |
| ANY | `/admin/{proxy+}` |
| ANY | `/cli/{proxy+}` |
| GET | `/health` |

---

### 10단계: ECS Services (`services.py`)

공통: Fargate, awsvpc, **private subnets**, `AssignPublicIp=DISABLED`, deploy minHealthy 100% / max 200%

| Service | Task Family | CPU/Mem | Port | Desired | LB / Discovery | Task Role |
|---------|-------------|---------|------|---------|----------------|-----------|
| gateway-proxy | `{prefix}-gateway-proxy` | 1024/2048 | 8000 | config (기본 1) | gateway TG | gateway-proxy |
| admin-api | `{prefix}-admin-api` | 1024/2048 | 8080 | 1 | api TG + Cloud Map | admin-api |
| admin-ui | `{prefix}-admin-ui` | 512/1024 | 3000 | 1 | ui TG | worker |
| scheduler | `{prefix}-scheduler` | 512/1024 | — | 1 | 없음 (admin-api 이미지 + `python -m app.scheduler.main`) | admin-api |
| cost-recorder | `{prefix}-cost-recorder` | 512/1024 | — | 1 | 없음 | worker |
| notification-worker | `{prefix}-notification-worker` | 512/1024 | — | 1 | 없음 | worker |
| migration | `{prefix}-migration` | 512/1024 | — | RunTask only | 없음 | worker |

**gateway-proxy autoscaling**: desired < max이면 CPU 70% target tracking (`{prefix}-gateway-cpu`), scale-in 300s / scale-out 60s (기본 max 3)

**공통 Secrets 주입**: `DATABASE_URL`, `DB_URL`, `REDIS_URL`, `VIRTUAL_KEY_ENCRYPTION_KEY`, `JWT_JWKS_CACHE_KEY`  
**admin-ui 추가**: `NEXTAUTH_SECRET`, `ADMIN_API_URL=http://admin-api.{project}.local:8080`

---

### 11단계: Chat Agent (선택, `chat_agent.py`)

`agentcore.enableChatAgent: true` 또는 `installer.py chat-agent` 시:

| 리소스 | 이름 / 패턴 |
|--------|-------------|
| S3 | `{prefix}-chat-staging-{account}` (AES256, public access block) |
| ECR | `{project}/admin-chat-agent` |
| AgentCore Runtime | `llm_gateway_chat_agent_{environment}` |
| Runtime IAM | `{prefix}-chat-agent-execution` (trust: `bedrock-agentcore`) |
| Lambda IAM | `{prefix}-chat-agent-lambda` |
| Lambda | `{prefix}-chat-agent-query-db`, `{prefix}-chat-agent-get-schema` (python3.12, 512MB, VPC) |
| Secret | `/{project}/{env}/chat-reader` |

---

## 네이밍 컨벤션

모든 리소스는 `{project}-{environment}` (`prefix`)를 사용합니다. 예: `llm-gateway-dev`

```
VPC:               llm-gateway-dev
IGW / NAT / EIP:   llm-gateway-dev-igw, llm-gateway-dev-nat
Subnets:           llm-gateway-dev-{public|private|database|elasticache}-{az}
Aurora:            llm-gateway-dev (+ instance-1, -aurora SG/subnet group)
Valkey:            llm-gateway-dev (+ -redis / -elasticache)
Cognito:           llm-gateway-dev-userpool, llm-gateway-dev-cli
Secrets:           /llm-gateway/dev/{app,db,redis/auth_token}
IAM (ECS):         llm-gateway-dev-ecs-{execution,gateway-proxy,admin-api,worker}
ECR:               llm-gateway/{gateway-proxy,admin-api,...}
Cluster:           llm-gateway-dev-ecs
Log Group:         /ecs/llm-gateway-dev-ecs
SG:                llm-gateway-dev-ecs-alb, llm-gateway-dev-ecs-tasks
Cloud Map:         llm-gateway.local / admin-api
ALB / TG:          llm-gateway-dev-{gw,ui,api}
API GW:            llm-gateway-dev-admin-api (+ -vpc-link)
Services:          llm-gateway-dev-{gateway-proxy,admin-api,admin-ui,...}
```

상태 파일: `deployment/ecs/.state-{environment}.json`

---

## 태깅 정책

생성 리소스에 공통 태그:

```
Key: Project         Value: {project}
Key: Environment     Value: {environment}
Key: ManagedBy       Value: installer.py
Key: DeployPlatform  Value: ecs
```

추가 태그 예: `Name`, `Module`, `Tier`, `Component`

---

## 재배포 및 업데이트

- **같은 리전·프로젝트·환경으로 재배포**: 데이터 플레인 재사용, Task Definition / Service만 업데이트
- **이미지 태그 변경**: ECR push 후 `deploy` → 새 Task Definition + rolling update
- **gateway replicas / autoscalingMax**: Service 스케일 / Application Autoscaling 갱신
- **migration만**: `python3 installer.py migrate -c config.yaml`

---

## 리소스 삭제 순서

`uninstaller.py` 또는 `installer.py destroy --yes --all`:

1. Chat-agent (Runtime → Lambda → S3 → IAM → chat-reader secret)
2. Application Autoscaling (gateway CPU)
3. ECS Services (desired 0 → delete) → task drain
4. API Gateway HTTP API → VPC Link
5. ALB ×3 → Target Group ×3
6. ECS Cluster
7. Cloud Map service → namespace
8. CloudWatch Log Group
9. Compute SGs (alb / tasks)
10. IAM roles (execution / gateway-proxy / admin-api / worker)
11. App secret
12. Cognito (domain + user pool)
13. ElastiCache → subnet group → SG → redis secret
14. Aurora (SkipFinalSnapshot) → subnet group → SG → db secret
15. NAT → EIP → IGW → Subnets → Route Tables → VPC
16. ECR repos (`--keep-ecr`로 유지 가능)

`destroy --yes` ( `--all` 없음 ): ECS 엣지만 (서비스·ALB·API GW·Cluster)

---

## 기본값 및 변경 옵션

```bash
# config.yaml (deployment/ecs/)
project: llm-gateway
environment: dev
provisionDataPlane: true
vpcCidr: "10.50.0.0/16"
aws:
  region: ap-northeast-2

gatewayProxy:
  replicas: 1
  autoscalingMax: 3
  workers: 2

imageTags:
  gatewayProxy: "..."
  adminApi: "..."
  adminUi: "..."
  # ...

agentcore:
  enableChatAgent: false
```

```bash
cd deployment/ecs
python3 installer.py provision -c config.yaml   # 데이터 플레인만
python3 installer.py deploy -c config.yaml      # 전체
python3 installer.py status -c config.yaml
python3 uninstaller.py -c config.yaml --yes
```

---

## 통신 흐름

```
클라이언트
    ├─ Claude Code / Codex ──:80──► ALB gw ──:8000──► gateway-proxy
    ├─ Browser (Admin UI) ──:80──► ALB ui ──:3000──► admin-ui
    │                                      └─Cloud Map─► admin-api:8080
    ├─ Browser (BI chat SSE) ──:80──► ALB api ──:8080──► admin-api
    └─ gateway-cli (REST) ──HTTPS──► API GW ──VPC Link──► ALB api ──► admin-api

ECS Tasks
    ├─ Aurora PostgreSQL :5432  — DATABASE_URL
    ├─ ElastiCache Valkey :6379 — REDIS_URL
    ├─ Secrets Manager          — app / db / redis
    ├─ Cognito                  — OIDC / 그룹
    ├─ CloudWatch Logs
    └─ Bedrock / Mantle / AgentCore — 모델·도구 추론
```

트래픽 규칙 (고정):

| 경로 | 진입점 | 이유 |
|------|--------|------|
| gateway-proxy 추론 / SSE | **gateway ALB** | API GW idle ~29s → SSE 불가 |
| admin-api BI chat SSE | **admin-api ALB** | 동일 |
| admin-api REST | **API Gateway → VPC Link → ALB** | REST만 |
| admin-ui | **admin-ui ALB** | Next.js |

---

## 비용 추정 (ap-northeast-2, 월간)

installer **기본 스펙**(Fargate 서비스 6 · 합계 ~4 vCPU/8 GB, ALB 3, Aurora Serverless v2 0.5–4 ACU, Valkey `cache.t4g.small`, NAT 1, API GW, Cognito, Secrets) 기준. **토큰(Bedrock) 요금 미포함**.

| 리소스 | SKU / 산식 | 추정 비용 |
|--------|------------|---------|
| ECS Fargate | 4 vCPU × $0.04048/h + 8 GB × $0.004445/h × 730h | ~$144 |
| NAT Gateway | $0.045/h × 730h + 데이터 처리 | ~$35–45 |
| ALB ×3 | 3 × (~$16) + LCU | ~$60–75 |
| Aurora Serverless v2 | 0.5 ACU 유휴 기준 (부하 시 상승) | ~$45–90 |
| ElastiCache Valkey | cache.t4g.small | ~$15–20 |
| API Gateway HTTP | 요청량 소량 | ~$1–5 |
| Cognito | MAU 소량 | ~$0–5 |
| Secrets Manager | 3–4 secrets | ~$1–2 |
| CloudWatch Logs | Container Insights + 로그 | ~$5–15 |
| EIP / 데이터 전송 | | ~$4–10 |
| **인프라 합계** | | **~$310–410 / 월** (전형적 ~$340) |

> NAT·ALB×3·Aurora·Fargate 다중 서비스가 LiteLLM 단일 스택(~$85) 대비 비용의 대부분입니다.  
> chat-agent(Lambda/S3/AgentCore)·Multi-AZ·gateway 스케일 아웃 시 상단으로 갈 수 있습니다.  
> README [비용 검토](README.md#비용-검토) 참고.

---

## 관련 문서

- [`deployment/ecs/installer.md`](deployment/ecs/installer.md) — 설치·함정·명령
- [`deployment/docs/ecs-apigateway/`](deployment/docs/ecs-apigateway/) — 트래픽 ADR
- [`deployment/docs/secrets-contract.md`](deployment/docs/secrets-contract.md) — Secret 계약
- [README.md](README.md) — 아키텍처·클라이언트 연동
