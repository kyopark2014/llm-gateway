# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Shared helpers."""
from __future__ import annotations

import json
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .config import InstallConfig


def log(msg: str) -> None:
    print(f"[installer] {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"[installer] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def client(service: str, cfg: InstallConfig):
    return boto3.client(service, region_name=cfg.region)


def account_id(cfg: InstallConfig) -> str:
    return client("sts", cfg).get_caller_identity()["Account"]


def tags(cfg: InstallConfig, extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    base = {
        "Project": cfg.project,
        "Environment": cfg.environment,
        "ManagedBy": "installer.py",
        "DeployPlatform": "ecs",
    }
    if extra:
        base.update(extra)
    return [{"Key": k, "Value": v} for k, v in base.items()]


def ecs_tags(cfg: InstallConfig, extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    """ECS APIs require lowercase key/value (unlike EC2 Key/Value)."""
    return [{"key": t["Key"], "value": t["Value"]} for t in tags(cfg, extra)]


def tag_dict(cfg: InstallConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    base = {
        "Project": cfg.project,
        "Environment": cfg.environment,
        "ManagedBy": "installer.py",
        "DeployPlatform": "ecs",
    }
    if extra:
        base.update(extra)
    return base


def ignore_exists(fn, *, codes: tuple[str, ...] = ("EntityAlreadyExists", "ResourceInUseException", "InvalidParameterException")):
    """Call fn(); swallow known already-exists errors."""
    try:
        return fn()
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in codes or "already" in str(e).lower():
            return None
        raise


def dump_policy(doc: dict[str, Any]) -> str:
    return json.dumps(doc)
