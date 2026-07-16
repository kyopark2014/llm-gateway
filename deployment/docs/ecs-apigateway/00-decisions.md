# 아키텍처 결정 기록 (ADR)

## ADR-001: 트래픽 경계 — 옵션 A

- Data plane: ALB → ECS (`gateway-proxy`, SSE)
- Control REST: API Gateway → VPC Link → admin-api ALB
- Control SSE / Admin UI: ALB 직결

## ADR-002: IaC — boto3 installer

- **`deployment/ecs/installer.py`** 가 VPC / Aurora / Valkey / Cognito / ECS / ALB / API GW 전부 프로비저닝
- Terraform·EKS Helm 운영 경로 제거
