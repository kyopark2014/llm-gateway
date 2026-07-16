# LLM Gateway — Deployment

프로덕션 배포는 **`deployment/ecs/installer.py` (boto3)** 입니다.

## 폴더 구조

```
deployment/
├── ecs/
│   ├── installer.py
│   ├── installer.md
│   ├── config.example.yaml
│   └── _installer/
├── docs/
│   ├── ecs-apigateway/   # ADR · 토폴로지
│   └── secrets-contract.md
└── scripts/
    ├── install-ecs.sh
    ├── deploy-tui.sh
    └── provision_agentcore_websearch.py
```

## 빠른 시작

```bash
cd deployment/ecs
pip3 install -r requirements.txt
cp config.example.yaml config.yaml
python3 installer.py deploy -c config.yaml
```

상세: [ecs/installer.md](ecs/installer.md)
