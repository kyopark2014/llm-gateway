# installer.py — 리소스 생성 가이드

`deployment/ecs/installer.py`는 boto3로 **데이터 플레인 + ECS 컴퓨트** 전체를 프로비저닝합니다.

## 한눈에 보기

```mermaid
flowchart TB
  subgraph edge [Public edge]
    ALBgw[ALB gateway SSE]
    ALBui[ALB admin-ui]
    ALBapi[ALB admin-api SSE]
    APIGW[API GW REST 29s]
  end
  subgraph created [installer.py]
    VPC[VPC / Subnets / NAT]
    Aurora[(Aurora Serverless v2)]
    Valkey[(ElastiCache Valkey)]
    Cognito[Cognito]
    SM[Secrets Manager]
    Cluster[ECS Fargate]
  end

  Clients --> ALBgw
  Clients --> ALBui
  Clients --> ALBapi
  Clients --> APIGW
  APIGW -->|VPC Link| ALBapi
  ALBgw --> Cluster
  ALBui --> Cluster
  ALBapi --> Cluster
  Cluster --> Aurora
  Cluster --> Valkey
  Cluster --> SM
  Cognito -.-> Cluster
```

**트래픽 규칙 (고정)**

| 경로 | 진입점 | 이유 |
|------|--------|------|
| gateway-proxy 추론 / SSE | **gateway ALB** | API GW idle ~29s → SSE 불가 |
| admin-api BI chat SSE | **admin-api ALB** | 동일 |
| admin-api REST | **API Gateway → VPC Link → ALB** | REST만 |
| admin-ui | **admin-ui ALB** | Next.js |

---

## 설치 (권장 순서)

실배포에서 검증된 순서입니다. **이미지를 먼저 push하지 않으면** 서비스는 뜨지만 `CannotPullContainerError`로 Running=0 입니다.

### 0. 전제

- AWS CLI 인증 (`aws sts get-caller-identity`)
- Python 3.10+, `pip3 install -r requirements.txt`
- Docker (`--platform linux/amd64` — Apple Silicon 포함)

### 1. config

```bash
cd deployment/ecs
cp config.example.yaml config.yaml
# imageTags / adminBootstrap.emails / aws.region 확인
```

### 2. 데이터 플레인 (+ ECS) 또는 단계 분리

```bash
# 한 번에 (이미지 없어도 인프라는 생성됨)
python3 installer.py deploy -c config.yaml --skip-migration

# 또는 데이터 플레인만 먼저 (~8–15분: NAT + Aurora + Valkey)
python3 installer.py provision -c config.yaml
```

### 3. ECR 이미지 빌드·푸시 (필수)

installer는 **ECR 리포지토리를 생성**하지만 이미지는 넣지 않습니다.

```bash
ECR=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.ap-northeast-2.amazonaws.com
REGION=ap-northeast-2
ROOT="$(git rev-parse --show-toplevel)"   # 리포 루트

aws ecr get-login-password --region "$REGION" | \
  docker login --username AWS --password-stdin "$ECR"

# config.yaml imageTags 와 태그를 맞출 것
build_push() {
  local ctx="$1" name="$2" tag="$3"
  docker build --platform linux/amd64 -t "$ECR/llm-gateway/$name:$tag" "$ROOT/$ctx"
  docker push "$ECR/llm-gateway/$name:$tag"
}

build_push gateway-proxy          gateway-proxy          1.0.48-websearch
build_push admin-api              admin-api              1.0.48-websearch
build_push admin-ui               admin-ui               1.0.97-brand
build_push cost-recorder-worker   cost-recorder-worker   1.0.47-websearch
build_push notification-worker    notification-worker    latest
build_push db                     migration              1.0.49-xacct
```

리포 이름: `llm-gateway/{gateway-proxy|admin-api|admin-ui|cost-recorder-worker|notification-worker|migration|scheduler}`

### 4. 최종 deploy (migration 포함)

```bash
python3 installer.py deploy -c config.yaml
```

성공 시:

1. 없으면 데이터 플레인 생성 / 있으면 재사용  
2. Task Definition 등록 + Service 업데이트  
3. **migration RunTask** (`./run_migration.sh`: init SQL → app user GRANT → `alembic upgrade head`)  
4. gateway / admin-api / admin-ui Running 대기  

### 5. 확인

```bash
python3 installer.py status -c config.yaml

# 헬스 (DNS는 status 출력 값)
curl -sf "http://<gateway_alb>/health"
curl -sf "http://<admin_api_alb>/health"
curl -sf "http://<admin_ui_alb>/api/health"
curl -sf "<api_gateway_endpoint>/health"
```

클라이언트 env:

```bash
export ANTHROPIC_BASE_URL=http://<gateway_alb>
export ADMIN_API_URL=<api_gateway_endpoint>   # https://....execute-api....amazonaws.com
```

### 6. 삭제

