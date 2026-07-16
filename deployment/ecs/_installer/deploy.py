# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Orchestrate deploy / status / migrate / destroy."""
from __future__ import annotations

from . import alb, apigw, iam, platform, services
from .config import InstallConfig
from .dataplane import destroy_data_plane, ensure_data_plane
from .discover import discover_and_fill, format_discovered_yaml
from .state import State
from .util import fail, log


def deploy(cfg: InstallConfig, *, skip_migration: bool = False) -> State:
    state = State(cfg.default_state_path())
    log(f"Config env={cfg.environment} region={cfg.region} state={state.path}")

    if cfg.provision_data_plane:
        ensure_data_plane(cfg, state)
    elif cfg.auto_discover:
        log("Discovering data-plane resources via boto3")
        discover_and_fill(cfg, force=False)

    # Ensure SM JSON has full DB/Redis URLs (apps embed credentials in URL)
    _refresh_connection_secrets(cfg, state)

    errors = cfg.validate()
    if errors:
        fail("\n".join(errors))

    iam.ensure_app_secret(cfg, state)
    iam.ensure_roles(cfg, state)
    iam.ensure_ecr_repositories(cfg, state)
    platform.ensure_log_group(cfg, state)
    platform.ensure_cluster(cfg, state)
    platform.ensure_security_groups(cfg, state)
    platform.ensure_cloudmap(cfg, state)
    alb.ensure_albs(cfg, state)
    apigw.ensure_api_gateway(cfg, state)
    services.ensure_services(cfg, state)

    if not skip_migration:
        services.run_migration(cfg, state)
    else:
        log("Skipping migration")

    services.wait_services(cfg, state)
    _print_endpoints(state)
    return state


def discover(cfg: InstallConfig, *, force: bool = True) -> None:
    found = discover_and_fill(cfg, force=force)
    print(format_discovered_yaml(found))
    print("# Merge into config.yaml, or use provisionDataPlane: true / autoDiscover: true.")


def provision(cfg: InstallConfig) -> State:
    """Data plane only (VPC/Aurora/Valkey/Cognito)."""
    state = State(cfg.default_state_path())
    ensure_data_plane(cfg, state)
    print(format_discovered_yaml({
        "vpcId": cfg.vpc_id,
        "privateSubnetIds": cfg.private_subnet_ids,
        "publicSubnetIds": cfg.public_subnet_ids,
        "dbHost": cfg.db_host,
        "dbSecretArn": cfg.db_secret_arn,
        "dbMasterSecretArn": cfg.db_master_secret_arn,
        "redisHost": cfg.redis_host,
        "redisAuthSecretArn": cfg.redis_auth_secret_arn,
        "cognitoIssuerUrl": cfg.cognito_issuer_url,
        "cognitoUserPoolId": cfg.cognito_user_pool_id,
        "ecrRegistry": cfg.ecr_registry,
    }))
    return state


def status(cfg: InstallConfig) -> None:
    state = State(cfg.default_state_path())
    if not state.data:
        log(f"No state file: {state.path}")
        return
    _print_endpoints(state)
    for k in sorted(state.data.keys()):
        v = state.data[k]
        if isinstance(v, str) and len(v) > 80:
            v = v[:77] + "..."
        print(f"  {k}: {v}")


def migrate(cfg: InstallConfig) -> None:
    state = State(cfg.default_state_path())
    if not state.get("migration_task_def"):
        fail("No migration_task_def in state — run deploy first")
    services.run_migration(cfg, state)


