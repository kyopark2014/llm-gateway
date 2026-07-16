# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""배포 폼 유틸 — 플레이스홀더 검증 + DeployConfig."""
from __future__ import annotations

from dataclasses import dataclass

PLACEHOLDER_TOKENS: tuple[str, ...] = (
    "CHANGE_ME",
    "CHANGE_ACCOUNT_ID",
    "ACCOUNT_ID",
    "YOUR_ROLE",
    "tvly-...",
    "BSA...",
    "sk-...",
)


def find_placeholders(values: dict[str, str]) -> list[str]:
    """값에 플레이스홀더 토큰이 남은 key 목록을 반환."""
    flagged = []
    for key, val in values.items():
        if isinstance(val, str) and any(tok in val for tok in PLACEHOLDER_TOKENS):
            flagged.append(key)
    return flagged


@dataclass
class BackendConfig:
    """배포 컨텍스트 (region). bucket/dynamodb_table 필드는 하위호환용 미사용."""

    bucket: str = ""
    dynamodb_table: str = ""
    region: str = ""
