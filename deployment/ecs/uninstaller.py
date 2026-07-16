#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""LLM Gateway uninstaller — delete all infrastructure created by installer.py.

Removes (in order):
  chat-agent → ECS/ALB/API GW → Cloud Map / logs / SG / IAM / secrets
  → Cognito / Valkey / Aurora → VPC → ECR → state file

Usage:
  python3 uninstaller.py -c config.yaml --yes
  python3 uninstaller.py -c config.yaml --yes --dry-run
  python3 uninstaller.py -c config.yaml --yes --keep-ecr
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _installer.config import load_config  # noqa: E402
from _installer.state import State  # noqa: E402
from _installer.uninstall import uninstall  # noqa: E402
from _installer.util import log  # noqa: E402


def _hydrate_cfg_from_state(cfg, state: State) -> None:
    """Fill empty config fields from .state so destroy can find resources."""
    mapping = {
        "vpc_id": "vpc_id",
        "private_subnet_ids": "private_subnet_ids",
        "public_subnet_ids": "public_subnet_ids",
        "db_host": "db_host",
        "db_secret_arn": "db_secret_arn",
        "db_master_secret_arn": "db_master_secret_arn",
        "redis_host": "redis_host",
        "redis_auth_secret_arn": "redis_auth_secret_arn",
        "cognito_user_pool_id": "cognito_user_pool_id",
        "cognito_issuer_url": "cognito_issuer_url",
        "agentcore_runtime_arn": "agentcore_runtime_arn",
        "chat_staging_bucket": "chat_staging_bucket",
        "ecr_registry": "ecr_registry",
    }
    for attr, key in mapping.items():
        cur = getattr(cfg, attr, None)
        if cur in (None, "", []):
            val = state.get(key)
            if val:
                setattr(cfg, attr, val)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LLM Gateway uninstaller — remove all installer.py resources",
    )
    parser.add_argument("-c", "--config", required=True, help="YAML config path")
    parser.add_argument("--yes", action="store_true", help="Confirm destructive uninstall")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without AWS deletes")
    parser.add_argument(
        "--keep-ecr",
        action="store_true",
        help="Keep ECR repositories and images",
    )
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="Keep .state-<env>.json after uninstall",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config, dry_run=args.dry_run)
    state = State(cfg.default_state_path())
    _hydrate_cfg_from_state(cfg, state)
    log(f"command=uninstall config={args.config}")

    uninstall(
        cfg,
        yes=args.yes,
        keep_ecr=args.keep_ecr,
        keep_state=args.keep_state,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