def destroy_compute(cfg: InstallConfig, *, yes: bool = False, all_resources: bool = False) -> None:
    """Delete ECS edge; with all_resources also delete data plane."""
    if not yes and not cfg.dry_run:
        fail("Pass --yes to confirm destroy")

    state = State(cfg.default_state_path())
    from .util import client

    ecs = client("ecs", cfg)
    elbv2 = client("elbv2", cfg)
    apigwv2 = client("apigatewayv2", cfg)
    cluster = state.get("cluster_name") or cfg.cluster_name
    prefix = cfg.name_prefix

    for suffix in (
        "gateway-proxy", "admin-api", "admin-ui",
        "scheduler", "cost-recorder", "notification-worker",
    ):
        name = f"{prefix}-{suffix}"
        if cfg.dry_run:
            log(f"[dry-run] delete service {name}")
            continue
        try:
            ecs.update_service(cluster=cluster, service=name, desiredCount=0)
            ecs.delete_service(cluster=cluster, service=name, force=True)
            log(f"Deleted service {name}")
        except Exception as e:  # noqa: BLE001
            log(f"Service {name}: {e}")

    if state.get("api_gateway_id") and not cfg.dry_run:
        try:
            apigwv2.delete_api(ApiId=state.get("api_gateway_id"))
            log("Deleted API Gateway")
        except Exception as e:  # noqa: BLE001
            log(f"API GW: {e}")

    if state.get("vpc_link_id") and not cfg.dry_run:
        try:
            apigwv2.delete_vpc_link(VpcLinkId=state.get("vpc_link_id"))
            log("Deleted VPC Link")
        except Exception as e:  # noqa: BLE001
            log(f"VPC Link: {e}")

    for key in ("gateway_alb_arn", "admin_ui_alb_arn", "admin_api_alb_arn"):
        arn = state.get(key)
        if not arn or cfg.dry_run:
            continue
        try:
            elbv2.delete_load_balancer(LoadBalancerArn=arn)
            log(f"Deleted ALB {key}")
        except Exception as e:  # noqa: BLE001
            log(f"ALB {key}: {e}")

    for key in ("gateway_tg_arn", "admin_ui_tg_arn", "admin_api_tg_arn"):
        arn = state.get(key)
        if not arn or cfg.dry_run:
            continue
        try:
            elbv2.delete_target_group(TargetGroupArn=arn)
            log(f"Deleted TG {key}")
        except Exception as e:  # noqa: BLE001
            log(f"TG {key}: {e}")

    if not cfg.dry_run and cluster:
        try:
            ecs.delete_cluster(cluster=cluster)
            log(f"Deleted cluster {cluster}")
        except Exception as e:  # noqa: BLE001
            log(f"Cluster: {e}")

    if all_resources:
        destroy_data_plane(cfg, state)
        log("Full destroy complete (incl. data plane).")
    else:
        log("Compute destroy complete. Data plane kept. Use --all to remove VPC/DB/Redis/Cognito.")


def _print_endpoints(state: State) -> None:
    ep = state.endpoints()
    print("")
    print("══════════════════════════════════════════════════════════")
    print(" LLM Gateway ECS endpoints")
    print("══════════════════════════════════════════════════════════")
    print(f" Gateway (data plane):  http://{ep.get('gateway_alb', '')}")
    print(f" Admin UI:                http://{ep.get('admin_ui_alb', '')}")
    print(f" Admin API (ALB/SSE):     http://{ep.get('admin_api_alb', '')}")
    print(f" Admin API (API GW REST): {ep.get('admin_api_gateway', '')}")
    print("")
    print(" Client env:")
    print(f"   ANTHROPIC_BASE_URL=http://{ep.get('gateway_alb', '')}")
    print(f"   ADMIN_API_URL={ep.get('admin_api_gateway', '')}")
    print("══════════════════════════════════════════════════════════")


def _refresh_connection_secrets(cfg: InstallConfig, state: State) -> None:
    """Idempotent: embed host+password into Secrets Manager JSON for ECS task injection."""
    if cfg.dry_run:
        return
    from .dataplane import aurora, redis
    from .util import client

    sm = client("secretsmanager", cfg)
    if cfg.db_host and cfg.db_secret_arn:
        try:
            aurora._ensure_db_secret(cfg, sm, state)
        except Exception as e:  # noqa: BLE001
            log(f"DB secret refresh warn: {e}")
    if cfg.redis_host and cfg.redis_auth_secret_arn:
        try:
            redis._sync_redis_url_secret(cfg, sm)
        except Exception as e:  # noqa: BLE001
            log(f"Redis secret refresh warn: {e}")
