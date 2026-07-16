"""리포 내 배포 아티팩트 경로 상수."""
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DEPLOY_DIR.parent
SCRIPTS_DIR = DEPLOY_DIR / "scripts"
ECS_DIR = DEPLOY_DIR / "ecs"
BUILD_LAMBDAS_SH = REPO_ROOT / "admin-chat-agent" / "lambdas" / "build-lambdas.sh"


def script(name: str) -> Path:
    return SCRIPTS_DIR / name


def installer_py() -> Path:
    return ECS_DIR / "installer.py"
