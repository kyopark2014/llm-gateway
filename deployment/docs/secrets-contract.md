# Secret 주입 계약

이 문서는 LLM Gateway가 **무엇이 Secret인지**, **어디서 어떻게 읽는지**, **로테이션은 누가 책임지는지**를 정의합니다.

## 1. Secret 인벤토리

| 이름 | 용도 | 소비 서비스 | 필수/선택 | 생성 방법 |
|------|-----|-----------|---------|---------|
| `virtual_key_encryption_key` | Virtual Key AES-256-GCM DEK (64-char hex) | admin-api, gateway-proxy | **필수** | installer 또는 `openssl rand -hex 32` |
| `nextauth_secret` | NextAuth.js 세션 서명 | admin-ui | **필수** | installer 또는 `openssl rand -hex 32` |
| `jwt_jwks_cache_key` | JWKS 캐시 HMAC | admin-api, gateway-proxy | **필수** | installer 또는 `openssl rand -hex 32` |
| DB `password` | Aurora/PostgreSQL 연결 | 전 서비스 + migration | **필수** | installer (Aurora / Secrets Manager) |
| Redis AUTH | ElastiCache 인증 | 전 서비스 | **선택** | installer |
| SMTP credentials | 메일 발송 | notification-worker | **선택** | 운영자 |

## 2. 주입 모드 (ECS — 권장)

```
  AWS Secrets Manager
  /{project}/{env}/app | db | redis/...
                │
                │  task definition secrets[] + task role
                ▼
  ECS task env
```

`deployment/ecs/installer.py`가 시크릿 생성·재사용 및 task definition에 ARN을 주입합니다.

경로 예:
- `/{project}/{env}/app`
- `/{project}/{env}/db`
- `/{project}/{env}/redis/auth_token`

## 3. 사전 생성 체크리스트

- [ ] `python3 installer.py deploy` 완료
- [ ] Secrets Manager에 app/db/redis 경로 존재
- [ ] 필요 시 app 키 값을 운영 정책에 맞게 교체 후 ECS 서비스 재배포

## 4. 로테이션

| Secret | 주기 | 비고 |
|--------|------|------|
| VK encryption key | 정책에 따름 | 구 키는 extraKeys로 grace period |
| nextauth_secret | 6개월 | 교체 시 관리자 재로그인 |
| DB password | 90일 | Secrets Manager + task 재시작 |
| Redis AUTH | 90일 | ElastiCache ROTATE 전략 후 재배포 |

시크릿 교체 후 ECS 서비스 force-new-deployment가 필요합니다.

## 5. 유출 사고 대응

1. 즉시 무효화 (DB ALTER / Redis AUTH / VK re-encrypt)
2. CloudTrail `secretsmanager:GetSecretValue` 감사
3. 영향 범위 확정 후 task role·네트워크 재검토

## 6. 절대 하면 안 되는 것

- git에 실비번 커밋
- 공용 IAM 사용자에 시크릿 장기 자격 부여
- 프로덕션에서 DEV_LOGIN / 하드코딩 시크릿
