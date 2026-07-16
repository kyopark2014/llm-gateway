#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""LLM Gateway installer — boto3 (Terraform 전면 대체).

전체 스택:
  1) Data plane: VPC, Aurora Serverless v2, Valkey, Cognito, Secrets
  2) Compute:    ECS Fargate, ALB×3, API Gateway, IAM, Cloud Map

Usage:
  python3 installer.py provision -c config.yaml          # 데이터 플레인만
  python3 installer.py deploy -c config.yaml             # 데이터+ECS (기본)
  python3 installer.py discover -c config.yaml           # 기존 리소스 조회
  python3 installer.py status|migrate -c config.yaml
  python3 installer.py destroy -c config.yaml --yes
  python3 installer.py destroy -c config.yaml --yes --all   # full teardown (= uninstaller.py)
  # Prefer: python3 uninstaller.py -c config.yaml --yes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _installer.config import load_config  # noqa: E402
from _installer.deploy import (  # noqa: E402
    deploy,
    destroy_compute,
    discover,
    migrate,
    provision,
    provision_chat,
    status,
)
from _installer.util import fail, log  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LLM Gateway installer (boto3 — replaces Terraform)",
    )
    parser.add_argument(
        "command",
        choices=[
            "deploy",
            "provision",
            "discover",
            "status",
            "migrate",
            "chat-agent",
            "destroy",
        ],
    )
    parser.add_argument("-c", "--config", required=True, help="YAML config path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-migration", action="store_true")
    parser.add_argument("--skip-image-build", action="store_true",
                        help="chat-agent: require existing ECR image")
    parser.add_argument("--yes", action="store_true", help="Confirm destroy")
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_resources",
        help="destroy: also remove VPC/Aurora/Valkey/Cognito",
    )
    parser.add_argument("--force", action="store_true", help="discover: overwrite fields")
    args = parser.parse_args(argv)

    cfg = load_config(args.config, dry_run=args.dry_run)
    log(f"command={args.command} config={args.config}")

    if args.command == "deploy":
        deploy(cfg, skip_migration=args.skip_migration)
    elif args.command == "provision":
        provision(cfg)
    elif args.command == "discover":
        discover(cfg, force=args.force)
    elif args.command == "status":
        status(cfg)
    elif args.command == "migrate":
        migrate(cfg)
    elif args.command == "chat-agent":
        provision_chat(
            cfg, config_path=args.config, skip_image_build=args.skip_image_build
        )
    elif args.command == "destroy":
        destroy_compute(cfg, yes=args.yes, all_resources=args.all_resources)
    else:
        fail(f"unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
