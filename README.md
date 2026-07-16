# AWSome AI Gateway (ECS fork)

> ⚠️ **샘플/프로토타입.** 프로덕션 사용 전 보안·하드닝 검토 필요.

EKS 기반 [**aws-samples / awsome-ai-gateway**](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/tree/main/projects/awsome-ai-gateway) 를  
**ECS Fargate + boto3 installer** 로 전환한 변형본입니다.

| | |
|--|--|
| **제품·기능·데모·데이터플레인·기여자** | upstream [README](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/README.md) · [ARCHITECTURE](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/ARCHITECTURE.md) |
| **이 레포에서 볼 것** | ECS 배포·트래픽 경계·installer Quick Guide·클라이언트 연동 |

**배포 마스터 = 이 문서.**  
상세 함정: [`deployment/ecs/installer.md`](deployment/ecs/installer.md) · 사용자: [`client-guide.md`](client-guide.md)

---

## 목차

1. [Upstream 대비 변경](#upstream-대비-변경)
2. [ECS 아키텍처 (이 레포)](#ecs-아키텍처-이-레포)
3. [설치 Quick Guide](#설치-quick-guide)
4. [일상 운영](#일상-운영)
5. [Observability](#observability)
6. [배포 시 주의사항](#배포-시-주의사항)
7. [디렉터리 맵](#디렉터리-맵)
8. [클라이언트 연동](#클라이언트-연동)
9. [관련 문서](#관련-문서)

---

## Upstream 대비 변경

원본: [projects/awsome-ai-gateway](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/tree/main/projects/awsome-ai-gateway)

| 구분 | Upstream | 이 레포 |
|------|----------|---------|
| 컴퓨트 | EKS Fargate | **ECS Fargate** |
| IaC | Terraform + Helm | **`deployment/ecs/installer.py` (boto3)** |
| 엣지 | Ingress / ALB Controller | **ALB × 3** + **API Gateway** (admin-api REST → VPC Link) |
| 앱 IAM | IRSA | **ECS Task Role** |
| 관측 | Helm observability | **CloudWatch Logs** `/ecs/<cluster>` |
| 클라이언트 URL | 커스텀 도메인 예시 | installer `status` / `.state-*.json` 의 ALB·API GW |

추가 수정:

- SSE(gateway·BI chat)는 **ALB만** (API GW idle ~29s)
- Secrets: 앱은 `DATABASE_URL` / `DB_URL` / `REDIS_URL` — installer가 JSON 맞춤
- Cognito Hosted UI **도메인** 필수 (installer 보장)
- `gateway-cli` ≥ **0.1.1** — macOS Cognito DNS `getaddrinfo` 실패 시 dig 폴백

제품 개요·기능 목록은 upstream README의 [주요 기능](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/README.md#%EC%A3%BC%EC%9A%94-%EA%B8%B0%EB%8A%A5) 참고.

---

## ECS 아키텍처 (이 레포)

**이 레포에만 다른 것 = 엣지·컴퓨트(ECS/ALB/API GW).**  
데이터 플레인·3-client 라우팅·스키마·웹서치·BI 상세는 upstream과 동일 → 중복 문서는 두지 않습니다.

| 보고 싶은 내용 | 문서 |
|----------------|------|
| 제품 아키텍처 개요 | [upstream README §아키텍처](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/README.md#%EC%95%84%ED%82%A4%ED%85%8D%EC%B2%98) |
| as-built 상세 (스키마·플로우·resilience) | [upstream ARCHITECTURE.md](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/ARCHITECTURE.md) |
| ECS 트래픽 ADR | [`deployment/docs/ecs-apigateway/`](deployment/docs/ecs-apigateway/) |

### 트래픽 경계 (ECS 고정)

| 경로 | 진입점 |
|------|--------|
| gateway-proxy (추론 / SSE) | **gateway ALB → ECS** |
| admin-api REST | **API GW → VPC Link → admin-api ALB → ECS** |
| admin-api BI chat SSE | **admin-api ALB** (API GW 금지) |
| admin-ui | **admin-ui ALB** |
| workers / scheduler | private ECS only |

```
Clients → ALB(gateway)  → gateway-proxy
Clients → ALB(admin-ui) → admin-ui
Clients → ALB(admin-api)→ admin-api        (SSE)
Clients → API Gateway → VPC Link → ALB(admin-api) → admin-api  (REST)
```

### 워크로드

| 서비스 | 역할 |
|--------|------|
| gateway-proxy / admin-api / admin-ui | 코어 (ECS Service) |
| scheduler | ROI·VK 만료 (admin-api 이미지) |
| notification-worker / cost-recorder-worker | 워커 |
| migration | Alembic RunTask |
| admin-chat-agent | AgentCore Runtime (ECS 외) |

배포 계층: `소스 → docker build (linux/amd64) → ECR → installer.py → ECS/ALB/API GW` (+ VPC/Aurora/Valkey/Cognito).

---

## 설치 Quick Guide

이미지를 ECR에 넣기 전에 서비스만 올리면 `CannotPullContainerError` 입니다.

### 전제

| 도구 | 최소 |
|------|------|
| AWS CLI | v2 · **배포 리전** (`ap-northeast-2` 등) |
| Python | 3.10+ |
| Docker | `--platform linux/amd64` |

```bash
export AWS_REGION=ap-northeast-2
export AWS_PROFILE=<your-profile>   # 필요 시
```

### 1. config

```bash
cd deployment/ecs
pip3 install -r requirements.txt
cp config.example.yaml config.yaml
# project / environment / aws.region / imageTags / adminBootstrap 확인
```

### 2. (권장) provision

```bash
python3 installer.py provision -c config.yaml
```

### 3. ECR push

```bash
REGION=ap-northeast-2
ECR=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${REGION}.amazonaws.com
ROOT="$(git rev-parse --show-toplevel)"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR"

build_push() {
  docker build --platform linux/amd64 -t "$ECR/llm-gateway/$2:$3" "$ROOT/$1"
  docker push "$ECR/llm-gateway/$2:$3"
}
# 태그는 config.yaml imageTags 와 동일하게
build_push gateway-proxy        gateway-proxy        1.0.48-websearch
build_push admin-api            admin-api            1.0.48-websearch
build_push admin-ui             admin-ui             1.0.97-brand
build_push cost-recorder-worker cost-recorder-worker 1.0.47-websearch
build_push notification-worker  notification-worker  latest
build_push db                   migration            1.0.49-xacct
```

### 4. deploy · 검증

```bash
python3 installer.py deploy -c config.yaml
# 또는 ./deployment/scripts/deploy-tui.sh

python3 installer.py status -c config.yaml
curl -sf "http://<gateway_alb_dns>/health"
curl -sf "<api_gateway_endpoint>/health"
```

```bash
export ANTHROPIC_BASE_URL=http://<gateway_alb_dns>
export ADMIN_API_URL=<api_gateway_endpoint>
```

### 명령

| 명령 | 동작 |
|------|------|
| `provision` / `discover` | 데이터 플레인 |
| `deploy` / `status` / `migrate` | 배포 / 상태 / DB |
| `destroy --yes` / `--all` | 엣지 / 전체 |

옵션: `--dry-run`, `--skip-migration`. 상세: [`installer.md`](deployment/ecs/installer.md)

---

## 일상 운영

- 이미지: ECR 푸시 → `imageTags` → `deploy --skip-migration`
- DB: `python3 installer.py migrate -c config.yaml`
- 재시작: `aws ecs update-service --cluster <cluster> --service <svc> --force-new-deployment --region $AWS_REGION`
- 로그: CloudWatch (§ Observability)

---

## Observability

로그 그룹은 **배포 리전**에 있습니다. CLI 기본 리전이 다르면 `ResourceNotFoundException`.

```bash
export AWS_REGION=ap-northeast-2
aws logs tail /ecs/llm-gateway-dev-ecs \
  --region "$AWS_REGION" \
  --follow \
  --log-stream-name-prefix gateway-proxy
```

이름: `.state-<env>.json` → `log_group_name` (보통 `/ecs/{project}-{env}-ecs`).

---

## 배포 시 주의사항

1. `imageTags` + ECR 실존  
2. 버전 태그 사용 (`latest` 지양)  
3. prod: `devLoginEnabled: false`  
4. 시크릿 URL — [`secrets-contract.md`](deployment/docs/secrets-contract.md)  
5. SSE ≠ API GW  
6. `--platform linux/amd64`

---

## 디렉터리 맵

| 경로 | 설명 |
|------|------|
| `gateway-proxy/` · `admin-api/` · `admin-ui/` · `db/` · workers | 앱 (upstream과 동일 계열) |
| `deployment/ecs/` | **installer.py** (이 레포 핵심) |
| `deployment/tui/` · `deployment/scripts/` | Deploy TUI · 보조 |
| `deployment/docs/` | ECS ADR · 시크릿 |
| `gateway-cli/` · `client-guide.md` | 클라이언트 (Claude Code / Codex / Cowork) |

Upstream의 `deployment/terraform/` · `deployment/charts/` 는 **이 레포에 없음**.

---

## 클라이언트 연동

```bash
uv tool install --force --from ./gateway-cli gateway-cli   # ≥ 0.1.1
gateway-cli login --timeout 600 --redirect-port 8091
gateway-cli setup --gateway-url "$ANTHROPIC_BASE_URL" --admin-api-url "$ADMIN_API_URL"
# setup Password = Mac 로컬 로그인 (sudo → /etc/claude-code/…)
claude
```

- `ANTHROPIC_BASE_URL` = gateway **ALB** · `ADMIN_API_URL` = **API Gateway** (admin-api ALB 아님)
- `oidc_dns_fallback` warning + `Login successful` → 정상
- 전체 (Claude Code / Codex / Cowork): [`client-guide.md`](client-guide.md)  
- 제품 관점 온보딩(일반): [upstream 빠른 시작](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/README.md#%EB%B9%A0%EB%A5%B8-%EC%8B%9C%EC%9E%91-%EC%82%AC%EC%9A%A9%EC%9E%90)

데모 영상·BI Chat 예시는 [upstream Demo / BI](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/README.md#demo).

---

## 관련 문서

| | |
|--|--|
| Upstream 제품 README | [awsome-ai-gateway/README.md](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/README.md) |
| Upstream 아키텍처 상세 | [ARCHITECTURE.md](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/ARCHITECTURE.md) |
| installer 상세 | [`deployment/ecs/installer.md`](deployment/ecs/installer.md) |
| 클라이언트 (Claude Code / Codex / Cowork) | [`client-guide.md`](client-guide.md) |
| ECS ADR · 시크릿 | [`ecs-apigateway/`](deployment/docs/ecs-apigateway/) · [`secrets-contract.md`](deployment/docs/secrets-contract.md) |
| 기여자·License | [upstream](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/README.md#original-builders) · [LICENSE](LICENSE) |

---

## 한 줄 요약

**ECS 배포 = `installer.py`:** `config → provision → ECR push → deploy → status`  
**사용 = `gateway-cli` 0.1.1+:** `login` → `setup` → `claude`  
제품 설명은 [upstream README](https://github.com/aws-samples/sample-agentic-ai-acceleration-kr/blob/main/projects/awsome-ai-gateway/README.md).
