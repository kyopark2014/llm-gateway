"""워크플로우별 Step 시퀀스 — boto3 installer (Terraform 대체)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import BackendConfig


@dataclass
class Step:
    name: str
    argv: list[str]
    cwd: Path | None = None
    env: dict[str, str] | None = None
    skippable: bool = False


def _installer() -> Path:
    return paths.installer_py()


def _config(env: str) -> Path:
    cfg = paths.DEPLOY_DIR / "ecs" / "config.yaml"
    if cfg.is_file():
        return cfg
    return paths.DEPLOY_DIR / "ecs" / "config.example.yaml"


def build_llm_workflow(*, env: str, backend: BackendConfig,
                       enable_chat_db_tools: bool, flags: dict[str, bool],
                       cluster_name: str = "llm-gateway") -> list[Step]:
    """ECS 스택: installer.py deploy (데이터 플레인 + ECS)."""
    # backend/enable_chat_db_tools/flags 는 하위호환용 — installer 경로에서는 미사용
    _ = (backend, enable_chat_db_tools, flags, cluster_name)
    cfg = _config(env)
    return [
        Step(
            "installer-deploy",
            ["python3", str(_installer()), "deploy", "-c", str(cfg)],
            cwd=paths.DEPLOY_DIR / "ecs",
        ),
        Step(
            "verify",
            ["python3", str(_installer()), "status", "-c", str(cfg)],
            cwd=paths.DEPLOY_DIR / "ecs",
            skippable=True,
        ),
    ]


def build_llm_teardown(*, env: str, backend: BackendConfig,
                       cluster_name: str = "llm-gateway") -> list[Step]:
    """ECS + 데이터 플레인 삭제: installer destroy --all."""
    _ = (backend, cluster_name)
    cfg = _config(env)
    return [
        Step(
            "installer-destroy",
            ["python3", str(_installer()), "destroy", "-c", str(cfg), "--yes", "--all"],
            cwd=paths.DEPLOY_DIR / "ecs",
        ),
    ]