```bash
# 전체 스택 (권장) — ECS + 데이터 플레인 + IAM/SG/Cloud Map/ECR/chat-agent
python3 uninstaller.py -c config.yaml --yes
python3 uninstaller.py -c config.yaml --yes --keep-ecr   # 이미지는 유지

# 또는 installer 경유
python3 installer.py destroy -c config.yaml --yes          # ECS 엣지만
python3 installer.py destroy -c config.yaml --yes --all    # = uninstaller.py
```

---

## 명령

| 명령 | 동작 |
|------|------|
| `provision` | VPC / Aurora / Valkey / Cognito만 |
| `discover` | 기존 데이터 플레인 조회 → YAML 스니펫 |
| `deploy` | 데이터 플레인 + ECS/ALB/API GW + migration |
| `status` | `.state-<env>.json` 엔드포인트·ARN |
| `migrate` | migration RunTask만 |
| `destroy --yes` | ECS 엣지 |
| `destroy --yes --all` | ECS + 데이터 플레인 |

옵션: `--dry-run`, `--skip-migration` (`deploy`), `--force` (`discover`)

---

## deploy 실행 순서

모두 **idempotent**.

| # | 단계 | 비고 |
|---|------|------|
| 0 | dataplane | VPC → Aurora → Valkey → Cognito |
| 0b | secret URL refresh | DB/Redis JSON에 접속 URL 갱신 |
| 1 | app secret | `/{project}/{env}/app` |
| 2 | IAM roles ×4 | |
| 3 | ECR repos | 없으면 생성 + **이미지 태그 존재 여부 경고** |
| 4 | log group / cluster / SG / Cloud Map | |
| 5 | ALB ×3 | |
| 6 | API GW + VPC Link | VPC Link **AVAILABLE** 대기 후 integration |
| 7 | Task Def + Services | |
| 8 | migration RunTask | `--skip-migration` 시 생략 |
| 9 | wait | pull 실패 시 조기 감지 (장시간 hang 방지) |

네이밍 prefix: `{project}-{environment}` (예: `llm-gateway-dev`).

---

## Secrets Manager (중요)

앱은 **URL에 비밀번호가 들어간 형태**만 읽습니다 (`DATABASE_URL` / `DB_URL` / `REDIS_URL`).  
별도 `DB_PASSWORD` env를 URL에 합치는 로직이 없습니다.

| Secret | 형식 | ECS에 주입되는 키 |
|--------|------|-------------------|
| `/{project}/{env}/app` | JSON | `virtual_key_encryption_key`, `nextauth_secret`, `jwt_jwks_cache_key` |
| `/{project}/{env}/db` | JSON | `database_url`, `db_url` (+ `password`, `username`, …) |
| `/{project}/{env}/redis/auth_token` | JSON | `redis_url` (+ `auth_token`) |
| Aurora master (`rds!cluster-…`) | RDS 관리 | migration의 `DB_MASTER_PASSWORD` |

`db` 예시 키:

```json
{
  "username": "gateway",
  "password": "…",
  "database_url": "postgresql+asyncpg://gateway:…@host:5432/gateway?ssl=require",
  "db_url": "postgresql+asyncpg://gateway:…@host:5432/gateway?ssl=require",
  "database_url_sync": "postgresql://gateway:…@host:5432/gateway?sslmode=require"
}
```

`redis/auth_token` 예시:

```json
{
  "auth_token": "…",
  "password": "…",
  "redis_url": "rediss://:…@master….cache.amazonaws.com:6379/0"
}
```

deploy마다 host가 확정된 뒤 URL 키를 **refresh**합니다.  
레거시 raw-string redis secret도 재실행 시 JSON으로 승격됩니다.

**migration env**

| env | 출처 |
|-----|------|
| `DB_MASTER_URL` | `postgresql://postgres_admin:PLACEHOLDER@host/…` (플레이스홀더) |
| `DB_MASTER_PASSWORD` | Aurora master secret |
| `APP_DB_USER` / `APP_DB_PASSWORD` | gateway 유저 (`/db` secret) |
| entrypoint | 이미지 기본 `./run_migration.sh` (init SQL + GRANT + alembic) |

---

## IAM / 플랫폼 / ALB / API GW

### IAM (신뢰: `ecs-tasks.amazonaws.com`)

| Role | 용도 |
|------|------|
| `{prefix}-ecs-execution` | ECR, Logs, Secrets read |
| `{prefix}-ecs-gateway-proxy` | Bedrock / Mantle / AgentCore Gateway |
| `{prefix}-ecs-admin-api` | Cognito sync, (선택) AgentCore Runtime |
| `{prefix}-ecs-worker` | workers / admin-ui / migration |

### CloudWatch

Log group: `/ecs/{project}-{env}-ecs` (dev 7일).  
stream prefix: `gateway-proxy`, `admin-api`, `admin-ui`, `scheduler`, `cost-recorder`, `notification-worker`, `migration`

### Security Groups

