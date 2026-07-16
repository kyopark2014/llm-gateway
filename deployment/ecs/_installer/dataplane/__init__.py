# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Data-plane provisioning (VPC / Aurora / Valkey / Cognito) — Terraform 대체."""
from __future__ import annotations

from ..config import InstallConfig
from ..state import State
from ..util import log
from . import aurora, cognito, redis, vpc


def ensure_data_plane(cfg: InstallConfig, state: State) -> None:
    """Create or reuse VPC + Aurora + Valkey + Cognito, then fill cfg fields."""
    if cfg.dry_run:
        log("[dry-run] data-plane provision skipped")
        return

    log("Ensuring data plane (VPC / Aurora / Valkey / Cognito)")
    vpc.ensure_vpc(cfg, state)
    aurora.ensure_aurora(cfg, state)
    redis.ensure_redis(cfg, state)
    cognito.ensure_cognito(cfg, state)

    if not cfg.ecr_registry:
        from ..util import account_id
        cfg.ecr_registry = f"{account_id(cfg)}.dkr.ecr.{cfg.region}.amazonaws.com"
        state.set("ecr_registry", cfg.ecr_registry)
        state.save()

    log(
        f"Data plane ready: vpc={cfg.vpc_id} db={cfg.db_host} "
        f"redis={cfg.redis_host} cognito={cfg.cognito_user_pool_id}"
    )


def destroy_data_plane(cfg: InstallConfig, state: State) -> None:
    """Best-effort teardown of installer-created data plane (order matters)."""
    log("Destroying data plane (Cognito → Redis → Aurora → VPC)")
    cognito.destroy_cognito(cfg, state)
    redis.destroy_redis(cfg, state)
    aurora.destroy_aurora(cfg, state)
    vpc.destroy_vpc(cfg, state)
