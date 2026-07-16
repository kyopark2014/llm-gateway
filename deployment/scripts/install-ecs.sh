#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# install-ecs.sh — installer.py 래퍼 (Terraform ECS 경로 대체)
# ------------------------------------------------------------------------------
# 사용법:
#   ./install-ecs.sh deploy [-c config.yaml] [--skip-migration] [--dry-run]
#   ./install-ecs.sh status [-c config.yaml]
#   ./install-ecs.sh migrate [-c config.yaml]
#   ./install-ecs.sh destroy [-c config.yaml] --yes
#
# 전제:
#   - VPC/Aurora/Valkey/Cognito 가 이미 존재하고 config.yaml 에 엔드포인트가 채워짐
#   - ECR 이미지 push 완료
#   - pip3 install -r deployment/ecs/requirements.txt (boto3, PyYAML)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ECS_DIR="$(cd "$SCRIPT_DIR/../ecs" && pwd)"
INSTALLER="$ECS_DIR/installer.py"
DEFAULT_CONFIG="$ECS_DIR/config.yaml"

CMD="${1:-deploy}"
shift || true

CONFIG=""
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2 ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

if [[ -z "$CONFIG" ]]; then
  if [[ -f "$DEFAULT_CONFIG" ]]; then
    CONFIG="$DEFAULT_CONFIG"
  else
    CONFIG="$ECS_DIR/config.example.yaml"
    echo "⚠  config.yaml 없음 — config.example.yaml 사용 (배포 전 실제 값으로 복사하세요)" >&2
  fi
fi

if [[ ! -f "$INSTALLER" ]]; then
  echo "✗ installer.py 없음: $INSTALLER" >&2
  exit 1
fi

if ! command -v python3 >/dev/null; then
  echo "✗ python3 필요" >&2
  exit 1
fi

if ! python3 -c "import boto3, yaml" 2>/dev/null; then
  echo "✗ boto3 / PyYAML 필요: pip3 install -r $ECS_DIR/requirements.txt" >&2
  exit 1
fi

exec python3 "$INSTALLER" "$CMD" -c "$CONFIG" "${EXTRA[@]}"