| SG | 규칙 |
|----|------|
| `{prefix}-ecs-alb` | :80 ← `0.0.0.0/0` |
| `{prefix}-ecs-tasks` | ALB→tasks, tasks self (UI→API) |

Aurora/Valkey는 VPC CIDR ingress (dataplane이 SG 생성).

### Cloud Map

- Namespace: `{project}.local`
- `admin-api.{project}.local:8080` ← admin-ui `ADMIN_API_URL`

### ALB ×3 (`target-type: ip`)

| short | 포트 | Health | Idle |
|-------|------|--------|------|
| `gw` | 8000 | `/health` | **600s** |
| `ui` | 3000 | `/api/health` | 120s |
| `api` | 8080 | `/health` | **600s** |

### API Gateway HTTP API

- VPC Link → private subnets (+ tasks SG)
- Integration: ALB listener, timeout **29s**
- Routes: `POST /v1/auth/exchange`, `GET /v1/usage/me`, `ANY /admin/{proxy+}`, `ANY /cli/{proxy+}`, `GET /health`
- Stage: `$default`

---

## ECS Services

| Service | Image | CPU/Mem | Desired | ALB |
|---------|-------|---------|---------|-----|
| gateway-proxy | gateway-proxy | 1024/2048 | config | gw |
| admin-api | admin-api | 1024/2048 | 1 | api + Cloud Map |
| admin-ui | admin-ui | 512/1024 | 1 | ui |
| scheduler | **admin-api** + `python -m app.scheduler.main` | 512/1024 | **1** | — |
| cost-recorder | cost-recorder-worker | 512/1024 | 1 | — |
| notification-worker | notification-worker | 512/1024 | 1 | — |
| migration (RunTask) | migration | 512/1024 | — | — |

gateway-proxy: desired < max 이면 CPU 70% target tracking.

---

## 상태 파일

`deployment/ecs/.state-{environment}.json` (gitignore)

재실행·`status`·`destroy`·`migrate`가 참조.  
주요 키: `cluster_name`, `gateway_alb_dns`, `admin_ui_alb_dns`, `admin_api_alb_dns`, `api_gateway_endpoint`, `migration_task_def`, `*_role_arn`, `*_tg_arn`, `vpc_link_id`, …

`config.yaml`의 `infrastructure:` 는 비워 둬도 `provisionDataPlane: true`면 이름으로 재사용합니다.  
`status` / provision 출력값을 복사해 두면 가독성에 좋습니다.

---

## 실운영에서 겪은 함정

| 증상 | 원인 | 대응 |
|------|------|------|
| `CannotPullContainerError` / Running=0 | ECR에 `imageTags` 이미지 없음 | §3 빌드·푸시 후 `deploy` 재실행 |
| ECS `create_cluster` ParamValidation `Key`/`Value` | ECS 태그는 **`key`/`value` 소문자** | installer가 `ecs_tags()` 사용 (수정됨) |
| `Operation cannot be paginated: get_vpc_links` | API GW v2는 pageable 아님 | 직접 `get_vpc_links()` (수정됨) |
| VPC Link integration 직후 실패 | Link가 아직 AVAILABLE 아님 | AVAILABLE 대기 후 integration (수정됨) |
| 앱 DB/Redis 연결 실패 | URL에 패스워드 미포함 | SM JSON에 `database_url`/`redis_url` (수정됨) |
| migration만 alembic → schema/유저 없음 | entrypoint가 `run_migration.sh`여야 함 | 이미지 CMD 유지, command override 금지 (수정됨) |
| `deploy \| tee` 후 EXIT=0 | `tee`가 파이프 마지막 | `set -o pipefail` 사용 |
| provision 소요 | NAT ~2분 + Aurora ~5–10분 + Valkey ~5–10분 | 정상 |

---

## destroy / uninstaller

| 도구 | 삭제 범위 |
|------|-----------|
| `installer.py destroy --yes` | ECS services, ALB, API GW, Cluster |
| `uninstaller.py --yes` / `destroy --yes --all` | 위 + Cognito/Redis/Aurora/VPC + IAM/SG/Cloud Map/logs/secrets/ECR/chat-agent |

---

## 모듈 맵

```
deployment/ecs/
├── installer.py / uninstaller.py
├── config.example.yaml / config.yaml   # config.yaml은 gitignore 권장
├── requirements.txt
├── .state-<env>.json                   # gitignore
└── _installer/
    ├── config.py · state.py · util.py · discover.py
    ├── dataplane/   # vpc · aurora · redis · cognito
    ├── iam.py · platform.py · alb.py · apigw.py · services.py
    ├── chat_agent.py · uninstall.py
    └── deploy.py
```

관련: [README §클라이언트 연동](../../README.md#클라이언트-연동) · [docs/ecs-apigateway/](../docs/ecs-apigateway/) (ADR·토폴로지) · [config.example.yaml](config.example.yaml) · [secrets-contract](../docs/secrets-contract.md)
