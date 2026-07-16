# ECS + API Gateway

| 문서 | 내용 |
|------|------|
| [00-decisions.md](00-decisions.md) | ADR (트래픽 경계·IaC) |
| [01-architecture.md](01-architecture.md) | 토폴로지 |
| [../../ecs/installer.md](../../ecs/installer.md) | 설치·운영·트러블슈팅 |

```bash
cd deployment/ecs
pip3 install -r requirements.txt
cp config.example.yaml config.yaml
python3 installer.py deploy -c config.yaml
```
