# ECS + API Gateway 배포 아키텍처

## 확정 결정

| 항목 | 결정 |
|------|------|
| 트래픽 경계 | **옵션 A** — data plane ALB→ECS, control REST만 API Gateway, UI는 ALB |
| IaC | **`installer.py` (boto3)** — VPC~ECS 전체 |

## 목표 토폴로지

```mermaid
flowchart TB
  subgraph public [Public Internet]
    Clients[Clients]
  end
  subgraph edge [Edge]
    ALB_GW[ALB_gateway]
    ALB_UI[ALB_admin_ui]
    ALB_API[ALB_admin_api]
    APIGW[API_Gateway]
  end
  subgraph ecs [ECS_Fargate]
    GW[gateway_proxy]
    UI[admin_ui]
    API[admin_api]
  end
  Clients --> ALB_GW --> GW
  Clients --> ALB_UI --> UI
  Clients --> ALB_API --> API
  Clients --> APIGW
  APIGW -->|VPC_Link| ALB_API
```

상세 리소스·설치: [../../ecs/installer.md](../../ecs/installer.md)
